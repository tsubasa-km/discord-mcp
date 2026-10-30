"""Discord notify / ask MCP server.

Claude のスケジュールタスクなど無人で動くエージェントから、Discord に通知を送ったり
人間に質問して返事を受け取ったりするための stdio MCP サーバー。

- 通知: 長文は分割し、レベルごとの絵文字・見出し・添付ファイルを付けられる
- 質問: 選択肢や承認はボタン（多いときは選択メニュー）で答えられる。スレッドへの文字の返信でも答えられる
- 返事が時間内に来なかった質問は state ファイルに保留として残し、次回の実行で check_answers から回収する

ボタンの押下は Discord Gateway（WebSocket）で受け取る。Gateway に接続できないときや
DISCORD_BUTTONS=0 のときは、ボタンを付けずにスレッドへの文字の返信だけで受け付ける。

環境変数:
  DISCORD_BOT_TOKEN   Bot トークン（必須）
  DISCORD_CHANNEL_ID  通知・質問を投稿するチャンネル ID（必須。フォーラムチャンネルも可）
  DISCORD_USER_ID     質問の宛先 = 回答として受け付けるユーザー ID（必須、カンマ区切りで複数可）
  DISCORD_BUTTONS     0 にするとボタンを使わない（既定は 1）
  DISCORD_MCP_STATE   保留中の質問を保存する JSON ファイル（省略時は server.py と同じ場所の state.json）
  DISCORD_API_BASE    API のベース URL（テスト用。省略時は https://discord.com/api/v10）
  DISCORD_GATEWAY_URL Gateway の URL（テスト用）
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

import components as ui
import formatting as fmt
import gateway
import media
from discord_api import DiscordError, api, channel_id as _channel_id, user_ids as _user_ids

log = logging.getLogger("discord-mcp")

STATE_PATH = Path(os.environ.get("DISCORD_MCP_STATE") or Path(__file__).with_name("state.json"))

MAX_CONTENT = 2000
MAX_THREAD_NAME = 100
POLL_SECONDS = 3
MAX_TIMEOUT_SECONDS = 1800
THREAD_ARCHIVE_MINUTES = 10080  # 7 日
RECEIVED_EMOJI = "✅"

EMPTY_ANSWER_WARNING = (
    "返信はあったが本文が空でした。Developer Portal の Bot 設定で "
    "MESSAGE CONTENT INTENT がオフになっている可能性があります。"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _buttons_enabled() -> bool:
    return os.environ.get("DISCORD_BUTTONS", "1").strip().lower() not in {"0", "false", "no", "off"}


# ---------------------------------------------------------------- 保留中の質問


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"questions": {}}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def _stored_prompt(q: dict[str, Any]) -> ui.Prompt:
    """state の 1 件から Prompt を復元する。prompt を持たない古い形式は文字で答える質問として扱う。"""
    if "prompt" in q:
        return ui.Prompt.from_dict(q["prompt"])
    return ui.Prompt(qid="legacy", kind="text", question=q.get("question", ""), allowed_user_ids=_user_ids())


# ---------------------------------------------------------------- 返信の読み取り


def _user_replies(thread_id: str, after_id: str) -> list[dict[str, Any]]:
    """スレッド内で after_id より後に、宛先ユーザーが書いたメッセージを古い順に返す。"""
    allowed = set(_user_ids())
    found: list[dict[str, Any]] = []
    cursor = after_id
    while True:
        batch = api("GET", f"/channels/{thread_id}/messages?after={cursor}&limit=100") or []
        if not batch:
            break
        batch.sort(key=lambda m: int(m["id"]))
        found.extend(m for m in batch if m.get("author", {}).get("id") in allowed)
        cursor = batch[-1]["id"]
        if len(batch) < 100:
            break
    return found


def _author_name(message: dict[str, Any]) -> str:
    author = message.get("author") or {}
    return author.get("global_name") or author.get("username") or str(author.get("id", ""))


def _text_answer(p: ui.Prompt, replies: list[dict[str, Any]], download_dir: str | None) -> dict[str, Any]:
    """スレッドへの返信をまとめて 1 つの回答にする（番号や「はい」は選択肢・承認として解釈する）。"""
    text = "\n".join(m.get("content", "") for m in replies if m.get("content"))
    attachments = [
        {"filename": a.get("filename"), "url": a.get("url"), "size": a.get("size")}
        for m in replies
        for a in m.get("attachments", [])
    ]
    result = ui.interpret_text_answer(p, text) if text else {"kind": "text", "answer": "", "via": "text"}
    result["answered_by"] = _author_name(replies[-1])
    if attachments:
        result["attachments"] = attachments
        if download_dir:
            saved, skipped = [], []
            for m in replies:
                got = media.download_attachments(m, download_dir)
                saved += got["saved"]
                skipped += got["skipped"]
            result["downloaded"] = saved
            if skipped:
                result["download_skipped"] = skipped
    elif not text:
        result["warning"] = EMPTY_ANSWER_WARNING
    return result


def _public(result: dict[str, Any]) -> dict[str, Any]:
    """components の回答から、エージェントに返す項目だけを取り出す。"""
    out = {k: v for k, v in result.items() if k != "user"}
    if "user" in result:
        out["answered_by"] = result["user"].get("name")
    return out


def _finish_text_answer(q: dict[str, Any], p: ui.Prompt, replies: list[dict[str, Any]], download_dir: str | None):
    """文字の返信で回答されたとき: 質問メッセージを回答済みに書き換え、返信に ✅ を付ける。"""
    result = _text_answer(p, replies, download_dir)
    p.resolved = True
    try:
        media.edit_message(q["message_channel"], q["message_id"], ui.answered_by_text_edit(p, result, result["answered_by"]))
    except DiscordError as e:
        log.warning("質問メッセージの書き換えに失敗しました: %s", e)
    media.add_reaction(q["thread_id"], replies[-1]["id"], RECEIVED_EMOJI)
    return result


# ---------------------------------------------------------------- 質問の投稿と待機


def _open_thread(channel: str, message_id: str, p: ui.Prompt) -> str:
    title = " ".join(f"{f'[{p.task}] ' if p.task else ''}{p.question}".split())
    thread = api(
        "POST",
        f"/channels/{channel}/messages/{message_id}/threads",
        {"name": fmt.clip(title, MAX_THREAD_NAME), "auto_archive_duration": THREAD_ARCHIVE_MINUTES},
    )
    return thread["id"]


def _connect_gateway() -> tuple[Any | None, str | None]:
    """ボタン用に Gateway へ接続する。使えないときは (None, 理由)。"""
    if not _buttons_enabled():
        return None, "DISCORD_BUTTONS=0 のためボタンを使いません"
    try:
        return gateway.get_gateway(), None
    except DiscordError as e:
        log.warning("Gateway に接続できないため、ボタンなしで質問します: %s", e)
        return None, f"Gateway に接続できないため、ボタンなしで質問しました: {e}"


def _ask(p: ui.Prompt, timeout_seconds: int, download_dir: str | None) -> dict[str, Any]:
    timeout_seconds = max(0, min(int(timeout_seconds), MAX_TIMEOUT_SECONDS))
    gw, note = _connect_gateway() if p.kind != "text" else (None, None)

    wake = threading.Event()
    holder: dict[str, Any] = {}
    lock = threading.Lock()

    def on_interaction(interaction: dict[str, Any]) -> dict[str, Any] | None:
        with lock:
            response, result = ui.handle_interaction(p, interaction)
            if result:
                holder["result"] = result
                wake.set()
        return response

    def button_answer() -> dict[str, Any] | None:
        result = holder.get("result")
        return result if result and result["kind"] != "other" else None

    payload = ui.build_message(p, _user_ids(), buttons=gw is not None)
    if gw is not None:
        gw.register(p.qid, on_interaction)

    try:
        sent = media.send_message(_channel_id(), payload)
        if "thread_id" in sent:  # フォーラムチャンネルでは投稿そのものがスレッド
            q = {"message_channel": sent["thread_id"], "message_id": sent["message_id"], "thread_id": sent["thread_id"]}
            has_thread = True
        else:
            q = {"message_channel": sent["channel_id"], "message_id": sent["message_id"], "thread_id": sent["message_id"]}
            has_thread = False

        def ensure_thread() -> None:
            """文字の返信が必要になったときだけスレッドを作る（ボタンで答えた質問に空のスレッドを残さない）。"""
            nonlocal has_thread
            if not has_thread:
                q["thread_id"] = _open_thread(q["message_channel"], q["message_id"], p)
                has_thread = True

        if gw is None:  # ボタンが無ければ文字で答えるしかないので、最初からスレッドを作る
            ensure_thread()

        deadline = time.monotonic() + timeout_seconds
        result: dict[str, Any] | None = None
        while True:
            if answer := button_answer():
                result = _public(answer)
                break
            if p.awaiting_text:  # 「その他」が押された
                ensure_thread()
            if has_thread:
                replies = _user_replies(q["thread_id"], q["message_id"])
                if replies:
                    with lock:
                        if button_answer():
                            continue
                        result = _finish_text_answer(q, p, replies, download_dir)
                    break
            if time.monotonic() >= deadline:
                break
            wake.wait(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
            wake.clear()
    finally:
        if gw is not None:
            # ボタンへの応答（メッセージの書き換え）を送り終えてから戻る。cli.py のようにすぐ終了する
            # プロセスで、応答が届かずに「時間内に応答しませんでした」と出るのを防ぐ
            gw.wait_idle()
            gw.unregister(p.qid)

    base = {"question_id": q["thread_id"], **({"note": note} if note else {})}
    if result is not None:
        return {"status": "answered", **base, **result}

    with lock:
        p.resolved = False  # 「その他」が押されていても、保留中は文字の返信を受け付ける
    ensure_thread()  # 保留中の質問にはスレッドへの文字の返信で答えてもらう
    try:
        media.edit_message(q["message_channel"], q["message_id"], ui.expired_edit(p))
    except DiscordError as e:
        log.warning("質問メッセージを保留表示にできませんでした: %s", e)
    state = _load_state()
    state["questions"][q["thread_id"]] = {**q, "prompt": p.to_dict(), "task": p.task, "asked_at": _now()}
    _save_state(state)
    return {
        "status": "pending",
        **base,
        "message": "まだ回答がありません。次回の実行で check_answers を呼んで回収してください。",
    }


# ---------------------------------------------------------------- MCP サーバー

server = MCPServer(
    name="discord",
    instructions=(
        "Discord で人間に通知・質問するためのツール。"
        "知らせるだけなら notify、進捗を 1 つのメッセージで更新したいなら notify の後に edit_notification、"
        "選択肢から選んでほしいときは ask に choices を、実行してよいか確認したいときは confirm を使う。"
        "ask / confirm が pending を返したら、作業はその質問に依存しない範囲で続け、"
        "次回の実行の最初に check_answers で回答を回収すること。"
        "本文や質問は Markdown で見やすく書く（1 行目に太字で要点、内訳は「- 」の箇条書き、補足は「-# 」の小さい文字）。"
        "関係するページ（申請・レコード・下書きなど）があれば本文に [ラベル](<URL>) の形でリンクを入れる"
        "（URL を < > で囲むとプレビューが出ない）。"
    ),
)


@server.tool()
def notify(
    message: str,
    level: str = "info",
    title: str | None = None,
    mention: bool = False,
    files: list[str] | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Discord に通知を送る。返事は待たない。

    長い本文は 2000 文字ごとに分けて送る（コードブロックは壊さない。最大 8 通）。Markdown の表は箇条書きに直す。
    本文は Markdown で見やすく書く（箇条書き「- 」、太字「**」、小さい補足「-# 」、リンク「[ラベル](<URL>)」）。

    Args:
        message: 本文（Markdown 可）
        level: info / success / warning / error。先頭に付く絵文字が変わる
        title: 見出し。付けると 1 行目が太字の見出しになる
        mention: True なら宛先ユーザーにメンションして気づきやすくする
        files: 添付するローカルファイルのパス（最大 10 個、1 個 20 MB まで）
        thread_id: 既存のスレッドに送るときのスレッド ID（省略時は既定のチャンネル）
    """
    users = _user_ids() if mention else []
    text = fmt.format_notification(message, level, title)
    if users:
        text = " ".join(f"<@{u}>" for u in users) + " " + text
    chunks = fmt.split_message(text)
    mentions = {"parse": [], "users": users}

    target = thread_id or _channel_id()
    first = media.send_message(target, {"content": chunks[0], "allowed_mentions": mentions}, files)
    target = first.get("thread_id") or first["channel_id"]
    ids = [first["message_id"]]
    for chunk in chunks[1:]:
        ids.append(media.send_message(target, {"content": chunk, "allowed_mentions": {"parse": []}})["message_id"])

    out: dict[str, Any] = {"status": "sent", "message_id": ids[0], "channel_id": target}
    if len(ids) > 1:
        out["message_ids"] = ids
    if "thread_id" in first:
        out["thread_id"] = first["thread_id"]
    return out


@server.tool()
def edit_notification(
    message_id: str,
    message: str,
    level: str = "info",
    title: str | None = None,
    channel_id: str | None = None,
) -> dict[str, Any]:
    """notify で送ったメッセージを書き換える。進捗を 1 つのメッセージで更新したいときに使う。

    書き換え後の本文は 2000 文字までに切り詰める（分割はしない）。

    Args:
        message_id: notify が返した message_id
        message: 新しい本文
        level: info / success / warning / error
        title: 見出し
        channel_id: notify が返した channel_id（省略時は既定のチャンネル）
    """
    text = fmt.clip(fmt.format_notification(message, level, title), MAX_CONTENT)
    media.edit_message(channel_id or _channel_id(), message_id, {"content": text, "allowed_mentions": {"parse": []}})
    return {"status": "edited", "message_id": message_id}


@server.tool()
def ask(
    question: str,
    choices: list[str] | None = None,
    multi: bool = False,
    allow_other: bool = True,
    timeout_seconds: int = 300,
    task: str | None = None,
    download_dir: str | None = None,
) -> dict[str, Any]:
    """人間に Discord で質問し、回答を待つ。

    質問文は Markdown で見やすく書く（1 行目に太字で何を聞くか、判断材料は箇条書き、補足は「-# 」）。
    判断に使うページがあれば、質問文に「[ラベル](<URL>)」でリンクを入れる。

    choices を渡すとボタン（21 個以上か multi=True なら選択メニュー）で答えられる。
    allow_other=True なら「その他」ボタンも付き、押されたらスレッドへの文字の返信を回答として待つ。
    どの質問もスレッドへの文字の返信で答えられる（choices があるときは番号や選択肢の文字でも可）。

    timeout_seconds の間に回答があれば {"status": "answered", "answer": ..., ...} を返す。
    選択肢なら "values"（選ばれた選択肢）と "indices"（0 始まり）も付く。
    回答がなければボタンを外して保留にし {"status": "pending", "question_id": ...} を返す。
    保留中の質問への返信は、後で check_answers を呼ぶと受け取れる。

    Args:
        question: 質問文。相手が前後の文脈を知らなくても答えられるように具体的に書く
        choices: 選択肢（最大 25 個）。省略すると自由記述の質問になる
        multi: True なら複数選択できる選択メニューにする
        allow_other: 選択肢以外の答えを書ける「その他」ボタンを付けるか
        timeout_seconds: 回答を待つ秒数（0 で待たずにすぐ保留にする。最大 1800）
        task: 質問したタスクの名前。check_answers でタスクごとに絞り込むのに使う
        download_dir: 指定すると、返信に付いた添付ファイルをこのフォルダに保存する
    """
    p = ui.Prompt(
        qid=ui.new_qid(),
        kind="choice" if choices else "text",
        question=question,
        choices=list(choices or []),
        multi=multi,
        allow_other=allow_other,
        task=task,
        allowed_user_ids=_user_ids(),
    )
    try:
        ui.build_message(p, [])
    except ValueError as e:
        raise DiscordError(str(e)) from e
    return _ask(p, timeout_seconds, download_dir)


@server.tool()
def confirm(
    question: str,
    timeout_seconds: int = 300,
    allow_other: bool = False,
    task: str | None = None,
) -> dict[str, Any]:
    """実行してよいかを人間に確認する。「承認」「却下」ボタンで答えてもらう。

    質問文は Markdown で見やすく書く（1 行目に太字で何をするか、内訳は箇条書き、補足は「-# 」）。
    確認対象のページ（下書きなど）があれば、質問文に「[ラベル](<URL>)」でリンクを入れる。

    回答があれば {"status": "answered", "approved": true/false, ...} を返す。
    スレッドに「はい」「いいえ」などと返信しても答えられる。
    回答がなければ ask と同じく保留にし、check_answers で回収する。保留中は承認されていないものとして扱うこと。

    Args:
        question: 何をしてよいかの確認。相手が判断できるように、何をどこに対して行うかを具体的に書く
        timeout_seconds: 回答を待つ秒数（0 で待たずにすぐ保留にする。最大 1800）
        allow_other: 「その他」ボタンを付けて、条件付きの返事などを文字で書けるようにするか
        task: 質問したタスクの名前
    """
    p = ui.Prompt(
        qid=ui.new_qid(),
        kind="confirm",
        question=question,
        allow_other=allow_other,
        task=task,
        allowed_user_ids=_user_ids(),
    )
    return _ask(p, timeout_seconds, None)


@server.tool()
def check_answers(task: str | None = None, download_dir: str | None = None) -> dict[str, Any]:
    """保留中の質問に返信が来たか確認する。

    回答が来た質問は answered に入れて返し、保留リストから取り除く（一度しか返さない）。
    まだ返信がない質問は pending に残る。

    Args:
        task: 指定するとそのタスクが ask / confirm した質問だけを確認する。省略時はすべて
        download_dir: 指定すると、返信に付いた添付ファイルをこのフォルダに保存する
    """
    state = _load_state()
    answered, pending = [], []
    for qid, q in list(state["questions"].items()):
        if task is not None and q.get("task") != task:
            continue
        p = _stored_prompt(q)
        # 古い形式（starter_id だけ持つ）は、通常チャンネルの質問メッセージ + 同じ ID のスレッド
        q = {"message_channel": _channel_id(), "message_id": q.get("starter_id"), "thread_id": qid, **q}
        item = {"question_id": qid, "question": p.question, "kind": p.kind, "task": q.get("task"), "asked_at": q["asked_at"]}
        replies = _user_replies(q["thread_id"], q["message_id"])
        if replies:
            answered.append({**item, **_finish_text_answer(q, p, replies, download_dir)})
            del state["questions"][qid]
        else:
            pending.append(item)
    if answered:
        _save_state(state)
    return {"answered": answered, "pending": pending}


@server.tool()
def cancel_question(question_id: str) -> dict[str, Any]:
    """保留中の質問を取り下げる。もう答えが要らなくなったときに使う。

    Args:
        question_id: ask / confirm が返した question_id
    """
    state = _load_state()
    q = state["questions"].pop(question_id, None)
    if q is None:
        return {"status": "not_found"}
    _save_state(state)
    try:
        media.edit_message(
            q.get("message_channel") or _channel_id(),
            q.get("message_id") or q["starter_id"],
            ui.cancelled_edit(_stored_prompt(q)),
        )
    except DiscordError as e:
        log.warning("質問メッセージを取り下げ表示にできませんでした: %s", e)
    return {"status": "cancelled"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    try:
        server.run("stdio")
    except KeyboardInterrupt:
        sys.exit(0)
