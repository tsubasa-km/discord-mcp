#!/bin/sh
# Discord MCP のセットアップ（macOS / Linux）
# 仮想環境を作って依存パッケージを入れ、対話式のセットアップ（configure.py）を実行する。
#
#   sh setup.sh            # 引数はそのまま configure.py に渡る
set -eu
root=$(cd "$(dirname "$0")" && pwd)

if [ ! -x "$root/.venv/bin/python" ]; then
    command -v python3 >/dev/null || { echo "python3 が見つかりません。Python 3.11 以降をインストールしてください。" >&2; exit 1; }
    echo "仮想環境を作成しています…"
    python3 -m venv "$root/.venv"
fi

echo "依存パッケージを確認しています…"
"$root/.venv/bin/python" -m pip install -q --disable-pip-version-check -r "$root/requirements.txt"

exec "$root/.venv/bin/python" "$root/configure.py" "$@"
