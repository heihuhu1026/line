"""真机开跑前自检（纯读 + 一个本地 HTTP 探测，**不启动任何东西、不改任何状态**）。

为什么要有它：真机一轮 30~60 分钟，最常见的"白跑"原因是环境而不是代码 ——
ollama 没起来（空 tag 列表）、`config.local.json` 被改成了非 qwen、目标仓库里还有
上一轮的残留文件（`--project-type new` 会把它们当存量代码）、或者单驻留下已有别的
运行在跑（新进程会被拒，而日志里只看得到一句 409）。

用法::

    python tools/check_preflight.py                       # 默认检查贪吃蛇场景
    python tools/check_preflight.py --req _req_snake.md --repo D:\\AI\\tcs
    python tools/check_preflight.py --no-scope-probe      # 跳过作用域回归探针
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import config, planir, presence, semantics  # noqa: E402

#: 贪吃蛇需求里"只允许这几个文件用 tkinter"的判定依据（作用域回归探针）
SNAKE_REQ = ROOT / "tools" / "_repro" / "requirement_snake_20260930.txt"
NEEDED_TAGS = ("qwen3-8b-pm-16k", "qwen3-14b-arch-8k", "qwen2.5-coder-7b-dev-24k")


def _ok(cond: bool) -> str:
    return "[OK]  " if cond else "[!!]  "


def check_scope(contract_path: pathlib.Path) -> bool:
    """作用域回归：`game_logic.py` 禁 tkinter，`ui.py` / `main.py` **必须**放行。

    真实需求就是这么写的（界面层要用 tkinter 画 Canvas）。若这里判负 ui.py，
    架构师怎么改都不对（改对的方式就是用 tkinter）⇒ 无限返工。
    """
    print("【1】硬约束作用域回归（game_logic.py 禁 tkinter，ui.py/main.py 放行）")
    if not contract_path.is_file():
        print("    跳过：找不到需求夹具", contract_path)
        return True
    contract = semantics.build_requirement_contract(
        contract_path.read_text(encoding="utf-8"), {}
    )
    scopes = {s["module"]: s["files"] for s in semantics.forbidden_module_scopes(contract)}
    print("    抽取到的禁止模块与作用域:", scopes)
    expected = {"tkinter": ["game_logic.py"], "pygame": [], "numpy": []}
    if scopes != expected:
        print(f"    {_ok(False)} 期望 {expected}")
        return False
    # 造一个"每个文件都用 tkinter"的探针方案，只有 game_logic.py 的文件该被判负
    probe = {"tasks": [
        {"id": f"T-{i:02d}", "target_files": [f], "symbols": [f.replace(".py", "")],
         "contracts": {"uses": ["tkinter.Canvas"]}}
        for i, f in enumerate(
            ["game_logic.py", "ui.py", "main.py", "game_logic_test.py", "ui_test.py"], 1
        )
    ]}
    hit = sorted({f["files"][0] for f in
                  planir.validate_architect_plan(probe, None, forbidden_modules=scopes)
                  if f["code"] == "PLAN_FORBIDDEN_DEPENDENCY"})
    print(f"    {_ok(hit == ['game_logic.py'])} 判负文件 = {hit}（只应有 game_logic.py）")
    return hit == ["game_logic.py"]


def check_running() -> bool:
    print("【2】运行在场状态（单驻留：有别的运行在跑，新进程会被拒）")
    live = presence.scan(ROOT / "runs")
    if live:
        print(f"    {_ok(False)} 在场运行: {live}")
        return False
    print("    [OK]  无在场运行")
    return True


def check_config() -> bool:
    print("【3】配置（模型 / 闸门）")
    ok = True
    if not all("qwen" in str(t).lower() for t in config.STAGE_MODELS.values()):
        print(f"    {_ok(False)} STAGE_MODELS 里出现非 qwen: {config.STAGE_MODELS}")
        ok = False
    else:
        print("    [OK]  全部阶段都是 qwen 本地模型")
    for stage, tag in sorted(config.STAGE_MODELS.items()):
        print(f"           {stage:<30} {tag}")
    # 闸门真源是 RUNTIME_FLAGS 里的**常量名**（不是同名小写键）
    for key, const in (getattr(config, "RUNTIME_FLAGS", {}) or {}).items():
        name = const if isinstance(const, str) else str(const)
        if hasattr(config, name):
            print(f"    {key:<26} = {getattr(config, name)}")
    local = ROOT / "pipeline" / "config.local.json"
    print(f"    本机覆盖文件 {local}:",
          json.dumps(json.loads(local.read_text(encoding="utf-8")), ensure_ascii=False)
          if local.is_file() else "（不存在，用代码默认值）")
    print("    提示：HUMAN_REVIEW_GATE=False ⇒ 评审通过就直接收尾、不等人；"
          "PM 未决项闸门是 Orchestrator 参数，PM 提出未决项时仍会停下等人。")
    return ok


def check_ollama() -> bool:
    print("【4】ollama 可达性与 tag（权重在 D:\\AI\\Models，须用 models\\start_ollama.ps1 启动）")
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=4) as resp:
            tags = [m["name"] for m in json.loads(resp.read().decode("utf-8")).get("models", [])]
    except Exception as exc:  # noqa: BLE001
        print(f"    {_ok(False)} 不可达（{type(exc).__name__}）→ 先跑 models\\start_ollama.ps1")
        return False
    print(f"    [OK]  可达，已加载 {len(tags)} 个模型")
    ok = True
    for want in NEEDED_TAGS:
        hit = [t for t in tags if t.startswith(want)]
        print(f"           {want:<26} {_ok(bool(hit))}")
        ok = ok and bool(hit)
    return ok


def check_inputs(req: pathlib.Path, repo: str) -> bool:
    print("【5】输入与目标目录")
    ok = True
    if req.is_file():
        print(f"    [OK]  需求文件 {req}")
    else:
        print(f"    {_ok(False)} 需求文件缺失: {req}")
        ok = False
    target = pathlib.Path(repo)
    if not target.exists():
        print(f"    [OK]  目标目录不存在（新建项目会自动创建）: {target}")
    else:
        items = [p for p in target.rglob("*") if p.is_file()]
        if items:
            print(f"    {_ok(False)} 目标目录有 {len(items)} 个存量文件 —— "
                  f"`--project-type new` 会把它们当存量代码，建议先清空: {target}")
            for p in items[:5]:
                print(f"           {p.relative_to(target)}")
            ok = False
        else:
            print(f"    [OK]  目标目录为空: {target}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="真机开跑前自检")
    ap.add_argument("--req", default="_req_snake.md", help="需求文件（默认 _req_snake.md）")
    ap.add_argument("--repo", default="D:\\AI\\tcs", help="目标仓库（新建项目用空目录）")
    ap.add_argument("--fixture", default=str(SNAKE_REQ), help="作用域回归用的需求夹具")
    ap.add_argument("--no-scope-probe", action="store_true", help="跳过作用域回归探针")
    args = ap.parse_args()

    results = [
        True if args.no_scope_probe else check_scope(pathlib.Path(args.fixture)),
        check_running(),
        check_config(),
        check_ollama(),
        check_inputs(pathlib.Path(args.req), args.repo),
    ]
    print()
    print("=" * 68)
    if all(results):
        print("自检通过 ⇒ 可以开跑：")
        print("  python -m pipeline.cli --requirement-file "
              f"{args.req} --project-type new --scale small "
              f"--repo {args.repo} --out runs --run-id snake-e2e-1")
        print("  （要先看页面就跑 python -m pipeline.server --port 8787 --no-browser；"
              "CLI 跑不需要重启服务）")
        return 0
    print("有未通过项 ⇒ **先别跑**（上面带 [!!] 的行就是原因）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
