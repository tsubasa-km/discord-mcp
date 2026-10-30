"""server.py の end-to-end テスト。偽の Discord REST API と偽の Gateway を立て、MCP クライアントから全ツールを呼ぶ。

  .venv\\Scripts\\python.exe -I tests\\e2e_test.py server.py
"""
import asyncio, json, os, re, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from websockets.sync.server import serve

sys.stdout.reconfigure(encoding="utf-8")
SERVER = sys.argv[1]
BOT, USER, OTHER, CH = "999", "111", "222", "500"
lock = threading.Lock()
next_id = [1000]
msgs: dict[str, list[dict]] = {}  # channel -> messages
calls = []


def new_id():
    with lock:
        next_id[0] += 1
        return str(next_id[0])


def add(ch, author, content, **extra):
    m = {"id": new_id(), "channel_id": ch, "author": {"id": author, "username": f"u{author}"},
         "content": content, "attachments": [], **extra}
    msgs.setdefault(ch, []).append(m)
    return m


def find(mid):
    for ms in msgs.values():
        for m in ms:
            if m["id"] == mid:
                return m


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, obj=None):
        b = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def do_POST(self):
        assert self.headers["Authorization"] == "Bot test-token"
        body = self._body(); p = urlparse(self.path).path
        calls.append(("POST", p, body))
        if m := re.fullmatch(r"/api/v10/channels/(\d+)/messages/(\d+)/threads", p):
            msgs.setdefault(m.group(2), [])  # Discord: thread id == starter message id
            return self._send(201, {"id": m.group(2), "name": body["name"]})
        if m := re.fullmatch(r"/api/v10/channels/(\d+)/messages", p):
            return self._send(200, add(m.group(1), BOT, body["content"], components=body.get("components", [])))
        if re.fullmatch(r"/api/v10/interactions/\d+/[\w-]+/callback", p):
            return self._send(204)
        self._send(404, {"message": "nf"})

    def do_PATCH(self):
        body = self._body(); p = urlparse(self.path).path
        calls.append(("PATCH", p, body))
        m = re.fullmatch(r"/api/v10/channels/(\d+)/messages/(\d+)", p)
        msg = find(m.group(2)); msg.update(body)
        self._send(200, msg)

    def do_PUT(self):
        calls.append(("PUT", urlparse(self.path).path, None)); self._send(204)

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if m := re.fullmatch(r"/api/v10/channels/(\d+)", u.path):
            return self._send(200, {"id": m.group(1), "type": 0})
        m = re.fullmatch(r"/api/v10/channels/(\d+)/messages", u.path)
        after = int(q.get("after", ["0"])[0]); lim = int(q.get("limit", ["50"])[0])
        out = sorted((x for x in msgs.get(m.group(1), []) if int(x["id"]) > after), key=lambda x: int(x["id"]))[:lim]
        self._send(200, list(reversed(out)))  # newest first, like Discord


# ---- 偽 Gateway: HELLO → IDENTIFY → READY のあと、テストからの INTERACTION_CREATE を流す
gw_conns = []
seq = [1]


def gw_handler(ws):
    ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 30000}}))
    assert json.loads(ws.recv())["op"] == 2
    ws.send(json.dumps({"op": 0, "t": "READY", "s": 1, "d": {"session_id": "s1", "resume_gateway_url": gw_url}}))
    gw_conns.append(ws)
    for raw in ws:
        if json.loads(raw).get("op") == 1:
            ws.send(json.dumps({"op": 11}))


def click(custom_id, user=USER, values=None):
    seq[0] += 1
    data = {"custom_id": custom_id, "component_type": 3 if values else 2, **({"values": values} if values else {})}
    gw_conns[-1].send(json.dumps({"op": 0, "t": "INTERACTION_CREATE", "s": seq[0], "d": {
        "id": new_id(), "token": "tok", "type": 3, "data": data, "channel_id": CH,
        "member": {"user": {"id": user, "username": f"u{user}", "global_name": f"User{user}"}}}}))


def last_question():
    return [c for c in calls if c[0] == "POST" and c[1] == f"/api/v10/channels/{CH}/messages"][-1][2]


def custom_ids(payload):
    return [c["custom_id"] for row in payload.get("components", []) for c in row["components"]]


def threads():
    return [c[1].split("/")[-2] for c in calls if c[0] == "POST" and c[1].endswith("/threads")]


def callbacks():
    return [c for c in calls if "/callback" in c[1]]


def later(delay, fn):
    threading.Thread(target=lambda: (time.sleep(delay), fn()), daemon=True).start()


def reply_later(delay, author, text):
    def run():
        add(threads()[-1], author, text)
    later(delay, run)


httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
gws = serve(gw_handler, "127.0.0.1", 0)
threading.Thread(target=gws.serve_forever, daemon=True).start()
gw_url = f"ws://127.0.0.1:{gws.socket.getsockname()[1]}"
state = os.path.join(tempfile.mkdtemp(), "state.json")
env = {**os.environ, "DISCORD_BOT_TOKEN": "test-token", "DISCORD_CHANNEL_ID": CH, "DISCORD_USER_ID": USER,
       "DISCORD_MCP_STATE": state, "DISCORD_API_BASE": f"http://127.0.0.1:{httpd.server_port}/api/v10",
       "DISCORD_GATEWAY_URL": gw_url}


def data(res):
    assert not res.is_error, res
    return json.loads(res.content[0].text)


async def session(env, body):
    params = StdioServerParameters(command=sys.executable, args=[SERVER], env=env)
    async with stdio_client(params) as streams:
        read, write = streams if isinstance(streams, tuple) else (streams.read, streams.write)
        async with ClientSession(read, write) as s:
            await s.initialize()
            await body(s)


async def main_flow(s):
    tools = sorted(t.name for t in (await s.list_tools()).tools)
    assert tools == ["ask", "cancel_question", "check_answers", "confirm", "edit_notification", "notify"], tools
    print("tools:", tools)

    # notify: レベル・見出し・メンション、長文は分割
    r = data(await s.call_tool("notify", {"message": "hello", "level": "success", "title": "完了", "mention": True}))
    last = calls[-1][2]
    assert r["status"] == "sent" and last["content"].startswith(f"<@{USER}> ✅ **完了**"), last
    assert last["allowed_mentions"]["users"] == [USER]
    r = data(await s.call_tool("notify", {"message": "x" * 4500}))
    assert len(r["message_ids"]) >= 3 and all(len(find(i)["content"]) <= 2000 for i in r["message_ids"]), r
    r2 = data(await s.call_tool("edit_notification", {"message_id": r["message_id"], "message": "進捗 50%", "channel_id": r["channel_id"]}))
    assert r2["status"] == "edited" and find(r["message_id"])["content"] == "ℹ️ 進捗 50%"
    print("notify / edit_notification ok")

    # ボタンで回答（他人のクリックは拒否される）
    def click_second():
        ids = custom_ids(last_question())
        click(ids[1], user=OTHER)
        time.sleep(0.5)
        click(ids[1])
    later(2, click_second)
    threads_before = len(threads())
    r = data(await s.call_tool("ask", {"question": "どっち?", "choices": ["A", "B"], "timeout_seconds": 20, "task": "daily"}))
    assert len(threads()) == threads_before, "ボタンで答えた質問にはスレッドを作らない"
    assert r["status"] == "answered" and r["answer"] == "B" and r["via"] == "button" and r["indices"] == [1], r
    assert r["answered_by"] == f"User{USER}", r
    time.sleep(1)  # インタラクションへの応答は Gateway のスレッドから非同期に送られる
    cbs = callbacks()
    assert cbs[-2][2]["data"]["flags"] == 64 and cbs[-1][2]["type"] == 7, cbs[-2:]
    print("ask button ok:", r["answer"])

    # confirm をボタンで却下
    later(2, lambda: click([i for i in custom_ids(last_question()) if i.endswith(":deny")][0]))
    r = data(await s.call_tool("confirm", {"question": "消してもいい?", "timeout_seconds": 20}))
    assert r["status"] == "answered" and r["approved"] is False, r
    print("confirm button ok")

    # 「その他」を押したときだけスレッドを作り、そこへの文字の返信を回答にする（✅ リアクション付き）
    def other_then_reply():
        click([i for i in custom_ids(last_question()) if i.endswith(":other")][0])
        time.sleep(4)
        assert threads(), "「その他」でスレッドが作られていない"
        add(threads()[-1], OTHER, "noise")
        add(threads()[-1], USER, "メロン")
    later(2, other_then_reply)
    threads_before = len(threads())
    r = data(await s.call_tool("ask", {"question": "その他で", "choices": ["りんご", "みかん"], "timeout_seconds": 20}))
    assert r["status"] == "answered" and r["kind"] == "text" and r["answer"] == "メロン", r
    assert len(threads()) == threads_before + 1
    assert any(c[0] == "PUT" and "/reactions/" in c[1] for c in calls)
    print("ask other -> thread ok")

    # テキストの質問は最初からスレッドを作る
    reply_later(2, USER, "自由回答")
    r = data(await s.call_tool("ask", {"question": "自由に", "timeout_seconds": 20}))
    assert r["status"] == "answered" and r["answer"] == "自由回答", r
    print("ask text ok")

    # 時間切れ → ボタンを外して保留 → check_answers で回収
    threads_before = len(threads())
    r = data(await s.call_tool("ask", {"question": "後で答えて", "choices": ["はい", "いいえ"], "timeout_seconds": 2, "task": "nightly"}))
    assert r["status"] == "pending", r
    qid = r["question_id"]
    assert threads()[threads_before:] == [qid], "保留にするときにスレッドを作る"
    patch = [c for c in calls if c[0] == "PATCH"][-1][2]
    assert patch["components"] == [] and "保留中" in patch["content"], patch
    r = data(await s.call_tool("check_answers", {}))
    assert r["answered"] == [] and [p["question_id"] for p in r["pending"]] == [qid], r
    add(qid, USER, "2")
    r = data(await s.call_tool("check_answers", {"task": "daily"}))
    assert r["answered"] == [] and r["pending"] == [], r
    r = data(await s.call_tool("check_answers", {"task": "nightly"}))
    assert [a["answer"] for a in r["answered"]] == ["いいえ"] and r["pending"] == [], r
    assert data(await s.call_tool("check_answers", {})) == {"answered": [], "pending": []}
    print("pending -> check_answers ok")

    r = data(await s.call_tool("ask", {"question": "取り下げ用", "timeout_seconds": 0}))
    assert data(await s.call_tool("cancel_question", {"question_id": r["question_id"]}))["status"] == "cancelled"
    assert json.load(open(state, encoding="utf-8"))["questions"] == {}
    print("cancel ok")


async def no_buttons_flow(s):
    # DISCORD_BUTTONS=0 ならボタンなしで投稿し、最初からスレッドを作って文字の返信で受け付ける
    reply_later(2, USER, "はい")
    r = data(await s.call_tool("confirm", {"question": "ボタンなし", "timeout_seconds": 20}))
    q = last_question()
    assert custom_ids(q) == [] and "「はい」か「いいえ」" in q["content"] and r["approved"] is True and "note" in r, (r, q)
    # 選択肢の質問には番号で答えられる
    reply_later(2, OTHER, "noise"); reply_later(3, USER, "2")
    r = data(await s.call_tool("ask", {"question": "番号で", "choices": ["りんご", "みかん"], "timeout_seconds": 20}))
    assert r["answer"] == "みかん" and r["via"] == "text", r
    print("no-buttons fallback ok")


async def missing_token_flow(s):
    res = await s.call_tool("notify", {"message": "x"})
    assert res.is_error and "DISCORD_BOT_TOKEN" in res.content[0].text, res
    print("missing-token error ok")


async def main():
    await session(env, main_flow)
    await session({**env, "DISCORD_BUTTONS": "0"}, no_buttons_flow)
    await session({k: v for k, v in env.items() if k != "DISCORD_BOT_TOKEN"}, missing_token_flow)
    print("ALL OK")


asyncio.run(main())
