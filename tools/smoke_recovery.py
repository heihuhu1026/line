"""崩溃恢复（启动对账）的离线冒烟：`server._reconcile_stale_runs`。

背景（§23.3 第 1 条，反复踩到）：跑流水线的是**独立子进程**，进程被杀/崩了之后
`state.status` 仍是 `running`。前端 `runPhase` 只看 `state.status`，于是把它显示成
「运行中」—— 续跑按钮置灰、停止按钮可点却无进程可停，**UI 死路**。
服务启动期做一次对账，把这类运行降级为 `paused`，人才救得回来。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
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


def check_presence_and_budget() -> None:
    """在场计时的两个口径 + 单次请求超时的上界（真机 20260928-110402 的教训）。

    那次运行的页面显示"dev 已用 1900s"，实际 dev 只占其中几百秒 —— `started_at` 是**进程**
    启动时间，被当成阶段时长用，人据此判断"dev 卡死"并直接杀掉了运行。阶段时长才是判断
    "卡没卡"的依据，所以两个键必须分开，且**阶段变了要重置**。
    """
    print("== 在场计时：进程时长与阶段时长必须分开 ==")
    from pipeline import config, presence

    run_dir = Path(tempfile.mkdtemp())
    presence.write(run_dir, stage="dev", run_id="smoke")
    first = json.loads((run_dir / ".running.json").read_text(encoding="utf-8"))
    check("stage_started_at" in first and "started_at" in first,
          "在场标记同时写进程时长与阶段时长")
    time.sleep(1.05)
    presence.touch(run_dir, stage="dev")
    same = json.loads((run_dir / ".running.json").read_text(encoding="utf-8"))
    check(same["stage_started_at"] == first["stage_started_at"],
          "同阶段刷心跳**不重置**阶段计时（否则阶段时长永远是 0）",
          f"{first['stage_started_at']} vs {same['stage_started_at']}")
    time.sleep(1.05)
    presence.touch(run_dir, stage="test")
    moved = json.loads((run_dir / ".running.json").read_text(encoding="utf-8"))
    check(moved["stage_started_at"] > same["stage_started_at"],
          "换阶段**重置**阶段计时")
    check(moved["started_at"] == first["started_at"],
          "进程启动时间不随阶段变化（两个数各有各的用处）")

    print("== 单次请求超时上界 ==")
    check(config.REQUEST_TIMEOUT <= 900,
          "单次 HTTP 超时不高于 900s —— 1800s 意味着卡死可静默 30 分钟"
          "（调用期间不写产物/追踪/日志，心跳照常 ⇒ 无法区分'在生成'与'已卡死'）",
          str(config.REQUEST_TIMEOUT))
    check(config.REQUEST_TIMEOUT >= 300,
          "也不能收到低于 300s：本机最慢实测调用 ≈210s（11794 tok 的 test）", str(config.REQUEST_TIMEOUT))


def main() -> int:
    check_presence_and_budget()
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
