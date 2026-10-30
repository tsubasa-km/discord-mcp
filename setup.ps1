# Discord MCP のセットアップ（Windows）
# 仮想環境を作って依存パッケージを入れ、対話式のセットアップ（configure.py）を実行する。
#
#   powershell -ExecutionPolicy Bypass -File setup.ps1
#
# 引数はそのまま configure.py に渡る（例: -File setup.ps1 --name discord-work）。

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$venvPython = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) { throw 'python が見つかりません。Python 3.11 以降をインストールしてください。' }
    Write-Host '仮想環境を作成しています…'
    & $python.Source -m venv (Join-Path $root '.venv')
}

Write-Host '依存パッケージを確認しています…'
& $venvPython -m pip install -q --disable-pip-version-check -r (Join-Path $root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install に失敗しました。' }

& $venvPython (Join-Path $root 'configure.py') @args
exit $LASTEXITCODE
