"""cli.py のテスト。偽の Discord REST API に対してサブコマンドを実行する（ボタンは使わない）。

  .venv\\Scripts\\python.exe -m unittest tests.test_cli -v
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
CH, USER, BOT = "500", "111", "999"


class FakeDiscord(BaseHTTPRequestHandler):
    msgs: dict = {}
    calls: list = []
    next_id = [1000]

    def log_message(self, *a):
        pass

    @classmethod
    def add(cls, ch, author, content):
        cls.next_id[0] += 1
        m = {"id": str(cls.next_id[0]), "channel_id": ch, "content": content, "attachments": [],
             "author": {"id": author, "username": f"u{author}"}}
        cls.msgs.setdefault(ch, []).append(m)
        return m

    def _send(self, code, obj=None):
        b = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _path(self):
        return urlparse(self.path).path.removeprefix("/api/v10")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        p = self._path()
        self.calls.append(("POST", p, body))
        if m := re.fullmatch(r"/channels/(\d+)/messages/(\d+)/threads", p):
            self.msgs.setdefault(m.group(2), [])
            return self._send(201, {"id": m.group(2)})
        if m := re.fullmatch(r"/channels/(\d+)/messages", p):
            return self._send(200, self.add(m.group(1), BOT, body["content"]))
        self._send(404, {"message": "nf"})

    def do_PATCH(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.calls.append(("PATCH", self._path(), json.loads(self.rfile.read(n))))
        self._send(200, {"id": "x"})

    def do_PUT(self):
        self.calls.append(("PUT", self._path(), None))
        self._send(204)

    def do_GET(self):
        p = self._path()
        if m := re.fullmatch(r"/channels/(\d+)", p):
            return self._send(200, {"id": m.group(1), "type": 0})
        m = re.fullmatch(r"/channels/(\d+)/messages", p)
        if not m or m.group(1) not in self.msgs:
            return self._send(404, {"message": "Unknown Channel", "code": 10003})
        after = int(parse_qs(urlparse(self.path).query).get("after", ["0"])[0])
        out = [x for x in self.msgs[m.group(1)] if int(x["id"]) > after]
        self._send(200, list(reversed(out)))


class CliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeDiscord)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def cli(self, *args, stdin=None, expect=0):
        env = {**os.environ, "DISCORD_BOT_TOKEN": "t", "DISCORD_CHANNEL_ID": CH, "DISCORD_USER_ID": USER,
               "DISCORD_BUTTONS": "0", "DISCORD_MCP_STATE": str(Path(tempfile.mkdtemp()) / "state.json"),
               "DISCORD_API_BASE": f"http://127.0.0.1:{self.httpd.server_port}/api/v10", "PYTHONIOENCODING": "utf-8"}
        r = subprocess.run([sys.executable, str(ROOT / "cli.py"), *args], input=stdin, env=env,
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, expect, r.stdout + r.stderr)
        return json.loads(r.stdout)

    def test_notify_from_stdin(self):
        out = self.cli("notify", "-", "--level", "success", "--title", "工数", stdin="本文です")
        self.assertEqual(out["status"], "sent")
        self.assertEqual(FakeDiscord.calls[-1][2]["content"], "✅ **工数**\n本文です")

    def test_pending_then_answer_by_id(self):
        out = self.cli("ask", "どの課題?", "--choice", "明治座保守", "--choice", "その他", "--timeout", "0", "--task", "koshu")
        self.assertEqual(out["status"], "pending")
        qid = out["question_id"]
        self.assertEqual(self.cli("answer", qid)["status"], "pending")
        FakeDiscord.add(qid, "222", "他人の返信")
        FakeDiscord.add(qid, USER, "2")
        out = self.cli("answer", qid, "--choice", "明治座保守", "--choice", "その他")
        self.assertEqual((out["status"], out["answer"], out["indices"]), ("answered", "その他", [1]))
        self.assertTrue(any(c[0] == "PUT" and f"/channels/{qid}/messages/" in c[1] for c in FakeDiscord.calls))
        out = self.cli("answer", qid)  # 選択肢を渡さなければ文字のまま
        self.assertEqual((out["kind"], out["answer"]), ("text", "2"))

    def test_answer_confirm_and_missing_thread(self):
        out = self.cli("confirm", "適用してよい?", "--timeout", "0")
        FakeDiscord.add(out["question_id"], USER, "はい")
        out = self.cli("answer", out["question_id"], "--confirm")
        self.assertIs(out["approved"], True)
        self.assertEqual(self.cli("answer", "424242")["status"], "not_found")

    def test_error_is_json(self):
        out = self.cli("ask", "x", *sum([["--choice", str(i)] for i in range(30)], []), expect=1)
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
