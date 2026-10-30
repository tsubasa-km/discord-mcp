# Third-party notices

このプロジェクトの一部は、[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
（MIT License）の設計・挙動を参考にしている（adapted designs/behaviors from NousResearch/hermes-agent）。
コードを大きくそのまま写したものではないが、移植した箇所には
`# Ported from NousResearch/hermes-agent (MIT): <file>` のコメントを付けている。

| このプロジェクトのファイル | 参考にした内容 | hermes-agent の該当箇所 |
|---|---|---|
| `formatting.py` | メッセージ分割（コードブロックの閉じ直しと再オープン、インラインコードの保護、`(i/N)` 表示、チャンク数の上限と省略の注記）、表の箇条書き化、Markdown リンクの整形 | `gateway/platforms/base.py`（`truncate_message`）、`plugins/platforms/discord/adapter.py`（`_cap_split_chunks`、`_format_discord_markdown_link`、`format_message`） |
| `components.py` | ボタンや選択肢を使った問いかけ（ClarifyChoiceView / ExecApprovalView / ChoicePickerView の見た目、押下時の判定順、回答後・時間切れ時の表示） | `plugins/platforms/discord/adapter.py`、`adapter_exec_approval_view.py`、`adapter_component_auth.py` |
| `media.py` | 添付ファイルの上限・送信方法、フォーラムチャンネルへの投稿、リアクション | `plugins/platforms/discord/adapter.py`、`adapter_media.py` |

`gateway.py` は hermes-agent のコードを参考にせず、Discord Gateway の公開仕様に沿って独自に実装している。

## NousResearch/hermes-agent — MIT License

出典: https://github.com/NousResearch/hermes-agent/blob/main/LICENSE

```
MIT License

Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
