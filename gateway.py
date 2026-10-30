"""Discord Gateway v10 の最小クライアント（ボタン・セレクト・モーダルのインタラクション受信用）。

server.py のツールは同期関数としてワーカースレッドで動くので、Gateway はバックグラウンドの
デーモンスレッドで websockets.sync.client を使って動かす。インタラクションには 3 秒以内に
応答しないといけないので、受信したらすぐ（別のデーモンスレッドで）ハンドラーを呼び、
返ってきた応答を REST の callback に POST する。

custom_id は "dmcp:{key}:{action}" の形にする。key ごとにハンドラーを登録し、登録のない key の
インタラクションには応答しない。同じ Bot トークンを複数のプロセスで共有していると、
インタラクションはすべてのプロセスの Gateway セッションに届くので、質問を出したプロセスだけが
応答するようにするため。

intents は 0 で IDENTIFY する（インタラクションは intent なしでも届く）。

環境変数:
  DISCORD_GATEWAY_URL  Gateway の URL（テスト用。省略時は GET /gateway/bot が返す url）
"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

import discord_api
from discord_api import DiscordError

log = logging.getLogger("discord_mcp.gateway")

DEFAULT_GATEWAY_URL = "wss://gateway.discord.gg"
GATEWAY_QUERY = "v=10&encoding=json"
CUSTOM_ID_PREFIX = "dmcp"

HELLO_TIMEOUT = 20.0  # 接続してから HELLO が来るまで待つ秒数
OPEN_TIMEOUT = 10.0
CLOSE_TIMEOUT = 3.0
MAX_BACKOFF = 60.0
INVALID_SESSION_DELAY = (1.0, 5.0)  # op 9（d=false）のあと IDENTIFY し直すまでの秒数の範囲
STOP_JOIN_TIMEOUT = 5.0
MAX_PAYLOAD = 16 * 1024 * 1024

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

INTERACTION_MESSAGE_COMPONENT = 3
INTERACTION_MODAL_SUBMIT = 5

# 1000 / 1001 で閉じるとセッションが無効になるので、RESUME したいときはこのコードで閉じる
RESUMABLE_CLOSE_CODE = 4000

# 再接続しても直らない close コード。Gateway を止めて last_error に理由を残す
FATAL_CLOSE_CODES = {
    4004: "Bot トークンが無効です",
    4010: "シャードの指定が無効です",
    4011: "シャーディングが必要です（Bot の参加サーバーが多すぎます）",
    4012: "Gateway の API バージョンが無効です",
    4013: "intent の指定が無効です",
    4014: "許可されていない intent です",
}
# セッションを捨てて IDENTIFY し直す close コード（4007 = seq が無効、4009 = セッションがタイムアウト）
NEW_SESSION_CLOSE_CODES = {4007, 4009}

ERROR_RESPONSE: dict[str, Any] = {"type": 4, "data": {"content": "⚠️ 処理に失敗しました", "flags": 64}}

Handler = Callable[[dict[str, Any]], "dict[str, Any] | None"]


class _Reconnect(Exception):
    """今の接続を捨てて繋ぎ直す合図。resume=False ならセッションも捨てて IDENTIFY し直す。"""

    def __init__(self, reason: str, *, resume: bool, delay: float = 0.0) -> None:
        super().__init__(reason)
        self.resume = resume
        self.delay = delay


class _Fatal(Exception):
    """繋ぎ直しても直らない失敗。Gateway を止める。"""


def parse_custom_id(custom_id: str) -> tuple[str, str] | None:
    """"dmcp:{key}:{action}" を (key, action) に分ける。この形でなければ None。

    action には ":" を含めてよい（最初の 2 つの ":" で分ける）。
    """
    if not isinstance(custom_id, str):
        return None
    parts = custom_id.split(":", 2)
    if len(parts) != 3 or parts[0] != CUSTOM_ID_PREFIX or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def _with_query(url: str) -> str:
    """Gateway の URL に ?v=10&encoding=json を付ける（クエリが既にあればそのまま）。"""
    if "?" in url:
        return url
    return f"{url.rstrip('/')}/?{GATEWAY_QUERY}"


def _is_loopback(url: str) -> bool:
    return (urlsplit(url).hostname or "") in ("127.0.0.1", "localhost", "::1")


class Gateway:
    """Discord Gateway に常時接続し、dmcp: の custom_id を持つインタラクションに応答する。"""

    def __init__(self, token: str, *, url: str | None = None) -> None:
        self._token = token
        self._url = url
        self._gateway_url: str | None = None
        self._handlers: dict[str, Handler] = {}
        self._cond = threading.Condition()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws: ClientConnection | None = None
        self._ready = False
        self._dead = False
        self._last_error: str | None = None
        self._inflight = 0  # 応答を送っている途中のインタラクションの数（_cond で守る）
        # 以下は受信スレッドだけが触る
        self._session_id: str | None = None
        self._resume_url: str | None = None
        self._seq: int | None = None
        self._failures = 0

    # ---- 公開 API ----

    @property
    def ready(self) -> bool:
        """READY / RESUMED を受け取り、いま接続中なら True。"""
        return self._ready

    @property
    def last_error(self) -> str | None:
        """最後に起きた接続エラー（READY になると消える）。致命的な停止の理由もここに入る。"""
        return self._last_error

    @property
    def alive(self) -> bool:
        """受信スレッドが動いていて、stop() も致命的エラーも起きていなければ True。"""
        thread = self._thread
        return thread is not None and thread.is_alive() and not self._stopping.is_set() and not self._dead

    def start(self, wait_ready: float = 15.0) -> None:
        """受信スレッドを起動し、READY になるまで待つ。

        致命的な close（トークン無効など）や時間切れのときは DiscordError を投げる。
        時間切れのときは Gateway を止める。
        """
        with self._cond:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="discord-gateway", daemon=True)
                self._thread.start()
            self._cond.wait_for(lambda: self._ready or self._dead, timeout=wait_ready)
            ready, dead, err = self._ready, self._dead, self._last_error
        if ready:
            return
        if dead:
            raise DiscordError(f"Discord Gateway に接続できません: {err or '停止されています'}")
        self.stop()
        raise DiscordError(
            f"Discord Gateway が {wait_ready:g} 秒以内に READY になりませんでした" + (f": {err}" if err else "")
        )

    def stop(self) -> None:
        """接続を閉じて受信スレッドを止める（最大 STOP_JOIN_TIMEOUT 秒待つ）。"""
        self._stopping.set()
        with self._cond:
            ws, thread = self._ws, self._thread
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - 閉じる途中の失敗は無視してよい
                log.debug("Gateway の close に失敗しました", exc_info=True)
        if thread is not None and thread is not threading.current_thread():
            thread.join(STOP_JOIN_TIMEOUT)

    def register(self, key: str, handler: Handler) -> None:
        """custom_id が dmcp:{key}:... のインタラクションを handler に渡すようにする。"""
        with self._cond:
            self._handlers[key] = handler

    def unregister(self, key: str) -> None:
        with self._cond:
            self._handlers.pop(key, None)

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """送っている途中のインタラクション応答が終わるまで待つ。終われば True。

        応答は別スレッドで送るので、すぐに終了するプロセス（cli.py など）はこれを待たないと
        応答が届かず、Discord に「アプリケーションが時間内に応答しませんでした」と出る。
        """
        with self._cond:
            return self._cond.wait_for(lambda: self._inflight == 0, timeout=timeout)

    # ---- 受信スレッド ----

    def _run(self) -> None:
        """接続 → セッション → 切れたら待って繋ぎ直す、を stop() まで繰り返す。"""
        try:
            while not self._stopping.is_set():
                delay = 0.0
                try:
                    self._session()
                except _Reconnect as e:
                    log.info("Gateway に再接続します: %s", e)
                    if not e.resume:
                        self._reset_session()
                    delay = e.delay
                except _Fatal as e:
                    self._set_error(str(e))
                    log.error("Gateway を停止します: %s", e)
                    return
                except Exception as e:  # noqa: BLE001 - ネットワーク系の失敗はすべて再試行する
                    if self._stopping.is_set():
                        return
                    self._failures += 1
                    delay = self._backoff()
                    self._set_error(f"Gateway に接続できません: {e}")
                    log.warning("Gateway の接続に失敗しました（%.1f 秒後に再試行）: %s", delay, e)
                finally:
                    self._set_ready(False)
                if delay > 0:
                    self._stopping.wait(delay)
        finally:
            with self._cond:
                self._dead = True
                self._ready = False
                self._cond.notify_all()

    def _session(self) -> None:
        """1 本の WebSocket 接続を開いて、閉じるまで受信する。"""
        resume = self._session_id is not None
        url = self._resume_url if resume and self._resume_url else self._base_url()
        with connect(
            url,
            open_timeout=OPEN_TIMEOUT,
            close_timeout=CLOSE_TIMEOUT,
            ping_interval=None,  # 生存確認は Gateway の heartbeat で行う
            compression=None,
            max_size=MAX_PAYLOAD,
            proxy=None if _is_loopback(url) else True,
            user_agent_header=discord_api.USER_AGENT,
        ) as ws:
            with self._cond:
                self._ws = ws
            try:
                if self._stopping.is_set():
                    return
                self._receive_loop(ws, resume)
            except ConnectionClosed as e:
                self._on_closed(e)
            finally:
                with self._cond:
                    self._ws = None
                # with を抜けると 1000 で閉じてセッションが無効になるので、その前に RESUME できるコードで閉じる
                # （stop() のときは既に 1000 で閉じている）
                ws.close(RESUMABLE_CLOSE_CODE)

    def _receive_loop(self, ws: ClientConnection, resume: bool) -> None:
        hello = self._recv_json(ws, HELLO_TIMEOUT)
        if hello.get("op") != OP_HELLO:
            raise RuntimeError(f"最初のメッセージが HELLO ではありません（op {hello.get('op')}）")
        interval = float(hello["d"]["heartbeat_interval"]) / 1000.0

        if resume:
            self._send(ws, OP_RESUME, {"token": self._token, "session_id": self._session_id, "seq": self._seq})
        else:
            self._send(ws, OP_IDENTIFY, self._identify_payload())

        next_beat = time.monotonic() + interval * random.random()
        acked = True
        while True:
            wait = next_beat - time.monotonic()
            if wait <= 0:
                if not acked:
                    raise _Reconnect("heartbeat の ACK が来ません（ゾンビ接続）", resume=True)
                self._send(ws, OP_HEARTBEAT, self._seq)
                acked = False
                next_beat = time.monotonic() + interval
                continue
            try:
                msg = self._recv_json(ws, wait)
            except TimeoutError:
                continue

            op = msg.get("op")
            if op == OP_HEARTBEAT_ACK:
                acked = True
            elif op == OP_HEARTBEAT:
                self._send(ws, OP_HEARTBEAT, self._seq)
            elif op == OP_DISPATCH:
                self._dispatch(msg)
            elif op == OP_RECONNECT:
                raise _Reconnect("サーバーから RECONNECT（op 7）を受け取りました", resume=True)
            elif op == OP_INVALID_SESSION:
                if msg.get("d"):
                    raise _Reconnect("INVALID_SESSION（再開可能）", resume=True)
                raise _Reconnect(
                    "INVALID_SESSION（再開不可）", resume=False, delay=random.uniform(*INVALID_SESSION_DELAY)
                )

    def _identify_payload(self) -> dict[str, Any]:
        return {
            "token": self._token,
            "intents": 0,
            "properties": {"os": sys.platform, "browser": "discord-mcp", "device": "discord-mcp"},
        }

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if msg.get("s") is not None:
            self._seq = msg["s"]
        kind = msg.get("t")
        data = msg.get("d") or {}
        if kind == "READY":
            self._session_id = data.get("session_id")
            resume_url = data.get("resume_gateway_url")
            self._resume_url = _with_query(resume_url) if resume_url else None
            self._on_ready()
            log.info("Gateway に接続しました（READY）")
        elif kind == "RESUMED":
            self._on_ready()
            log.info("Gateway のセッションを再開しました（RESUMED）")
        elif kind == "INTERACTION_CREATE":
            self._route(data)

    def _on_closed(self, e: ConnectionClosed) -> None:
        """サーバー側から切られたときに、止めるか・どう繋ぎ直すかを決める。"""
        if self._stopping.is_set():
            return
        code = e.rcvd.code if e.rcvd is not None else None
        if code in FATAL_CLOSE_CODES:
            raise _Fatal(f"{FATAL_CLOSE_CODES[code]}（close {code}）")
        self._failures += 1
        raise _Reconnect(
            f"接続が切れました（close {code}）", resume=code not in NEW_SESSION_CLOSE_CODES, delay=self._backoff()
        )

    # ---- インタラクション ----

    def _route(self, interaction: dict[str, Any]) -> None:
        """自分が登録した key のインタラクションだけ、別スレッドでハンドラーに渡す。"""
        if interaction.get("type") not in (INTERACTION_MESSAGE_COMPONENT, INTERACTION_MODAL_SUBMIT):
            return
        parsed = parse_custom_id((interaction.get("data") or {}).get("custom_id") or "")
        if parsed is None:
            return
        with self._cond:
            handler = self._handlers.get(parsed[0])
        if handler is None:
            # 同じトークンを使うほかのプロセス宛てかもしれないので、応答しない
            log.debug("未登録の key のインタラクションを無視しました: %s", parsed[0])
            return
        with self._cond:
            self._inflight += 1
        threading.Thread(
            target=self._respond, args=(handler, interaction), name="discord-interaction", daemon=True
        ).start()

    def _respond(self, handler: Handler, interaction: dict[str, Any]) -> None:
        try:
            try:
                response = handler(interaction)
            except Exception:  # noqa: BLE001 - ハンドラーの失敗はユーザーに知らせて続ける
                log.exception("インタラクションのハンドラーが失敗しました")
                response = ERROR_RESPONSE
            if response is None:
                return
            try:
                discord_api.api(
                    "POST", f"/interactions/{interaction['id']}/{interaction['token']}/callback", response
                )
            except (DiscordError, KeyError) as e:
                log.warning("インタラクションへの応答に失敗しました: %s", e)
        finally:
            with self._cond:
                self._inflight -= 1
                self._cond.notify_all()

    # ---- 小物 ----

    def _base_url(self) -> str:
        if self._gateway_url is None:
            url = self._url or os.environ.get("DISCORD_GATEWAY_URL", "").strip() or self._fetch_gateway_url()
            self._gateway_url = _with_query(url)
        return self._gateway_url

    def _fetch_gateway_url(self) -> str:
        """GET /gateway/bot で接続先を聞く。聞けなければ既定の URL を使う。"""
        try:
            info = discord_api.api("GET", "/gateway/bot") or {}
        except DiscordError as e:
            if e.status == 401:
                raise _Fatal(f"{FATAL_CLOSE_CODES[4004]}（GET /gateway/bot が 401）") from e
            log.warning("GET /gateway/bot に失敗したので既定の URL を使います: %s", e)
            return DEFAULT_GATEWAY_URL
        return info.get("url") or DEFAULT_GATEWAY_URL

    def _recv_json(self, ws: ClientConnection, timeout: float) -> dict[str, Any]:
        raw = ws.recv(timeout=timeout)
        msg = json.loads(raw)
        return msg if isinstance(msg, dict) else {}

    def _send(self, ws: ClientConnection, op: int, data: Any) -> None:
        ws.send(json.dumps({"op": op, "d": data}))

    def _backoff(self) -> float:
        """失敗回数に応じた待ち秒数（1, 2, 4, ... 最大 MAX_BACKOFF。ゆらぎ付き）。"""
        return min(MAX_BACKOFF, 2.0 ** max(0, self._failures - 1)) * random.uniform(0.5, 1.0)

    def _reset_session(self) -> None:
        self._session_id = None
        self._resume_url = None
        self._seq = None

    def _on_ready(self) -> None:
        self._failures = 0
        with self._cond:
            self._last_error = None
            self._ready = True
            self._cond.notify_all()

    def _set_ready(self, value: bool) -> None:
        with self._cond:
            self._ready = value
            self._cond.notify_all()

    def _set_error(self, message: str) -> None:
        with self._cond:
            self._last_error = message
            self._cond.notify_all()


_instance: Gateway | None = None
_instance_lock = threading.Lock()


def get_gateway() -> Gateway:
    """プロセスで 1 つの Gateway を返す。初回は作って READY まで待つ。止まっていたら作り直す。"""
    global _instance
    with _instance_lock:
        if _instance is not None and _instance.alive:
            return _instance
        if _instance is not None:
            _instance.stop()
            _instance = None
        gw = Gateway(discord_api.token())
        gw.start()
        _instance = gw
        return gw
