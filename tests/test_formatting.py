"""formatting.py の単体テスト（標準ライブラリの unittest のみ、通信なし）。

実行: .venv\\Scripts\\python.exe -m unittest tests.test_formatting -v
"""

from __future__ import annotations

import random
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from formatting import (  # noqa: E402
    LEVELS,
    clip,
    format_notification,
    level_color,
    md_link,
    split_message,
    tables_to_bullets,
)

SUFFIX = re.compile(r" \((\d+)/(\d+)\)$")
NOTICE = re.compile(r"\n\n…（長すぎるため残り (\d+) 文字を省略しました）$")


def strip_suffix(chunk: str) -> str:
    return SUFFIX.sub("", chunk)


def fence_balanced(chunk: str) -> bool:
    return sum(1 for line in chunk.split("\n") if line.lstrip().startswith("```")) % 2 == 0


class ClipTest(unittest.TestCase):
    def test_short_unchanged(self) -> None:
        self.assertEqual(clip("abc", 5), "abc")
        self.assertEqual(clip("abcde", 5), "abcde")

    def test_cut_with_ellipsis(self) -> None:
        self.assertEqual(clip("abcdef", 5), "abcd…")
        self.assertEqual(len(clip("x" * 3000, 2000)), 2000)


class SplitMessageTest(unittest.TestCase):
    def assert_valid(self, chunks: list[str], limit: int, max_chunks: int = 8) -> None:
        self.assertLessEqual(len(chunks), max_chunks)
        for c in chunks:
            self.assertLessEqual(len(c), limit, repr(c[:80]))

    def test_short_text_unchanged(self) -> None:
        text = "  そのまま  \n送る\n"
        self.assertEqual(split_message(text), [text])
        self.assertEqual(split_message("x" * 2000), ["x" * 2000])

    def test_empty_raises(self) -> None:
        for text in ("", "   ", "\n\t\n"):
            with self.assertRaises(ValueError) as cm:
                split_message(text)
            self.assertEqual(str(cm.exception), "空のメッセージは送れません")

    def test_splits_at_newlines(self) -> None:
        lines = [f"line {i:03d} " + "あ" * 30 for i in range(60)]
        chunks = split_message("\n".join(lines), limit=500)
        self.assert_valid(chunks, 500)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            body = strip_suffix(c)
            self.assertTrue(body.startswith("line "), body[:20])
            self.assertTrue(body.endswith("あ"), body[-20:])
        # 改行区切りなので行は失われない
        rejoined = "\n".join(strip_suffix(c) for c in chunks)
        self.assertEqual(rejoined, "\n".join(lines))

    def test_splits_at_spaces(self) -> None:
        words = [f"word{i}" for i in range(400)]
        text = " ".join(words)
        chunks = split_message(text, limit=200)
        self.assert_valid(chunks, 200, max_chunks=8)
        for c in chunks:
            body = strip_suffix(c)
            self.assertNotIn(body[0], " ")
            for w in body.split(" "):
                if NOTICE.search(body) is None:
                    self.assertRegex(w, r"^word\d+$")

    def test_suffix_numbering(self) -> None:
        text = "\n".join("x" * 80 for _ in range(15))
        chunks = split_message(text, limit=300)
        n = len(chunks)
        self.assertGreater(n, 1)
        for i, c in enumerate(chunks, 1):
            m = SUFFIX.search(c)
            self.assertIsNotNone(m, c[-20:])
            self.assertEqual((int(m.group(1)), int(m.group(2))), (i, n))

    def test_long_word_hard_split(self) -> None:
        text = "z" * 5000
        chunks = split_message(text, limit=2000)
        self.assert_valid(chunks, 2000)
        self.assertEqual("".join(strip_suffix(c) for c in chunks), text)

    def test_many_newlines(self) -> None:
        text = "a" + "\n" * 5000 + "b"
        chunks = split_message(text, limit=300)
        self.assert_valid(chunks, 300)

    def test_code_fence_reopens_with_language(self) -> None:
        code = "\n".join(f"    print({i})  # {'c' * 40}" for i in range(200))
        text = f"前置き\n```py\n{code}\n```\n後書き"
        chunks = split_message(text, limit=1000, max_chunks=50)
        self.assert_valid(chunks, 1000, max_chunks=50)
        self.assertGreater(len(chunks), 2)
        for i, c in enumerate(chunks):
            body = strip_suffix(c)
            self.assertTrue(fence_balanced(body), f"chunk {i} has unbalanced fence")
            if i > 0 and i < len(chunks) - 1:
                self.assertTrue(body.startswith("```py\n"), body[:20])
        # コードブロック内のインデントは保たれる
        for c in chunks[1:-1]:
            first_code = strip_suffix(c).split("\n")[1]
            self.assertTrue(first_code.startswith("    print("), first_code)

    def test_cap_and_notice(self) -> None:
        text = "y" * 10000  # 改行も空白もない → 強制分割で文字がそのまま数えられる
        chunks = split_message(text, limit=500, max_chunks=4)
        self.assert_valid(chunks, 500, max_chunks=4)
        self.assertEqual(len(chunks), 4)
        m = NOTICE.search(strip_suffix(chunks[-1]))
        self.assertIsNotNone(m, chunks[-1][-80:])
        sent = sum(len(strip_suffix(c)) for c in chunks[:-1]) + len(NOTICE.sub("", strip_suffix(chunks[-1])))
        self.assertEqual(int(m.group(1)), len(text) - sent)
        # N は打ち切り前の総数
        total = int(SUFFIX.search(chunks[0]).group(2))
        self.assertGreater(total, 4)
        self.assertEqual(SUFFIX.search(chunks[-1]).groups(), ("4", str(total)))

    def test_default_cap(self) -> None:
        chunks = split_message("word " * 10000)
        self.assert_valid(chunks, 2000)
        self.assertEqual(len(chunks), 8)
        self.assertIsNotNone(NOTICE.search(strip_suffix(chunks[-1])))

    def test_inline_backtick_guard(self) -> None:
        # 区切り候補の空白がインラインコードの中にある
        prefix = "a" * 150 + " "
        text = prefix + "`code with spaces " + "b" * 20 + " c`" + " tail" * 40
        chunks = split_message(text, limit=200)
        self.assert_valid(chunks, 200)
        first = strip_suffix(chunks[0])
        self.assertEqual(first.count("`") % 2, 0, first)
        for c in chunks:
            self.assertEqual(strip_suffix(c).count("`") % 2, 0, c)

    def test_random_inputs_respect_limits(self) -> None:
        rng = random.Random(12345)
        pieces = ["word", " ", "\n", "\n\n", "```py\n", "```\n", "```javascript\n", "`", "\\`",
                  "あいうえお", "x" * 300, "|a|b|", "😀", "  indented", "\t"]
        for trial in range(300):
            limit = rng.choice([64, 80, 100, 150, 200, 500, 2000])
            max_chunks = rng.choice([1, 2, 3, 8, 20])
            text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 600)))
            if not text.strip():
                continue
            with self.subTest(trial=trial, limit=limit, max_chunks=max_chunks):
                chunks = split_message(text, limit=limit, max_chunks=max_chunks)
                self.assert_valid(chunks, limit, max_chunks)
                self.assertTrue(all(c for c in chunks))

    def test_too_small_limit(self) -> None:
        with self.assertRaises(ValueError):
            split_message("x" * 100, limit=10)
        with self.assertRaises(ValueError):
            split_message("x" * 100, limit=50, max_chunks=0)


class TablesTest(unittest.TestCase):
    def test_table_to_bullets(self) -> None:
        text = "結果:\n| 名前 | 状態 |\n|---|:--:|\n| build | OK |\n| test | NG |\n\n以上"
        self.assertEqual(
            tables_to_bullets(text),
            "結果:\n- **名前**: build / **状態**: OK\n- **名前**: test / **状態**: NG\n\n以上",
        )

    def test_without_outer_pipes_and_empty_cells(self) -> None:
        text = "a | b\n--- | ---\n1 |\n| | 2 |\n\\| | x \\| y"
        self.assertEqual(
            tables_to_bullets(text),
            "- **a**: 1\n- **b**: 2\n- **a**: | / **b**: x | y",
        )

    def test_table_inside_fence_untouched(self) -> None:
        text = "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```"
        self.assertEqual(tables_to_bullets(text), text)

    def test_non_table_pipes_untouched(self) -> None:
        text = "cmd | grep foo\n| only one row |\nA || B"
        self.assertEqual(tables_to_bullets(text), text)

    def test_header_without_rows_untouched(self) -> None:
        text = "| a | b |\n|---|---|"
        self.assertEqual(tables_to_bullets(text), text)


class MdLinkTest(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertEqual(md_link("ドキュメント", "https://example.com/a"), "[ドキュメント](<https://example.com/a>)")

    def test_strips_scheme_from_label(self) -> None:
        self.assertEqual(md_link("https://example.com/x", "https://example.com/x"),
                         "[example.com/x](<https://example.com/x>)")
        self.assertEqual(md_link("HTTP://a.b", "http://a.b"), "[a.b](<http://a.b>)")

    def test_escapes_label(self) -> None:
        self.assertEqual(md_link("a [b] \\c", "https://x.y"), "[a \\[b\\] \\\\c](<https://x.y>)")

    def test_encodes_url(self) -> None:
        self.assertEqual(md_link("x", "https://x.y/a b<c>?q=1&r=%20"), "[x](<https://x.y/a%20b%3Cc%3E?q=1&r=%20>)")


class NotificationTest(unittest.TestCase):
    def test_levels(self) -> None:
        for level, (emoji, color) in LEVELS.items():
            self.assertEqual(format_notification("本文", level), f"{emoji} 本文")
            self.assertEqual(level_color(level), color)
        self.assertEqual(format_notification("本文"), "ℹ️ 本文")

    def test_title(self) -> None:
        self.assertEqual(
            format_notification("1 行目\n2 行目", "success", "完了"),
            "✅ **完了**\n1 行目\n2 行目",
        )
        self.assertEqual(format_notification("", "error", "失敗"), "❌ **失敗**")

    def test_emoji_on_own_line_for_block_start(self) -> None:
        self.assertEqual(format_notification("```py\nx\n```", "warning"), "⚠️\n```py\nx\n```")
        self.assertEqual(format_notification("# 見出し"), "ℹ️\n# 見出し")

    def test_tables_converted(self) -> None:
        out = format_notification("| k | v |\n|---|---|\n| a | 1 |", "info", "表")
        self.assertEqual(out, "ℹ️ **表**\n- **k**: a / **v**: 1")

    def test_errors(self) -> None:
        with self.assertRaises(ValueError) as cm:
            format_notification("x", "fatal")
        for level in LEVELS:
            self.assertIn(level, str(cm.exception))
        with self.assertRaises(ValueError):
            level_color("fatal")
        with self.assertRaises(ValueError):
            format_notification("   ")


if __name__ == "__main__":
    unittest.main()
