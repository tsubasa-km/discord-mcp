# Discord MCP（通知と質問）

Claude のスケジュールタスクなど無人で動くエージェントから、Discord に通知を送ったり、
人間に質問して返事を受け取ったりするための MCP サーバー（stdio）。

- 質問は宛先ユーザーにメンションして投稿する
- 選択肢や承認は **ボタン**（選択肢が 21 個以上か複数選択なら **選択メニュー**）で答える。
  このときスレッドは作らず、「その他」が押されたときだけスレッドを作って文字の返信を待つ
- 自由記述の質問（`choices` なし）は、最初からスレッドを作ってそこへの返信を待つ
- 決めた時間内に返事がなければボタンを外し、スレッドを作って「保留」として `state.json` に記録する。
  スレッドに返信しておけば、次回の実行で回収できる
- 通知は長文を自動で分割し（コードブロックを壊さない）、レベルごとの絵文字・見出し・添付ファイルを付けられる

ボタンの押下は Discord Gateway（WebSocket）で受け取る。接続はこの MCP サーバーが動いている間だけ張る
（ボタン付きの質問を初めて出したときに接続する）。Gateway に接続できないときは、ボタンを付けずに
スレッドへの文字の返信だけで受け付ける。

ボタンや選択肢の見た目・挙動は [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
（MIT）の Discord 連携を参考にしている。詳しくは [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## ツール

| ツール | 内容 |
|---|---|
| `notify(message, level="info", title=None, mention=False, files=None, thread_id=None)` | 通知を送る。返事は待たない。`level` は info / success / warning / error。2000 文字を超える本文は分割する（最大 8 通） |
| `edit_notification(message_id, message, level, title, channel_id)` | notify で送ったメッセージを書き換える。進捗を 1 つのメッセージで更新するのに使う |
| `ask(question, choices=None, multi=False, allow_other=True, timeout_seconds=300, task=None, download_dir=None)` | 質問して回答を待つ。`choices` を渡すとボタンで答えられ、「その他」を押すと文字で答えられる。最大 1800 秒待ち、来なければ `pending` |
| `confirm(question, timeout_seconds=300, allow_other=False, task=None)` | 「承認」「却下」ボタンで実行の可否を確認する。結果は `approved: true/false` |
| `check_answers(task=None, download_dir=None)` | 保留中の質問に返信が来たか確認する。回答が来たものは一度だけ返して保留リストから消す |
| `cancel_question(question_id)` | 保留中の質問を取り下げる |

回答として受け付けるのは `DISCORD_USER_ID` のユーザーのボタン操作と、スレッドへの返信だけ
（ほかの人がボタンを押すと、その人にだけ見える通知で断る）。
保留中の質問やボタンが使えないときのスレッドへの文字の返信は、選択肢の質問なら番号（`2`、複数選択なら `1, 3`）や選択肢と同じ文字列を、
`confirm` なら「はい / いいえ」「承認 / 却下」「yes / no」などを選択として解釈する。
複数のメッセージに分けて返信した場合はまとめて 1 つの回答になり、受け取った返信には ✅ が付く。
返信の添付ファイルは URL で返し、`download_dir` を指定するとそのフォルダに保存する。

回答の主な項目:

| 項目 | 内容 |
|---|---|
| `status` | `answered` / `pending` |
| `kind` | `choice`（選択肢）/ `confirm`（承認）/ `text`（自由記述・「その他」） |
| `answer` | 回答。選択肢ならその文字列（複数なら「、」区切り）、confirm なら `approve` / `deny` |
| `values` / `indices` | 選ばれた選択肢と、その番号（0 始まり） |
| `approved` | confirm の結果 |
| `via` | `button` / `text` |
| `answered_by` | 回答した人の表示名 |

## セットアップ

### かんたんセットアップ（推奨）

1. [Discord Developer Portal](https://discord.com/developers/applications) で **New Application** を作り、
   左メニューの **Bot** → **Reset Token** でトークンを表示しておく
2. clone して、セットアップスクリプトを実行する

```powershell
git clone https://github.com/tsubasa-km/discord-mcp.git
cd discord-mcp
powershell -ExecutionPolicy Bypass -File setup.ps1     # macOS / Linux は sh setup.sh
```

スクリプトが仮想環境を作って依存パッケージを入れたあと、対話しながら次を進める。

| ステップ | 内容 |
|---|---|
| トークン | 伏せ字で入力。形と有効性をその場で確かめる |
| MESSAGE CONTENT INTENT | オフなら設定画面の URL を出し、オンにするまで待つ |
| サーバーへの招待 | Bot がどのサーバーにも入っていなければ、必要な権限付きの招待 URL を出して待つ |
| 投稿先チャンネル | Bot から見えるテキスト・フォーラムチャンネルを一覧にして、番号で選ぶ |
| 宛先ユーザー | Bot の持ち主（チームならメンバー）を既定にする。別の人の ID も入れられる |
| 登録 | `claude mcp add --scope user` で登録する（同じ名前の登録があれば置き換えるか確認する） |
| テスト通知 | 選んだチャンネルに完了の通知を送る（`--no-test` で省略） |

登録名を変えたいとき（例: 仕事用と個人用で 2 つ登録する）は `setup.ps1 --name discord-work` のように指定する。
登録後に新しく開いた Claude Code のセッションやスケジュールタスクから使える。

トークンは Claude Code の設定（`~/.claude.json`）に平文で保存される。また登録の瞬間だけ
`claude mcp add` の引数としてプロセス一覧に現れる。気になる場合は下の「手動で設定する」の A（Proton Pass）を使う。

### 手動で設定する

#### 1. Discord Bot を作る

1. [Discord Developer Portal](https://discord.com/developers/applications) で New Application を作る
2. 左メニューの **Bot** で
   - **Reset Token** でトークンを発行し、Proton Pass などに保存する（チャットやファイルに貼らない）
   - **Privileged Gateway Intents** の **MESSAGE CONTENT INTENT** をオンにする（返信の本文を読むのに必要）
3. **OAuth2 → URL Generator** で scope に `bot`、権限に次を選び、出た URL から自分のサーバーに招待する
   - View Channels / Send Messages / Create Public Threads / Send Messages in Threads / Read Message History /
     Embed Links / Attach Files / Add Reactions
   - URL を直接作るなら `https://discord.com/oauth2/authorize?client_id=<アプリケーションID>&scope=bot&permissions=309237763136`
   - 招待済みの Bot に権限を足すときも、同じ URL でもう一度認証すればよい
4. Discord の 設定 → 詳細設定 → **開発者モード** をオンにして、
   - 投稿先チャンネルを右クリック → **チャンネル ID をコピー**
   - 自分のアイコンを右クリック → **ユーザー ID をコピー**

#### 2. Python 環境

```powershell
git clone https://github.com/tsubasa-km/discord-mcp.git
cd discord-mcp
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

#### 3. Claude Code に登録する

`--scope user` にすると、どのフォルダで動くセッションやスケジュールタスクからも使える。

##### A. Proton Pass からトークンを渡す（トークンをどこにも平文で置かない）

`discord.env.example` を `discord.env` にコピーし、ID とトークンの `pass://` 参照を書く
（参照だけなのでこのファイル自体は秘密ではない。BOM なし UTF-8 で保存すること）。

```powershell
claude mcp add discord --scope user -- pass-cli run --env-file C:\path\to\discord-mcp\discord.env -- C:\path\to\discord-mcp\.venv\Scripts\python.exe C:\path\to\discord-mcp\server.py
```

注意：`pass-cli run` は Proton Pass にログインしていないと起動できない。セッションが切れていると、
スケジュールタスクの実行時にこの MCP サーバーが立ち上がらず、通知も質問もできなくなる。

##### B. 環境変数を Claude の設定に直接書く

トークンは `~/.claude.json` に平文で保存される。自分で端末から実行すること。
トークンをコマンドに直接書くと PowerShell の履歴に残るので、`Read-Host` で伏せ字入力して変数経由で渡す。

```powershell
$t = Read-Host -MaskInput "Discord bot token"
claude mcp add discord --scope user -e "DISCORD_BOT_TOKEN=$t" -e DISCORD_CHANNEL_ID=<チャンネルID> -e DISCORD_USER_ID=<ユーザーID> -- C:\path\to\discord-mcp\.venv\Scripts\python.exe C:\path\to\discord-mcp\server.py
Remove-Variable t
```

登録できたかは `claude mcp list` で確認できる。

## コマンドラインから使う（cli.py）

MCP サーバーを登録できない環境（Claude のクラウドで動く定期実行など）やシェルスクリプトからは、
同じ機能を `cli.py` で使える。環境変数は MCP サーバーと同じ。結果は JSON で標準出力に出る。

```sh
python cli.py notify "本文" --level success --title "週次反映"      # 本文に - を渡すと標準入力から読む
python cli.py ask "どの課題に計上しますか？" --choice 明治座保守 --choice その他 --timeout 600 --task koshu
python cli.py confirm "適用してよいですか？" --timeout 600
python cli.py answer <question_id> --choice 明治座保守 --choice その他   # 保留中の質問への返信を ID だけで読む
```

実行ごとにファイルが消える環境では、保留中の質問を覚えておく `state.json` が残らない。
`ask` / `confirm` が返した `question_id` を自分で記録しておき、次の実行で `answer` を呼んで返信を読む
（`--choice` / `--confirm` を付けると、番号や「はい」を選択肢・承認として解釈する）。

クラウドの定期実行で使う例:

```sh
git clone -q --depth 1 --branch v0.3.0 https://github.com/tsubasa-km/discord-mcp /tmp/discord-mcp
pip install -q -r /tmp/discord-mcp/requirements.txt
python /tmp/discord-mcp/cli.py notify "週次反映が終わりました" --level success
```

## スケジュールタスクのプロンプトに入れる例

```text
作業を始める前に、discord の check_answers を task="<タスク名>" で呼び、
前回までに保留した質問への回答があればそれに従って処理すること。

判断に迷ったら discord の ask を task="<タスク名>"、timeout_seconds=300 で呼んで質問すること。
選べる答えが決まっているときは choices に選択肢を渡すこと。
ファイルの削除や外部への送信など取り消せない操作の前には、discord の confirm で確認し、approved が true のときだけ実行すること。
pending が返ったら、その質問に依存しない作業だけ進め、依存する作業は次回に回すこと。

終わったら discord の notify で結果を 3 行以内で知らせること。
成功したら level="success"、失敗したら level="error" と mention=True にすること。
```

## 環境変数

| 変数 | 内容 |
|---|---|
| `DISCORD_BOT_TOKEN` | Bot トークン（必須） |
| `DISCORD_CHANNEL_ID` | 通知・質問を投稿するチャンネル ID（必須。フォーラムチャンネルなら通知・質問ごとに投稿を作る） |
| `DISCORD_USER_ID` | 質問の宛先で、回答として受け付けるユーザー ID（必須。カンマ区切りで複数可） |
| `DISCORD_BUTTONS` | `0` にするとボタンを使わず、文字の返信だけで受け付ける（既定は `1`） |
| `DISCORD_MCP_STATE` | 保留中の質問の保存先（省略時は `server.py` と同じフォルダの `state.json`） |
| `DISCORD_API_BASE` / `DISCORD_GATEWAY_URL` | API / Gateway の URL（テスト用） |

## ファイル構成

| ファイル | 内容 |
|---|---|
| `server.py` | MCP サーバー本体（ツールの定義と、質問の投稿・待機・保留） |
| `discord_api.py` | REST API の共通部分（認証、レート制限、環境変数） |
| `gateway.py` | Gateway クライアント（ボタン・選択メニューの押下を受け取り、3 秒以内に応答する） |
| `components.py` | 質問メッセージ・ボタン・選択メニューの組み立てと、押下時の判定（通信しない） |
| `formatting.py` | 長文の分割、表の箇条書き化、通知の見出し（通信しない） |
| `media.py` | 添付ファイルの送信・保存、フォーラムチャンネル、リアクション |
| `cli.py` | コマンドラインから同じ機能を使う入口（MCP を登録できない環境向け） |
| `configure.py` | 対話式のセットアップ（トークン・Intent・招待・チャンネル・宛先の確認と Claude Code への登録） |
| `setup.ps1` / `setup.sh` | 仮想環境を作って依存パッケージを入れ、`configure.py` を実行する |

## テスト

偽の Discord API と偽の Gateway を立てて、MCP クライアントから全ツールを呼ぶ end-to-end テストと、
モジュールごとのユニットテスト。本物の Discord には接続しない。

```powershell
.venv\Scripts\python.exe -I tests\e2e_test.py server.py
.venv\Scripts\python.exe -m unittest tests.test_components tests.test_formatting tests.test_media tests.test_gateway tests.test_configure tests.test_cli
```

本物の Discord での確認には、`~/.claude.json` に登録した設定をそのまま使う次のスクリプトを使う（トークンは表示しない）。

```powershell
.venv\Scripts\python.exe -I tests\diagnose.py        # Bot が入っているサーバーと、設定したチャンネルが見えるか
.venv\Scripts\python.exe -I tests\live_test.py 180   # 通知を送り、質問して 180 秒返信を待つ
```

## うまく動かないとき

| 症状 | 原因 |
|---|---|
| 401 Unauthorized | トークンが違う、またはリセット済み |
| 403 Missing Access (50001) | Bot がサーバーに招待されていない、またはプライベートチャンネルに Bot が追加されていない |
| 返信はあるのに `answer` が空で `warning` が付く | Bot 設定の MESSAGE CONTENT INTENT がオフ |
| ボタンが出ず、結果に `note`（Gateway に接続できない）が付く | ネットワークで WebSocket（`wss://gateway.discord.gg`）が通らない、またはトークンが無効 |
| 返信に ✅ が付かない、添付ファイルが送れない | Bot に Add Reactions / Attach Files の権限がない |
| ボタンを押すと「インタラクションに失敗しました」と出る | 質問を出した MCP サーバーがもう終了している（時間切れ前にセッションが終わった）。スレッドに文字で返信すれば次回回収される |

## 制限

- `ask` で長く待つと、MCP クライアント側のツールのタイムアウトに先に引っかかることがある。
  その場合は `timeout_seconds` を短くして、保留と `check_answers` で回収する運用にする
- ボタンは質問を出したプロセスが動いている間（`timeout_seconds` の間）だけ使える。
  保留になった質問はボタンが外れ、スレッドへの文字の返信で答える
- `state.json` は同時に複数のプロセスから書き換えることを想定していない。
  スケジュールタスクが同時刻に重なると、保留中の質問の記録が片方だけになることがある

## ライセンス

MIT License。詳しくは [LICENSE](LICENSE)。参考にした NousResearch/hermes-agent（MIT）の表示は [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) にある。
