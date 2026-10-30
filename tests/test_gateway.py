"""gateway.py のテスト。偽の Gateway（WebSocket）と偽の REST API をローカルに立てて使う。

本物の Discord には接続しない。プロジェクトのフォルダで次のように実行する:
  .venv\\Scripts\\python.exe -m unittest tests.test_gateway -v
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from websockets.exceptions import ConnectionClosed  # noqa: E402
from websockets.sync.server import ServerConnection, serve  # noqa: E402

import gateway  # noqa: E402
from discord_api import DiscordError  # noqa: E402

# 想定どおりの警告（4004 など）でテストの出力が埋まらないようにする。assertLogs は引き続き使える
logging.getLogger("discord_mcp.gateway").addHandler(logging.NullHandler())

TOKEN = "dummy-token"
WAIT = 5.0


class FakeGateway:
    """最低限の Discord Gateway。受け取ったメッセージを (接続番号, payload) で記録する。"""

    def __init__(self, interval_ms: int = 100) -> None:
        self.interval_ms = interval_ms
        self.ack = True
        self.identify_close_code: int | None = None
        self.seq = 0
        self.paths: list[str] = []
        self.connections: list[ServerConnection] = []
        self.messages: list[tuple[int, dict[str, Any]]] = []
        self.cond = threading.Condition()
        self.server = serve(self._handler, "127.0.0.1", 0, compression=None, ping_interval=None)
        self.url = f"ws://127.0.0.1:{self.server.socket.getsockname()[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handler(self, ws: ServerConnection) -> None:
        with self.cond:
            idx = len(self.connections)
            self.connections.append(ws)
            self.paths.append(ws.request.path)
        ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": self.interval_ms}}))
        try:
            for raw in ws:
                msg = json.loads(raw)
                with self.cond:
                    self.messages.append((idx, msg))
                    self.cond.notify_all()
                op = msg["op"]
                if op == 1 and self.ack:
                    ws.send(json.dumps({"op": 11}))
                elif op == 2:
                    if self.identify_close_code:
                        ws.close(self.identify_close_code, "fake fatal")
                        return
                    self.dispatch(
                        "READY", {"session_id": "sess-1", "resume_gateway_url": self.url, "v": 10}, conn=ws
                    )
                elif op == 6:
                    self.dispatch("RESUMED", {}, conn=ws)
        except ConnectionClosed:
            pass

    def dispatch(self, kind: str, data: dict[str, Any], conn: ServerConnection | None = None) -> int:
        with self.cond:
            self.seq += 1
            seq = self.seq
            ws = conn or self.connections[-1]
        ws.send(json.dumps({"op": 0, "t": kind, "s": seq, "d": data}))
        return seq

    def send_raw(self, payload: dict[str, Any]) -> None:
        self.connections[-1].send(json.dumps(payload))

    def wait_for(self, pred: Callable[[int, dict[str, Any]], bool], timeout: float = WAIT) -> tuple[int, dict]:
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                for idx, msg in self.messages:
                    if pred(idx, msg):
                        return idx, msg
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"期待したメッセージが来ませんでした: {self.messages}")
                self.cond.wait(remaining)

    def count(self, pred: Callable[[int, dict[str, Any]], bool]) -> int:
        with self.cond:
            return sum(1 for idx, msg in self.messages if pred(idx, msg))

    def close(self) -> None:
        self.server.shutdown()


class FakeRest:
    """偽の Discord REST API。リクエストを (method, path, body) で記録する。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.cond = threading.Condition()
        self.gateway_url: str | None = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _record(self, body: Any) -> None:
                with outer.cond:
                    outer.calls.append((self.command, self.path, body))
                    outer.cond.notify_all()

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                self._record(json.loads(raw) if raw else None)
                self.send_response(204)
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                self._record(None)
                if self.path.endswith("/gateway/bot") and outer.gateway_url:
                    body = json.dumps({"url": outer.gateway_url}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/api/v10"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait_for(self, pred: Callable[[str, str, Any], bool], timeout: float = WAIT) -> tuple[str, str, Any]:
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                for call in self.calls:
                    if pred(*call):
                        return call
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"期待したリクエストが来ませんでした: {self.calls}")
                self.cond.wait(remaining)

    def callbacks(self) -> list[tuple[str, str, Any]]:
        with self.cond:
            return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/callback")]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def interaction(custom_id: str, *, iid: str = "111", itype: int = 3) -> dict[str, Any]:
    return {"id": iid, "token": f"tok-{iid}", "type": itype, "data": {"custom_id": custom_id}}


class GatewayTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeGateway()
        self.rest = FakeRest()
        self.env = mock.patch.dict(
            os.environ, {"DISCORD_BOT_TOKEN": TOKEN, "DISCORD_API_BASE": self.rest.base}, clear=False
        )
        self.env.start()
        os.environ.pop("DISCORD_GATEWAY_URL", None)
        self.gateways: list[gateway.Gateway] = []

    def tearDown(self) -> None:
        for gw in self.gateways:
            gw.stop()
        self.env.stop()
        self.fake.close()
        self.rest.close()

    def make(self, **kwargs: Any) -> gateway.Gateway:
        kwargs.setdefault("url", self.fake.url)
        gw = gateway.Gateway(TOKEN, **kwargs)
        self.gateways.append(gw)
        return gw

    def started(self) -> gateway.Gateway:
        gw = self.make()
        gw.start(wait_ready=WAIT)
        return gw


class ParseCustomIdTest(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(gateway.parse_custom_id("dmcp:q1:yes"), ("q1", "yes"))
        self.assertEqual(gateway.parse_custom_id("dmcp:q1:choice:2"), ("q1", "choice:2"))
        for bad in ("", "dmcp", "dmcp:q1", "dmcp::yes", "dmcp:q1:", "other:q1:yes", "q1:yes"):
            self.assertIsNone(gateway.parse_custom_id(bad), bad)


class ConnectTest(GatewayTestBase):
    def test_identify_and_heartbeat(self) -> None:
        gw = self.started()
        self.assertTrue(gw.ready)
        self.assertIsNone(gw.last_error)

        _, identify = self.fake.wait_for(lambda i, m: m["op"] == 2)
        self.assertEqual(identify["d"]["token"], TOKEN)
        self.assertEqual(identify["d"]["intents"], 0)
        self.assertEqual(identify["d"]["properties"]["browser"], "discord-mcp")
        self.assertIn("v=10", self.fake.paths[0])
        self.assertIn("encoding=json", self.fake.paths[0])

        # interval 100ms なので 2 回目の heartbeat もすぐ来る。d は最後の seq（READY = 1）
        deadline = time.monotonic() + WAIT
        while self.fake.count(lambda i, m: m["op"] == 1) < 2:
            self.assertLess(time.monotonic(), deadline, "heartbeat が 2 回来ませんでした")
            time.sleep(0.02)
        beats = [m for _, m in self.fake.messages if m["op"] == 1]
        self.assertEqual(beats[-1]["d"], 1)
        # ACK が返っているのでつなぎ直していない
        self.assertEqual(len(self.fake.connections), 1)

    def test_server_heartbeat_request(self) -> None:
        self.fake.interval_ms = 600_000  # 定期の heartbeat は来ないようにする
        with mock.patch.object(gateway.random, "random", return_value=0.9):
            self.started()
        self.assertEqual(self.fake.count(lambda i, m: m["op"] == 1), 0)
        self.fake.send_raw({"op": 1, "d": None})
        _, beat = self.fake.wait_for(lambda i, m: m["op"] == 1)
        self.assertEqual(beat["d"], 1)

    def test_gateway_url_from_rest(self) -> None:
        self.rest.gateway_url = self.fake.url
        gw = self.make(url=None)
        gw.start(wait_ready=WAIT)
        self.assertTrue(gw.ready)
        self.rest.wait_for(lambda method, path, body: method == "GET" and path == "/api/v10/gateway/bot")

    def test_gateway_url_from_env(self) -> None:
        os.environ["DISCORD_GATEWAY_URL"] = self.fake.url
        gw = self.make(url=None)
        gw.start(wait_ready=WAIT)
        self.assertTrue(gw.ready)
        self.assertEqual([c for c in self.rest.calls if c[0] == "GET"], [])

    def test_stop(self) -> None:
        gw = self.started()
        gw.stop()
        self.assertFalse(gw.ready)
        self.assertFalse(gw.alive)
        self.assertFalse(gw._thread.is_alive())

    def test_fatal_close_4004(self) -> None:
        self.fake.identify_close_code = 4004
        gw = self.make()
        with self.assertRaises(DiscordError) as cm:
            gw.start(wait_ready=WAIT)
        self.assertIn("Bot トークンが無効です", str(cm.exception))
        self.assertIn("Bot トークンが無効です", gw.last_error or "")
        self.assertFalse(gw.ready)
        self.assertFalse(gw.alive)
        self.assertEqual(len(self.fake.connections), 1)  # 繋ぎ直さない

    def test_fatal_close_4014(self) -> None:
        self.fake.identify_close_code = 4014
        gw = self.make()
        with self.assertRaises(DiscordError):
            gw.start(wait_ready=WAIT)
        self.assertIn("許可されていない intent です", gw.last_error or "")

    def test_start_timeout(self) -> None:
        gw = self.make(url="ws://127.0.0.1:1")  # 誰も待ち受けていない
        with self.assertRaises(DiscordError) as cm:
            gw.start(wait_ready=0.5)
        self.assertIn("READY になりませんでした", str(cm.exception))
        self.assertFalse(gw.alive)


class ReconnectTest(GatewayTestBase):
    def test_op7_resume(self) -> None:
        gw = self.started()
        seq = self.fake.dispatch("TYPING_START", {})
        self.fake.send_raw({"op": 7, "d": None})
        _, resume = self.fake.wait_for(lambda i, m: i == 1 and m["op"] == 6)
        self.assertEqual(resume["d"], {"token": TOKEN, "session_id": "sess-1", "seq": seq})
        self.assertIn("v=10", self.fake.paths[1])
        self._wait_ready(gw)
        # 再開後のセッションでもインタラクションを受け取れる
        gw.register("q1", lambda it: {"type": 6})
        self.fake.dispatch("INTERACTION_CREATE", interaction("dmcp:q1:yes"))
        self.rest.wait_for(lambda method, path, body: path.endswith("/callback"))

    def test_zombie_connection_resumes(self) -> None:
        self.fake.ack = False
        self.started()
        _, resume = self.fake.wait_for(lambda i, m: i >= 1 and m["op"] == 6)
        self.assertEqual(resume["d"]["session_id"], "sess-1")

    def test_invalid_session_resumable(self) -> None:
        self.started()
        self.fake.send_raw({"op": 9, "d": True})
        self.fake.wait_for(lambda i, m: i == 1 and m["op"] == 6)

    def test_invalid_session_not_resumable(self) -> None:
        with mock.patch.object(gateway, "INVALID_SESSION_DELAY", (0.0, 0.05)):
            self.started()
            self.fake.send_raw({"op": 9, "d": False})
            self.fake.wait_for(lambda i, m: i == 1 and m["op"] == 2)
        self.assertEqual(self.fake.count(lambda i, m: m["op"] == 6), 0)

    def test_unexpected_close_resumes(self) -> None:
        self.started()
        self.fake.connections[-1].close(4000, "boom")
        self.fake.wait_for(lambda i, m: i == 1 and m["op"] == 6)

    def _wait_ready(self, gw: gateway.Gateway) -> None:
        deadline = time.monotonic() + WAIT
        while not gw.ready:
            self.assertLess(time.monotonic(), deadline, "READY に戻りませんでした")
            time.sleep(0.02)


class InteractionTest(GatewayTestBase):
    def test_routed_to_handler(self) -> None:
        gw = self.started()
        got: list[dict[str, Any]] = []
        other: list[dict[str, Any]] = []
        gw.register("q1", lambda it: got.append(it) or {"type": 7, "data": {"content": "ok"}})
        gw.register("q2", lambda it: other.append(it) or {"type": 6})
        self.fake.dispatch("INTERACTION_CREATE", interaction("dmcp:q1:yes", iid="111"))
        _, path, body = self.rest.wait_for(lambda m, p, b: p.endswith("/callback"))
        self.assertEqual(path, "/api/v10/interactions/111/tok-111/callback")
        self.assertEqual(body, {"type": 7, "data": {"content": "ok"}})
        self.assertEqual(got[0]["data"]["custom_id"], "dmcp:q1:yes")
        self.assertEqual(other, [])

    def test_modal_submit_routed(self) -> None:
        gw = self.started()
        gw.register("q1", lambda it: {"type": 4, "data": {"content": "受け付けました"}})
        self.fake.dispatch("INTERACTION_CREATE", interaction("dmcp:q1:modal", iid="222", itype=5))
        _, path, body = self.rest.wait_for(lambda m, p, b: p.endswith("/callback"))
        self.assertEqual(path, "/api/v10/interactions/222/tok-222/callback")
        self.assertEqual(body["data"]["content"], "受け付けました")

    def test_unknown_key_ignored(self) -> None:
        gw = self.started()
        gw.register("q1", lambda it: {"type": 6})
        gw.register("gone", lambda it: {"type": 6})
        gw.unregister("gone")
        self.fake.dispatch("INTERACTION_CREATE", interaction("dmcp:other:yes", iid="1"))
        self.fake.dispatch("INTERACTION_CREATE", interaction("dmcp:gone:yes", iid="2"))
        self.fake.dispatch("INTERACTION_CREATE", interaction("someone-else:q1:yes", iid="3"))
        self.fake.dispatch("INTERACTION_CREATE", interaction("dmcp:q1:yes", iid="4", itype=2))  # スラッシュコマンド
        # 最後に自分宛てを送り、その応答が届いた時点で前の 4 件に応答していないことを確かめる
        self.fake.dispatch("INTERACTION_CREATE", interaction("dmcp:q1:yes", iid="9"))
        self.rest.wait_for(lambda m, p, b: p.endswith("/callback"))
        time.sleep(0.2)
        self.assertEqual([c[1] for c in self.rest.callbacks()], ["/api/v10/interactions/9/tok-9/callback"])

    def test_handler_exception(self) -> None:
        gw = self.started()

        def boom(it: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("boom")

        gw.register("q1", boom)
        with self.assertLogs("discord_mcp.gateway", level="ERROR"):
            self.fake.dispatch("INTERACTION_CREATE", interaction("dmcp:q1:yes", iid="5"))
            _, path, body = self.rest.wait_for(lambda m, p, b: p.endswith("/callback"))
        self.assertEqual(path, "/api/v10/interactions/5/tok-5/callback")
        self.assertEqual(body, {"type": 4, "data": {"content": "⚠️ 処理に失敗しました", "flags": 64}})

    def test_handler_none_sends_nothing(self) -> None:
        gw = self.started()
        gw.register("silent", lambda it: None)
        gw.register("q1", lambda it: {"type": 6})
        self.fake.dispatch("INTERACTION_CREATE", interaction("dmcp:silent:yes", iid="6"))
        self.fake.dispatch("INTERACTION_CREATE", interaction("dmcp:q1:yes", iid="7"))
        self.rest.wait_for(lambda m, p, b: p.endswith("/callback"))
        time.sleep(0.2)
        self.assertEqual([c[1] for c in self.rest.callbacks()], ["/api/v10/interactions/7/tok-7/callback"])


class WaitIdleTest(unittest.TestCase):
    """wait_idle は、送っている途中の応答が終わるまで待つ（すぐ終了する cli.py で応答を取りこぼさないため）。"""

    def test_waits_for_inflight_callback(self) -> None:
        gw = gateway.Gateway("dummy", url="ws://127.0.0.1:1")
        sent = threading.Event()

        def slow_api(*args: Any, **kwargs: Any) -> None:
            time.sleep(0.5)
            sent.set()

        gw.register("q1", lambda _i: {"type": 7, "data": {}})
        with mock.patch.object(gateway.discord_api, "api", side_effect=slow_api):
            gw._route({"id": "1", "token": "t", "type": 3, "data": {"custom_id": "dmcp:q1:approve"}})
            self.assertTrue(gw.wait_idle(5))
            self.assertTrue(sent.is_set())

    def test_idle_immediately_and_after_handler_error(self) -> None:
        gw = gateway.Gateway("dummy", url="ws://127.0.0.1:1")
        self.assertTrue(gw.wait_idle(0.1))

        def boom(_i: dict) -> dict:
            raise RuntimeError("x")

        gw.register("q2", boom)
        with mock.patch.object(gateway.discord_api, "api"):
            gw._route({"id": "2", "token": "t", "type": 3, "data": {"custom_id": "dmcp:q2:deny"}})
            self.assertTrue(gw.wait_idle(5))


class SingletonTest(GatewayTestBase):
    def tearDown(self) -> None:
        if gateway._instance is not None:
            gateway._instance.stop()
            gateway._instance = None
        super().tearDown()

    def test_get_gateway(self) -> None:
        os.environ["DISCORD_GATEWAY_URL"] = self.fake.url
        g1 = gateway.get_gateway()
        self.assertTrue(g1.ready)
        self.assertIs(gateway.get_gateway(), g1)
        g1.stop()
        g2 = gateway.get_gateway()
        self.assertIsNot(g2, g1)
        self.assertTrue(g2.ready)
        _, identify = self.fake.wait_for(lambda i, m: m["op"] == 2)
        self.assertEqual(identify["d"]["token"], TOKEN)

    def test_get_gateway_concurrent(self) -> None:
        os.environ["DISCORD_GATEWAY_URL"] = self.fake.url
        results: list[gateway.Gateway] = []
        threads = [threading.Thread(target=lambda: results.append(gateway.get_gateway())) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(WAIT * 2)
        self.assertEqual(len(results), 4)
        self.assertTrue(all(r is results[0] for r in results))
        self.assertEqual(len(self.fake.connections), 1)


if __name__ == "__main__":
    unittest.main()
