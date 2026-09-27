"""补丁「累积套用」的离线冒烟 —— ``patches.analyze_all`` / ``apply_all``。

**为什么必须有这一个**：新建项目的文件是由**同一份 edits 里的 `add`** 创建出来的，
而 `modify` 一度固定去**仓库快照**里找原文（仓库是空的）→ 判 ``unchecked`` →
`apply_all` 只收 ``status == "ok"`` 的行 → **返工轮的修复被整批静默丢弃**。
后果不是"少写一点"，而是**永远修不上**：verify 在未修复的代码上报同一个错 →
评审 rework_dev → 再生成同一个修复 → 再丢。

真机 20260927-123032 的原文：5 条 `add` + 5 条 `modify`，5 条 modify 全被丢弃，
其中一条正是把 `from tkinter import event` 改成 `Event as event` 的修复 ——
而 verify 照旧报 `ImportError: cannot import name 'event' from 'tkinter'`，8 轮全 fail。

锁定的语义：
  ① 同批 add 新建出的文件上，modify 必须能**核对**（ok）且能**套上**
  ② 同一文件多条补丁按 edits 顺序套用，每条在**当前**内容上重新定位
  ③ 定位不到 / 有歧义 ⇒ **跳过并说明原因**，绝不猜位置（贴错比不贴危险）
  ④ 幂等重放（闸门预览 → 正式交付）不重复追加
  ⑤ 真 orphan（本批没有 add、仓库里也没有）仍判未核对 —— 不能把"确实套不上"也放过
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import patches  # noqa: E402

PASS = FAIL = 0


def check(cond: bool, name: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  <- {extra}" if extra else ""))


def add(path: str, patch: str) -> dict:
    return {"path": path, "change_type": "add", "patch": patch}


def mod(path: str, anchor: str, patch: str, symbol: str = "",
        mode: str = "replace_span") -> dict:
    return {"path": path, "change_type": "modify", "patch_mode": mode, "anchor": anchor,
            "target_symbol": symbol, "patch": patch}


def reasons(report: dict) -> str:
    return " | ".join(str((s or {}).get("reason") or "") for s in report.get("skipped") or [])


# 与真机 123032 同形：入口文件里把 tkinter 的事件类名写成了小写 `event`（幻觉导入），
# 而**同批**的 modify 就是修它的那条补丁。
MAIN_ADD = (
    "import tkinter\n"
    "from tkinter import event\n"
    "\n"
    "def run():\n"
    "    return event\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    run()\n"
)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="smoke-apply-") as tmp:
        root = Path(tmp)
        empty_repo = root / "empty-repo"        # 新建项目：仓库是空的
        empty_repo.mkdir()

        print("== ① 新建项目：同批 add 建出的文件，modify 必须能核、能套 ==")
        impl = {"edits": [
            add("main.py", MAIN_ADD),
            mod("main.py", "from tkinter import event", "from tkinter import Event as event"),
        ]}
        audit = patches.analyze_all(str(empty_repo), impl)
        r_add, r_mod = audit["edits"][0], audit["edits"][1]
        check(r_add["status"] == "ok" and r_add.get("patch_mode_used") == "new_file",
              "add 判为「新增文件·整份写入」", str(r_add.get("status")))
        check(r_mod["status"] == "ok",
              "同批 add 新建的文件上，modify 判 ok（不再「未核对」）",
              f'{r_mod.get("status")} {r_mod.get("notes")}')
        out = root / "out1"
        report = patches.apply_all(str(empty_repo), impl, audit, in_place=False, out_dir=str(out))
        text = (out / "main.py").read_text(encoding="utf-8")
        check("from tkinter import Event as event" in text,
              "修复真的落进产物（这就是 verify 能转绿的前提）", text.splitlines()[:2])
        check("目标文件不存在" not in reasons(report),
              "不再出现「目标文件不存在」式的静默丢弃", reasons(report))

        print("== ② 真机形态：一条 modify 定位不到 ⇒ 必须**可见**，不许静默 ==")
        impl2 = {"edits": [
            add("main.py", MAIN_ADD),
            # anchor 在 add 内容里根本不存在（真机 123032 的 modify #5 就是这样）
            mod("main.py", "def main():", "def main():\n    run()\n"),
            # 真正的修复：改名 + 补出缺失的入口函数
            mod("main.py", "from tkinter import event", "from tkinter import Event as event"),
            mod("main.py", "if __name__ == '__main__':",
                "def main():\n    run()\n\n\nif __name__ == '__main__':"),
        ]}
        audit2 = patches.analyze_all(str(empty_repo), impl2)
        statuses = [r["status"] for r in audit2["edits"]]
        check(statuses[1] == "anchor_not_found",
              "anchor 对不上 ⇒ analyze 就判出来（不拖到 verify）", str(statuses))
        check(statuses[2] == "ok" and statuses[3] == "ok",
              "其余两条修复判 ok", str(statuses))
        out2 = root / "out2"
        rep2 = patches.apply_all(str(empty_repo), impl2, audit2, in_place=False, out_dir=str(out2))
        text2 = (out2 / "main.py").read_text(encoding="utf-8")
        check("from tkinter import Event as event" in text2, "改名修复落地")
        check("def main():" in text2, "缺失的入口函数被补上")
        check(text2.count("def main():") == 1, "没有出现两份 main()（幂等/去重生效）",
              str(text2.count("def main():")))
        check("无法确定该贴哪儿" in reasons(rep2) or "anchor" in reasons(rep2),
              "定位不到的那条**出现在 skipped 里并说明原因**", reasons(rep2))
        check("目标文件不存在" not in reasons(rep2), "仍不出现「目标文件不存在」", reasons(rep2))

        print("== ③ 同一文件多条补丁：按 edits 顺序套用（行号会变，靠重新定位） ==")
        three = "def a():\n    return 1\n\n\ndef b():\n    return 2\n\n\ndef c():\n    return 3\n"
        impl3 = {"edits": [
            add("m.py", three),
            mod("m.py", "def a():\n    return 1", "def a2():\n    return 11", mode="insert_after"),
            mod("m.py", "def b():\n    return 2", "def b2():\n    return 22", mode="insert_after"),
            mod("m.py", "def c():\n    return 3", "def c2():\n    return 33", mode="insert_after"),
        ]}
        audit3 = patches.analyze_all(str(empty_repo), impl3)
        out3 = root / "out3"
        rep3 = patches.apply_all(str(empty_repo), impl3, audit3, in_place=False, out_dir=str(out3))
        text3 = (out3 / "m.py").read_text(encoding="utf-8")
        check(all(f"def {n}():" in text3 for n in ("a2", "b2", "c2")),
              "三条插入全部落地（前一条改动不影响后一条定位）", text3)
        check([r["status"] for r in audit3["edits"]] == ["ok"] * 4, "四条全判 ok",
              str([r["status"] for r in audit3["edits"]]))

        print("== ④ 幂等重放（预览 → 正式交付）：不重复追加 ==")
        repo4 = root / "repo4"
        repo4.mkdir()
        (repo4 / "cli.py").write_text("def go():\n    return 1\n", encoding="utf-8")
        impl4 = {"edits": [mod("cli.py", "    return 1", "    return 1\n    # 加一行")]}
        audit4 = patches.analyze_all(str(repo4), impl4)
        rep4a = patches.apply_all(str(repo4), impl4, audit4, in_place=True)
        after1 = (repo4 / "cli.py").read_text(encoding="utf-8")
        rep4b = patches.apply_all(str(repo4), impl4, audit4, in_place=True)
        after2 = (repo4 / "cli.py").read_text(encoding="utf-8")
        check(after1 == after2, "第二次套用内容不变（幂等）", f"{after1!r} vs {after2!r}")
        check(all(patches.is_benign_skip(s) for s in rep4b["skipped"] if s.get("status")),
              "第二次的跳过都带 already_applied（良性，不判交付残缺）", str(rep4b["skipped"]))
        check(len([ln for ln in after2.splitlines() if "加一行" in ln]) == 1,
              "改动只有一份，没有追加第二遍", after2)

        print("== ⑤ 真 orphan：本批没有 add、仓库里也没有 ⇒ 仍判未核对 ==")
        impl5 = {"edits": [mod("ghost.py", "x = 1", "x = 2")]}
        audit5 = patches.analyze_all(str(empty_repo), impl5)
        check(audit5["edits"][0]["status"] == "unchecked",
              "没有原文可核 ⇒ unchecked（不能因为修 ① 就把这条放过）",
              str(audit5["edits"][0]["status"]))
        out5 = root / "out5"
        rep5 = patches.apply_all(str(empty_repo), impl5, audit5, in_place=False, out_dir=str(out5))
        check(rep5["files"] == [] and any(
            not patches.is_benign_skip(s) for s in rep5["skipped"]),
            "一条也没套上，且如实报告（不是假成功）", str(rep5))

        print("== ⑥ 二开项目不回归：仓库里已有文件照常套用 ==")
        repo6 = root / "repo6"
        repo6.mkdir()
        (repo6 / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        impl6 = {"edits": [mod("a.py", "    return 1", "    return 2")]}
        audit6 = patches.analyze_all(str(repo6), impl6)
        out6 = root / "out6"
        rep6 = patches.apply_all(str(repo6), impl6, audit6, in_place=False, out_dir=str(out6))
        check([r["status"] for r in audit6["edits"]] == ["ok"], "存量文件上的 modify 照常 ok")
        check("return 2" in (out6 / "a.py").read_text(encoding="utf-8"),
              "改动写进 out_dir，原仓库不动",
              (repo6 / "a.py").read_text(encoding="utf-8"))
        check("return 1" in (repo6 / "a.py").read_text(encoding="utf-8"),
              "原仓库保持原样（非 in_place 绝不写回）")

        print("== ⑦ 多段 add 指向同一新文件 ⇒ 合并写入，不互相覆盖 ==")
        impl7 = {"edits": [
            add("game.py", "class Snake:\n    pass\n"),
            add("game.py", "class Food:\n    pass\n"),
        ]}
        audit7 = patches.analyze_all(str(empty_repo), impl7)
        out7 = root / "out7"
        patches.apply_all(str(empty_repo), impl7, audit7, in_place=False, out_dir=str(out7))
        text7 = (out7 / "game.py").read_text(encoding="utf-8")
        check("class Snake:" in text7 and "class Food:" in text7,
              "两段 add 的内容都在（没有后写覆盖先写）", text7)

        print("== ⑨ 多段 add：物化**写出去的那一份**（合并结果）也要过内容校验 ==")
        # 合并结果才是真正写盘的内容，而内容校验以前只看每一条 add 各自的正文。
        # 这条断言锁两件事：① 正常的多块合并**不许**被误报；② 单块写残照旧判出
        # （不许因为加了合并校验就把既有能力盖掉）。
        impl9 = {"edits": [add("m2.py", "class A:\n    pass\n"),
                           add("m2.py", "class B:\n    pass\n")]}
        audit9 = patches.analyze_all(str(empty_repo), impl9)
        check([r["status"] for r in audit9["edits"]] == ["ok", "ok"],
              "多块各自正常 ⇒ 不误报", str([r["status"] for r in audit9["edits"]]))
        impl10 = {"edits": [add("m3.py", "def f(:\n    pass\n")]}
        audit10 = patches.analyze_all(str(empty_repo), impl10)
        check(audit10["edits"][0]["status"] == "new_file_syntax_error",
              "单块写残仍判出（既有能力不许退化）", str(audit10["edits"][0]["status"]))

        print("== ⑧ 定位函数：歧义 / 找不到一律返回 None（不猜位置） ==")
        locate = patches._locate_span
        lines8 = ["x = 1", "", "x = 1", ""]
        row = {"patch_mode_used": "replace_span", "symbol": ""}
        check(locate(lines8, {"anchor": "x = 1"}, row) is None,
              "anchor 不唯一 ⇒ 不猜（返回 None）")
        check(locate(lines8, {"anchor": "y = 9"}, row) is None,
              "anchor 找不到 ⇒ 不猜（返回 None）")
        check(locate(["a = 1", "b = 2"], {"anchor": "b = 2"}, row) == (1, 1),
              "唯一命中 ⇒ 给出 0-based 区间", str(locate(["a = 1", "b = 2"], {"anchor": "b = 2"}, row)))

    print(f"\n通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
