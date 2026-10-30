"""質問メッセージのボタン・セレクトメニューの組み立てと、クリックの処理（純粋なロジックだけ）。

Discord への通信もスレッドも持たない。ここで作った dict を server / gateway がそのまま
Discord に送る。

- build_message        質問を投稿するときの POST /channels/{id}/messages のボディ
- handle_interaction   ボタン・セレクトが押されたときのインタラクション応答と回答
- interpret_text_answer スレッドへの文字の返信を回答に解釈する
- *_edit               回答済み・期限切れ・取り下げのときに質問メッセージを書き換える PATCH ボディ

custom_id は "dmcp:{qid}:{action}"。action は選択肢の番号（0 始まり）、"select"、
"approve"、"deny"、"other" のどれか。

全文は content に入れ、embed には状態（タイトル・色・フッター）だけを載せる。
web やモバイルでは embed が見えないことがあるため。
"""

# Ported from NousResearch/hermes-agent (MIT): plugins/platforms/discord/adapter.py
# （ClarifyChoiceView / ExecApprovalView / ChoicePickerView の見た目と、クリック時の
#   判定順・確定処理・期限切れ処理の振る舞いを、discord.py を使わない dict 生成に移したもの）

from __future__ import annotations

import re
import secrets
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal

ORANGE = 0xE67E22
GREEN = 0x2ECC71
RED = 0xE74C3C
BLUE = 0x3498DB
GREYPLE = 0x99AAB5

PREFIX = "dmcp"

# Discord の上限
MAX_CONTENT = 2000
MAX_EMBED_TITLE = 256
MAX_FOOTER = 2048
MAX_BUTTON_LABEL = 80
MAX_OPTION_LABEL = 100
MAX_PLACEHOLDER = 150
MAX_OPTIONS = 25
MAX_PER_ROW = 5
MAX_ROWS = 5
MAX_CUSTOM_ID = 100

# これより多い選択肢はボタンではなくセレクトメニューにする
MAX_CHOICE_BUTTONS = 20
# content が長すぎるとき、質問文に最低限残す文字数
MIN_QUESTION_ROOM = 300
# 書き換え時に案内の代わりに入れる状態行の上限
MAX_STATUS = 500

# ボタンのスタイル
PRIMARY, SECONDARY, SUCCESS, DANGER = 1, 2, 3, 4
# コンポーネントの種類
ACTION_ROW, BUTTON, STRING_SELECT = 1, 2, 3
# インタラクション応答の種類
CHANNEL_MESSAGE, UPDATE_MESSAGE = 4, 7
EPHEMERAL = 64

TITLE = "❓ 質問"
FOOTER_WAITING = "回答待ち"
FOOTER_EXPIRED = "⏳ 時間内に回答がなかったため保留中 — スレッドに返信すれば次回の実行で回収します"
FOOTER_CANCELLED = "🚫 この質問は取り下げられました"
HINT_BUTTONS = "ボタンで答えてください"
HINT_OTHER = "（選択肢にない答えは「その他」から書けます）"
HINT_TEXT = "↳ このメッセージのスレッドに返信してください"
HINT_NO_BUTTONS_CHOICE = "↳ このメッセージのスレッドに、番号か選択肢をそのまま返信してください"
HINT_NO_BUTTONS_CONFIRM = "↳ このメッセージのスレッドに「はい」か「いいえ」で返信してください"
PLACEHOLDER = "選択してください"
NOTICE_RESOLVED = "この質問はすでに回答済みです"
NOTICE_UNAUTHORIZED = "この質問に回答できるのは指定されたユーザーだけです"
NOTICE_UNKNOWN = "この操作は受け付けられません"

APPROVE_WORDS = {"はい", "yes", "y", "ok", "承認", "approve", "1", "✅"}
DENY_WORDS = {"いいえ", "no", "n", "却下", "deny", "2", "❌"}
CONFIRM_LABELS = {"approve": "承認", "deny": "却下"}

_SPLIT_NUMBERS = re.compile(r"[\s,、，]+")
_TRAILING_PUNCT = "。．.！!"


@dataclass
class Prompt:
    """ボタン付きの質問 1 件の状態。保留中の質問として state ファイルにも保存する。"""

    qid: str
    kind: Literal["choice", "confirm", "text"]
    question: str
    choices: list[str] = field(default_factory=list)
    multi: bool = False
    allow_other: bool = True
    task: str | None = None
    allowed_user_ids: list[str] = field(default_factory=list)
    resolved: bool = False  # handle_interaction が書き換える
    awaiting_text: bool = False  # 「その他」が押されたら True

    def to_dict(self) -> dict[str, Any]:
        """JSON にそのまま書ける dict にする。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Prompt":
        """to_dict の逆。知らないキーは無視し、無いキーは既定値にする。"""
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in d.items() if k in known}
        for key in ("choices", "allowed_user_ids"):
            if key in data:
                data[key] = [str(x) for x in data[key] or []]
        return cls(**data)


# ---------------------------------------------------------------- custom_id


def new_qid() -> str:
    """custom_id に入れても安全な短いランダム ID（16 進 12 文字）。"""
    return secrets.token_hex(6)


def custom_id(qid: str, action: str) -> str:
    """"dmcp:{qid}:{action}" を作る。qid に ":" は使えない。"""
    if not qid or ":" in qid:
        raise ValueError(f"qid に使えない値です: {qid!r}")
    if not action:
        raise ValueError("action が空です")
    cid = f"{PREFIX}:{qid}:{action}"
    if len(cid) > MAX_CUSTOM_ID:
        raise ValueError(f"custom_id が {MAX_CUSTOM_ID} 文字を超えます: {cid!r}")
    return cid


def parse_custom_id(s: str) -> tuple[str, str] | None:
    """custom_id から (qid, action) を取り出す。このモジュールの形式でなければ None。"""
    if not isinstance(s, str):
        return None
    parts = s.split(":", 2)
    if len(parts) != 3 or parts[0] != PREFIX or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


# ---------------------------------------------------------------- 文字列の切り詰め


def _clip(text: str, limit: int) -> str:
    """limit 文字を超えたら末尾を「…」にして切る。"""
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _clip_label(text: str, limit: int) -> str:
    """ラベル用。改行をまとめ、長ければ単語の切れ目で切って「…」を付ける。"""
    # Ported from NousResearch/hermes-agent (MIT): plugins/platforms/discord/adapter.py
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space >= limit // 2:  # 切れ目が前すぎるときは（日本語など）文字数で切る
        cut = cut[:space]
    return cut.rstrip() + "…"


# ---------------------------------------------------------------- メッセージ本体


def _hint(p: Prompt, buttons: bool) -> str:
    """投稿時の末尾の案内。ボタンがある質問ではスレッドを作らないので、スレッドへの返信は案内しない。"""
    if p.kind == "text":
        return HINT_TEXT
    if buttons:
        return HINT_BUTTONS + (HINT_OTHER if p.allow_other else "")
    return HINT_NO_BUTTONS_CONFIRM if p.kind == "confirm" else HINT_NO_BUTTONS_CHOICE


def _content(p: Prompt, mention_ids: list[str], status: str | None = None, buttons: bool = True) -> str:
    """質問の全文。2000 文字に収まるよう、まず質問文、足りなければ選択肢の一覧を切る。

    status を渡すと、末尾の案内の代わりにその状態行を入れる（書き換え用）。
    """
    mentions = " ".join(f"<@{u}>" for u in mention_ids)
    head = (f"{mentions} " if mentions else "") + "❓ **質問**" + (f"（{p.task}）" if p.task else "")
    show_choices = p.kind != "confirm" and bool(p.choices)
    listing = "\n".join(f"{i}. {c}" for i, c in enumerate(p.choices, 1)) if show_choices else ""
    if status:
        hint = _clip(" ".join(status.split()), MAX_STATUS)
    else:
        hint = _hint(p, buttons)
    question = p.question.strip()

    def join(q: str, lst: str) -> str:
        return "\n\n".join(x for x in (head, q, lst, hint) if x)

    if len(join(question, listing)) > MAX_CONTENT:
        room = MAX_CONTENT - len(join("", listing)) - 2
        if room >= min(len(question), MIN_QUESTION_ROOM):
            question = _clip(question, room)
        else:
            question = _clip(question, MIN_QUESTION_ROOM)
            listing = _clip(listing, max(1, MAX_CONTENT - len(join(question, "")) - 2))
    return _clip(join(question, listing), MAX_CONTENT)


def _embed(p: Prompt, color: int, footer: str) -> dict[str, Any]:
    """状態だけを載せた embed。"""
    # Ported from NousResearch/hermes-agent (MIT): plugins/platforms/discord/adapter.py
    title = TITLE + (f"（{p.task}）" if p.task else "")
    return {
        "title": _clip(title, MAX_EMBED_TITLE),
        "color": color,
        "footer": {"text": _clip(footer, MAX_FOOTER)},
    }


def _button(
    p: Prompt, action: str, label: str, style: int, emoji: str | None = None, disabled: bool = False
) -> dict[str, Any]:
    b: dict[str, Any] = {
        "type": BUTTON,
        "style": style,
        "label": _clip_label(label, MAX_BUTTON_LABEL),
        "custom_id": custom_id(p.qid, action),
        "disabled": disabled,
    }
    if emoji:
        b["emoji"] = {"name": emoji}
    return b


def _other_button(p: Prompt, disabled: bool) -> dict[str, Any]:
    return _button(p, "other", "その他", SECONDARY, "✏️", disabled)


def _rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ボタンを 1 行 5 個ずつのアクション行に並べる。"""
    rows = [
        {"type": ACTION_ROW, "components": items[i : i + MAX_PER_ROW]}
        for i in range(0, len(items), MAX_PER_ROW)
    ]
    if len(rows) > MAX_ROWS:
        raise ValueError(f"ボタンが {MAX_ROWS} 行に収まりません")
    return rows


def _uses_select(p: Prompt) -> bool:
    return p.kind == "choice" and (p.multi or len(p.choices) > MAX_CHOICE_BUTTONS)


def _validate(p: Prompt) -> None:
    if p.kind not in ("choice", "confirm", "text"):
        raise ValueError(f"kind が不正です: {p.kind!r}")
    if p.kind == "choice":
        if not p.choices:
            raise ValueError("kind='choice' には選択肢が 1 つ以上必要です")
        if len(p.choices) > MAX_OPTIONS:
            raise ValueError(f"選択肢は {MAX_OPTIONS} 個までです（{len(p.choices)} 個）")


def _components(p: Prompt, disabled: bool = False) -> list[dict[str, Any]]:
    """kind に応じたボタン・セレクトメニューのアクション行。"""
    _validate(p)
    if p.kind == "text":
        return []

    if p.kind == "confirm":
        # Ported from NousResearch/hermes-agent (MIT): plugins/platforms/discord/adapter.py (ExecApprovalView)
        items = [
            _button(p, "approve", "承認", SUCCESS, "✅", disabled),
            _button(p, "deny", "却下", DANGER, "❌", disabled),
        ]
        if p.allow_other:
            items.append(_other_button(p, disabled))
        return _rows(items)

    if _uses_select(p):
        # Ported from NousResearch/hermes-agent (MIT): plugins/platforms/discord/adapter.py (ChoicePickerView)
        select = {
            "type": STRING_SELECT,
            "custom_id": custom_id(p.qid, "select"),
            "placeholder": _clip_label(PLACEHOLDER, MAX_PLACEHOLDER),
            "min_values": 1,
            "max_values": len(p.choices) if p.multi else 1,
            "options": [
                {"label": _clip_label(f"{i + 1}. {c}", MAX_OPTION_LABEL), "value": str(i)}
                for i, c in enumerate(p.choices)
            ],
            "disabled": disabled,
        }
        rows = [{"type": ACTION_ROW, "components": [select]}]
        if p.allow_other:
            rows.append({"type": ACTION_ROW, "components": [_other_button(p, disabled)]})
        return rows

    # Ported from NousResearch/hermes-agent (MIT): plugins/platforms/discord/adapter.py (ClarifyChoiceView)
    items = []
    for i, c in enumerate(p.choices):
        prefix = f"{i + 1}. "
        items.append(_button(p, str(i), prefix + _clip_label(c, MAX_BUTTON_LABEL - len(prefix)), PRIMARY, None, disabled))
    if p.allow_other:
        items.append(_other_button(p, disabled))
    return _rows(items)


def build_message(p: Prompt, mention_ids: list[str], buttons: bool = True) -> dict[str, Any]:
    """質問を投稿する POST /channels/{id}/messages のボディ。

    buttons=False なら（Gateway が使えないとき）ボタンを付けず、スレッドへの返信を案内する。
    選択肢が無い choice、選択肢が 25 個を超える choice、不正な kind は ValueError。
    """
    components = _components(p)
    return {
        "content": _content(p, mention_ids, buttons=buttons),
        "embeds": [_embed(p, ORANGE, FOOTER_WAITING)],
        "components": components if buttons else [],
        "allowed_mentions": {"parse": [], "users": list(mention_ids)},
    }


def _edit_body(
    p: Prompt, color: int, footer: str, components: list[dict[str, Any]], status: str | None = None
) -> dict[str, Any]:
    """書き換え用のボディ。content は Prompt から作り直す（メンションは付け直さない）。

    embed が見えない環境でも状態がわかるよう、content の案内は状態行（省略時は footer と同じ）に置き換える。
    """
    return {
        "content": _content(p, [], status or footer),
        "embeds": [_embed(p, color, footer)],
        "components": components,
        "allowed_mentions": {"parse": []},
    }


# ---------------------------------------------------------------- クリックの処理


def _ephemeral(text: str) -> dict[str, Any]:
    return {"type": CHANNEL_MESSAGE, "data": {"content": text, "flags": EPHEMERAL}}


def _interaction_user(interaction: dict[str, Any]) -> dict[str, str]:
    """押した人。サーバー内なら member.user、DM なら user。"""
    user = (interaction.get("member") or {}).get("user") or interaction.get("user") or {}
    name = user.get("global_name") or user.get("username") or "不明なユーザー"
    return {"id": str(user.get("id", "")), "name": name}


def _choice_result(p: Prompt, indices: list[int]) -> dict[str, Any]:
    """選択肢の回答。indices は 0 始まり。1 つだけ選ばれたときは index も付ける。"""
    indices = sorted(set(indices))
    values = [p.choices[i] for i in indices]
    result: dict[str, Any] = {"kind": "choice", "answer": "、".join(values)}
    if len(indices) == 1:
        result["index"] = indices[0]
    result["values"] = values
    result["indices"] = indices
    return result


def _parse_indices(raw: list[Any], n: int) -> list[int] | None:
    out = []
    for v in raw:
        try:
            i = int(v)
        except (TypeError, ValueError):
            return None
        if not 0 <= i < n:
            return None
        out.append(i)
    return out or None


def _button_result(p: Prompt, action: str, interaction: dict[str, Any]) -> dict[str, Any] | None:
    """action を回答にする。この Prompt では受け付けない操作なら None。"""
    if action == "other":
        return {"kind": "other"} if p.allow_other and p.kind != "text" else None
    if p.kind == "confirm":
        if action in CONFIRM_LABELS:
            return {"kind": "confirm", "answer": action, "approved": action == "approve"}
        return None
    if p.kind != "choice":
        return None
    if action == "select":
        if not _uses_select(p):
            return None
        values = (interaction.get("data") or {}).get("values") or []
        indices = _parse_indices(values, len(p.choices))
        if indices is None or (not p.multi and len(indices) != 1):
            return None
        return _choice_result(p, indices)
    if action.isdecimal() and not _uses_select(p):
        indices = _parse_indices([action], len(p.choices))
        return _choice_result(p, indices) if indices else None
    return None


def _answer_label(result: dict[str, Any]) -> str:
    if result.get("kind") == "confirm":
        return CONFIRM_LABELS.get(result.get("answer", ""), str(result.get("answer", "")))
    return " ".join(str(result.get("answer", "")).split())


def _is_deny(result: dict[str, Any]) -> bool:
    return result.get("kind") == "confirm" and not result.get("approved")


def _answered_body(
    p: Prompt, result: dict[str, Any], name: str, components: list[dict[str, Any]]
) -> dict[str, Any]:
    """回答済みの書き換えボディ。却下なら赤と ❌、それ以外は緑と ✅。"""
    footer = f"回答: {_answer_label(result)} — {name}"
    deny = _is_deny(result)
    return _edit_body(p, RED if deny else GREEN, footer, components, ("❌ " if deny else "✅ ") + footer)


def handle_interaction(p: Prompt, interaction: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """ボタン・セレクトが押されたときの (インタラクション応答, 回答) を返す。

    判定の順番は「回答済み → 権限 → 操作の妥当性」。回答済みの確認は権限より先に行う。
    受け付けなかったときは押した人にだけ見える通知を返し、回答は None。
    受け付けたら p.resolved を True にし（「その他」なら p.awaiting_text も True）、
    すべてのコンポーネントを無効にしてメッセージをその場で書き換える応答を返す。
    """
    # Ported from NousResearch/hermes-agent (MIT): plugins/platforms/discord/adapter.py
    if p.resolved:
        return _ephemeral(NOTICE_RESOLVED), None
    user = _interaction_user(interaction)
    if user["id"] not in {str(u) for u in p.allowed_user_ids}:
        return _ephemeral(NOTICE_UNAUTHORIZED), None

    parsed = parse_custom_id((interaction.get("data") or {}).get("custom_id", ""))
    if parsed is None or parsed[0] != p.qid:
        return _ephemeral(NOTICE_UNKNOWN), None
    result = _button_result(p, parsed[1], interaction)
    if result is None:
        return _ephemeral(NOTICE_UNKNOWN), None

    p.resolved = True
    components = _components(p, disabled=True)
    if result["kind"] == "other":
        p.awaiting_text = True
        body = _edit_body(p, BLUE, f"✏ {user['name']} さんが入力中… スレッドに返信してください", components)
    else:
        body = _answered_body(p, result, user["name"], components)
    return {"type": UPDATE_MESSAGE, "data": body}, {**result, "user": user, "via": "button"}


# ---------------------------------------------------------------- 文字の返信


def interpret_text_answer(p: Prompt, text: str) -> dict[str, Any]:
    """スレッドへの文字の返信を回答にする。

    choice: 番号（1 始まり。multi なら「1, 3」「1 3」「1、3」も可）か、選択肢と同じ文字列
    confirm: はい / yes / ok / 承認 / 1 / ✅ → approve、いいえ / no / 却下 / 2 / ❌ → deny
    どれにも当てはまらない、または「その他」が押された後なら {"kind": "text"}。
    """
    s = text.strip()
    if not p.awaiting_text:
        if p.kind == "choice" and p.choices:
            n = len(p.choices)
            tokens = [t for t in _SPLIT_NUMBERS.split(s) if t] if p.multi else [s]
            if tokens and all(t.isdecimal() for t in tokens):
                indices = _parse_indices([int(t) - 1 for t in tokens], n)
                if indices and (p.multi or len(indices) == 1):
                    return {**_choice_result(p, indices), "via": "text"}
            folded = s.casefold()
            for i, c in enumerate(p.choices):
                if c.strip().casefold() == folded:
                    return {**_choice_result(p, [i]), "via": "text"}
        elif p.kind == "confirm":
            word = s.rstrip(_TRAILING_PUNCT).strip().casefold()
            if word in APPROVE_WORDS:
                return {"kind": "confirm", "answer": "approve", "approved": True, "via": "text"}
            if word in DENY_WORDS:
                return {"kind": "confirm", "answer": "deny", "approved": False, "via": "text"}
    return {"kind": "text", "answer": text, "via": "text"}


# ---------------------------------------------------------------- 書き換え（PATCH）


def answered_by_text_edit(p: Prompt, result: dict[str, Any], user_name: str) -> dict[str, Any]:
    """スレッドへの返信で回答されたときに質問メッセージを書き換える PATCH ボディ。"""
    return _answered_body(p, result, user_name, [])


def expired_edit(p: Prompt) -> dict[str, Any]:
    """時間内に回答がなく保留にしたときの PATCH ボディ（ボタンは外す）。"""
    # Ported from NousResearch/hermes-agent (MIT): plugins/platforms/discord/adapter.py
    return _edit_body(p, GREYPLE, FOOTER_EXPIRED, [])


def cancelled_edit(p: Prompt) -> dict[str, Any]:
    """質問を取り下げたときの PATCH ボディ（ボタンは外す）。"""
    return _edit_body(p, GREYPLE, FOOTER_CANCELLED, [])
