"""media.py のテスト。ローカルの偽 Discord REST / CDN サーバーに対して動かす（本物の Discord には繋がない）。

実行: .venv\\Scripts\\python.exe -m unittest tests.test_media -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import media  # noqa: E402
from discord_api import DiscordError  # noqa: E402


def parse_multipart(body: bytes, content_type: str) -> list[dict]:
    """テスト用の簡単な multipart パーサー。[{"name", "filename", "content_type", "data"}] を返す。"""
    boundary = re.search(r"boundary=(\S+)", content_type).group(1).encode()
    parts = []
    for chunk in body.split(b"--" + boundary)[1:]:
        if chunk.startswith(b"--"):
            break
        head, _, data = chunk[2:].partition(b"\r\n\r\n")
        headers = head.decode("utf-8")
        disp = re.search(r"Content-Disposition: (.*)", headers).group(1)
        ctype = re.search(r"Content-Type: (.*)", headers)
        fname = re.search(r'filename="([^"]*)"', disp)
        parts.append(
            {
                "name": re.search(r'name="([^"]*)"', disp).group(1),
                "filename": fname.group(1) if fname else None,
                "content_type": ctype.group(1).strip() if ctype else None,
                "data": data[:-2],  # 末尾の \r\n
            }
        )
    return parts


class FakeDiscord(BaseHTTPRequestHandler):
    """偽の Discord API（/api/v10/...）と CDN（/cdn/...）。"""

    calls: list[dict] = []
    channel_types: dict[str, int] = {}
    drop_attachments = False
    cdn_files: dict[str, bytes] = {}
    lie_length = False  # Content-Length を付けずにデータを流す（size 申告をすり抜けるケース）

    def log_message(self, *args):
        pass

    def _send(self, code: int, obj=None, raw: bytes | None = None, ctype="application/json"):
        data = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b"")
        self.send_response(code)
        if data:
            self.send_header("Content-Type", ctype)
        if not (raw is not None and FakeDiscord.lie_length):
            self.send_header("Content-Length", str(len(data)))
        else:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method: str):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        path = urlsplit(self.path).path
        call = {"method": method, "path": path, "headers": dict(self.headers), "body": body}
        FakeDiscord.calls.append(call)

        if path.startswith("/cdn/"):
            name = path[len("/cdn/") :]
            if name == "redirect":
                self.send_response(302)
                self.send_header("Location", "https://evil.example.com/x")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if name not in FakeDiscord.cdn_files:
                return self._send(404, {"message": "nf"})
            return self._send(200, raw=FakeDiscord.cdn_files[name], ctype="text/plain")

        if not path.startswith("/api/v10/"):
            return self._send(404, {"message": "nf"})
        p = path[len("/api/v10") :]
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("multipart/form-data"):
            parts = parse_multipart(body, ctype)
            call["parts"] = parts
            payload = json.loads(next(x for x in parts if x["name"] == "payload_json")["data"])
            nfiles = sum(1 for x in parts if x["name"].startswith("files["))
        else:
            payload = json.loads(body) if body else None
            nfiles = 0
        call["json"] = payload

        def message(ch: str, data: dict) -> dict:
            atts = [] if FakeDiscord.drop_attachments else [
                {"id": str(9000 + i), "filename": a["filename"]} for i, a in enumerate((data or {}).get("attachments") or [])
            ][:nfiles]
            return {"id": "777", "channel_id": ch, "content": (data or {}).get("content", ""), "attachments": atts}

        if m := re.fullmatch(r"/channels/(\d+)", p):
            if method == "GET":
                if m.group(1) == "404":
                    return self._send(404, {"message": "Unknown Channel", "code": 10003})
                if m.group(1) == "500":
                    return self._send(500, {"message": "boom"})
                return self._send(200, {"id": m.group(1), "type": FakeDiscord.channel_types.get(m.group(1), 0)})
        if m := re.fullmatch(r"/channels/(\d+)/messages", p):
            return self._send(200, message(m.group(1), payload))
        if m := re.fullmatch(r"/channels/(\d+)/messages/(\d+)", p):
            if method == "PATCH":
                return self._send(200, message(m.group(1), payload))
        if m := re.fullmatch(r"/channels/(\d+)/threads", p):
            return self._send(201, {"id": "888", "type": 11, "parent_id": m.group(1), "message": message("888", payload["message"])})
        if m := re.fullmatch(r"/channels/(\d+)/messages/(\d+)/reactions/[^/]+/@me", p):
            if m.group(1) == "403":
                return self._send(403, {"message": "Missing Access", "code": 50001})
            return self._send(204)
        if m := re.fullmatch(r"/channels/(\d+)/typing", p):
            if m.group(1) == "403":
                return self._send(403, {"message": "Missing Access", "code": 50001})
            return self._send(204)
        self._send(404, {"message": "nf"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


class MediaTestBase(unittest.TestCase):
    ENV = ("DISCORD_API_BASE", "DISCORD_BOT_TOKEN", "DISCORD_CDN_HOSTS")

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeDiscord)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.saved_env = {k: os.environ.get(k) for k in cls.ENV}
        os.environ["DISCORD_API_BASE"] = f"http://127.0.0.1:{cls.port}/api/v10"
        os.environ["DISCORD_BOT_TOKEN"] = "dummy"
        os.environ.pop("DISCORD_CDN_HOSTS", None)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for k, v in cls.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        FakeDiscord.calls = []
        FakeDiscord.channel_types = {}
        FakeDiscord.drop_attachments = False
        FakeDiscord.cdn_files = {}
        FakeDiscord.lie_length = False
        media._channel_types.clear()
        self.tmp = Path(tempfile.mkdtemp(prefix="discord-media-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("DISCORD_CDN_HOSTS", None)

    def write(self, name: str, data: bytes) -> str:
        p = self.tmp / name
        p.write_bytes(data)
        return str(p)

    def api_calls(self, method: str | None = None) -> list[dict]:
        return [c for c in FakeDiscord.calls if c["path"].startswith("/api/") and (method is None or c["method"] == method)]


class SendTests(MediaTestBase):
    def test_plain_send(self):
        res = media.send_message("100", {"content": "hello"})
        self.assertEqual(res["message_id"], "777")
        self.assertEqual(res["channel_id"], "100")
        self.assertNotIn("thread_id", res)
        post = self.api_calls("POST")[0]
        self.assertEqual(post["path"], "/api/v10/channels/100/messages")
        self.assertEqual(post["json"], {"content": "hello"})
        self.assertEqual(post["headers"]["Authorization"], "Bot dummy")
        self.assertTrue(post["headers"]["Content-Type"].startswith("application/json"))

    def test_multipart_send(self):
        a = self.write("report.txt", b"hello world")
        b = self.write("グラフ.png", b"\x89PNG\r\n\x1a\nbinary--data\r\n")
        res = media.send_message("100", {"content": "files"}, [a, b])
        self.assertEqual(len(res["message"]["attachments"]), 2)
        post = self.api_calls("POST")[0]
        self.assertEqual(post["path"], "/api/v10/channels/100/messages")
        self.assertTrue(post["headers"]["Content-Type"].startswith("multipart/form-data; boundary="))
        self.assertNotIn("filename*", post["body"].decode("utf-8", "replace"))
        self.assertEqual(
            post["json"],
            {"content": "files", "attachments": [{"id": 0, "filename": "report.txt"}, {"id": 1, "filename": "グラフ.png"}]},
        )
        files = {p["name"]: p for p in post["parts"] if p["name"].startswith("files[")}
        self.assertEqual(files["files[0]"]["data"], b"hello world")
        self.assertEqual(files["files[0]"]["filename"], "report.txt")
        self.assertEqual(files["files[0]"]["content_type"], "text/plain")
        self.assertEqual(files["files[1]"]["data"], b"\x89PNG\r\n\x1a\nbinary--data\r\n")
        self.assertEqual(files["files[1]"]["filename"], "グラフ.png")
        self.assertEqual(files["files[1]"]["content_type"], "image/png")

    def test_unknown_extension_and_quote_in_name(self):
        a = self.write("weird.zzzunknown", b"x")
        media.send_message("100", {"content": "x"}, [a])
        part = next(p for p in self.api_calls("POST")[0]["parts"] if p["name"] == "files[0]")
        self.assertEqual(part["content_type"], "application/octet-stream")
        # Windows では " を含むファイル名を作れないので、ヘッダー用の変換は関数を直接確かめる
        self.assertEqual(media._header_filename('we"ird\r\n.txt'), "we%22ird%0D%0A.txt")

    def test_existing_attachments_in_payload_keep_ids_unique(self):
        a = self.write("a.txt", b"a")
        media.send_message("100", {"content": "x", "attachments": [{"id": 0, "filename": "old.txt"}]}, [a])
        post = self.api_calls("POST")[0]
        self.assertEqual(post["json"]["attachments"], [{"id": 0, "filename": "old.txt"}, {"id": 1, "filename": "a.txt"}])
        self.assertIn("files[1]", [p["name"] for p in post["parts"]])

    def test_errors_raised_before_any_request(self):
        big = self.tmp / "big.bin"
        with open(big, "wb") as f:
            f.truncate(media.MAX_UPLOAD_BYTES + 1)
        with self.assertRaises(DiscordError) as cm:
            media.send_message("100", {"content": "x"}, [str(big)])
        self.assertIn("ファイルが大きすぎます: big.bin は 20.0 MB（上限 20 MB）", str(cm.exception))

        with self.assertRaises(DiscordError) as cm:
            media.send_message("100", {"content": "x"}, [str(self.tmp / "missing.txt")])
        self.assertIn("ファイルが見つかりません", str(cm.exception))

        with self.assertRaises(DiscordError) as cm:
            media.send_message("100", {"content": "x"}, [str(self.tmp)])  # ディレクトリ
        self.assertIn("ファイルが見つかりません", str(cm.exception))

        many = [self.write(f"f{i}.txt", b"x") for i in range(media.MAX_FILES_PER_MESSAGE + 1)]
        with self.assertRaises(DiscordError):
            media.send_message("100", {"content": "x"}, many)

        with self.assertRaises(DiscordError):
            media.edit_message("100", "5", {"content": "x"}, [str(self.tmp / "missing.txt")])
        self.assertEqual(FakeDiscord.calls, [])

    def test_exactly_ten_files_ok(self):
        files = [self.write(f"f{i}.txt", b"x") for i in range(10)]
        res = media.send_message("100", {"content": "x"}, files)
        self.assertEqual(len(res["message"]["attachments"]), 10)

    def test_no_attachments_in_response_is_error(self):
        FakeDiscord.drop_attachments = True
        a = self.write("a.txt", b"a")
        with self.assertRaises(DiscordError):
            media.send_message("100", {"content": "x"}, [a])
        with self.assertRaises(DiscordError):
            media.edit_message("100", "5", {"content": "x"}, [a])

    def test_edit_message(self):
        res = media.edit_message("100", "5", {"content": "edited"})
        self.assertEqual(res["content"], "edited")
        call = self.api_calls("PATCH")[0]
        self.assertEqual(call["path"], "/api/v10/channels/100/messages/5")
        a = self.write("a.txt", b"abc")
        res = media.edit_message("100", "5", {"content": "with file"}, [a])
        self.assertEqual(len(res["attachments"]), 1)
        call = self.api_calls("PATCH")[1]
        self.assertEqual(call["json"]["attachments"], [{"id": 0, "filename": "a.txt"}])


class ForumTests(MediaTestBase):
    def test_forum_creates_thread_with_derived_name(self):
        FakeDiscord.channel_types["200"] = 15
        res = media.send_message("200", {"content": "\n  ## <@123> 夜間バッチ完了  \n詳細はこちら"})
        self.assertEqual(res["thread_id"], "888")
        self.assertEqual(res["channel_id"], "888")
        self.assertEqual(res["message_id"], "777")
        post = self.api_calls("POST")[0]
        self.assertEqual(post["path"], "/api/v10/channels/200/threads")
        self.assertEqual(post["json"]["name"], "夜間バッチ完了")
        self.assertEqual(post["json"]["message"]["content"], "\n  ## <@123> 夜間バッチ完了  \n詳細はこちら")

    def test_media_channel_with_files_multipart(self):
        FakeDiscord.channel_types["201"] = 16
        a = self.write("a.txt", b"abc")
        res = media.send_message("201", {"content": "x" * 300}, [a])
        self.assertEqual(res["thread_id"], "888")
        post = self.api_calls("POST")[0]
        self.assertEqual(post["path"], "/api/v10/channels/201/threads")
        self.assertEqual(post["json"]["name"], "x" * 100)
        self.assertEqual(post["json"]["message"]["attachments"], [{"id": 0, "filename": "a.txt"}])
        self.assertEqual(next(p for p in post["parts"] if p["name"] == "files[0]")["data"], b"abc")

    def test_forum_fallback_name_and_missing_attachments(self):
        FakeDiscord.channel_types["200"] = 15
        media.send_message("200", {"content": "<@1> <#2>", "embeds": [{"description": "d"}]})
        self.assertEqual(self.api_calls("POST")[0]["json"]["name"], "通知")
        FakeDiscord.drop_attachments = True
        with self.assertRaises(DiscordError):
            media.send_message("200", {"content": "x"}, [self.write("a.txt", b"a")])

    def test_thread_name_helper(self):
        self.assertEqual(media._thread_name({"content": "### タイトル\n本文"}), "タイトル")
        self.assertEqual(media._thread_name({"content": "<@!1> <@&2> @everyone 件名"}), "件名")
        self.assertEqual(media._thread_name({}), "通知")

    def test_channel_type_cached(self):
        FakeDiscord.channel_types["300"] = 0
        self.assertEqual(media.channel_type("300"), 0)
        self.assertEqual(media.channel_type("300"), 0)
        media.send_message("300", {"content": "a"})
        media.send_message("300", {"content": "b"})
        gets = self.api_calls("GET")
        self.assertEqual(len(gets), 1)
        self.assertEqual(gets[0]["path"], "/api/v10/channels/300")

    def test_channel_lookup_404_reraises(self):
        with self.assertRaises(DiscordError) as cm:
            media.send_message("404", {"content": "x"})
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(self.api_calls("POST"), [])

    def test_channel_lookup_500_falls_back_to_plain(self):
        with self.assertLogs("media", level="WARNING"):
            media.send_message("500", {"content": "x"})
        self.assertEqual(self.api_calls("POST")[0]["path"], "/api/v10/channels/500/messages")


class SoftHelperTests(MediaTestBase):
    def test_reaction_url_encoding(self):
        self.assertTrue(media.add_reaction("100", "5", "✅"))
        self.assertTrue(media.remove_own_reaction("100", "5", "✅"))
        self.assertTrue(media.add_reaction("100", "5", "<:party:123456>"))
        put, delete, custom = self.api_calls()
        self.assertEqual(put["method"], "PUT")
        self.assertEqual(put["path"], "/api/v10/channels/100/messages/5/reactions/%E2%9C%85/@me")
        self.assertEqual(delete["method"], "DELETE")
        self.assertEqual(delete["path"], put["path"])
        self.assertEqual(custom["path"], "/api/v10/channels/100/messages/5/reactions/party%3A123456/@me")

    def test_reaction_soft_failure(self):
        with self.assertLogs("media", level="WARNING"):
            self.assertFalse(media.add_reaction("403", "5", "✅"))
        with self.assertLogs("media", level="WARNING"):
            self.assertFalse(media.remove_own_reaction("403", "5", "✅"))

    def test_typing(self):
        self.assertTrue(media.trigger_typing("100"))
        self.assertEqual(self.api_calls("POST")[0]["path"], "/api/v10/channels/100/typing")
        with self.assertLogs("media", level="WARNING"):
            self.assertFalse(media.trigger_typing("403"))

    def test_soft_failure_when_unreachable(self):
        os.environ["DISCORD_API_BASE"] = "http://127.0.0.1:1/api/v10"
        try:
            with self.assertLogs("media", level="WARNING"):
                self.assertFalse(media.trigger_typing("100"))
        finally:
            os.environ["DISCORD_API_BASE"] = f"http://127.0.0.1:{self.port}/api/v10"


class DownloadTests(MediaTestBase):
    def setUp(self):
        super().setUp()
        os.environ["DISCORD_CDN_HOSTS"] = "127.0.0.1"
        self.dest = self.tmp / "out" / "nested"

    def cdn(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/cdn/{name}"

    def test_download_allowed_host(self):
        FakeDiscord.cdn_files["a"] = b"hello"
        msg = {"attachments": [{"id": "1", "filename": "a.txt", "size": 5, "url": self.cdn("a"), "content_type": "text/plain"}]}
        res = media.download_attachments(msg, self.dest)
        self.assertEqual(res["skipped"], [])
        [item] = res["saved"]
        self.assertEqual(item["filename"], "a.txt")
        self.assertEqual(item["size"], 5)
        self.assertEqual(item["content_type"], "text/plain")
        self.assertEqual(item["url"], self.cdn("a"))
        self.assertEqual(Path(item["path"]).read_bytes(), b"hello")
        self.assertEqual(Path(item["path"]).parent, self.dest.resolve())
        cdn_call = [c for c in FakeDiscord.calls if c["path"].startswith("/cdn/")][0]
        self.assertNotIn("Authorization", cdn_call["headers"])
        self.assertIn("User-Agent", cdn_call["headers"])

    def test_oversize_skipped(self):
        FakeDiscord.cdn_files["big"] = b"x" * 100
        msg = {"attachments": [{"filename": "big.bin", "size": 100, "url": self.cdn("big")}]}
        res = media.download_attachments(msg, self.dest, max_bytes=50)
        self.assertEqual(res["saved"], [])
        self.assertEqual(res["skipped"], [{"filename": "big.bin", "reason": "大きすぎます"}])
        self.assertEqual(FakeDiscord.calls, [])

    def test_oversize_by_content_length_and_stream(self):
        FakeDiscord.cdn_files["big"] = b"x" * 100
        msg = {"attachments": [{"filename": "big.bin", "size": 10, "url": self.cdn("big")}]}
        res = media.download_attachments(msg, self.dest, max_bytes=50)
        self.assertEqual(res["skipped"][0]["reason"], "大きすぎます")
        FakeDiscord.lie_length = True
        res = media.download_attachments(msg, self.dest, max_bytes=50)
        self.assertEqual(res["skipped"][0]["reason"], "大きすぎます")
        self.assertEqual(list(self.dest.iterdir()), [])  # 途中まで書いたファイルは消す

    def test_disallowed_hosts_skipped(self):
        os.environ.pop("DISCORD_CDN_HOSTS")
        msg = {
            "attachments": [
                {"filename": "a", "url": "https://evil.example.com/a"},
                {"filename": "b", "url": "http://cdn.discordapp.com/b"},  # 既定ホストでも http は不可
                {"filename": "c", "url": self.cdn("c")},  # env なしなら 127.0.0.1 は不可
                {"filename": "d", "url": "file:///etc/passwd"},
                {"filename": "e"},
                {"filename": "f", "url": "https://cdn.discordapp.com.evil.example/f"},
            ]
        }
        res = media.download_attachments(msg, self.dest)
        self.assertEqual(res["saved"], [])
        self.assertEqual([s["reason"] for s in res["skipped"]], ["許可されていない URL"] * 6)
        self.assertEqual(FakeDiscord.calls, [])

    def test_redirect_to_disallowed_host_skipped(self):
        msg = {"attachments": [{"filename": "r.txt", "url": self.cdn("redirect")}]}
        res = media.download_attachments(msg, self.dest)
        self.assertEqual(res["saved"], [])
        self.assertIn("ダウンロードに失敗しました", res["skipped"][0]["reason"])

    def test_network_error_skipped(self):
        msg = {
            "attachments": [
                {"filename": "missing.txt", "url": self.cdn("missing")},  # 404
                {"filename": "down.txt", "url": "http://127.0.0.1:1/cdn/x"},  # 接続できない
            ]
        }
        res = media.download_attachments(msg, self.dest)
        self.assertEqual(res["saved"], [])
        self.assertEqual(len(res["skipped"]), 2)
        self.assertTrue(all("ダウンロードに失敗しました" in s["reason"] for s in res["skipped"]))

    def test_filename_sanitize_dedupe_and_traversal(self):
        FakeDiscord.cdn_files["a"] = b"A"
        names = [
            "../../evil.txt",
            "..\\..\\evil.txt",
            "/etc/evil.txt",
            "C:\\Windows\\evil.txt",
            'bad<>:"|?*\x01\x1fname.txt',
            "CON.txt",
            "..",
            "trailing. ",
        ]
        msg = {"attachments": [{"id": str(i), "filename": n, "url": self.cdn("a")} for i, n in enumerate(names)]}
        (self.dest).mkdir(parents=True)
        (self.dest / "evil.txt").write_bytes(b"existing")  # 既存ファイルは上書きしない
        res = media.download_attachments(msg, self.dest)
        self.assertEqual(res["skipped"], [])
        got = [s["filename"] for s in res["saved"]]
        self.assertEqual(
            got,
            [
                "evil (2).txt",
                "evil (3).txt",
                "evil (4).txt",
                "evil (5).txt",
                "bad_______name.txt",
                "_CON.txt",
                "attachment-6",
                "trailing",
            ],
        )
        self.assertEqual((self.dest / "evil.txt").read_bytes(), b"existing")
        for s in res["saved"]:
            self.assertEqual(Path(s["path"]).parent, self.dest.resolve())
        self.assertFalse((self.tmp / "evil.txt").exists())
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["out"])

    def test_no_attachments(self):
        self.assertEqual(media.download_attachments({}, self.dest), {"saved": [], "skipped": []})
        self.assertEqual(media.download_attachments({"attachments": []}, self.dest), {"saved": [], "skipped": []})


if __name__ == "__main__":
    unittest.main()
