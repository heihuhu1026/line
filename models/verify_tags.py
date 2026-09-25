"""校验流水线三个 tag 的运行时配置是否生效（上下文长度、是否全量 GPU、单驻留是否成立）。

用法:
    python models/verify_tags.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

HOST = "http://localhost:11434"
TAGS = [
    ("qwen3-8b-pm-16k", 16384),
    ("qwen2.5-coder-7b-dev-24k", 24576),
    ("qwen3-14b-arch-8k", 8192),
]


def post(path: str, payload: dict, timeout: int = 600) -> dict:
    req = urllib.request.Request(
        HOST + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get(path: str) -> dict:
    with urllib.request.urlopen(HOST + path, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def unload(tag: str) -> None:
    post("/api/generate", {"model": tag, "keep_alive": 0})
    time.sleep(2)


def main() -> int:
    failures = []
    for tag, want_ctx in TAGS:
        t0 = time.time()
        try:
            post(
                "/api/chat",
                {
                    "model": tag,
                    "messages": [{"role": "user", "content": "回复 OK"}],
                    "stream": False,
                    "think": False,
                    "options": {"num_predict": 4},
                },
                timeout=900,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {tag}: 调用失败 {exc}")
            failures.append(tag)
            continue
        load_s = round(time.time() - t0, 1)

        loaded = get("/api/ps").get("models", [])
        names = [m.get("name") for m in loaded]
        me = next((m for m in loaded if m.get("name", "").startswith(tag)), None)
        if me is None:
            print(f"[FAIL] {tag}: 加载后 /api/ps 未找到该模型, loaded={names}")
            failures.append(tag)
            continue

        ctx = me.get("context_length")
        processor = me.get("size_vram", 0), me.get("size", 0)
        vram_gb = round(processor[0] / 1024**3, 2)
        total_gb = round(processor[1] / 1024**3, 2)
        pct = round(processor[0] / processor[1] * 100, 1) if processor[1] else 0.0
        resident = len(loaded)

        ok = ctx == want_ctx and resident == 1
        flag = "OK  " if ok else "FAIL"
        print(
            f"[{flag}] {tag}: ctx={ctx}(want {want_ctx}) 驻留模型数={resident} "
            f"显存={vram_gb}/{total_gb}GB ({pct}%) load={load_s}s loaded={names}"
        )
        if not ok:
            failures.append(tag)

        unload(tag)
        left = get("/api/ps").get("models", [])

        if left:
            print(f"[FAIL] {tag}: 卸载后仍驻留 {[m.get('name') for m in left]}")
            failures.append(tag)
            for m in left:
                unload(m.get("name", ""))
        else:
            print(f"       卸载确认: /api/ps 已空 (单驻留成立)")

    print()
    if failures:
        print("失败项:", failures)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
