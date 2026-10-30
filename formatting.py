"""Discord に送る本文の整形（分割、表の箇条書き化、リンク、通知の見出し）。

Discord のメッセージは 2000 文字までなので、長い本文はコードブロックやインラインコードを
壊さないように複数のメッセージへ分割する。Discord は Markdown の表を表示できないので、
表は箇条書きに直す。

標準ライブラリだけで動き、Discord とは通信しない（純粋な文字列処理）。
"""

from __future__ import annotations

import re
from urllib.parse import quote

LEVELS: dict[str, tuple[str, int]] = {
    "info": ("ℹ️", 0x3498DB),
    "success": ("✅", 0x2ECC71),
    "warning": ("⚠️", 0xF1C40F),
    "error": ("❌", 0xE74C3C),
}

FENCE = "```"
FENCE_CLOSE = "\n```"
INDICATOR_RESERVE = 10  # " (i/N)" 用に各チャンクで空けておく文字数（足りなければ自動で広げる）
MIN_HEADROOM = 8  # 1 チャンクに本文を最低これだけ入れられないなら limit が小さすぎる
TRUNCATION_NOTICE = "\n\n…（長すぎるため残り {dropped} 文字を省略しました）"
EMPTY_MESSAGE_ERROR = "空のメッセージは送れません"


def clip(text: str, limit: int) -> str:
    """text を limit 文字以内に切り詰める。切ったときは末尾を「…」にする。"""
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# 分割
# ---------------------------------------------------------------------------

_UNESCAPED_BACKTICK = re.compile(r"(?<!\\)`")


def _is_fence_line(line: str) -> bool:
    return line.lstrip().startswith(FENCE)


def _fence_state(text: str, lang: str | None) -> str | None:
    """text を先頭から読んで、末尾でコードブロックが開いていればその言語（無名なら ""）を返す。

    lang は text の直前の時点の状態（None = コードブロックの外）。
    """
    for line in text.split("\n"):
        if not _is_fence_line(line):
            continue
        if lang is None:
            info = line.lstrip()[len(FENCE):].strip()
            lang = info.split()[0] if info else ""
        else:
            lang = None
    return lang


def _odd_backtick_pos(piece: str) -> int:
    """フェンス行以外にある、エスケープされていないバッククォートが奇数個なら最後の位置を返す。

    偶数個（インラインコードが閉じている）なら -1。
    """
    positions: list[int] = []
    offset = 0
    for line in piece.split("\n"):
        if not _is_fence_line(line):
            positions.extend(offset + m.start() for m in _UNESCAPED_BACKTICK.finditer(line))
        offset += len(line) + 1
    return positions[-1] if len(positions) % 2 == 1 else -1


def _split_pass(
    text: str,
    limit: int,
    reserve: int,
    *,
    max_count: int | None = None,
    extra_last: int = 0,
) -> tuple[list[str], str]:
    """text をチャンクに分け、(チャンク, 送りきれなかった残り) を返す。

    各チャンクは limit - reserve 文字以内（max_count 個目だけはさらに extra_last 文字短い）。
    max_count を指定するとその個数で打ち切り、残りを返す。
    """
    # Ported from NousResearch/hermes-agent (MIT): gateway/platforms/base.py (truncate_message)
    # 巨大な入力でも O(n) で済むよう、残りは text のスライスではなく位置 pos で持つ
    chunks: list[str] = []
    n = len(text)
    pos = 0
    lang: str | None = None  # 直前のチャンクの末尾で開いていたコードブロックの言語
    while pos < n:
        if max_count is not None and len(chunks) >= max_count:
            break
        is_last_slot = max_count is not None and len(chunks) == max_count - 1
        budget = limit - reserve - (extra_last if is_last_slot else 0)

        prefix = ""
        if lang is not None:
            prefix = f"{FENCE}{lang}\n"
            # 異常に長い言語名で本文が入らなくなるのを防ぐ
            if len(prefix) > (budget - len(FENCE_CLOSE)) // 4:
                prefix = f"{FENCE}\n"
        headroom = budget - len(prefix) - len(FENCE_CLOSE)
        if headroom < MIN_HEADROOM:
            raise ValueError(f"limit={limit} は小さすぎて分割できません")

        # 残りが丸ごと入るならそれで終わり
        if len(prefix) + (n - pos) <= budget:
            rest_text = text[pos:]
            tail = prefix + rest_text
            if _fence_state(rest_text, lang) is not None:
                tail += FENCE_CLOSE
            if len(tail) <= budget:
                chunks.append(tail)
                pos = n
                break

        end = pos + headroom
        split_at = text.rfind("\n", pos, end) - pos
        if split_at < headroom // 2:
            split_at = text.rfind(" ", pos, end) - pos
            if split_at < 1:
                split_at = headroom

        # インラインコードの途中で切らない
        tick = _odd_backtick_pos(text[pos : pos + split_at])
        if tick >= 0:
            safe = max(text.rfind(" ", pos, pos + tick), text.rfind("\n", pos, pos + tick)) - pos
            if safe > max(0, headroom // 4):
                split_at = safe

        body = text[pos : pos + split_at]
        pos += split_at
        lang = _fence_state(body, lang)
        chunk = prefix + body
        if lang is not None:
            chunk += FENCE_CLOSE
            # コードブロック内はインデントを保つため、区切りに使った 1 文字だけ落とす
            if pos < n and text[pos] in "\n ":
                pos += 1
        else:
            while pos < n and text[pos].isspace():
                pos += 1
        chunks.append(chunk)
    return chunks, text[pos:]


def split_message(text: str, limit: int = 2000, max_chunks: int = 8) -> list[str]:
    """本文を Discord のメッセージ上限（limit 文字）以内のチャンクに分ける。

    - 収まるならそのまま [text] を返す
    - 改行 → 空白 → 強制の順で区切り位置を探す
    - コードブロック（```）の途中で切ったら閉じ、次のチャンクで同じ言語で開き直す
    - インラインコード（`...`）の途中ではなるべく切らない
    - 複数に分けたときは各チャンクの末尾に " (i/N)" を付ける
    - max_chunks を超える分は送らず、最後のチャンクに省略した文字数の注記を付ける

    返すチャンクはすべて limit 文字以内、個数は max_chunks 以下であることを保証する。
    """
    if not text or not text.strip():
        raise ValueError(EMPTY_MESSAGE_ERROR)
    if max_chunks < 1:
        raise ValueError("max_chunks は 1 以上を指定してください")
    if len(text) <= limit:
        return [text]

    # " (i/N)" が予約幅に収まるまで予約を広げてやり直す
    reserve = INDICATOR_RESERVE
    while True:
        chunks, _ = _split_pass(text, limit, reserve)
        total = len(chunks)
        need = len(f" ({total}/{total})")
        if need <= reserve:
            break
        reserve = need

    if total <= max_chunks:
        return [f"{c} ({i}/{total})" for i, c in enumerate(chunks, 1)]

    # Ported from NousResearch/hermes-agent (MIT): plugins/platforms/discord/adapter.py (_cap_split_chunks)
    # 最後に残すチャンクは注記の分だけ短くして作り直す（N は打ち切り前の総数のまま）
    extra = len(TRUNCATION_NOTICE.format(dropped=len(text)))
    kept, rest = _split_pass(text, limit, reserve, max_count=max_chunks, extra_last=extra)
    out = [f"{c} ({i}/{total})" for i, c in enumerate(kept, 1)]
    if rest:
        out[-1] = f"{kept[-1]}{TRUNCATION_NOTICE.format(dropped=len(rest))} ({len(kept)}/{total})"
    return out


# ---------------------------------------------------------------------------
# 表 → 箇条書き
# ---------------------------------------------------------------------------

_SEPARATOR_CELL = re.compile(r"^\s*:?-+:?\s*$")
_CELL_SPLIT = re.compile(r"(?<!\\)\|")


def _cells(line: str) -> list[str]:
    """表の 1 行をセルに分ける。両端のパイプは省略可。\\| はパイプとして扱う。"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in _CELL_SPLIT.split(s)]


def _is_separator(line: str, columns: int) -> bool:
    if "-" not in line or "|" not in line:
        return False
    cells = _cells(line)
    return len(cells) == columns and all(_SEPARATOR_CELL.match(c) for c in cells)


def _row_to_bullet(headers: list[str], values: list[str]) -> str | None:
    parts = []
    for i, value in enumerate(values):
        if not value:
            continue
        header = headers[i] if i < len(headers) else ""
        parts.append(f"**{header}**: {value}" if header else value)
    return f"- {' / '.join(parts)}" if parts else None


def tables_to_bullets(text: str) -> str:
    """GFM のパイプ表を箇条書きに直す（Discord は表を表示できないため）。

    表 = ヘッダー行（`| a | b |`）+ 区切り行（`|---|:--:|`）+ データ行。
    データ行 1 行が箇条書き 1 項目になる::

        | 名前 | 状態 |
        |---|---|          →   - **名前**: build / **状態**: OK
        | build | OK |

    空のセルは省き、ヘッダーが空の列は値だけを書く。データ行のない表、表でないパイプ、
    コードブロック（```）の中はそのまま残す。
    """
    # Ported from NousResearch/hermes-agent (MIT): plugins/platforms/discord/adapter.py (format_message → convert_table_to_bullets)
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if _is_fence_line(line):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if not in_fence and "|" in line and i + 1 < len(lines):
            headers = _cells(line)
            if len(headers) >= 2 and _is_separator(lines[i + 1], len(headers)):
                j = i + 2
                bullets: list[str] = []
                while j < len(lines) and "|" in lines[j] and lines[j].strip() and not _is_fence_line(lines[j]):
                    bullet = _row_to_bullet(headers, _cells(lines[j]))
                    if bullet:
                        bullets.append(bullet)
                    j += 1
                if j > i + 2:
                    out.extend(bullets)
                    i = j
                    continue
        out.append(line)
        i += 1
    return "\n".join(out)


# ---------------------------------------------------------------------------
# リンク・通知
# ---------------------------------------------------------------------------

_SCHEME = re.compile(r"^https?://", re.IGNORECASE)


def md_link(label: str, url: str) -> str:
    """Discord 用の Markdown リンク "[label](<url>)" を作る。

    - label 先頭の http(s):// は外す（URL の形のラベルは Discord で崩れるため）
    - label の \\ [ ] はエスケープする
    - URL を <> で囲んで埋め込みプレビュー（embed）を抑止する。空白や <> などは % エンコードする
    """
    # Ported from NousResearch/hermes-agent (MIT): plugins/platforms/discord/adapter.py (_format_discord_markdown_link)
    text = _SCHEME.sub("", label.strip(), count=1) or _SCHEME.sub("", url.strip(), count=1)
    text = text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
    safe_url = quote(url.strip(), safe=":/?#[]@!$&'()*+,;=%~")
    return f"[{text}](<{safe_url}>)"


def _check_level(level: str) -> tuple[str, int]:
    try:
        return LEVELS[level]
    except (KeyError, TypeError):
        raise ValueError(
            f"level は {', '.join(LEVELS)} のいずれかを指定してください（指定値: {level!r}）"
        ) from None


def level_color(level: str) -> int:
    """level に対応する埋め込み（embed）の色を返す。"""
    return _check_level(level)[1]


# 先頭に絵文字を足すと Markdown のブロック記法が効かなくなる行
_BLOCK_START = re.compile(r"^(```|#{1,3}\s|>|[-*+]\s|\d+\.\s)")


def format_notification(message: str, level: str = "info", title: str | None = None) -> str:
    """通知の本文を整える。分割はしない（必要なら split_message に渡す）。

    - title があれば 1 行目を "{絵文字} **{title}**" にし、その下に本文を置く
    - title がなければ本文の 1 行目の先頭に絵文字を付ける。ただし 1 行目がコードブロック・見出し・
      引用・箇条書きのときは記法を壊さないよう絵文字を単独の行にする
    - 本文の表は箇条書きに直す
    """
    emoji, _ = _check_level(level)
    title = " ".join(title.split()) if title else ""
    body = tables_to_bullets(message.strip("\n")) if message and message.strip() else ""
    if title:
        return f"{emoji} **{title}**" + (f"\n{body}" if body else "")
    if not body:
        raise ValueError(EMPTY_MESSAGE_ERROR)
    sep = "\n" if _BLOCK_START.match(body) else " "
    return f"{emoji}{sep}{body}"
