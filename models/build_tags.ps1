# Idempotently create/update the Ollama tags used by this pipeline.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File <this file>
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 as GBK by default,
# so non-ASCII characters inside quoted strings get mangled and can swallow the closing quote.
$ErrorActionPreference = 'Stop'
$ollama = 'C:\Users\Administrator\AppData\Local\Programs\Ollama\ollama.exe'
if (-not (Test-Path $ollama)) { throw "ollama not found: $ollama" }

$dir = $PSScriptRoot
$files = Get-ChildItem -Path $dir -Filter '*.Modelfile'
if (-not $files) { throw "no *.Modelfile under $dir" }

foreach ($f in $files) {
    $tag = $f.BaseName
    Write-Host "==> create $tag  (from $($f.Name))"
    & $ollama create $tag -f $f.FullName
    if ($LASTEXITCODE -ne 0) { throw "create $tag failed (exit=$LASTEXITCODE)" }
}

Write-Host ''
Write-Host '==> tags:'
& $ollama list | Select-String -Pattern 'qwen3-8b-pm-16k|qwen3-14b-arch-8k|qwen2.5-coder-7b-dev-24k'

Write-Host ''
Write-Host '==> optional cleanup: the old 32K PM preset is unused, remove it with:'
Write-Host '    ollama rm qwen3-8b-pm-32k'
