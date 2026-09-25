# Start (or restart) the Ollama server with the settings this project needs.
# Pure ASCII on purpose: Windows PowerShell 5.1 reads .ps1 as GBK and non-ASCII
# inside strings can swallow the closing quote.
#
# Why this script exists (found the hard way, 2026-09-23):
#   * Weight files live in D:\AI\Models (57 GB). OLLAMA_MODELS is NOT set in the
#     user/machine environment and the startup shortcut does not set it either,
#     so a plain `ollama serve` (or the tray app after a reboot) sees ZERO models.
#   * The server degrades after many hours of load/unload cycles: the same 14B
#     request went from 157 t/s prefill down to 6 t/s, and `size_vram == size`
#     still claimed "100% GPU". Restarting the server fixes it instantly.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File models\start_ollama.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File models\start_ollama.ps1 -SkipRestart
#   powershell -NoProfile -ExecutionPolicy Bypass -File models\start_ollama.ps1 -NoVulkan
param(
    [switch]$SkipRestart,
    [switch]$NoVulkan
)

$ErrorActionPreference = 'Stop'
$exe = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
if (-not (Test-Path $exe)) { throw "ollama.exe not found at $exe" }

if (-not $SkipRestart) {
    Write-Host 'Stopping existing ollama processes...'
    # 必须连推理子进程一起收：ollama 的推理进程叫 llama-server.exe，**不匹配** '*ollama*'。
    # 只按 '*ollama*' 收会把子进程留成孤儿继续占显存，症状是「重启后依然很慢」，
    # 而 /api/ps 的 size_vram 与 preflight 都看不出异常（真机踩过，排查了很久）。
    $stale = @(
        Get-Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.ProcessName -like '*ollama*' -or $_.ProcessName -like '*llama-server*'
            }
    )
    if ($stale.Count -gt 0) {
        Write-Host ("  stopping {0}: {1}" -f $stale.Count, (($stale | ForEach-Object { $_.ProcessName + '#' + $_.Id }) -join ', '))
        $stale | Stop-Process -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 3
}

# --- server settings (keep in sync with CONTEXT.md section 3) ---
$env:OLLAMA_MODELS          = 'D:\AI\Models'   # <- without this the tags are invisible
$env:OLLAMA_CONTEXT_LENGTH  = '16384'
$env:OLLAMA_FLASH_ATTENTION = '1'
$env:OLLAMA_KV_CACHE_TYPE   = 'q8_0'            # halves KV memory: 14B @8K needs the headroom
$env:OLLAMA_KEEP_ALIVE      = '30m'
$env:OLLAMA_MAX_LOADED_MODELS = '1'             # single-residency is enforced by the pipeline too
if ($NoVulkan) {
    $env:OLLAMA_VULKAN = $null
} else {
    $env:OLLAMA_VULKAN = '1'
}

Write-Host "Starting ollama serve (MODELS=$env:OLLAMA_MODELS, VULKAN=$env:OLLAMA_VULKAN)..."
Start-Process -FilePath $exe -ArgumentList 'serve' -WindowStyle Hidden

$ready = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    try {
        $null = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 2
        $ready = $true
        break
    } catch { }
}
if (-not $ready) { throw 'ollama did not become ready within 20s' }

Write-Host ''
Write-Host 'Server is up. Tags visible to it:'
& $exe list 2>&1 | Select-String -Pattern 'NAME|qwen3-8b-pm-16k|qwen3-14b-arch-8k|qwen2.5-coder-7b-dev-24k'
Write-Host ''
Write-Host 'Next: run  python tools\preflight.py  (30s) to confirm VRAM + prefill before a real run.'
Write-Host 'Tip: to make the model path permanent for the tray app, run once:'
Write-Host '     setx OLLAMA_MODELS D:\AI\Models    (then log out/in)'
