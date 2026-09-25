$base = "http://127.0.0.1:8787"
$req = Get-Content -Raw -Encoding UTF8 "D:\AI\line\tools\_repro\req.md"
$fb  = Get-Content -Raw -Encoding UTF8 "D:\AI\line\tools\_repro\feedback.md"
$repo = "E:\CODE\project\1.18.0 source code"

function Post-Json($uri, $obj) {
    $body = $obj | ConvertTo-Json -Depth 6 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    return Invoke-RestMethod -Uri $uri -Method Post -ContentType "application/json; charset=utf-8" -Body $bytes
}
function Get-State($id) {
    $d = Invoke-RestMethod -Uri "$base/api/runs/$id" -Method Get
    return $d
}

# 1) 启动：暂停在 PM 闸门
$r = Post-Json "$base/api/runs" @{requirement=$req; repo=$repo; pause_after=@("pm")}
$runId = $r.run_id
"STARTED run_id=$runId"

# 2) 等 PM 闸门
for ($i=0; $i -lt 120; $i++) {
    $st = Get-State $runId
    if ($st.state.status -eq 'paused' -or $st.state.status -eq 'done') { break }
    Start-Sleep -Seconds 10
}
"after-start status=$($st.state.status) cursor=$($st.state.cursor)"

# 3) 带人工事实续跑（注入 architect_assess，机制会扩散到所有阶段）
$r2 = Post-Json "$base/api/runs/$runId/resume" @{from="architect_assess"; feedback=$fb; issue_kind="human_directive"}
"RESUMED"

# 4) 等跑完（两遍 dev，给足 35 分钟）
for ($i=0; $i -lt 210; $i++) {
    $st = Get-State $runId
    $s = $st.state.status
    if ($s -eq 'done' -or $s -eq 'needs_human') { break }
    if ($i % 6 -eq 0) {
        $calls = ($st.state.calls | ForEach-Object { "$($_.stage)($($_.wall_s)s)" }) -join " "
        "  t=$([int]($i*10))s status=$s cursor=$($st.state.cursor) calls=[$calls]"
    }
    Start-Sleep -Seconds 10
}
$final = Get-State $runId
"FINAL status=$($final.state.status) cursor=$($final.state.cursor) verdict=$($final.summary.verdict) needs_human=$($final.state.needs_human)"
$final.state.calls | ForEach-Object { "  {0,-18} {1,7}s prefill={2,7} gen={3,6}" -f $_.stage, $_.wall_s, $_.prefill_tps, $_.gen_tps }
"run_id=$runId"
