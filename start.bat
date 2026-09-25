@echo off
chcp 65001 >nul 2>&1
rem ============================================================
rem  Pipeline quick start (Windows)
rem
rem  Usage:
rem    start.bat              start Ollama + console, open the browser
rem    start.bat restart      restart Ollama first (use when prefill slows down)
rem    start.bat nobrowser    start the services only, do not open a browser
rem
rem  --- ENCODING: read before editing this file ---
rem  Save as UTF-8 WITHOUT BOM, CRLF line endings, and keep the chcp line above.
rem  The Chinese text below is decoded using the code page that chcp sets, so:
rem    * every comment must stay ASCII-only. If a comment holds multi-byte text
rem      and the file starts under a different code page, cmd decodes the comment
rem      into garbage and tries to run the fragments as commands;
rem    * never add call/goto labels. After chcp, cmd re-seeks the file by byte
rem      offset, and multi-byte text near the seek point gets split in half,
rem      producing "xxx is not recognized" errors plus half-executed lines.
rem  Both failure modes were reproduced on 2026-09-24 under code page 936.
rem ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PORT=8787"
set "URL=http://127.0.0.1:%PORT%/"
set "OLLAMA_ARGS=-SkipRestart"

rem restart / nobrowser switches
if /i "%~1"=="restart" set "OLLAMA_ARGS="
if /i "%~1"=="nobrowser" set "NOBROWSER=1"
if /i "%~2"=="nobrowser" set "NOBROWSER=1"

echo.
echo  ============================================================
echo    需求流水线 · 快速启动
echo  ============================================================
echo.

rem ---------- 0. sanity ----------
if not exist "pipeline\server.py" (
    echo  [错误] 当前目录不是项目根：%CD%
    echo         请把本脚本放在含 pipeline\ 的那一层运行。
    pause
    exit /b 1
)
where python >nul 2>&1
if errorlevel 1 (
    echo  [错误] 找不到 python，请确认它已加入 PATH。
    pause
    exit /b 1
)

rem ---------- 1/3 Ollama ----------
netstat -ano | findstr /R /C:":11434 .*LISTENING" >nul 2>&1
if errorlevel 1 (
    echo  [1/3] Ollama 未运行，正在启动...
    powershell -NoProfile -ExecutionPolicy Bypass -File "models\start_ollama.ps1" %OLLAMA_ARGS%
    if errorlevel 1 (
        echo  [错误] Ollama 启动失败，原因见上方输出。
        pause
        exit /b 1
    )
) else (
    echo  [1/3] Ollama 已在运行（端口 11434）
)

rem ---------- 2/3 console service ----------
set "NEED_WAIT=0"
netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul 2>&1
if errorlevel 1 (
    echo  [2/3] 启动操作台服务（端口 %PORT%）...
    start "pipeline-server" /min cmd /c "python -m pipeline.server --port %PORT% --no-browser"
    set "NEED_WAIT=1"
) else (
    echo  [2/3] 操作台服务已在运行（端口 %PORT%）
)

rem ---------- 3/3 readiness ----------
rem NOTE: this section deliberately does NOT wrap powershell in a parenthesised
rem block. cmd also treats brackets inside quotes as block terminators, so an
rem index loop written with brackets truncated the whole block and the script
rem hung silently (reproduced 2026-09-24). Hence: one condition per line, and the
rem powershell one-liner below uses only curly braces, never brackets.
if not "!NEED_WAIT!"=="1" echo  [3/3] 跳过等待（服务本来就在跑）
if "!NEED_WAIT!"=="1" echo  [3/3] 等待服务就绪...
if "!NEED_WAIT!"=="1" ping -n 5 127.0.0.1 >nul 2>&1
if "!NEED_WAIT!"=="1" powershell -NoProfile -Command "try{$null=Invoke-RestMethod 'http://127.0.0.1:%PORT%/api/config' -TimeoutSec 3;exit 0}catch{exit 1}" >nul 2>&1
if "!NEED_WAIT!"=="1" if errorlevel 1 ping -n 6 127.0.0.1 >nul 2>&1
if "!NEED_WAIT!"=="1" if errorlevel 1 powershell -NoProfile -Command "try{$null=Invoke-RestMethod 'http://127.0.0.1:%PORT%/api/config' -TimeoutSec 3;exit 0}catch{exit 1}" >nul 2>&1
if "!NEED_WAIT!"=="1" if errorlevel 1 echo  [警告] 服务未就绪，请看最小化的 pipeline-server 窗口里的报错。
if "!NEED_WAIT!"=="1" if errorlevel 1 pause
if "!NEED_WAIT!"=="1" if errorlevel 1 exit /b 1
if "!NEED_WAIT!"=="1" echo        就绪。

echo.
if defined NOBROWSER (
    echo  操作台地址：%URL%
) else (
    echo  正在打开操作台：%URL%
    start "" "%URL%"
)
echo.
echo  提示：关闭最小化的 pipeline-server 窗口即停止服务。
echo        跑真机前建议先执行 python tools\preflight.py 确认显存与 prefill。
echo.
timeout /t 4 >nul 2>&1
if errorlevel 1 ping -n 5 127.0.0.1 >nul 2>&1
exit /b 0
