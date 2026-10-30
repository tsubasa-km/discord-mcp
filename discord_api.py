"""Discord REST API の共通部分（認証、リクエスト、レート制限、環境変数）。

ほかのモジュール（gateway / components / formatting / media / server）はすべてここを経由して
Discord と通信する。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

USER_AGENT = "DiscordBot (https://github.com/tsubasa-km/discord-mcp, 0.2)"
MAX_RATE_LIMIT_RETRIES = 3
MAX_RATE_LIMIT_SLEEP = 30.0


class DiscordError(ToolError):
    """エージェントに理由をそのまま見せたい失敗（設定漏れ、API エラーなど）。"""

    def __init__(self, message: str, *, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def api_base() -> str:
    return os.environ.get("DISCORD_API_BASE", "https://discord.com/api/v10").rstrip("/")


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise DiscordError(f"環境変数 {name} が設定されていません")
    return value


def token() -> str:
    return env("DISCORD_BOT_TOKEN")


def channel_id() -> str:
    return env("DISCORD_CHANNEL_ID")


def user_ids() -> list[str]:
    return [u.strip() for u in env("DISCORD_USER_ID").split(",") if u.strip()]


def _retry_after(headers: Any, body: str) -> float:
    """429 の待ち秒数。body の retry_after → Retry-After → X-RateLimit-Reset-After の順。最低 1 秒。"""
    try:
        return max(1.0, float(json.loads(body).get("retry_after")))
    except (ValueError, TypeError, AttributeError):
        pass
    for name in ("Retry-After", "X-RateLimit-Reset-After"):
        value = headers.get(name) if headers else None
        if not value:
            continue
        try:
            return max(1.0, float(value))
        except ValueError:
            try:
                return max(1.0, parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError):
                pass
    return 1.0


def request(
    method: str,
    path: str,
    data: bytes | None = None,
    content_type: str | None = None,
    *,
    timeout: float = 30,
) -> Any:
    """Discord API を呼んで JSON を返す（空のレスポンスなら None）。multipart などは data を自分で組み立てて渡す。

    path は "/channels/123/messages" のような API ベース以下のパス。
    """
    headers = {"Authorization": f"Bot {token()}", "User-Agent": USER_AGENT}
    if content_type:
        headers["Content-Type"] = content_type

    for _ in range(MAX_RATE_LIMIT_RETRIES + 1):
        req = urllib.request.Request(api_base() + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                raw = res.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if e.code == 429:
                time.sleep(min(_retry_after(e.headers, detail), MAX_RATE_LIMIT_SLEEP) + 0.1)
                continue
            try:
                code = json.loads(detail).get("code")
            except (ValueError, AttributeError):
                code = None
            raise DiscordError(
                f"Discord API {method} {path} が {e.code} を返しました: {detail[:500]}", status=e.code, code=code
            ) from e
        except urllib.error.URLError as e:
            raise DiscordError(f"Discord API に接続できません: {e.reason}") from e
    raise DiscordError(f"Discord API {method} {path} がレート制限から回復しませんでした", status=429)


def api(method: str, path: str, body: Any = None) -> Any:
    """JSON ボディで Discord API を呼ぶ。"""
    data = json.dumps(body).encode() if body is not None else None
    return request(method, path, data, "application/json" if data is not None else None)
