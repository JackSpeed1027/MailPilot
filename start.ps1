$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    $projectPython = (Get-Command python -ErrorAction Stop).Source
}
& $projectPython -m mailpilot --help *> $null
if ($LASTEXITCODE -ne 0) { throw '请安装 Python 3.11 或更新版本并加入 PATH。' }
& $projectPython -m mailpilot ui
