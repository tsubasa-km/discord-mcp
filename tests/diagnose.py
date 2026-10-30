"""Bot が入っているサーバーと、設定したチャンネルが見えるかを表示する（トークンは表示しない）。"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
env = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))["mcpServers"]["discord"]["env"]
H = {"Authorization": f"Bot {env['DISCORD_BOT_TOKEN']}", "User-Agent": "DiscordBot (local, 0.1)"}


def get(path):
    req = urllib.request.Request("https://discord.com/api/v10" + path, headers=H)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


code, me = get("/users/@me")
print("bot:", code, me.get("username"), me.get("id"))
code, guilds = get("/users/@me/guilds")
print("guilds:", code, [(g["name"], g["id"]) for g in guilds] if code == 200 else guilds)
code, ch = get(f"/channels/{env['DISCORD_CHANNEL_ID']}")
print("channel:", code, {k: ch.get(k) for k in ("name", "type", "guild_id", "message", "code")})
for g in guilds if isinstance(guilds, list) else []:
    code, chans = get(f"/guilds/{g['id']}/channels")
    if code == 200:
        print(f"  {g['name']} text channels:", [(c["name"], c["id"]) for c in chans if c.get("type") == 0])
