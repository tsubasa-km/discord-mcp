"""Discord MCP の対話式セットアップ。

Bot トークンを確認し、MESSAGE CONTENT INTENT・サーバーへの招待・投稿先チャンネル・宛先ユーザーを
順に確かめてから、Claude Code に MCP サーバーとして登録する。

  .venv\\Scripts\\python.exe configure.py            # Windows
  .venv/bin/python configure.py                    # macOS / Linux

オプション:
  --name NAME     登録名（既定: discord）
  --scope SCOPE   claude mcp add の scope（既定: user）
  --no-test       最後のテスト通知を送らない

トークンは画面に表示せず、Claude Code の設定（~/.claude.json）にだけ書き込む。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API_BASE = os.environ.get("DISCORD_API_BASE", "https://discord.com/api/v10").rstrip("/")
USER_AGENT = "DiscordBot (https://github.com/tsubasa-km/discord-mcp, 0.2)"
PROJECT_DIR = Path(__file__).resolve().parent
SERVER = PROJECT_DIR / "server.py"

# View Channels / Send Messages / Create Public Threads / Send Messages in Threads / Read Message History /
# Embed Links / Attach Files / Add Reactions
PERMISSIONS = 309237763136
FLAG_MESSAGE_CONTENT = 1 << 19
FLAG_MESSAGE_CONTENT_LIMITED = 1 << 18
CHANNEL_TYPES = {0: "テキスト", 5: "アナウンス", 15: "フォーラム"}


class SetupError(Exception):
    pass


# ---------------------------------------------------------------- 入出力


def say(text: str = "") -> None:
    print(text, flush=True)


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        raise SetupError("入力が終了しました") from None
    return value or (default or "")


def ask_secret(prompt: str) -> str:
    """伏せ字で入力してもらう（端末でないとき＝テストでは標準入力から 1 行読む）。"""
    if sys.stdin.isatty():
        return getpass.getpass(f"{prompt}（入力は表示されません）: ").strip()
    return sys.stdin.readline().strip()


def yes(prompt: str, default: bool = True) -> bool:
    answer = ask(f"{prompt} ({'Y/n' if default else 'y/N'})").lower()
    return default if not answer else answer in {"y", "yes", "はい"}


def wait_enter(prompt: str) -> None:
    ask(f"{prompt}（終わったら Enter）")


def choose(prompt: str, items: list[str]) -> int:
    for i, item in enumerate(items, 1):
        say(f"  {i}. {item}")
    while True:
        raw = ask(prompt, "1" if len(items) == 1 else None)
        if raw.isdecimal() and 1 <= int(raw) <= len(items):
            return int(raw) - 1
        say(f"  1〜{len(items)} の番号で選んでください")


# ---------------------------------------------------------------- Discord API


class Discord:
    def __init__(self, token: str) -> None:
        self.token = token

    def call(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bot {self.token}", "User-Agent": USER_AGENT}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(API_BASE + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                raw = res.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise SetupError(f"{method} {path} が {e.code} を返しました: {detail[:300]}") from e
        except urllib.error.URLError as e:
            raise SetupError(f"Discord に接続できません: {e.reason}") from e


def check_token(token: str) -> tuple[Discord, dict[str, Any]]:
    if token.count(".") != 2:
        raise SetupError(
            "トークンの形ではありません。Developer Portal の Bot ページで Reset Token を押して出てきた、"
            "ドットで 3 つに区切られた文字列を入れてください（APPLICATION ID や PUBLIC KEY ではありません）"
        )
    d = Discord(token)
    try:
        me = d.call("GET", "/users/@me")
    except SetupError as e:
        if " 401 " in str(e):
            raise SetupError("トークンが無効です。Reset Token で発行し直したものを入れてください") from None
        raise
    if not me.get("bot"):
        raise SetupError("これは Bot のトークンではありません")
    return d, me


def ensure_message_content(d: Discord, app: dict[str, Any]) -> None:
    while not (app.get("flags", 0) & (FLAG_MESSAGE_CONTENT | FLAG_MESSAGE_CONTENT_LIMITED)):
        say("\n⚠️ MESSAGE CONTENT INTENT がオフです。これがオフだと、返信の本文を読めません。")
        say(f"   https://discord.com/developers/applications/{app['id']}/bot を開き、")
        say("   Privileged Gateway Intents の MESSAGE CONTENT INTENT をオンにして Save Changes を押してください。")
        wait_enter("   ")
        app = d.call("GET", "/oauth2/applications/@me")
    say("✅ MESSAGE CONTENT INTENT: オン")


def invite_url(app_id: str) -> str:
    return f"https://discord.com/oauth2/authorize?client_id={app_id}&scope=bot&permissions={PERMISSIONS}"


def ensure_guild(d: Discord, app_id: str) -> list[dict[str, Any]]:
    guilds = d.call("GET", "/users/@me/guilds")
    while not guilds:
        say("\n⚠️ Bot がまだどのサーバーにも招待されていません。次の URL を開いて、使うサーバーに招待してください。")
        say(f"   {invite_url(app_id)}")
        wait_enter("   ")
        guilds = d.call("GET", "/users/@me/guilds")
    return guilds


def pick_channel(d: Discord, guilds: list[dict[str, Any]], app_id: str) -> tuple[str, str]:
    say("\n投稿先のサーバーを選んでください。")
    guild = guilds[choose("番号", [g["name"] for g in guilds])] if len(guilds) > 1 else guilds[0]
    if len(guilds) == 1:
        say(f"  サーバー: {guild['name']}")
    while True:
        channels = [c for c in d.call("GET", f"/guilds/{guild['id']}/channels") if c.get("type") in CHANNEL_TYPES]
        channels.sort(key=lambda c: (c.get("position", 0), c["name"]))
        if channels:
            break
        say("\n⚠️ Bot から見えるテキストチャンネルがありません。チャンネルの権限で Bot を追加してください。")
        wait_enter("   ")
    say("\n通知と質問を投稿するチャンネルを選んでください。")
    labels = [f"#{c['name']}（{CHANNEL_TYPES[c['type']]}）" for c in channels]
    channel = channels[choose("番号", labels)]
    return guild["id"], channel["id"]


def owner_ids(app: dict[str, Any]) -> list[tuple[str, str]]:
    """アプリの持ち主（チームならメンバー）の (ID, 名前)。"""
    team = app.get("team")
    if team:
        return [(m["user"]["id"], m["user"].get("global_name") or m["user"]["username"]) for m in team.get("members", [])]
    owner = app.get("owner") or {}
    return [(owner["id"], owner.get("global_name") or owner.get("username", ""))] if owner.get("id") else []


def pick_users(app: dict[str, Any]) -> str:
    candidates = owner_ids(app)
    say("\n質問の宛先（ボタンを押したり返信したりして答える人）のユーザー ID を決めます。")
    if candidates:
        for uid, name in candidates:
            say(f"  - {name}（{uid}）")
        default = ",".join(uid for uid, _ in candidates[:1])
    else:
        default = None
    say("  別の人にしたいときは、Discord の開発者モードで「ユーザー ID をコピー」した値を入れてください（カンマ区切りで複数可）。")
    while True:
        value = ask("ユーザー ID", default).replace(" ", "")
        if value and all(part.isdecimal() for part in value.split(",")):
            return value
        say("  数字の ID を入れてください")


# ---------------------------------------------------------------- Claude Code への登録


def claude_bin() -> str:
    path = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
    if not path:
        raise SetupError("claude コマンドが見つかりません。Claude Code をインストールしてから実行してください")
    return path


def run_claude(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([claude_bin(), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


def register(name: str, scope: str, env: dict[str, str]) -> None:
    if run_claude(["mcp", "get", name]).returncode == 0:  # 出力にはトークンが含まれうるので表示しない
        if not yes(f"\nClaude Code に「{name}」がすでに登録されています。置き換えますか？"):
            raise SetupError("登録を中止しました")
        removed = run_claude(["mcp", "remove", name, "--scope", scope])
        if removed.returncode != 0:
            raise SetupError(f"既存の登録を削除できませんでした: {removed.stderr.strip() or removed.stdout.strip()}")
    args = ["mcp", "add", name, "--scope", scope]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    args += ["--", sys.executable, str(SERVER)]
    added = run_claude(args)
    if added.returncode != 0:
        raise SetupError(f"claude mcp add に失敗しました: {added.stderr.strip() or added.stdout.strip()}")
    say(f"✅ Claude Code に「{name}」を登録しました（scope: {scope}）")


def check_python() -> None:
    try:
        import mcp  # noqa: F401
        import websockets  # noqa: F401
    except ImportError:
        raise SetupError(
            "依存パッケージが入っていません。setup.ps1 を使うか、仮想環境を作って "
            "`pip install -r requirements.txt` してから、その Python で実行してください"
        ) from None


def send_test(d: Discord, channel_id: str, users: str, app_id: str) -> None:
    first = users.split(",")[0]
    try:
        d.call("POST", f"/channels/{channel_id}/messages", {
            "content": f"<@{first}> ✅ **Discord MCP のセットアップが完了しました**\nこのチャンネルに通知と質問が届きます。",
            "allowed_mentions": {"parse": [], "users": [first]},
        })
    except SetupError as e:
        if "50001" in str(e) or "50013" in str(e):
            raise SetupError(
                "テスト通知を送れませんでした。Bot にこのチャンネルを見る・書き込む権限がありません。"
                f"プライベートチャンネルなら Bot を追加し、権限が足りなければ {invite_url(app_id)} で招待し直してください"
            ) from None
        raise
    say("✅ テスト通知を送りました。Discord を確認してください")


# ---------------------------------------------------------------- 本体


def main() -> int:
    parser = argparse.ArgumentParser(description="Discord MCP の対話式セットアップ")
    parser.add_argument("--name", default="discord")
    parser.add_argument("--scope", default="user", choices=["user", "project", "local"])
    parser.add_argument("--no-test", action="store_true")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    say("Discord MCP のセットアップ\n")
    check_python()
    claude_bin()

    say("1. Bot トークン")
    say("   Developer Portal（https://discord.com/developers/applications）→ アプリ → Bot → Reset Token で表示されたもの。")
    say("   まだアプリが無ければ New Application で作ってください。")
    while True:
        try:
            d, me = check_token(ask_secret("   トークン"))
            break
        except SetupError as e:
            say(f"   ⚠️ {e}")
            if not sys.stdin.isatty():
                raise
    say(f"✅ Bot: {me.get('username')}（{me['id']}）")

    app = d.call("GET", "/oauth2/applications/@me")
    ensure_message_content(d, app)
    guilds = ensure_guild(d, app["id"])
    _, channel_id = pick_channel(d, guilds, app["id"])
    users = pick_users(app)

    env = {"DISCORD_BOT_TOKEN": d.token, "DISCORD_CHANNEL_ID": channel_id, "DISCORD_USER_ID": users}
    register(args.name, args.scope, env)
    if not args.no_test:
        send_test(d, channel_id, users, app["id"])
    say("\n完了しました。新しく開いた Claude Code のセッションやスケジュールタスクから使えます。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SetupError as e:
        print(f"\n❌ {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n中止しました", file=sys.stderr)
        sys.exit(130)
