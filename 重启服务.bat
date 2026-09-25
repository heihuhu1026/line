@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ============================================================
rem  line 流水线 · 重启本地服务
rem  做三件事：停掉占用端口的旧实例 -> 起一个新实例 -> 探活确认
rem  为什么要重启：后端是常驻进程，Python 在启动时就把模块加载完了，
rem  改了 pipeline/*.py 不重启不生效（前端 console.html 是每次请求现读，不必重启）。
rem ============================================================

set "PORT=8787"
set "URL=http://127.0.0.1:%PORT%"

echo.
echo === line 流水线 · 重启服务（端口 %PORT%）===
echo.

rem ---- 1) 有没有运行在跑？有就先提醒（重启会把它中断）
set "BUSY="
for /f "delims=" %%b in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "try{(Invoke-RestMethod -Uri '%URL%/api/runs' -TimeoutSec 4).busy_with}catch{}"') do set "BUSY=%%b"
if defined BUSY (
  echo [警告] 有运行正在进行中：%BUSY%
  echo        重启会中断它。子进程可能留在后台继续占显存，可在页面上点「中断」清掉。
  echo        按 Ctrl+C 取消；或
  pause
  echo.
)

rem ---- 2) 停掉占用端口的旧实例
set "OLDPID="
for /f "delims=" %%p in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "try{(Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue).OwningProcess}catch{}"') do set "OLDPID=%%p"
if defined OLDPID (
  echo [1/3] 停止旧实例 PID=%OLDPID%
  taskkill /f /pid %OLDPID% >nul 2>&1
  rem 用 ping 等 2 秒：timeout 在 stdin 被重定向时会直接报错退出（脚本被别的程序调用时就是这种情况）
  ping -n 3 127.0.0.1 >nul
) else (
  echo [1/3] 端口 %PORT% 上没有旧实例，直接启动
)

rem ---- 3) 起新实例（新窗口最小化；关掉那个窗口 = 停服务）
echo [2/3] 启动服务...
start "line-pipeline-server(%PORT%)" /min python -m pipeline.server --port %PORT% --no-browser

rem ---- 4) 探活（重试轮询：模型服务冷启动/机器忙时，起来可能要十几秒）
echo [3/3] 探活...
powershell -NoProfile -ExecutionPolicy Bypass -Command "for($i=0;$i -lt 15;$i++){try{$r=Invoke-RestMethod -Uri '%URL%/api/runs' -TimeoutSec 3; Write-Host ('[OK] 服务已就绪：运行数 ' + $r.runs.Count + '，busy=' + $r.busy_with); exit 0}catch{Start-Sleep -Seconds 1}}; Write-Host '[失败] 服务没起来 —— 看新开的那个窗口里的报错'; exit 1"
if errorlevel 1 (
  echo.
  echo 提示：常见原因是 python 不在 PATH、或依赖没装、或端口被别的程序占着。
  pause
)

echo.
echo 服务地址：%URL%
echo （改过 pipeline\*.py 后就要重跑这个脚本；只改 console.html 刷新页面即可）
echo.
endlocal
