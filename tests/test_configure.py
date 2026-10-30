"""configure.py のテスト。偽の Discord API と偽の claude コマンドで、対話の流れを最後まで通す。

  .venv\\Scripts\\python.exe -m unittest tests.test_configure -v
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
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "MTIz.abcdef.ghijklmnopqrstuvwxyz0123456789"
APP_ID, OWNER, GUILD, CH_TEXT, CH_FORUM = "42", "111", "7", "500", "501"


class FakeDiscord(BaseHTTPRequestHandler):
    state: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        s = self.state
        if self.headers["Authorization"] != f"Bot {TOKEN}":
            return self._send(401, {"message": "401: Unauthorized", "code": 0})
        p = urlparse(self.path).path.removeprefix("/api/v10")
        s.setdefault("gets", []).append(p)
        if p == "/users/@me":
            return self._send(200, {"id": APP_ID, "username": "TestBot", "bot": True})
        if p == "/oauth2/applications/@me":
            # 1 回目は MESSAGE CONTENT INTENT がオフ、2 回目からオン
            s["app_calls"] = s.get("app_calls", 0) + 1
            flags = 0 if s["app_calls"] == 1 else 1 << 19
            return self._send(200, {"id": APP_ID, "flags": flags, "owner": {"id": OWNER, "username": "owner"}})
        if p == "/users/@me/guilds":
            # 1 回目はどのサーバーにも入っていない
            s["guild_calls"] = s.get("guild_calls", 0) + 1
            return self._send(200, [] if s["guild_calls"] == 1 else [{"id": GUILD, "name": "テストサーバー"}])
        if p == f"/guilds/{GUILD}/channels":
            return self._send(200, [
                {"id": "499", "name": "カテゴリ", "type": 4, "position": 0},
                {"id": CH_TEXT, "name": "general", "type": 0, "position": 1},
                {"id": CH_FORUM, "name": "claude", "type": 15, "position": 2},
            ])
        self._send(404, {"message": "nf"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n))
        self.state.setdefault("posts", []).append((urlparse(self.path).path, body))
        self._send(200, {"id": "900", **body})


FAKE_CLAUDE = r'''
import json, sys
log, exists = sys.argv[1], sys.argv[2] == "1"
args = sys.argv[3:]
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\n")
if args[:2] == ["mcp", "get"]:
    print("discord: ... DISCORD_BOT_TOKEN=secret")
    sys.exit(0 if exists else 1)
sys.exit(0)
'''


class ConfigureTest(unittest.TestCase):
    def setUp(self):
        FakeDiscord.state = {}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeDiscord)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tmp = Path(tempfile.mkdtemp())
        self.log = self.tmp / "claude.log"
        (self.tmp / "fake_claude.py").write_text(FAKE_CLAUDE, encoding="utf-8")

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def run_setup(self, stdin: str, exists: bool, *extra: str):
        wrapper = self.tmp / ("claude.cmd" if os.name == "nt" else "claude")
        if os.name == "nt":
            wrapper.write_text(f'@"{sys.executable}" "{self.tmp / "fake_claude.py"}" "{self.log}" {int(exists)} %*\n')
        else:
            wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{self.tmp / "fake_claude.py"}" "{self.log}" {int(exists)} "$@"\n')
            wrapper.chmod(0o755)
        env = {**os.environ, "DISCORD_API_BASE": f"http://127.0.0.1:{self.httpd.server_port}/api/v10",
               "CLAUDE_BIN": str(wrapper), "PYTHONIOENCODING": "utf-8"}
        return subprocess.run([sys.executable, str(ROOT / "configure.py"), *extra], input=stdin, env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=60)

    def claude_calls(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_full_flow(self):
        # トークン → (Intent を直して Enter) → (招待して Enter) → チャンネル 2 番 → ユーザー ID は既定 → 置き換え y
        r = self.run_setup(f"{TOKEN}\n\n\n2\n\ny\n", exists=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = r.stdout
        self.assertIn("MESSAGE CONTENT INTENT がオフ", out)
        self.assertIn(f"client_id={APP_ID}&scope=bot&permissions=309237763136", out)
        self.assertNotIn("カテゴリ", out)  # カテゴリは選択肢に出さない
        self.assertNotIn(TOKEN, out + r.stderr)  # トークンも、claude mcp get の出力も表示しない
        self.assertNotIn("secret", out + r.stderr)

        calls = self.claude_calls()
        self.assertEqual(calls[0], ["mcp", "get", "discord"])
        self.assertEqual(calls[1], ["mcp", "remove", "discord", "--scope", "user"])
        add = calls[2]
        self.assertEqual(add[:5], ["mcp", "add", "discord", "--scope", "user"])
        self.assertIn(f"DISCORD_BOT_TOKEN={TOKEN}", add)
        self.assertIn(f"DISCORD_CHANNEL_ID={CH_FORUM}", add)
        self.assertIn(f"DISCORD_USER_ID={OWNER}", add)
        self.assertEqual(add[-1], str(ROOT / "server.py"))

        path, body = FakeDiscord.state["posts"][-1]
        self.assertEqual(path, f"/api/v10/channels/{CH_FORUM}/messages")
        self.assertIn(f"<@{OWNER}>", body["content"])

    def test_new_registration_custom_user_no_test(self):
        r = self.run_setup(f"{TOKEN}\n\n\n1\n222,333\n", False, "--name", "discord-x", "--no-test")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.claude_calls()
        self.assertEqual([c[:2] for c in calls], [["mcp", "get"], ["mcp", "add"]])
        self.assertIn("discord-x", calls[1])
        self.assertIn("DISCORD_USER_ID=222,333", calls[1])
        self.assertIn(f"DISCORD_CHANNEL_ID={CH_TEXT}", calls[1])
        self.assertNotIn("posts", FakeDiscord.state)

    def test_declining_replace_aborts(self):
        r = self.run_setup(f"{TOKEN}\n\n\n1\n\nn\n", exists=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("登録を中止しました", r.stderr)
        self.assertEqual([c[:2] for c in self.claude_calls()], [["mcp", "get"]])

    def test_bad_token(self):
        r = self.run_setup("1565553601617596626\n", exists=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("トークンの形ではありません", r.stdout + r.stderr)
        r = self.run_setup("aaa.bbb.ccc\n", exists=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("トークンが無効です", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
