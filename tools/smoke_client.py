"""Ollama 客户端「截断 / 重复循环」处置口径的离线冒烟。

**为什么要有这一个**：真机 20260927-134222 里，一次 dev 调用的 7B 输出陷入了**重复循环**
（同一个对象反复往下写，`format=schema` 的语法并不阻止无限重复），于是：

    6144 →（撞顶）12288 →（又撞顶）20161 →（又撞顶）失败，白烧 ≈38k token ≈16 分钟

而且两次跑到这里共约 30 分钟 —— 全是纯白等，最后照样失败。抬高上限只对「内容合法、
只是写长了」有意义；对重复循环是**纯粹的放大**。

锁定两条口径：
  ① 普通截断：至多抬高 **1** 次上限（不再 6144→12288→20161 连抬两次）；
  ② 重复循环：**不抬高，反而压低**上限并明确叫停（换条件重试才有意义）；
  ③ 判定本身要保守：正常的长输出（每行都在描述不同东西）不许被判成重复。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config import STAGE_MODELS  # noqa: E402
from pipeline.ollama_client import OllamaClient, OllamaError, _looks_degenerate  # noqa: E402

PASS = FAIL = 0


def check(cond: bool, name: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  <- {extra}" if extra else ""))


def _truncated(content: str) -> dict:
    """一条「撞上限被截断」的假响应（done_reason=length）。"""
    return {
        "message": {"content": content, "thinking": ""},
        "done_reason": "length",
        "prompt_eval_count": 3000,
        "eval_count": 6144,
        "prompt_eval_duration": 10_000_000_000,
        "eval_duration": 150_000_000_000,
    }


def _client(replies: list[dict]) -> tuple[OllamaClient, list[dict]]:
    """造一个不连真服务的客户端：`_request` 换成按序返回假响应的桩。"""
    client = OllamaClient.__new__(OllamaClient)  # 不走 __init__：不检查/不连接服务
    client.host = "http://127.0.0.1:1"
    client.timeout = 5
    sent: list[dict] = []

    def fake(path: str, payload: dict | None, method: str = "POST", timeout: int | None = None) -> dict:  # noqa: ARG001
        sent.append(payload or {})
        return replies[min(len(sent) - 1, len(replies) - 1)]

    client._request = fake  # type: ignore[method-assign]
    return client, sent


def _limits(sent: list[dict]) -> list[int]:
    return [int((p.get("options") or {}).get("num_predict") or 0) for p in sent]


def _last_user(sent: list[dict]) -> str:
    if not sent:
        return ""
    return "".join(str(m.get("content") or "") for m in sent[-1].get("messages") or [])


def main() -> int:
    spec = STAGE_MODELS["dev"]
    print(f"dev 规格：num_ctx={spec.num_ctx} num_predict={spec.num_predict}")

    print("== ① 判定要保守：正常的长输出不算重复 ==")
    normal = "\n".join(f'"field_{i}": "value {i}",' for i in range(80))
    check(_looks_degenerate(normal) is False, "80 行各不相同的输出 ⇒ 不判重复")
    check(_looks_degenerate("") is False and _looks_degenerate("abc") is False,
          "空串 / 极短输出 ⇒ 不判重复")
    check(_looks_degenerate('{"a": 1}') is False, "短 JSON ⇒ 不判重复")

    print("== ② 判定要准：真重复要抓到 ==")
    check(_looks_degenerate("\n".join(['"same": "line",'] * 40)) is True,
          "同一行连续 40 次 ⇒ 判重复（连续行判据）")
    mostly_dup = "\n".join(['"x": 1,'] * 38 + [f'"y{i}": {i},' for i in range(2)])
    check(_looks_degenerate(mostly_dup) is True, "40 行里只有 2 行不同 ⇒ 判重复（占比判据）")
    check(_looks_degenerate("\n".join(['"x": 1,'] * 40 + ['"z": 9,'] * 3)) is True,
          "主体重复、尾部才有变化 ⇒ 仍判重复")
    # 真机 20260927-134222 的实际形态：`format=schema` 的产物**整段没有换行**，
    # 行判据只看得到 1 行 —— 必须靠字符窗口兜住，否则又会白等一次 12k token。
    one_line_loop = '"insertion": {"label": "score", "target": "score_list"}, ' * 60
    check("\n" not in one_line_loop.strip(), "（用例前提：这段确实没有换行）")
    check(_looks_degenerate(one_line_loop) is True,
          "单行（无换行）的重复 JSON ⇒ 也能判出（周期性判据）")
    long_distinct = "{" + ", ".join(f'"k{i}": "v{i}"' for i in range(300)) + "}"
    check("\n" not in long_distinct, "（用例前提：这段也没有换行）")
    check(_looks_degenerate(long_distinct) is False,
          "单行但字段各不相同（正常的宽 schema 产物）⇒ 不误判")

    # 真机 20260927-150931 崩溃现场的**原文尾部**：`segment_segment_segment_…` 一路复述，
    # 中间偶尔夹 `_direction` / `, ` —— 属于"脏重复"，周期性判据的"尾两轮必须完全相等"
    # 预筛被它漏掉（那次的后果是白等到进程崩溃）。
    dirty_loop = ("segment_" * 3 + "segment_segment_direction, " + "segment_" * 9 + ", ") * 40
    check(_looks_degenerate(dirty_loop) is True,
          "脏重复（单元间夹着零散差异，周期性预筛会漏）⇒ n-gram 支配判据抓到")
    code_like = "\n".join(
        f"    def method_{i}(self, value):\n        return value + {i}\n" for i in range(30)
    )
    check(_looks_degenerate(code_like) is False, "正常的类方法集合（30 个各不相同）⇒ 不误判")

    print("== ③ 普通截断：至多抬高 1 次上限，不再连抬 ==")
    client, sent = _client([_truncated(normal)])
    try:
        client.chat_json(spec, "sys", "user", {"type": "object"}, attempts=3)
        check(False, "三次都截断应当报错", "没有抛错")
    except OllamaError as exc:
        check("已抬高上限" in str(exc), "报错里说明「抬高已到顶」（不再默默再抬）", str(exc)[:90])
    check(_limits(sent) == [spec.num_predict, spec.num_predict * 2],
          "上限序列 = 原值 → ×2 一次（不再 6144→12288→20161）", str(_limits(sent)))
    check(len(sent) == 2, "一共只发 2 次请求就止损（此前 3 次）", str(len(sent)))

    print("== ④ 重复循环：不抬高，反而压低上限 + 点名 ==")
    loop = "\n".join(['"same": "line",'] * 40)
    client, sent = _client([_truncated(loop)])
    try:
        client.chat_json(spec, "sys", "user", {"type": "object"}, attempts=3)
        check(False, "重复三次也应当报错", "没有抛错")
    except OllamaError:
        pass
    check(_limits(sent) == [spec.num_predict, spec.num_predict // 2, spec.num_predict // 4],
          "上限序列 = 原值 → ÷2 → ÷4（越重复越短）", str(_limits(sent)))
    check("重复循环" in _last_user(sent),
          "重试时明确告诉模型「你在重复，只输出最小合法 JSON」", _last_user(sent)[-160:])
    check(max(_limits(sent)) <= spec.num_predict,
          "整段过程一次都没抬高上限（不再放大白等）", str(_limits(sent)))

    print("== ⑤ 第一次就正常返回：不受影响 ==")
    ok = {"message": {"content": '{"ok": true}'}, "done_reason": "stop",
          "prompt_eval_count": 100, "eval_count": 5, "prompt_eval_duration": 1e9, "eval_duration": 1e9}
    client, sent = _client([ok])
    data, meta = client.chat_json(spec, "sys", "user", {"type": "object", "properties": {"ok": {"type": "boolean"}}})
    check(data == {"ok": True} and len(sent) == 1, "一次成功、不重试", str(meta.get("done_reason")))
    check(int((sent[0].get("options") or {}).get("num_predict")) == spec.num_predict,
          "首次请求用的就是该阶段的 num_predict", str(_limits(sent)))

    print(f"\n通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
