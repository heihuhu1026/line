"""Test Compiler 离线冒烟（规格§二十九–三十一）：

A. 机械场景：syntax / import 从规划文件确定性生成
B. 可信源命令：白名单/危险片段安全筛，非法命令只登记不执行
C. 覆盖：required PO 必须有 executable scenario，否则 coverage_gap
D. 弱证据：行为类场景仅 rc=0 无断言 ⇒ weak_evidence，按缺口处理
E. 空 automated_commands ⇒ automated_commands_empty=True（语义=UNPROVEN）
F. 身份稳定：同输入 scenario id 稳定；target_po 显式归档；无主命令只归 delivery
G. §12.5：contract / interface_freeze / materialization 由编译器机械生成（就地检查器，
   无 shell 命令）；命令不能作为它们的证据 ⇒ UNBOUND
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import ontology as O  # noqa: E402
from pipeline import testcompiler as TC  # noqa: E402

PASS = FAIL = 0


def check(cond: bool, name: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  <- {extra}" if extra else ""))


def _po(po_id: str, kind: str, *, required: bool = True, claim: str = "") -> dict:
    return {"id": po_id, "kind": kind, "required": required,
            "claim": claim or po_id, "name": claim[:40] or po_id}


def _by_target(result: dict) -> dict[str, dict]:
    return {s["target_po"]: s for s in result["scenarios"]}


def main() -> int:
    # ---- A：syntax / import 机械场景 ----
    print("== A. syntax / import 场景由规划文件机械生成 ==")
    pos = [
        _po("po:syn", O.PO_KIND_SYNTAX),
        _po("po:imp", O.PO_KIND_IMPORT),
        _po("po:beh", O.PO_KIND_BEHAVIOR, claim="运行命令后写入一条流水"),
    ]
    res = TC.compile_scenarios(
        obligations=pos,
        files=["src/store.py", "src/cli.py", "README.md"],
    )
    by = _by_target(res)
    syn = by["po:syn"]
    imp = by["po:imp"]
    check(syn["status"] == TC.STATUS_EXECUTABLE and syn["automated_commands"]
          and "py_compile" in syn["automated_commands"][0]
          and "src/store.py" in syn["automated_commands"][0],
          "syntax PO 获得 py_compile 可执行场景（命令由模块机械生成）")
    check(imp["status"] == TC.STATUS_EXECUTABLE
          and "src.store" in imp["automated_commands"][0]
          and "IMPORT_CHECK" in imp["automated_commands"][0]
          and any(a["source"] == "mechanical:import_check" for a in imp["actions"]),
          "import PO 获得带 stdout 标记的 import 检查（.py→点分模块，非 .py 被忽略）")
    check(all(a["assertions"] == ["exit_code==0"] for a in syn["actions"]),
          "syntax 场景断言恰为 exit_code==0（语法通过不证明行为）")
    check(any("stdout_contains:IMPORT_CHECK OK" in a["assertions"] for a in imp["actions"]),
          "import 场景不止 rc：还要求 stdout 标记（裸导入不算证据）")
    check("po:beh" in res["coverage_gap"] and "po:syn" not in res["coverage_gap"],
          "无命令的行为 PO 落 coverage_gap；已覆盖的 syntax PO 不落")

    # ---- B：安全筛 ----
    print("== B. 可信源命令仍过白名单 + 危险片段筛 ==")
    pos2 = [_po("po:beh2", O.PO_KIND_BEHAVIOR), _po("po:del", O.PO_KIND_DELIVERY)]
    res2 = TC.compile_scenarios(
        obligations=pos2,
        files=["main.py"],
        planned_commands=[
            {"command": "python main.py add 10 午餐", "source": "api_digest:entry",
             "target_po": "po:beh2", "assertions": ["stdout_contains:OK"]},
            {"command": "python main.py", "source": "skeleton:entry"},
            {"command": "rm -rf runs", "source": "api_digest:entry"},
            {"command": "evil-binary --x", "source": "api_digest:entry"},
            {"command": "python main.py | sh", "source": "api_digest:entry"},
        ],
    )
    by2 = _by_target(res2)
    check(by2["po:beh2"]["status"] == TC.STATUS_EXECUTABLE
          and by2["po:beh2"]["automated_commands"] == ["python main.py add 10 午餐"],
          "显式 target_po 的命令确定性归档到对应 PO")
    check(by2["po:del"]["status"] == TC.STATUS_WEAK
          and by2["po:del"]["automated_commands"] == ["python main.py"]
          and "po:del" in res2["coverage_gap"]
          and by2["po:del"]["id"] in res2["weak_evidence"],
          "无 target_po 的入口命令只归 delivery PO；裸 rc=0 仍判 weak（不冒充行为证据）")
    unsafe = {u["command"] for u in res2["unsafe_commands"]}
    check({"rm -rf runs", "evil-binary --x", "python main.py | sh"} <= unsafe
          and len(res2["unsafe_commands"]) == 3,
          "危险片段/非白名单程序/管道一律只登记不执行（3 条）")
    check(res2["automated_commands_empty"] is False,
          "存在安全命令时 automated_commands_empty=False")

    # ---- C/D：弱证据 + 覆盖 ----
    print("== C/D. 仅 rc=0 无断言 = weak_evidence；required PO 全覆盖判定 ==")
    res3 = TC.compile_scenarios(
        obligations=[_po("po:b1", O.PO_KIND_BEHAVIOR), _po("po:b2", O.PO_KIND_BEHAVIOR)],
        files=["a.py"],
        planned_commands=[
            {"command": "python a.py", "target_po": "po:b1"},
            {"command": "python a.py --check", "target_po": "po:b2",
             "assertions": ["stdout_regex:^(ok|done)$"]},
        ],
    )
    by3 = _by_target(res3)
    check(by3["po:b1"]["status"] == TC.STATUS_WEAK and by3["po:b1"]["id"] in res3["weak_evidence"]
          and "po:b1" in res3["coverage_gap"],
          "行为 PO 只有 rc=0 ⇒ weak_evidence 且计入 coverage_gap（假绿）")
    check(by3["po:b2"]["status"] == TC.STATUS_EXECUTABLE and "po:b2" not in res3["coverage_gap"],
          "带正则行为断言的场景 executable 且覆盖该 PO")

    # 负向断言：期望非零退出是有效行为断言
    res3b = TC.compile_scenarios(
        obligations=[_po("po:n", O.PO_KIND_BEHAVIOR, claim="缺参退出码 2")],
        planned_commands=[{"command": "python a.py", "target_po": "po:n",
                           "expect_exit": 2, "assertions": ["exit_code!=0"]}],
    )
    check(_by_target(res3b)["po:n"]["status"] == TC.STATUS_EXECUTABLE,
          "exit_code!=0 负向断言是强证据（不是弱证据）")

    # ---- E：空 automated_commands ----
    print("== E. 空 automated_commands 语义 = UNPROVEN ==")
    res4 = TC.compile_scenarios(
        obligations=[_po("po:x", O.PO_KIND_BEHAVIOR)],
        planned_commands=[],
    )
    check(res4["automated_commands_empty"] is True
          and res4["coverage_gap"] == ["po:x"]
          and _by_target(res4)["po:x"]["status"] == TC.STATUS_UNPROVEN,
          "一条安全命令都没有：空标志 + 全行为 PO 缺口 + 场景 UNPROVEN")
    # 非 required 的 PO 不进缺口
    res4b = TC.compile_scenarios(obligations=[_po("po:opt", O.PO_KIND_BEHAVIOR, required=False)])
    check(res4b["coverage_gap"] == [] and res4b["scenarios"] == [],
          "非 required PO 不产生场景也不产生缺口")

    # ---- F：身份稳定 / 无主命令无 delivery 时不猜 ----
    print("== F. scenario id 稳定 + 无主命令不猜 ==")
    r_a = TC.compile_scenarios(
        obligations=[_po("po:f", O.PO_KIND_BEHAVIOR)],
        planned_commands=[{"command": "python f.py", "target_po": "po:f",
                           "assertions": ["stdout_contains:yes"]}],
    )
    r_b = TC.compile_scenarios(
        obligations=[_po("po:f", O.PO_KIND_BEHAVIOR)],
        planned_commands=[{"command": "python f.py", "target_po": "po:f",
                           "assertions": ["stdout_contains:yes"]}],
    )
    check(_by_target(r_a)["po:f"]["id"] == _by_target(r_b)["po:f"]["id"]
          and _by_target(r_a)["po:f"]["id"].startswith("tscn:"),
          "同输入 scenario id 稳定（tscn: 前缀）")
    r_c = TC.compile_scenarios(
        obligations=[_po("po:g", O.PO_KIND_BEHAVIOR)],
        planned_commands=[{"command": "python orphan.py", "source": "api_digest:entry"}],
    )
    check(r_c["unclaimed_commands"] == ["python orphan.py"]
          and r_c["coverage_gap"] == ["po:g"]
          and _by_target(r_c)["po:g"]["status"] == TC.STATUS_UNPROVEN,
          "无 delivery PO 时无主命令不猜归属：登记 unclaimed，目标 PO 仍 UNPROVEN")
    # 命令顺序变化不影响 id（命令集合同），断言变化则 id 变
    r_d = TC.compile_scenarios(
        obligations=[_po("po:f", O.PO_KIND_BEHAVIOR)],
        planned_commands=[{"command": "python f.py", "target_po": "po:f",
                           "assertions": ["stdout_contains:NO"]}],
    )
    check(_by_target(r_d)["po:f"]["id"] != _by_target(r_a)["po:f"]["id"],
          "断言口径变化 ⇒ scenario 身份变化（测试不是同一个证明）")

    # ---- G：§12.5 就地机械检查器场景（contract / interface_freeze / materialization）----
    print("== G. contract / interface_freeze / materialization 由编译器机械生成 ==")
    resg = TC.compile_scenarios(
        obligations=[
            _po("po:ctr", O.PO_KIND_CONTRACT),
            _po("po:ifz", O.PO_KIND_INTERFACE_FREEZE),
            _po("po:mat", O.PO_KIND_MATERIALIZATION),
            _po("po:behg", O.PO_KIND_BEHAVIOR),
        ],
        files=["main.py"],
        planned_commands=[
            # LLM 声称能证明 contract PO 的命令：命令不是它的证据 ⇒ UNBOUND（不静默丢）
            {"command": "python main.py", "target_po": "po:ctr",
             "assertions": ["stdout_contains:OK"]},
        ],
    )
    byg = _by_target(resg)
    check(byg["po:ctr"]["status"] == TC.STATUS_MECHANICAL
          and byg["po:ctr"]["mechanical_check"] == ["contract_check"]
          and byg["po:ctr"]["automated_commands"] == [],
          "contract PO：编译器生成 mechanical 场景（无 shell 命令，不进执行器）")
    check(byg["po:ifz"]["status"] == TC.STATUS_MECHANICAL
          and byg["po:ifz"]["mechanical_check"] == ["contract_check", "skeleton_conformance"],
          "interface_freeze PO：双就地检查器（contract_check + skeleton_conformance）")
    check(byg["po:mat"]["status"] == TC.STATUS_MECHANICAL
          and byg["po:mat"]["mechanical_check"] == ["patch_apply"],
          "materialization PO：patch_apply 就地检查器")
    check(all(p not in resg["coverage_gap"] for p in ("po:ctr", "po:ifz", "po:mat"))
          and "po:behg" in resg["coverage_gap"],
          "三类机械 PO 不再落 coverage_gap；无命令的行为 PO 仍落缺口")
    check(len(resg["unbound_commands"]) == 1
          and resg["unbound_commands"][0]["candidate"] == "po:ctr"
          and "就地机械检查器" in resg["unbound_commands"][0]["reason"],
          "指向机械 PO 的命令记为 UNBOUND（具名原因，不静默丢弃也不塞给别人）")
    check(byg["po:ctr"]["id"] == TC.compile_scenarios(
        obligations=[_po("po:ctr", O.PO_KIND_CONTRACT)], files=["main.py"],
    )["scenarios"][0]["id"],
          "mechanical 场景 id 稳定（同 PO 同检查器 ⇒ 同身份）")
    gate_g = TC.proof_coverage_gate(
        [_po("po:ctr", O.PO_KIND_CONTRACT), _po("po:behg", O.PO_KIND_BEHAVIOR)],
        resg,
    )
    check(gate_g["covered"] == 1 and "po:ctr" in gate_g["covered_ids"]
          and gate_g["unexecutable"] == 1 and "po:behg" in gate_g["unexecutable_ids"],
          "Proof Gate：机械场景计入 covered；无命令行为 PO 计 unexecutable（不伪装成 PASS）")

    # ---- H：P0-13 验证方式机械分类
    print("== H. 行为项 → 验证方式机械分类（mechanical / unit / gui_smoke / resident / human） ==")
    check(TC.classify_obligation(_po("po:s", O.PO_KIND_SYNTAX)) == TC.MODE_MECHANICAL
          and TC.classify_obligation(_po("po:c", O.PO_KIND_CONTRACT)) == TC.MODE_MECHANICAL,
          "机械 verifier 类 PO 归 mechanical")
    check(TC.classify_obligation(_po("po:g", O.PO_KIND_BEHAVIOR, claim="窗口渲染蛇身颜色"))
          == TC.MODE_GUI_SMOKE,
          "图形界面字面证据归 gui_smoke（本沙箱只能冒烟）")
    check(TC.classify_obligation(_po("po:r", O.PO_KIND_BEHAVIOR, claim="游戏主循环持续运行"))
          == TC.MODE_RESIDENT,
          "常驻进程字面证据归 resident（只能短超时跑起来）")
    check(TC.classify_obligation(_po("po:h", O.PO_KIND_BEHAVIOR, claim="配色美观需人工确认"))
          == TC.MODE_HUMAN,
          "明确的主观/人工口径归 human_only")
    check(TC.classify_obligation(_po("po:b", O.PO_KIND_BEHAVIOR, claim="build guide 生成文档"))
          == TC.MODE_UNIT,
          "两字母 token 不误伤（build / guide 不该被判成界面义务）")
    res_h = TC.compile_scenarios(
        obligations=[_po("po:g", O.PO_KIND_BEHAVIOR, claim="窗口渲染蛇身颜色"),
                     _po("po:u", O.PO_KIND_BEHAVIOR, claim="吃到食物得分 +10")],
        files=["main.py"],
    )
    check(res_h["external_required"] == ["po:g"]
          and res_h["verification_modes"].get(TC.MODE_GUI_SMOKE) == ["po:g"],
          "机械不可验的义务单列 external_required（不与\"忘了测\"混在一起）")
    gate_h = TC.proof_coverage_gate(
        [_po("po:g", O.PO_KIND_BEHAVIOR, claim="窗口渲染蛇身颜色")], res_h)
    check(gate_h["external_required"] == ["po:g"] and gate_h["covered"] == 0,
          "Proof Gate 单列 external_required，但仍不把 GUI 义务当成 PASS")

    print()
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAIL:
        print("TestCompiler smoke 未通过")
        return 1
    print("TestCompiler smoke 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
