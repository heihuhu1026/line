"""跑流水线之前的预检：三个 tag 是否真的全量在显存里、prefill 速度是否正常。

**为什么需要**：ollama 的 `/api/ps` 会报 `size_vram == size`（"100% GPU"），但当桌面/远程桌面/IDE/
浏览器把显存吃到 6GB+ 时，WDDM 会把模型换到共享内存，prefill 掉一个数量级，而**报表上看不出来**。
真机踩过：14B 从 157 t/s 掉到 17 t/s，一次评审要 7 分钟，跑完整条流水线才发现。
这个脚本 30 秒就能给出结论，并明确告诉你"是环境问题，不是模型的问题"。

用法:
    python tools/preflight.py            # 默认用 ~300 token 的探针 prompt
    python tools/preflight.py --tokens 800
    python tools/preflight.py --json
退出码：全部正常 0；有告警 1（可以直接用它做跑前门槛）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config import (  # noqa: E402
    BASELINE_PREFILL,
    DEFAULT_BASELINE_PREFILL,
    OLLAMA_HOST,
    STAGE_MODELS,
)

DEFAULT_BASELINE = DEFAULT_BASELINE_PREFILL


def post(path: str, payload: dict, host: str, timeout: int = 900) -> dict:
    req = urllib.request.Request(
        f"{host}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def get(path: str, host: str, timeout: int = 30) -> dict:
    with urllib.request.urlopen(f"{host}{path}", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def probe(tag: str, host: str, tokens: int, num_ctx: int = 8192) -> dict:
    prompt = ""
    while len(prompt) < tokens * 1.6:
        prompt += "预检探针占位文本。" * 8 + "\n"
    payload = {
        "model": tag,
        "messages": [{"role": "user", "content": prompt + "\n只回复 OK"}],
        "stream": False,
        "think": False,
        "options": {"num_ctx": num_ctx, "num_predict": 4},
    }
    t0 = time.time()
    data = post("/api/chat", payload, host)
    wall = time.time() - t0
    prompt_tokens = data.get("prompt_eval_count", 0)
    prompt_s = (data.get("prompt_eval_duration") or 1) / 1e9
    loaded = next((m for m in get("/api/ps", host).get("models", []) if m.get("name", "").startswith(tag)), {})
    size, vram = loaded.get("size") or 0, loaded.get("size_vram") or 0
    return {
        "tag": tag,
        "prefill_tps": round(prompt_tokens / prompt_s, 1) if prompt_s else None,
        "wall_s": round(wall, 1),
        "ctx": loaded.get("context_length"),
        "vram_ratio": round(vram / size, 3) if size else None,
        "vram_gb": round(vram / 1024**3, 2) if vram else None,
        "model_gb": round(size / 1024**3, 2) if size else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="流水线跑前预检（显存是否全量 + prefill 速度）")
    parser.add_argument("--host", default=OLLAMA_HOST, help=f"ollama 地址，默认 {OLLAMA_HOST}")
    parser.add_argument("--tokens", type=int, default=300, help="探针 prompt 的估算 token 数")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args()

    # tag -> 该档位实际请求用的 num_ctx（探针要用同一个值，否则测的不是线上配置）
    tags: dict[str, int] = {}
    for spec in STAGE_MODELS.values():
        tags.setdefault(spec.tag, spec.num_ctx)
    rows: list[dict] = []
    warnings: list[str] = []
    for tag, num_ctx in tags.items():
        # 单驻留：测下一个之前先卸掉当前（顺便验证卸载是否干净）
        for model in get("/api/ps", args.host).get("models", []):
            if not model.get("name", "").startswith(tag):
                post("/api/generate", {"model": model["name"], "keep_alive": 0}, args.host, timeout=120)
                time.sleep(1.5)
        try:
            row = probe(tag, args.host, args.tokens, num_ctx)
        except Exception as exc:  # noqa: BLE001
            row = {"tag": tag, "error": f"{type(exc).__name__}: {exc}"}
            warnings.append(f"{tag} 调用失败：{exc}")
            rows.append(row)
            continue
        rows.append(row)
        ratio = row.get("vram_ratio")
        base = BASELINE_PREFILL.get(tag, DEFAULT_BASELINE)
        if ratio is not None and ratio < 0.99:
            warnings.append(
                f"{tag} 只有 {ratio:.0%} 在显存（{row['vram_gb']}/{row['model_gb']}GB）"
                "：多半是桌面/远程桌面/IDE/浏览器占走了显存，跑起来会慢一个数量级"
            )
        if (row.get("prefill_tps") or 0) < base * 0.5:
            warnings.append(
                f"{tag} prefill {row['prefill_tps']} t/s，基线约 {base} t/s（低于一半）"
                "：先释放显存再跑，否则一次 14B 调用要 5~7 分钟"
            )
        post("/api/generate", {"model": tag, "keep_alive": 0}, args.host, timeout=120)

    if args.json:
        print(json.dumps({"rows": rows, "warnings": warnings}, ensure_ascii=False, indent=1))
        return 1 if warnings else 0

    print(f"{'tag':<26}{'ctx':>7}{'显存':>16}{'prefill':>12}{'耗时':>8}")
    print("-" * 72)
    for row in rows:
        if row.get("error"):
            print(f"{row['tag']:<26}  失败：{row['error']}")
            continue
        vram = f"{row['vram_gb']}/{row['model_gb']}GB ({row['vram_ratio']:.0%})" if row.get("vram_ratio") else "-"
        print(
            f"{row['tag']:<26}{str(row.get('ctx') or '-'):>7}{vram:>16}"
            f"{str(row.get('prefill_tps') or '-') + ' t/s':>12}{str(row.get('wall_s')) + 's':>8}"
        )
    print()
    if warnings:
        print("预检有告警（先解决再跑，否则会白等很久）：")
        for item in warnings:
            print(f"  - {item}")
        print()
        print("提示：关掉占用显存的程序（远程桌面/浏览器/IDE 的 GPU 加速）后重跑本脚本确认。")
        return 1
    print("预检通过：三个档位都全量在显存里，吞吐正常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
