"""Discord への添付ファイル送信・リアクション・タイピング表示・添付ファイルの取得。

- send_message / edit_message: ファイルパスを渡すと multipart/form-data で添付する。
  フォーラム / メディアチャンネル（type 15 / 16）には直接投稿できないので、
  1 回の送信ごとにスレッド（投稿）を作り、メッセージをその最初の投稿にする。
- add_reaction / remove_own_reaction / trigger_typing: 失敗しても例外にしない（ログだけ残して False）。
- download_attachments: 受信メッセージの添付ファイルを Discord CDN から保存する。

multipart のファイル名について:
  RFC 7578 4.2 は multipart/form-data で RFC 5987 の filename* を使うことを禁じているので、
  ブラウザと同じく filename="..." に UTF-8 をそのまま入れる（" と CR / LF だけ %22 / %0D / %0A に
  置き換える）。加えて payload_json の attachments[].filename にも元の名前を入れる。Discord は
  表示名にこちらを使うので、パートヘッダー側の解釈が揺れても日本語のファイル名が保たれる。

stdout は MCP の通信路なので print しない。ログは logging（stderr）に出す。
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from discord_api import USER_AGENT, DiscordError, api, request

log = logging.getLogger(__name__)

# Ported from NousResearch/hermes-agent (MIT): adapter.py（_DISCORD_DEFAULT_UPLOAD_LIMIT_BYTES ほか）
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_FILES_PER_MESSAGE = 10
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024

FORUM_CHANNEL_TYPES = {15, 16}  # GUILD_FORUM / GUILD_MEDIA
MAX_THREAD_NAME = 100
DEFAULT_THREAD_NAME = "通知"
UPLOAD_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 60
DEFAULT_CDN_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})
CHUNK = 64 * 1024

_channel_types: dict[str, int] = {}
_channel_types_lock = threading.Lock()


# ---------------------------------------------------------------- チャンネル種別


def channel_type(channel_id: str) -> int:
    """チャンネルの type を返す（GET /channels/{id}）。プロセス内でキャッシュする。"""
    channel_id = str(channel_id)
    with _channel_types_lock:
        if channel_id in _channel_types:
            return _channel_types[channel_id]
    info = api("GET", f"/channels/{channel_id}") or {}
    try:
        kind = int(info["type"])
    except (KeyError, TypeError, ValueError) as e:
        raise DiscordError(f"チャンネル {channel_id} の種別が取得できませんでした") from e
    with _channel_types_lock:
        _channel_types[channel_id] = kind
    return kind


def _is_forum(channel_id: str) -> bool:
    """フォーラム系チャンネルか。権限不足・存在しないときはそのまま失敗させ、それ以外の失敗は通常チャンネル扱い。"""
    try:
        return channel_type(channel_id) in FORUM_CHANNEL_TYPES
    except DiscordError as e:
        if e.status in (401, 403, 404):
            raise
        log.warning("チャンネル %s の種別が取れなかったので通常チャンネルとして送ります: %s", channel_id, e)
        return False


# ---------------------------------------------------------------- multipart


def _validate_files(files: list[str]) -> list[Path]:
    """送信前にファイルの存在・サイズ・個数を確認する。問題があれば 1 回もリクエストせずに失敗させる。"""
    if len(files) > MAX_FILES_PER_MESSAGE:
        raise DiscordError(
            f"添付ファイルが多すぎます: {len(files)} 個（1 メッセージ {MAX_FILES_PER_MESSAGE} 個まで）"
        )
    paths: list[Path] = []
    for f in files:
        p = Path(f).expanduser()
        if not p.is_file():
            raise DiscordError(f"ファイルが見つかりません: {f}")
        size = p.stat().st_size
        # Ported from NousResearch/hermes-agent (MIT): adapter.py（大きすぎるファイルはアップロード前に断る）
        if size > MAX_UPLOAD_BYTES:
            raise DiscordError(
                f"ファイルが大きすぎます: {p.name} は {size / 1024 / 1024:.1f} MB"
                f"（上限 {MAX_UPLOAD_BYTES // 1024 // 1024} MB）"
            )
        paths.append(p)
    return paths


def _header_filename(name: str) -> str:
    """Content-Disposition の filename="..." に入れる形にする（RFC 7578 / ブラウザと同じ）。"""
    return name.replace('"', "%22").replace("\r", "%0D").replace("\n", "%0A")


def _attachment_entries(payload: dict[str, Any], paths: list[Path]) -> tuple[list[dict[str, Any]], int]:
    """payload_json に入れる attachments 配列と、新しいファイルの最初の id を返す。

    payload に既存の attachments（編集時に残す添付など）があれば先頭に残し、新しいファイルの id は
    それと重ならない番号から振る。
    """
    existing = list(payload.get("attachments") or [])
    used = {str(a.get("id")) for a in existing if isinstance(a, dict)}
    start = 0
    while any(str(i) in used for i in range(start, start + len(paths))):
        start += 1
    new = [{"id": start + i, "filename": p.name} for i, p in enumerate(paths)]
    return existing + new, start


def _multipart(payload_json: dict[str, Any], paths: list[Path], first_id: int) -> tuple[bytes, str]:
    """payload_json と files[N] から multipart/form-data の本文を作る。(body, content_type) を返す。"""
    boundary = "----DiscordMCP" + secrets.token_hex(16)
    parts: list[bytes] = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="payload_json"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        json.dumps(payload_json, ensure_ascii=False).encode("utf-8"),
        b"\r\n",
    ]
    for i, p in enumerate(paths):
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        parts += [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="files[{first_id + i}]"; '
                f'filename="{_header_filename(p.name)}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {ctype}\r\n\r\n".encode(),
            p.read_bytes(),
            b"\r\n",
        ]
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _require_attachments(message: Any, count: int, what: str) -> None:
    # Ported from NousResearch/hermes-agent (MIT): adapter_media.py（添付したのに attachments が空なら失敗）
    if not isinstance(message, dict) or not message.get("attachments"):
        raise DiscordError(f"{what}: {count} 個のファイルを送りましたが、Discord 側に添付が付いていません")


# ---------------------------------------------------------------- 送信・編集


def _thread_name(payload: dict[str, Any]) -> str:
    """フォーラム投稿のタイトル。本文の最初の空でない行から作る。"""
    # Ported from NousResearch/hermes-agent (MIT): adapter.py（先頭行から "#" を除いてスレッド名にする）
    candidates = [payload.get("content") or ""]
    candidates += [e.get("title") or "" for e in payload.get("embeds") or [] if isinstance(e, dict)]
    for text in candidates:
        for line in str(text).splitlines():
            line = re.sub(r"<(?:@[!&]?|#)\d+>", "", line)
            line = re.sub(r"@(?:everyone|here)\b", "", line)
            line = " ".join(line.split()).lstrip("#").strip()
            if line:
                return line[:MAX_THREAD_NAME]
    return DEFAULT_THREAD_NAME


def send_message(channel_id: str, payload: dict, files: list[str] | None = None) -> dict:
    """メッセージを送る。files にパスを渡すと添付する。フォーラムチャンネルなら投稿（スレッド）を作る。

    Returns:
        {"message_id", "channel_id", "message": 生のメッセージ}。フォーラム投稿のときは
        "thread_id"（= 作られた投稿のチャンネル ID。channel_id も同じ値）が付く。
    """
    channel_id = str(channel_id)
    paths = _validate_files(files) if files else []
    forum = _is_forum(channel_id)

    message_body: dict[str, Any] = dict(payload)
    first_id = 0
    if paths:
        message_body["attachments"], first_id = _attachment_entries(payload, paths)

    if forum:
        path = f"/channels/{channel_id}/threads"
        body: dict[str, Any] = {"name": _thread_name(payload), "message": message_body}
    else:
        path = f"/channels/{channel_id}/messages"
        body = message_body

    if paths:
        data, ctype = _multipart(body, paths, first_id)
        res = request("POST", path, data, ctype, timeout=UPLOAD_TIMEOUT)
    else:
        res = api("POST", path, body)
    res = res or {}

    if forum:
        thread_id = str(res.get("id", ""))
        message = res.get("message") or {}
        if paths:
            _require_attachments(message, len(paths), "フォーラムへの投稿")
        # フォーラムの最初の投稿のメッセージ ID はスレッド ID と同じ
        return {
            "message_id": str(message.get("id") or thread_id),
            "channel_id": thread_id,
            "thread_id": thread_id,
            "message": message,
        }

    if paths:
        _require_attachments(res, len(paths), "メッセージの送信")
    return {"message_id": str(res.get("id", "")), "channel_id": str(res.get("channel_id") or channel_id), "message": res}


def edit_message(channel_id: str, message_id: str, payload: dict, files: list[str] | None = None) -> dict:
    """メッセージを編集する（PATCH）。files を渡すと添付を追加する。生のメッセージを返す。

    Discord の仕様では PATCH の attachments は「残す添付の一覧」なので、既存の添付を残したいときは
    payload["attachments"] に [{"id": "<既存の添付 ID>"}] を入れておく（新しいファイルはその後ろに足す）。
    """
    path = f"/channels/{channel_id}/messages/{message_id}"
    if not files:
        return api("PATCH", path, payload) or {}
    paths = _validate_files(files)
    body: dict[str, Any] = dict(payload)
    body["attachments"], first_id = _attachment_entries(payload, paths)
    data, ctype = _multipart(body, paths, first_id)
    res = request("PATCH", path, data, ctype, timeout=UPLOAD_TIMEOUT) or {}
    _require_attachments(res, len(paths), "メッセージの編集")
    return res


# ---------------------------------------------------------------- リアクション・タイピング（失敗しても止めない）


def _emoji_path(emoji: str) -> str:
    """URL に入れる絵文字。<:name:id> / <a:name:id> 形式のカスタム絵文字は name:id にする。"""
    m = re.fullmatch(r"<a?:(\w+):(\d+)>", emoji.strip())
    value = f"{m.group(1)}:{m.group(2)}" if m else emoji.strip()
    return urllib.parse.quote(value, safe="")


def _soft(what: str, method: str, path: str) -> bool:
    # Ported from NousResearch/hermes-agent (MIT): adapter.py（リアクションは失敗してもログだけ）
    try:
        api(method, path)
        return True
    except (DiscordError, OSError) as e:
        log.warning("%sに失敗しました（続行します）: %s", what, e)
        return False


def add_reaction(channel_id: str, message_id: str, emoji: str) -> bool:
    """メッセージにリアクションを付ける。失敗したら False（例外にしない）。"""
    return _soft(
        "リアクションの追加", "PUT", f"/channels/{channel_id}/messages/{message_id}/reactions/{_emoji_path(emoji)}/@me"
    )


def remove_own_reaction(channel_id: str, message_id: str, emoji: str) -> bool:
    """自分（Bot）が付けたリアクションを外す。失敗したら False（例外にしない）。"""
    return _soft(
        "リアクションの削除",
        "DELETE",
        f"/channels/{channel_id}/messages/{message_id}/reactions/{_emoji_path(emoji)}/@me",
    )


def trigger_typing(channel_id: str) -> bool:
    """「入力中…」を表示する（約 10 秒で消える）。失敗したら False（例外にしない）。"""
    return _soft("入力中表示", "POST", f"/channels/{channel_id}/typing")


# ---------------------------------------------------------------- 添付ファイルの取得


def _allowed_hosts() -> tuple[set[str], set[str]]:
    """(https で許可するホスト, http でも許可するホスト)。後者は DISCORD_CDN_HOSTS（テスト用）のものだけ。"""
    extra = {h.strip().lower() for h in os.environ.get("DISCORD_CDN_HOSTS", "").split(",") if h.strip()}
    return set(DEFAULT_CDN_HOSTS) | extra, extra


def _url_allowed(url: str) -> bool:
    try:
        u = urllib.parse.urlsplit(url)
        host = (u.hostname or "").lower()
        netloc = f"{host}:{u.port}" if u.port else host
    except ValueError:
        return False
    https_hosts, http_hosts = _allowed_hosts()
    if u.scheme == "https":
        return host in https_hosts or netloc in https_hosts
    if u.scheme == "http":
        return host in http_hosts or netloc in http_hosts
    return False


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    """リダイレクト先も許可リストで確認する（CDN から別ホストへ飛ばされないように）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if not _url_allowed(newurl):
            raise urllib.error.URLError(f"許可されていないリダイレクト先です: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_CheckedRedirect)

_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])$", re.IGNORECASE)
MAX_FILENAME = 200


def _safe_filename(name: Any, fallback: str = "attachment") -> str:
    """保存用のファイル名にする。ディレクトリ部分を捨て、制御文字・Windows で使えない文字を置き換える。"""
    name = str(name or "").replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if ord(c) >= 32 and ord(c) != 127)
    name = re.sub(r'[<>:"|?*]', "_", name)
    name = name.strip().rstrip(". ")
    if not name or set(name) <= {"."}:
        name = fallback
    if _WINDOWS_RESERVED.match(name.split(".")[0].strip()):
        name = "_" + name
    if len(name) > MAX_FILENAME:
        base, ext = os.path.splitext(name)
        ext = ext if len(ext) <= 20 else ""
        name = base[: MAX_FILENAME - len(ext)] + ext
    return name


def _open_unique(dest: Path, name: str) -> tuple[Path, Any]:
    """dest/name を新規作成で開く。既にあれば "name (2).ext" のように番号を付ける（上書きしない）。"""
    base, ext = os.path.splitext(name)
    n = 1
    while True:
        candidate = dest / (name if n == 1 else f"{base} ({n}){ext}")
        try:
            return candidate, open(candidate, "xb")
        except FileExistsError:
            n += 1


def download_attachments(message: dict, dest_dir: str | Path, max_bytes: int = MAX_DOWNLOAD_BYTES) -> dict:
    """メッセージの添付ファイルを dest_dir に保存する。1 件の失敗では例外にせず skipped に入れる。

    Discord CDN（cdn.discordapp.com / media.discordapp.net）の https URL だけを取りに行き、
    Authorization ヘッダーは付けない。サイズ上限は max_bytes（既定 32 MB）。

    Returns:
        {"saved": [{"filename", "path", "size", "content_type", "url"}], "skipped": [{"filename", "reason"}]}
    """
    saved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    attachments = [a for a in (message or {}).get("attachments") or [] if isinstance(a, dict)]
    if not attachments:
        return {"saved": saved, "skipped": skipped}

    dest = Path(dest_dir).expanduser()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise DiscordError(f"保存先のフォルダーを作れません: {dest}（{e}）") from e

    for att in attachments:
        original = str(att.get("filename") or "")
        url = str(att.get("url") or att.get("proxy_url") or "")
        try:
            declared = int(att.get("size") or 0)
        except (TypeError, ValueError):
            declared = 0
        # Ported from NousResearch/hermes-agent (MIT): adapter_media.py（受信ファイルの上限）
        if declared > max_bytes:
            skipped.append({"filename": original, "reason": "大きすぎます"})
            continue
        if not url or not _url_allowed(url):
            skipped.append({"filename": original, "reason": "許可されていない URL"})
            continue

        name = _safe_filename(original, fallback=f"attachment-{att.get('id') or len(saved) + len(skipped) + 1}")
        path: Path | None = None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with _opener.open(req, timeout=DOWNLOAD_TIMEOUT) as res:
                length = res.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > max_bytes:
                    skipped.append({"filename": original, "reason": "大きすぎます"})
                    continue
                header_type = (res.headers.get("Content-Type") or "").split(";")[0].strip()
                path, fh = _open_unique(dest, name)
                total = 0
                too_big = False
                with fh:
                    while chunk := res.read(CHUNK):
                        total += len(chunk)
                        if total > max_bytes:
                            too_big = True
                            break
                        fh.write(chunk)
                if too_big:
                    path.unlink(missing_ok=True)
                    skipped.append({"filename": original, "reason": "大きすぎます"})
                    continue
        except (urllib.error.URLError, OSError, ValueError) as e:
            if path is not None:
                path.unlink(missing_ok=True)
            reason = getattr(e, "reason", None) or e
            skipped.append({"filename": original, "reason": f"ダウンロードに失敗しました: {reason}"})
            continue

        saved.append(
            {
                "filename": path.name,
                "path": str(path.resolve()),
                "size": total,
                "content_type": att.get("content_type")
                or header_type
                or mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
                "url": url,
            }
        )
    return {"saved": saved, "skipped": skipped}
