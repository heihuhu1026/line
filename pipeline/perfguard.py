"""prefill 退化护栏：ollama 在**按请求**退化时，提前探一下，别白等十几分钟。

**实测（2026-09-27，本机 AMD RX 6700 + Vulkan 后端，依据 ollama 的 server-1.log 里 633 条
`prompt processing` 计时行）：**
  · 健康：300 token 探针 300~500 t/s；真实评审 prompt（5226 tok）74~80 t/s；
    日志里更长的上下文按 n=1536→507、n=3072→255、n=5120→163、n=7680→114 t/s 正常衰减。
  · 退化：**同一条曲线整体除以 12~30** —— n=1536 只有 39.9 t/s，n=5120 只有 5.5 t/s。
  · **约 17~25% 的请求命中退化态**；退化在**请求之间翻转**，而且**会自行恢复**
    （日志里 5.5 t/s 的下一条请求就是 503 t/s）。所以**不需要重启服务** —— 这一点修正了
    `models/start_ollama.ps1` 注释里「重启才恢复」的说法（重启当然也有效，但不是必须）。
  · 全量 GPU（`load_tensors: offloaded 41/41 layers to GPU`）、无 OOM、无告警 ⇒ 这是**静默**的
    per-request 现象，所以 `ollama ps` 的 `size_vram == size`（"100% GPU"）以及依赖它的
    `gpu_partial_offload` 判据（`vram_ratio < 0.99`）**都抓不到它**。

**为什么「等待+重探」有效 —— 由状态转移实测支撑**（把 server-1.log 里 119 个请求按首行速率
打标签为 健康/退化，再统计转移）：

    P(退化|上一请求退化) = 0.33    P(健康|上一请求退化) = 0.67
    P(退化|上一请求健康) = 0.22    P(健康|上一请求健康) = 0.78    （无条件 P(退化) = 0.25）
    退化连续段长度：最长 3，绝大多数为 **1**；健康连续段：最长 30

即：退化**几乎不会连续超过 1~2 个请求**，所以重探一次就有 2/3 概率落到健康；
连试 3 次仍退化约 3.6%。因此退避**不必很长** —— 本模块用递增退避（3s/6s/12s）就够，
把预算花在"多探一次"而不是"等更久"上。

**14B 比 7B/8B 更容易命中退化**（`tools/perf_report.py` 按阶段统计的实测慢调用占比：
review 82%（>5k 桶）/ 43%，architect_plan 42%，而 dev 0%、test 19%）——
所以这道护栏对 14B 阶段（plan / skeleton / review）最值钱。

**为什么值得**：一次退化的 14B 调用要多花 ~1000s（评审实测 1092s，健康只要 ~70s），
而探针健康时只花 ~1s、退化时 ~40s。用 1s 买一个 ~1000s 的侥幸，账很划算。

**刻意不做的事**：不擅自重启 ollama。它会自行恢复，而重启会杀掉同一台机器上别人的会话；
真要重启，人按提示手动执行（消息里带上命令）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from typing import Any, Callable

from .config import BASELINE_PREFILL, DEFAULT_BASELINE_PREFILL, OLLAMA_HOST

#: 探针 prompt 的估算 token 数。取 300：足够让 prefill 进入稳态，又小到健康时只花 ~1s。
GUARD_TOKENS = 300
#: 探到退化后「等待 → 重探」的次数与递增退避（秒）。
#: 退避刻意短：实测退化**最长只连续 3 个请求、绝大多数为 1**（见模块头部的转移矩阵），
#: 所以「多探一次」比「等更久」有效得多 —— 探针本身在退化时也要 ~60s（300 token @ 5 t/s），
#: 已经是一段天然的等待，不必再叠加长时间 sleep。
GUARD_TRIES = 4
GUARD_WAITS_S = (3.0, 6.0, 12.0)
#: 判「退化」的阈值系数（相对该 tag 的基线）。0.5 与 tools/preflight.py 的口径一致。
DEGRADED_RATIO = 0.5
#: 探针请求的超时：退化时 300 token 约 40s，给足余量但不至于挂死。
PROBE_TIMEOUT_S = 300


def baseline_for(tag: str) -> float:
    """该 tag 的 prefill 健康基线（t/s）。未登记的用通用基线。"""
    return float(BASELINE_PREFILL.get(tag, DEFAULT_BASELINE_PREFILL))


def _post(host: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8") or "{}")


def probe_prefill(
    tag: str,
    num_ctx: int,
    *,
    tokens: int = GUARD_TOKENS,
    host: str = OLLAMA_HOST,
    timeout: int = PROBE_TIMEOUT_S,
) -> float | None:
    """探一次 prefill 速率（t/s）；探不动返回 None。

    **num_ctx 必须与线上调用一致**：ollama 的上下文长度变了会**重新装载模型**，
    那样探针不但白测，还会把刚卸好的驻留再翻一遍（单驻留下这一步很贵）。
    """
    prompt = ""
    while len(prompt) < tokens * 1.6:
        prompt += "预检探针占位文本。" * 8 + "\n"
    payload = {
        "model": tag,
        "messages": [{"role": "user", "content": prompt + "\n只回复 OK"}],
        "stream": False,
        # 所有 tag 都下发 False：7B 那档**不支持 think**，下发 true 会直接 HTTP 400。
        "think": False,
        "options": {"num_ctx": num_ctx, "num_predict": 4},
    }
    try:
        data = _post(host, payload, timeout)
    except Exception:  # noqa: BLE001
        return None
    n = int(data.get("prompt_eval_count") or 0)
    sec = float(data.get("prompt_eval_duration") or 0) / 1e9
    if not n or sec <= 0:
        return None
    return n / sec


def guard(
    tag: str,
    num_ctx: int,
    *,
    log: Callable[[str], None] | None = None,
    host: str = OLLAMA_HOST,
    tokens: int = GUARD_TOKENS,
    tries: int = GUARD_TRIES,
    waits: tuple[float, ...] = GUARD_WAITS_S,
) -> dict[str, Any]:
    """调用**前**的 prefill 护栏：探到退化就等一会儿重探，仍退化则告警放行。

    返回 ``{"checked": bool, "tps": float|None, "baseline": float, "degraded": bool,
    "waited_s": float}``，调用方可以落进 state 供离线分析（`issues.json` 只在暂停/结束时
    落盘，所以运行期的问题必须另有载体）。
    """
    emit = log or (lambda _m: None)
    base = baseline_for(tag)
    t0 = time.time()
    tps: float | None = None
    waited = 0.0
    for attempt in range(1, max(1, tries) + 1):
        tps = probe_prefill(tag, num_ctx, tokens=tokens, host=host)
        if tps is None:
            # 探针本身失败（服务没起来 / 超时）：**不拦**，让正常调用去暴露真正的错误。
            emit(f"        [prefill 护栏] {tag} 探针无结果，跳过（不拦调用）")
            return {"checked": False, "tps": None, "baseline": base, "degraded": False,
                    "waited_s": round(time.time() - t0, 1)}
        if tps >= base * DEGRADED_RATIO:
            if attempt > 1:
                emit(f"        [prefill 护栏] 等待后恢复：{tps:.0f} t/s（基线 {base:.0f}）")
            return {"checked": True, "tps": round(tps, 1), "baseline": base, "degraded": False,
                    "waited_s": round(time.time() - t0, 1)}
        if attempt < tries:
            nap = waits[min(attempt - 1, len(waits) - 1)] if waits else 0.0
            emit(
                f"        [prefill 护栏] {tag} 探针只有 {tps:.0f} t/s（基线 {base:.0f}）"
                f"→ 等 {nap:.0f}s 重探（第 {attempt}/{tries} 次）"
            )
            if nap:
                time.sleep(nap)
                waited += nap
    emit(
        f"        [prefill 护栏] {tag} 连续 {tries} 次探针仍只有 {tps:.0f} t/s"
        f"（基线 {base:.0f}，约 1/{max(1.0, base / max(tps or 1, 1)):.0f}）→ 仍继续调用，"
        "但这轮大概率会白等很久。实测这是 ollama 的**按请求**退化（约 1/6 请求命中、会自行恢复），"
        r"若持续不恢复：powershell -NoProfile -ExecutionPolicy Bypass -File models\start_ollama.ps1"
    )
    return {"checked": True, "tps": round(tps or 0, 1), "baseline": base, "degraded": True,
            "waited_s": round(time.time() - t0, 1)}


def main() -> int:  # pragma: no cover - 手工探针入口
    """命令行快速探一下某个 tag 的 prefill（默认三个档位全探）。"""
    from .config import STAGE_MODELS

    tags: dict[str, int] = {}
    for spec in STAGE_MODELS.values():
        tags.setdefault(spec.tag, spec.num_ctx)
    worst = 0
    for tag, ctx in tags.items():
        tps = probe_prefill(tag, ctx)
        base = baseline_for(tag)
        flag = "OK" if (tps or 0) >= base * DEGRADED_RATIO else "DEGRADED"
        print(f"  {tag:<28} ctx={ctx:<6} {('%.1f' % tps) if tps else '-':>8} t/s  "
              f"(基线 {base:.0f})  {flag}")
        if flag != "OK":
            worst = 1
    return worst


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
