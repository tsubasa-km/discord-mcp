"""コマンドラインから Discord MCP の機能を使う入口。

MCP サーバーを登録できない環境（クラウドの定期実行など）や、シェルスクリプトから使うためのもの。
環境変数は MCP サーバーと同じ（DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID / DISCORD_USER_ID ほか）。

  python cli.py notify "本文" [--level success] [--title 見出し] [--mention] [--file PATH ...] [--thread-id ID]
  python cli.py edit MESSAGE_ID "本文" [--level info] [--title 見出し] [--channel-id ID]
  python cli.py ask "質問" [--choice A --choice B ...] [--multi] [--no-other] [--timeout 300] [--task 名前]
  python cli.py confirm "質問" [--timeout 300] [--allow-other] [--task 名前]
  python cli.py answer QUESTION_ID [--choice A --choice B ...] [--confirm]
  python cli.py check-answers [--task 名前]
  python cli.py cancel QUESTION_ID

本文や質問に "-" を渡すと標準入力から読む。結果は JSON で標準出力に出す。
失敗したときは {"error": ...} を出して終了コード 1 で終わる。

answer は、保留中の質問への返信を question_id だけで読む。実行ごとにファイルが消える環境では
check-answers が使う state ファイルが残らないので、question_id を自分で覚えておき、次の実行で answer を呼ぶ。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

import components as ui
import media
import server
from discord_api import DiscordError, user_ids


def _text(value: str) -> str:
    return sys.stdin.read() if value == "-" else value


def answer(question_id: str, choices: list[str] | None, confirm: bool) -> dict[str, Any]:
    """保留中の質問（question_id）のスレッドへの返信を読む。state ファイルは使わない。

    choices / confirm を渡すと、番号や「はい」を選択肢・承認として解釈する。
    質問文を知らないので質問メッセージは書き換えず、受け取った返信に ✅ を付けるだけにする。
    """
    kind = "confirm" if confirm else "choice" if choices else "text"
    p = ui.Prompt(qid="cli", kind=kind, question="", choices=list(choices or []), allowed_user_ids=user_ids())
    # スレッドの ID は質問メッセージ（フォーラムなら投稿の最初のメッセージ）の ID と同じ
    try:
        replies = server._user_replies(question_id, question_id)
    except DiscordError as e:
        if e.status == 404:  # ボタンで答え済みの質問などで、スレッドが無い
            return {"status": "not_found", "question_id": question_id}
        raise
    if not replies:
        return {"status": "pending", "question_id": question_id}
    result = server._text_answer(p, replies, None)
    media.add_reaction(question_id, replies[-1]["id"], server.RECEIVED_EMOJI)
    return {"status": "answered", "question_id": question_id, **result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Discord MCP のコマンドライン版")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("notify", help="通知を送る")
    p.add_argument("message")
    p.add_argument("--level", default="info", choices=["info", "success", "warning", "error"])
    p.add_argument("--title")
    p.add_argument("--mention", action="store_true")
    p.add_argument("--file", action="append", dest="files")
    p.add_argument("--thread-id")

    p = sub.add_parser("edit", help="notify で送ったメッセージを書き換える")
    p.add_argument("message_id")
    p.add_argument("message")
    p.add_argument("--level", default="info", choices=["info", "success", "warning", "error"])
    p.add_argument("--title")
    p.add_argument("--channel-id")

    p = sub.add_parser("ask", help="質問して回答を待つ")
    p.add_argument("question")
    p.add_argument("--choice", action="append", dest="choices")
    p.add_argument("--multi", action="store_true")
    p.add_argument("--no-other", action="store_true")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--task")
    p.add_argument("--download-dir")

    p = sub.add_parser("confirm", help="承認・却下ボタンで確認する")
    p.add_argument("question")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--allow-other", action="store_true")
    p.add_argument("--task")

    p = sub.add_parser("answer", help="保留中の質問への返信を question_id だけで読む")
    p.add_argument("question_id")
    p.add_argument("--choice", action="append", dest="choices")
    p.add_argument("--confirm", action="store_true")

    p = sub.add_parser("check-answers", help="state ファイルにある保留中の質問の回答を回収する")
    p.add_argument("--task")
    p.add_argument("--download-dir")

    p = sub.add_parser("cancel", help="保留中の質問を取り下げる")
    p.add_argument("question_id")

    a = parser.parse_args(argv)
    if a.command == "notify":
        out = server.notify(_text(a.message), a.level, a.title, a.mention, a.files, a.thread_id)
    elif a.command == "edit":
        out = server.edit_notification(a.message_id, _text(a.message), a.level, a.title, a.channel_id)
    elif a.command == "ask":
        out = server.ask(_text(a.question), a.choices, a.multi, not a.no_other, a.timeout, a.task, a.download_dir)
    elif a.command == "confirm":
        out = server.confirm(_text(a.question), a.timeout, a.allow_other, a.task)
    elif a.command == "answer":
        out = answer(a.question_id, a.choices, a.confirm)
    elif a.command == "check-answers":
        out = server.check_answers(a.task, a.download_dir)
    else:
        out = server.cancel_question(a.question_id)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except (DiscordError, ValueError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)
