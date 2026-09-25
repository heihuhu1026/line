$repo = "E:\CODE\project\1.18.0 source code"
$req  = "D:\AI\line\tools\_repro\req.md"
$fb   = "D:\AI\line\tools\_repro\feedback.md"
$rid  = "twopass5-20260923"
$rundir = "D:\AI\line\runs\$rid"
$env:PYTHONUTF8 = "1"

# 1) 启动：暂停在 PM 闸门
python -m pipeline.cli --requirement-file $req --repo $repo --pause-after pm --run-id $rid --out D:\AI\line\runs
"=== 启动结束，等待 pm 暂停 ==="

for ($i=0; $i -lt 60; $i++) {
    if (-not (Test-Path "$rundir\state.json")) { Start-Sleep -Seconds 5; continue }
    $st = Get-Content -Raw -Encoding UTF8 "$rundir\state.json" | ConvertFrom-Json
    if ($st.status -eq 'paused') { break }
    Start-Sleep -Seconds 5
}
"status after start = $($st.status) cursor = $($st.cursor)"

# 2) 注入人工反馈（用 python 可靠写 JSON，避免 PowerShell 版本差异）
python -c "
import json
p=r'$rundir\state.json'.replace(chr(92),'/')
s=json.load(open(p,encoding='utf-8'))
s.setdefault('human_feedback',{})['architect_assess']=[open(r'$fb'.replace(chr(92),'/'),encoding='utf-8').read()]
json.dump(s,open(p,'w',encoding='utf-8'),ensure_ascii=False,indent=2)
print('injected human_feedback keys:', list(s['human_feedback'].keys()))
"

# 3) 后台续跑：从 architect_assess 一路跑到 done（两遍 dev）
$p = Start-Process -FilePath python -ArgumentList "-m","pipeline.cli","--resume",$rid,"--from","architect_assess","--no-pause","--out","D:\AI\line\runs" `
    -WorkingDirectory D:\AI\line -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput "$rundir\_resume_out.log" -RedirectStandardError "$rundir\_resume_err.log"

# 4) 轮询
for ($i=0; $i -lt 240; $i++) {
    if (-not (Test-Path "$rundir\state.json")) { Start-Sleep -Seconds 10; continue }
    $st = Get-Content -Raw -Encoding UTF8 "$rundir\state.json" | ConvertFrom-Json
    $s = $st.status
    if ($s -eq 'done' -or $s -eq 'needs_human') { break }
    if ($i % 6 -eq 0) {
        $calls = ($st.calls | ForEach-Object { "$($_.stage)($($_.wall_s)s)" }) -join " "
        "  t=$($i*10)s status=$s cursor=$($st.cursor) calls=[$calls]"
    }
    Start-Sleep -Seconds 10
}
$final = Get-Content -Raw -Encoding UTF8 "$rundir\state.json" | ConvertFrom-Json
"FINAL status=$($final.status) cursor=$($final.cursor) verdict=$($final.summary.verdict) needs_human=$($final.needs_human)"
$final.calls | ForEach-Object { "  {0,-18} {1,7}s prefill={2,7} gen={3,6}" -f $_.stage, $_.wall_s, $_.prefill_tps, $_.gen_tps }
"run_id=$rid"
