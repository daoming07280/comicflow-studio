$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$env:UV_CACHE_DIR = Join-Path $projectRoot '.cache\uv'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'Install uv and Python 3.11+ first: https://docs.astral.sh/uv/getting-started/installation/' }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw 'Install FFmpeg and add ffmpeg/ffprobe to PATH first.' }
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    uv venv --python 3.12 .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create Python environment.' }
}
uv pip install --python .venv\Scripts\python.exe -r requirements.lock
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
uv pip install --python .venv\Scripts\python.exe --no-deps -e .
if ($LASTEXITCODE -ne 0) { throw 'Project installation failed.' }
Write-Host 'Ready. Open the launcher in the project root.'
