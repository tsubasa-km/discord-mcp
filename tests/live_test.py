"""本物の Discord に対する動作確認。

~/.claude.json に登録した discord MCP サーバーの設定（command / args / env）をそのまま使って起動し、
notify・edit_notification・ask（ボタン）・confirm（ボタン）を順に呼ぶ。
トークンなどの環境変数は子プロセスに渡すだけで表示しない。

  .venv\\Scripts\\python.exe -I tests\\live_test.py [各質問の待ち秒数]
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.stdout.reconfigure(encoding="utf-8")
TIMEOUT = int(sys.argv[1]) if len(sys.argv) > 1 else 180


def result(res):
    text = res.content[0].text if res.content else ""
    return {"error": text} if res.is_error else json.loads(text)


async def main():
    conf = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))["mcpServers"]["discord"]
    params = StdioServerParameters(
        command=conf["command"], args=conf.get("args", []), env={**os.environ, **conf.get("env", {})}
    )
    async with stdio_client(params) as streams:
        read, write = streams if isinstance(streams, tuple) else (streams.read, streams.write)
        async with ClientSession(read, write) as s:
            await s.initialize()

            async def call(name, args):
                r = result(await s.call_tool(name, args))
                print(f"{name}:", r, flush=True)
                return r

            await call("check_answers", {"task": "discord-mcp-test"})
            sent = await call("notify", {"message": "テスト中です…", "level": "info", "title": "🧪 Discord MCP テスト"})
            await call("ask", {
                "question": "ボタンのテストです。好きな果物を選んでください。",
                "choices": ["りんご", "みかん", "ぶどう"],
                "timeout_seconds": TIMEOUT,
                "task": "discord-mcp-test",
            })
            approved = await call("confirm", {
                "question": "承認ボタンのテストです。「承認」か「却下」を押してください。",
                "timeout_seconds": TIMEOUT,
                "task": "discord-mcp-test",
            })
            if "message_id" in sent:
                await call("edit_notification", {
                    "message_id": sent["message_id"],
                    "channel_id": sent["channel_id"],
                    "message": f"テスト完了（confirm: {approved.get('answer', approved.get('status'))}）",
                    "level": "success",
                    "title": "🧪 Discord MCP テスト",
                })


asyncio.run(main())
