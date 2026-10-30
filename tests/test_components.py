"""components.py の単体テスト（通信なし）。

実行: .venv\\Scripts\\python.exe -m unittest tests.test_components -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import components as c  # noqa: E402
from components import Prompt  # noqa: E402

USER = "111"
OTHER = "222"


def click(cid: str, user_id: str = USER, values: list[str] | None = None, dm: bool = False) -> dict:
    user = {"id": user_id, "username": "tsubasa", "global_name": "つばさ"}
    data: dict = {"custom_id": cid, "component_type": 3 if values is not None else 2}
    if values is not None:
        data["values"] = values
    inter: dict = {"type": 3, "data": data}
    if dm:
        inter["user"] = user
    else:
        inter["member"] = {"user": user}
    return inter


def all_components(msg: dict) -> list[dict]:
    return [comp for row in msg["components"] for comp in row["components"]]


def with_status(p: Prompt, status: str) -> str:
    """書き換え後の content の期待値: 投稿時の content（メンションなし）の案内を状態行に置き換えたもの。"""
    original = c.build_message(p, [])["content"]
    hint = c._hint(p, True)
    assert original.endswith("\n\n" + hint)
    return original[: -len(hint)] + status


def prompt(kind: str = "choice", **kw) -> Prompt:
    kw.setdefault("qid", "abc123")
    kw.setdefault("question", "どれにしますか？")
    kw.setdefault("allowed_user_ids", [USER])
    if kind == "choice":
        kw.setdefault("choices", ["りんご", "みかん", "ぶどう"])
    return Prompt(kind=kind, **kw)


class CustomIdTest(unittest.TestCase):
    def test_round_trip(self):
        qid = c.new_qid()
        self.assertRegex(qid, r"^[0-9a-f]+$")
        self.assertLessEqual(len(qid), 16)
        for action in ("0", "12", "select", "approve", "deny", "other"):
            cid = c.custom_id(qid, action)
            self.assertEqual(cid, f"dmcp:{qid}:{action}")
            self.assertEqual(c.parse_custom_id(cid), (qid, action))

    def test_new_qid_unique(self):
        self.assertEqual(len({c.new_qid() for _ in range(200)}), 200)

    def test_parse_rejects_foreign(self):
        for s in ("", "dmcp", "dmcp:abc", "dmcp::x", "dmcp:abc:", "clarify:abc:0", "x:abc:0", None):
            self.assertIsNone(c.parse_custom_id(s), s)

    def test_custom_id_rejects_bad_qid(self):
        with self.assertRaises(ValueError):
            c.custom_id("a:b", "0")
        with self.assertRaises(ValueError):
            c.custom_id("", "0")
        with self.assertRaises(ValueError):
            c.custom_id("x" * 100, "0")


class BuildMessageTest(unittest.TestCase):
    def test_choice_buttons(self):
        p = prompt(task="毎朝の整理")
        msg = c.build_message(p, [USER])
        self.assertEqual(
            msg["content"],
            f"<@{USER}> ❓ **質問**（毎朝の整理）\n\nどれにしますか？\n\n1. りんご\n2. みかん\n3. ぶどう\n\n"
            "ボタンで答えてください（選択肢にない答えは「その他」から書けます）",
        )
        self.assertEqual(msg["allowed_mentions"], {"parse": [], "users": [USER]})
        self.assertEqual(
            msg["embeds"],
            [{"title": "❓ 質問（毎朝の整理）", "color": c.ORANGE, "footer": {"text": "回答待ち"}}],
        )
        comps = all_components(msg)
        self.assertEqual([x["label"] for x in comps], ["1. りんご", "2. みかん", "3. ぶどう", "その他"])
        self.assertEqual([x["style"] for x in comps], [1, 1, 1, 2])
        self.assertEqual([x["custom_id"] for x in comps], ["dmcp:abc123:0", "dmcp:abc123:1", "dmcp:abc123:2", "dmcp:abc123:other"])
        self.assertEqual(comps[-1]["emoji"], {"name": "✏️"})
        self.assertTrue(all(x["type"] == 2 and x["disabled"] is False for x in comps))
        self.assertTrue(all(row["type"] == 1 for row in msg["components"]))
        json.dumps(msg)  # JSON にできること

    def test_no_mentions_no_task(self):
        msg = c.build_message(prompt(), [])
        self.assertTrue(msg["content"].startswith("❓ **質問**\n\n"))
        self.assertEqual(msg["embeds"][0]["title"], "❓ 質問")
        self.assertEqual(msg["allowed_mentions"], {"parse": [], "users": []})

    def test_choice_without_other(self):
        msg = c.build_message(prompt(allow_other=False), [])
        self.assertNotIn("その他", [x["label"] for x in all_components(msg)])

    def test_choice_20_buttons_fit_rows(self):
        p = prompt(choices=[f"案{i}" for i in range(20)])
        msg = c.build_message(p, [])
        rows = msg["components"]
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(len(r["components"]) <= 5 for r in rows))
        self.assertEqual(len(all_components(msg)), 21)
        self.assertEqual(rows[-1]["components"][0]["custom_id"], "dmcp:abc123:other")
        self.assertTrue(all(x["type"] == 2 for x in all_components(msg)))

    def test_choice_over_20_uses_select(self):
        p = prompt(choices=[f"案{i}" for i in range(21)])
        msg = c.build_message(p, [])
        rows = msg["components"]
        self.assertEqual(len(rows), 2)
        sel = rows[0]["components"][0]
        self.assertEqual(sel["type"], 3)
        self.assertEqual(sel["custom_id"], "dmcp:abc123:select")
        self.assertEqual(sel["placeholder"], "選択してください")
        self.assertEqual((sel["min_values"], sel["max_values"]), (1, 1))
        self.assertEqual(len(sel["options"]), 21)
        self.assertEqual(sel["options"][0], {"label": "1. 案0", "value": "0"})
        self.assertEqual(rows[1]["components"][0]["label"], "その他")

    def test_multi_uses_select(self):
        p = prompt(multi=True, allow_other=False)
        msg = c.build_message(p, [])
        self.assertEqual(len(msg["components"]), 1)
        sel = msg["components"][0]["components"][0]
        self.assertEqual(sel["type"], 3)
        self.assertEqual(sel["max_values"], 3)

    def test_25_choices_ok_26_error(self):
        c.build_message(prompt(choices=[str(i) for i in range(25)]), [])
        with self.assertRaises(ValueError):
            c.build_message(prompt(choices=[str(i) for i in range(26)]), [])
        with self.assertRaises(ValueError):
            c.build_message(prompt(choices=[str(i) for i in range(26)], multi=True), [])

    def test_choice_without_choices_error(self):
        with self.assertRaises(ValueError):
            c.build_message(prompt(choices=[]), [])

    def test_bad_kind_error(self):
        with self.assertRaises(ValueError):
            c.build_message(Prompt(qid="q", kind="bogus", question="?"), [])  # type: ignore[arg-type]

    def test_confirm(self):
        p = prompt("confirm", question="デプロイしてよい？")
        msg = c.build_message(p, [USER])
        comps = all_components(msg)
        self.assertEqual(
            [(x["label"], x["style"], x["emoji"]["name"], x["custom_id"]) for x in comps],
            [
                ("承認", 3, "✅", "dmcp:abc123:approve"),
                ("却下", 4, "❌", "dmcp:abc123:deny"),
                ("その他", 2, "✏️", "dmcp:abc123:other"),
            ],
        )
        self.assertTrue(msg["content"].endswith(c.HINT_BUTTONS + c.HINT_OTHER))
        no_other = c.build_message(prompt("confirm", allow_other=False), [])
        self.assertEqual(len(all_components(no_other)), 2)

    def test_text(self):
        p = prompt("text", question="URL を教えて")
        msg = c.build_message(p, [USER])
        self.assertEqual(msg["components"], [])
        self.assertEqual(msg["content"], f"<@{USER}> ❓ **質問**\n\nURL を教えて\n\n↳ このメッセージのスレッドに返信してください")

    def test_without_buttons(self):
        msg = c.build_message(prompt(), [], buttons=False)
        self.assertEqual(msg["components"], [])
        self.assertTrue(msg["content"].endswith(c.HINT_NO_BUTTONS_CHOICE))
        msg = c.build_message(prompt("confirm"), [], buttons=False)
        self.assertTrue(msg["content"].endswith(c.HINT_NO_BUTTONS_CONFIRM))
        no_other = c.build_message(prompt(allow_other=False), [])
        self.assertTrue(no_other["content"].endswith("\n\n" + c.HINT_BUTTONS))

    def test_button_label_clipped_at_word(self):
        long = "please " * 30
        p = prompt(choices=[long.strip(), "あ" * 200])
        comps = all_components(c.build_message(p, []))
        a, b = comps[0]["label"], comps[1]["label"]
        self.assertLessEqual(len(a), 80)
        self.assertTrue(a.endswith("please…"), a)  # 単語の途中で切らない
        self.assertTrue(a.startswith("1. "))
        self.assertEqual(len(b), 80)
        self.assertTrue(b.endswith("…"))

    def test_option_label_clipped(self):
        p = prompt(multi=True, choices=["x" * 300, "short"])
        sel = c.build_message(p, [])["components"][0]["components"][0]
        self.assertEqual(len(sel["options"][0]["label"]), 100)
        self.assertTrue(sel["options"][0]["label"].endswith("…"))

    def test_clip_label_word_boundary(self):
        self.assertEqual(c._clip_label("hello world foo", 13), "hello world…")
        self.assertEqual(c._clip_label("short", 80), "short")
        self.assertEqual(c._clip_label("a\nb", 80), "a b")

    def test_embed_title_cap(self):
        msg = c.build_message(prompt(task="t" * 400), [])
        self.assertEqual(len(msg["embeds"][0]["title"]), 256)

    def test_content_cap_long_question(self):
        p = prompt(question="質" * 5000, task="長い")
        content = c.build_message(p, [USER])["content"]
        self.assertLessEqual(len(content), 2000)
        self.assertIn("…", content)
        self.assertIn("1. りんご", content)  # 選択肢と案内は残る
        self.assertTrue(content.endswith(c.HINT_BUTTONS + c.HINT_OTHER))

    def test_content_cap_long_choices(self):
        p = prompt(question="質" * 1500, choices=["選" * 99 for _ in range(25)])
        content = c.build_message(p, [])["content"]
        self.assertLessEqual(len(content), 2000)
        self.assertTrue(content.endswith(c.HINT_BUTTONS + c.HINT_OTHER))


class HandleInteractionTest(unittest.TestCase):
    def test_choice_button(self):
        p = prompt(task="t")
        resp, result = c.handle_interaction(p, click("dmcp:abc123:1"))
        self.assertEqual(
            result,
            {
                "kind": "choice",
                "answer": "みかん",
                "index": 1,
                "values": ["みかん"],
                "indices": [1],
                "user": {"id": USER, "name": "つばさ"},
                "via": "button",
            },
        )
        self.assertTrue(p.resolved)
        self.assertFalse(p.awaiting_text)
        self.assertEqual(resp["type"], 7)
        data = resp["data"]
        self.assertEqual(data["embeds"][0]["color"], c.GREEN)
        self.assertEqual(data["embeds"][0]["footer"]["text"], "回答: みかん — つばさ")
        self.assertEqual(data["embeds"][0]["title"], "❓ 質問（t）")
        comps = [x for row in data["components"] for x in row["components"]]
        self.assertEqual(len(comps), 4)
        self.assertTrue(all(x["disabled"] is True for x in comps))
        self.assertEqual(data["content"], with_status(p, "✅ 回答: みかん — つばさ"))
        self.assertNotIn(c.HINT_BUTTONS, data["content"])
        self.assertNotIn("<@", data["content"])

    def test_username_fallback_and_dm_user(self):
        p = prompt()
        inter = click("dmcp:abc123:0", dm=True)
        inter["user"]["global_name"] = None
        _, result = c.handle_interaction(p, inter)
        self.assertEqual(result["user"], {"id": USER, "name": "tsubasa"})

    def test_resolved_checked_before_auth(self):
        p = prompt(resolved=True)
        resp, result = c.handle_interaction(p, click("dmcp:abc123:0", user_id=OTHER))
        self.assertIsNone(result)
        self.assertEqual(resp, {"type": 4, "data": {"content": "この質問はすでに回答済みです", "flags": 64}})

    def test_unauthorized(self):
        p = prompt()
        resp, result = c.handle_interaction(p, click("dmcp:abc123:0", user_id=OTHER))
        self.assertIsNone(result)
        self.assertEqual(
            resp, {"type": 4, "data": {"content": "この質問に回答できるのは指定されたユーザーだけです", "flags": 64}}
        )
        self.assertFalse(p.resolved)

    def test_empty_allowlist_denies(self):
        p = prompt(allowed_user_ids=[])
        _, result = c.handle_interaction(p, click("dmcp:abc123:0"))
        self.assertIsNone(result)

    def test_second_click_rejected(self):
        p = prompt()
        c.handle_interaction(p, click("dmcp:abc123:0"))
        resp, result = c.handle_interaction(p, click("dmcp:abc123:1"))
        self.assertIsNone(result)
        self.assertEqual(resp["data"]["content"], "この質問はすでに回答済みです")

    def test_unknown_actions(self):
        for cid in ("dmcp:abc123:9", "dmcp:abc123:approve", "dmcp:other:0", "dmcp:abc123:select", "garbage"):
            p = prompt()
            resp, result = c.handle_interaction(p, click(cid))
            self.assertIsNone(result, cid)
            self.assertEqual(resp["type"], 4)
            self.assertEqual(resp["data"]["flags"], 64)
            self.assertFalse(p.resolved, cid)

    def test_select_single(self):
        p = prompt(choices=[f"案{i}" for i in range(22)])
        resp, result = c.handle_interaction(p, click("dmcp:abc123:select", values=["21"]))
        self.assertEqual(result["answer"], "案21")
        self.assertEqual(result["indices"], [21])
        self.assertEqual(result["index"], 21)
        sel = resp["data"]["components"][0]["components"][0]
        self.assertTrue(sel["disabled"])

    def test_select_multi(self):
        p = prompt(multi=True)
        resp, result = c.handle_interaction(p, click("dmcp:abc123:select", values=["2", "0"]))
        self.assertEqual(result["kind"], "choice")
        self.assertEqual(result["answer"], "りんご、ぶどう")
        self.assertEqual(result["values"], ["りんご", "ぶどう"])
        self.assertEqual(result["indices"], [0, 2])
        self.assertEqual(result["via"], "button")
        self.assertEqual(resp["data"]["embeds"][0]["footer"]["text"], "回答: りんご、ぶどう — つばさ")

    def test_select_bad_values(self):
        for values in ([], ["5"], ["x"]):
            p = prompt(multi=True)
            _, result = c.handle_interaction(p, click("dmcp:abc123:select", values=values))
            self.assertIsNone(result, values)
            self.assertFalse(p.resolved)

    def test_numeric_click_on_select_prompt_rejected(self):
        p = prompt(multi=True)
        _, result = c.handle_interaction(p, click("dmcp:abc123:0"))
        self.assertIsNone(result)

    def test_approve(self):
        p = prompt("confirm")
        resp, result = c.handle_interaction(p, click("dmcp:abc123:approve"))
        self.assertEqual(
            result,
            {"kind": "confirm", "answer": "approve", "approved": True, "user": {"id": USER, "name": "つばさ"}, "via": "button"},
        )
        self.assertEqual(resp["data"]["embeds"][0]["color"], c.GREEN)
        self.assertEqual(resp["data"]["embeds"][0]["footer"]["text"], "回答: 承認 — つばさ")
        self.assertTrue(resp["data"]["content"].endswith("\n\n✅ 回答: 承認 — つばさ"))

    def test_deny(self):
        p = prompt("confirm")
        resp, result = c.handle_interaction(p, click("dmcp:abc123:deny"))
        self.assertEqual(result["answer"], "deny")
        self.assertFalse(result["approved"])
        self.assertEqual(resp["data"]["embeds"][0]["color"], c.RED)
        self.assertEqual(resp["data"]["embeds"][0]["footer"]["text"], "回答: 却下 — つばさ")
        self.assertEqual(resp["data"]["content"], with_status(p, "❌ 回答: 却下 — つばさ"))
        self.assertTrue(all(x["disabled"] for row in resp["data"]["components"] for x in row["components"]))

    def test_other(self):
        p = prompt("confirm")
        resp, result = c.handle_interaction(p, click("dmcp:abc123:other"))
        self.assertEqual(result, {"kind": "other", "user": {"id": USER, "name": "つばさ"}, "via": "button"})
        self.assertTrue(p.resolved)
        self.assertTrue(p.awaiting_text)
        emb = resp["data"]["embeds"][0]
        self.assertEqual(emb["color"], c.BLUE)
        self.assertEqual(emb["footer"]["text"], "✏ つばさ さんが入力中… スレッドに返信してください")
        self.assertEqual(resp["data"]["content"], with_status(p, "✏ つばさ さんが入力中… スレッドに返信してください"))
        self.assertTrue(all(x["disabled"] for row in resp["data"]["components"] for x in row["components"]))

    def test_other_when_not_allowed(self):
        p = prompt(allow_other=False)
        _, result = c.handle_interaction(p, click("dmcp:abc123:other"))
        self.assertIsNone(result)


class InterpretTextTest(unittest.TestCase):
    def test_choice_number(self):
        r = c.interpret_text_answer(prompt(), " 2 ")
        self.assertEqual(r, {"kind": "choice", "answer": "みかん", "index": 1, "values": ["みかん"], "indices": [1], "via": "text"})

    def test_choice_fullwidth_number(self):
        self.assertEqual(c.interpret_text_answer(prompt(), "３")["answer"], "ぶどう")

    def test_choice_out_of_range(self):
        for s in ("0", "4", "-1"):
            self.assertEqual(c.interpret_text_answer(prompt(), s)["kind"], "text", s)

    def test_choice_label(self):
        p = prompt(choices=["Apple", "Banana"])
        r = c.interpret_text_answer(p, "  banana ")
        self.assertEqual((r["kind"], r["answer"], r["index"]), ("choice", "Banana", 1))

    def test_choice_multi_numbers(self):
        p = prompt(multi=True)
        for s in ("1,3", "1 3", "3、1", "1, 3"):
            r = c.interpret_text_answer(p, s)
            self.assertEqual(r["indices"], [0, 2], s)
            self.assertEqual(r["answer"], "りんご、ぶどう")
            self.assertEqual(r["via"], "text")

    def test_single_rejects_multiple_numbers(self):
        self.assertEqual(c.interpret_text_answer(prompt(), "1,2")["kind"], "text")

    def test_free_text(self):
        r = c.interpret_text_answer(prompt(), "どれでもない\n別案です")
        self.assertEqual(r, {"kind": "text", "answer": "どれでもない\n別案です", "via": "text"})

    def test_confirm(self):
        p = prompt("confirm")
        for s in ("はい", "Yes", "y", "OK", "承認", "approve", "1", "✅", "はい。"):
            r = c.interpret_text_answer(p, s)
            self.assertEqual(r, {"kind": "confirm", "answer": "approve", "approved": True, "via": "text"}, s)
        for s in ("いいえ", "NO", "n", "却下", "deny", "2", "❌"):
            r = c.interpret_text_answer(p, s)
            self.assertEqual(r, {"kind": "confirm", "answer": "deny", "approved": False, "via": "text"}, s)
        self.assertEqual(c.interpret_text_answer(p, "明日なら")["kind"], "text")

    def test_text_kind(self):
        self.assertEqual(c.interpret_text_answer(prompt("text"), "1")["kind"], "text")

    def test_awaiting_text_is_free_text(self):
        p = prompt(awaiting_text=True, resolved=True)
        self.assertEqual(c.interpret_text_answer(p, "1"), {"kind": "text", "answer": "1", "via": "text"})


class EditTest(unittest.TestCase):
    def test_answered_by_text(self):
        p = prompt()
        r = c.interpret_text_answer(p, "1")
        body = c.answered_by_text_edit(p, r, "つばさ")
        self.assertEqual(body["components"], [])
        self.assertEqual(body["embeds"][0]["color"], c.GREEN)
        self.assertEqual(body["embeds"][0]["footer"]["text"], "回答: りんご — つばさ")
        self.assertEqual(body["content"], with_status(p, "✅ 回答: りんご — つばさ"))
        self.assertEqual(body["allowed_mentions"], {"parse": []})

    def test_answered_by_text_deny_and_free_text(self):
        p = prompt("confirm")
        body = c.answered_by_text_edit(p, c.interpret_text_answer(p, "いいえ"), "つばさ")
        self.assertEqual(body["embeds"][0]["color"], c.RED)
        self.assertEqual(body["embeds"][0]["footer"]["text"], "回答: 却下 — つばさ")
        self.assertTrue(body["content"].endswith("\n\n❌ 回答: 却下 — つばさ"))
        body = c.answered_by_text_edit(p, c.interpret_text_answer(p, "x" * 5000), "つばさ")
        self.assertEqual(body["embeds"][0]["color"], c.GREEN)
        self.assertEqual(len(body["embeds"][0]["footer"]["text"]), 2048)
        self.assertLessEqual(len(body["content"]), 2000)
        self.assertIn("\n\n✅ 回答: xxx", body["content"])

    def test_edit_content_cap_with_long_question_and_answer(self):
        p = prompt(question="質" * 5000, task="長い")
        body = c.answered_by_text_edit(p, c.interpret_text_answer(p, "答\n" * 3000), "つばさ")
        self.assertLessEqual(len(body["content"]), 2000)
        self.assertIn("1. りんご", body["content"])
        self.assertTrue(body["content"].endswith("…"))  # 状態行も上限で切る
        self.assertIn("✅ 回答: 答 答", body["content"])

    def test_expired(self):
        p = prompt(task="t")
        body = c.expired_edit(p)
        self.assertEqual(body["components"], [])
        self.assertEqual(body["embeds"][0]["color"], c.GREYPLE)
        self.assertEqual(
            body["embeds"][0]["footer"]["text"],
            "⏳ 時間内に回答がなかったため保留中 — スレッドに返信すれば次回の実行で回収します",
        )
        self.assertEqual(
            body["content"],
            with_status(p, "⏳ 時間内に回答がなかったため保留中 — スレッドに返信すれば次回の実行で回収します"),
        )

    def test_cancelled(self):
        p = prompt("text")
        body = c.cancelled_edit(p)
        self.assertEqual(body["content"], with_status(p, "🚫 この質問は取り下げられました"))
        self.assertNotIn(c.HINT_TEXT, body["content"])
        self.assertEqual(body["components"], [])
        self.assertEqual(body["embeds"][0]["color"], c.GREYPLE)
        self.assertEqual(body["embeds"][0]["footer"]["text"], "🚫 この質問は取り下げられました")


class PromptDictTest(unittest.TestCase):
    def test_round_trip(self):
        p = prompt(task="t", multi=True, awaiting_text=True, resolved=True, allowed_user_ids=["1", "2"])
        d = json.loads(json.dumps(p.to_dict(), ensure_ascii=False))
        self.assertEqual(Prompt.from_dict(d), p)

    def test_from_dict_defaults_and_unknown_keys(self):
        p = Prompt.from_dict({"qid": "q", "kind": "text", "question": "?", "extra": 1})
        self.assertEqual(p, Prompt(qid="q", kind="text", question="?"))

    def test_from_dict_copies_lists(self):
        d = {"qid": "q", "kind": "choice", "question": "?", "choices": ["a"]}
        p = Prompt.from_dict(d)
        p.choices.append("b")
        self.assertEqual(d["choices"], ["a"])


if __name__ == "__main__":
    unittest.main()
