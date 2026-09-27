"""崩溃恢复（启动对账）的离线冒烟：`server._reconcile_stale_runs`。

背景（§23.3 第 1 条，反复踩到）：跑流水线的是**独立子进程**，进程被杀/崩了之后
`state.status` 仍是 `running`。前端 `runPhase` 只看 `state.status`，于是把它显示成
「运行中」—— 续跑按钮置灰、停止按钮可点却无进程可停，**UI 死路**。
服务启动期做一次对账，把这类运行降级为 `paused`，人才救得回来。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import runstore, server  # noqa: E402

PASS = FAIL = 0


def check(cond: bool, name: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  <- {extra}" if extra else ""))


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        # ① 僵死运行（running 但无进程/无在场标记）⇒ 降级为 paused
        stale = root / "20260101-000000"
        stale.mkdir()
        runstore.write_state(stale, {"status": "running", "cursor": "dev", "attempt": 1})
        fixed = server._reconcile_stale_runs(root)
        st = runstore.read_state(stale)
        check(fixed == ["20260101-000000"], "僵死的 running 被识别出来", str(fixed))
        check(st.get("status") == "paused", "状态降级为 paused（续跑按钮才能亮）", str(st.get("status")))
        check(st.get("stale_recovered") is True, "留痕 stale_recovered（说明是启动对账改的）")
        # 产物不能被顺手动过：那是这一轮真实跑出来的证据
        check(st.get("cursor") == "dev" and st.get("attempt") == 1,
              "只改状态，不动 cursor/attempt 等产物信息")

        # ② 幂等：再对账一次不应重复改
        check(server._reconcile_stale_runs(root) == [], "重复对账幂等（不会反复改）")

        # ③ 其他状态不受影响
        for name, status in (("a", "done"), ("b", "paused"), ("c", "idle")):
            d = root / f"20260101-00000{name}"
            d.mkdir()
            runstore.write_state(d, {"status": status})
        server._reconcile_stale_runs(root)
        kept = all(
            runstore.read_state(root / f"20260101-00000{n}").get("status") == s
            for n, s in (("a", "done"), ("b", "paused"), ("c", "idle"))
        )
        check(kept, "done / paused / idle 不被误改")

        # ④ 隐藏目录与作业目录跳过
        hidden = root / ".tmp-run"
        hidden.mkdir()
        runstore.write_state(hidden, {"status": "running"})
        jobs = root / "_jobs"
        jobs.mkdir()
        check(server._reconcile_stale_runs(root) == [], "点开头的目录与 _jobs 被跳过")

        # ⑤ 不存在的目录不崩
        check(server._reconcile_stale_runs(root / "不存在") == [], "目录不存在时不崩")

    print(f"\n通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
