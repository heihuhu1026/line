"""Ollama 客户端「截断 / 重复循环」处置口径的离线冒烟。

**为什么要有这一个**：真机 20260927-134222 里，一次 dev 调用的 7B 输出陷入了**重复循环**
（同一个对象反复往下写，`format=schema` 的语法并不阻止无限重复），于是：

    6144 →（撞顶）12288 →（又撞顶）20161 →（又撞顶）失败，白烧 ≈38k token ≈16 分钟

而且两次跑到这里共约 30 分钟 —— 全是纯白等，最后照样失败。抬高上限只对「内容合法、
只是写长了」有意义；对重复循环是**纯粹的放大**。

锁定三条口径（**2026-09-28 校准**：原先的"普通截断只准抬 1 次"过紧了）：
  ① 普通截断（内容合法、只是写长）：抬高上限，最多 `_MAX_ESCALATIONS` 次，
     且被**上下文余量**夹住 —— 真机 `20260928-000351` 的 `num_ctx=24576`、prompt 才 4016，
     余量约 20k，却因为"只准抬一次"在 12288 就放弃：**限制根本不是上下文**；
  ② 重复循环：先**压低**上限并明确叫停（换条件重试才有意义），但**只压一次** ——
     压下去还写不完，说明它不是"在重复"而是"这张图本来就长"，那一次要走抬高路线
     （真机 `20260928-000351` 报错里留下的 `num_predict=1536` 就是一路压到 6144//4 的后果）；
  ③ 判定本身要保守：正常的长输出（每行都在描述不同东西）不许被判成重复。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dataclasses import replace

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


def _last_user(sent: list[dict], index: int = -1) -> str:
    """第 `index` 次请求的完整 user 文本（默认最后一次）。

    带下标是必要的：退化路径的提示**只在被判定重复的那一次**重试里出现，之后会切到
    抬高路线（提示语随之改变）。只断言"最后一次"会把这条机制测成空转。
    """
    if not sent:
        return ""
    return "".join(str(m.get("content") or "") for m in sent[index].get("messages") or [])


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

    print("== ③ 普通截断（非 dev 角色）：抬高上限（上限 3 次），但抬不出上下文余量 ==")
    # dev 角色另有「先压短、不加码」口径（见 ⑨）；这里用架构师角色钉住长输出角色的加码语义。
    arch_spec = replace(spec, role="架构师", tag="smoke-arch", num_ctx=16384, num_predict=6144)
    client, sent = _client([_truncated(normal)])
    try:
        client.chat_json(arch_spec, "sys", "user", {"type": "object"}, attempts=3)
        check(False, "三次都截断应当报错", "没有抛错")
    except OllamaError as exc:
        check("已抬高上限" in str(exc), "报错里说明「抬高已到顶」（不再默默再抬）", str(exc)[:90])
    limits = _limits(sent)
    check(limits[:2] == [6144, 6144 * 2] and len(limits) == 3,
          "上限序列 = 原值 → ×2 → 余量上界（抬高上限；非 dev 角色行为不变）", str(limits))
    check(all(limits[i] < limits[i + 1] for i in range(len(limits) - 1))
          and max(limits) <= arch_spec.num_ctx,
          "抬高只增不减，且**抬不出 num_ctx**（余量才是硬上界）", str(limits))
    check(len(sent) == 3, "每次都用新上限重试（不做同上限白试）", str(len(sent)))

    print("== ④ 重复循环：先压低上限 + 点名，但**只压一次** ==")
    loop = "\n".join(['"same": "line",'] * 40)
    client, sent = _client([_truncated(loop)])
    try:
        client.chat_json(spec, "sys", "user", {"type": "object"}, attempts=3)
        check(False, "重复三次也应当报错", "没有抛错")
    except OllamaError:
        pass
    limits = _limits(sent)
    check(limits[0] == spec.num_predict and limits[1] == spec.num_predict // 2,
          "第一步把上限压一半（逼它写短，而不是给更多空间重复）", str(limits))
    check(limits[2] > limits[1],
          "**只压一次**：压下去还写不完 ⇒ 说明不是重复而是本来就长，改走抬高路线", str(limits))
    check(min(limits) == spec.num_predict // 2 and len(sent) == 3,
          "不会一路压到下限（真机 20260928-000351：越压越写不完）", str(limits))
    check("重复" in _last_user(sent, 1),
          "**被判定重复的那一次**重试里点名「你在重复，只输出最小合法 JSON」",
          _last_user(sent, 1)[-160:])

    print("== ⑤ 第一次就正常返回：不受影响 ==")
    ok = {"message": {"content": '{"ok": true}'}, "done_reason": "stop",
          "prompt_eval_count": 100, "eval_count": 5, "prompt_eval_duration": 1e9, "eval_duration": 1e9}
    client, sent = _client([ok])
    data, meta = client.chat_json(spec, "sys", "user", {"type": "object", "properties": {"ok": {"type": "boolean"}}})
    check(data == {"ok": True} and len(sent) == 1, "一次成功、不重试", str(meta.get("done_reason")))
    check(int((sent[0].get("options") or {}).get("num_predict")) == spec.num_predict,
          "首次请求用的就是该阶段的 num_predict", str(_limits(sent)))

    print("== ⑥ 被丢弃的尝试必须即时进账（真机 20260928-110402：连发 6 次全丢弃、13 分钟无痕）==")
    bad = {"message": {"content": '{"oops": '}, "done_reason": "stop",
           "prompt_eval_count": 2000, "eval_count": 800,
           "prompt_eval_duration": 4_000_000_000, "eval_duration": 20_000_000_000}
    client, sent = _client([bad])
    seen: list[dict] = []
    try:
        client.chat_json(spec, "sys", "user", {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                         attempts=3, on_attempt=seen.append)
        check(False, "三次都不合契约应当报错", "没有抛错")
    except OllamaError:
        pass
    check(len(seen) == 3, "**每一次**被丢弃的尝试都上报（不是整次调用只留一句异常）", str(len(seen)))
    check(all(r.get("attempt_failed") for r in seen), "每条都带 attempt_failed 标记")
    check([r.get("attempt") for r in seen] == [1, 2, 3],
          "带尝试序号 ⇒ 「这轮到底发了几次」数得出来", str([r.get("attempt") for r in seen]))
    check(bool(seen[-1].get("gave_up")) and seen[-1].get("attempts_used") == 3
          and seen[-1].get("wall_total_s") is not None,
          "最后一条是「放弃」，带整次调用的总耗时 ⇒ 「这一轮为什么慢」有账可查",
          str({k: seen[-1].get(k) for k in ("gave_up", "attempts_used", "wall_total_s")}))
    check(all(r.get("wall_s") is not None and r.get("schema_errors") for r in seen),
          "每条都带耗时与契约错误（少了这些就等于没记）")
    check(seen[0].get("prefill_tps") == 500.0 and seen[0].get("output_tokens") == 800,
          "带当次吞吐与输出 token ⇒ 能分辨「退化烧掉的」还是「内容不合格」",
          str((seen[0].get("prefill_tps"), seen[0].get("output_tokens"))))
    check(seen[0].get("raw_tail"), "带原文尾部 ⇒ 事后能看出模型写成了什么样")

    print("== ⑦ 成功路径不重复上报（成功那次由编排器统一记录）==")
    client, sent = _client([ok])
    seen_ok: list[dict] = []
    client.chat_json(spec, "sys", "user", {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                     on_attempt=seen_ok.append)
    check(not seen_ok, "成功不触发 on_attempt（否则同一次调用会记两遍，账就重了）", str(len(seen_ok)))

    print("== ⑧ HTTP 500「token repeat limit」：降载重试一次 + 失败必留痕 ==")
    # 真机 run 20260928-180933 的 dev 轮：服务端主动中止（prediction aborted, token
    # repeat limit reached），原链路既不重试也不留痕（traces / llm-calls 都是成功后才落），
    # 异常直接冒泡暂停整轮，事后连请求规模都复盘不了。
    repeat_500 = OllamaError("POST /api/chat -> HTTP 500: prediction aborted, token repeat limit reached")
    other_500 = OllamaError("POST /api/chat -> HTTP 500: internal inference error")

    def _scripted(script: list) -> tuple[OllamaClient, list[dict]]:
        c = OllamaClient.__new__(OllamaClient)
        c.host = "http://127.0.0.1:1"
        c.timeout = 5
        sent2: list[dict] = []

        def fake(path, payload, method="POST", timeout=None):  # noqa: ARG001
            sent2.append(payload or {})
            item = script[min(len(sent2) - 1, len(script) - 1)]
            if isinstance(item, Exception):
                raise item
            return item

        c._request = fake  # type: ignore[method-assign]
        return c, sent2

    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    seen_r: list[dict] = []
    client, sent = _scripted([repeat_500, ok])
    data, _meta = client.chat_json(spec, "sys", "user", schema,
                                   on_attempt=seen_r.append, log=lambda m: None)
    check(data == {"ok": True}, "500 中止后降载重试成功", str(data))
    check(len(sent) == 2, "只多发一次（降载名额 1 个）", str(len(sent)))
    opts0, opts1 = (sent[0].get("options") or {}), (sent[1].get("options") or {})
    check(opts0.get("repeat_penalty") is None, "首次请求不画蛇添足（默认采样）", str(opts0))
    check(float(opts1.get("repeat_penalty") or 0) == 1.15
          and abs(float(opts1.get("temperature")) - min(spec.temperature + 0.15, 1.0)) < 1e-9,
          "重试改采样：repeat_penalty=1.15、temperature +0.15（打散重复循环）", str(opts1))
    check(len(seen_r) == 1 and seen_r[0].get("done_reason") == "http_500_repeat_abort"
          and "token repeat limit" in str(seen_r[0].get("http_error") or "")
          and seen_r[0].get("prompt_est_tokens"),
          "失败的那次 HTTP 尝试即时上账（错误原文 + 请求规模都在，不再零留痕）",
          str({k: seen_r[0].get(k) for k in ("done_reason", "http_error", "prompt_est_tokens")}) if seen_r else "无记录")

    seen_o: list[dict] = []
    client, sent = _scripted([other_500])
    try:
        client.chat_json(spec, "sys", "user", schema, on_attempt=seen_o.append)
        check(False, "非 repeat 的 500 应当抛出", "没有抛错")
    except OllamaError as exc:
        check("internal inference error" in str(exc), "其他 500 原样上抛（不猜着重试）", str(exc)[:80])
    check(len(sent) == 1, "非 repeat 失败不重试（只有改条件才有意义）", str(len(sent)))
    check(bool(seen_o) and seen_o[0].get("done_reason") == "http_error",
          "非 repeat 的 HTTP 失败同样留痕", str(seen_o and seen_o[0].get("done_reason")))

    seen_t: list[dict] = []
    client, sent = _scripted([repeat_500, repeat_500])
    try:
        client.chat_json(spec, "sys", "user", schema, on_attempt=seen_t.append)
        check(False, "降载后仍 500 应当抛出", "没有抛错")
    except OllamaError as exc:
        check("token repeat limit" in str(exc), "降载后仍中止 ⇒ 上抛交由上层暂停", str(exc)[:80])
    check(len(sent) == 2, "降载**只给一次**（不无限烧算力）", str(len(sent)))
    check(len(seen_t) == 2 and all(r.get("done_reason") == "http_500_repeat_abort" for r in seen_t),
          "两次中止各留一条痕", str([r.get("done_reason") for r in seen_t]))

    print("== ⑨ dev 角色截断：先压短，恢复不超过基础上限，再截断就放弃（不加码 12288） ==")
    # 真机 run 20260928-200631：分图后单任务补丁正常只有几百~一两千 token，撞满 6144
    # 就是整份重写/发散。旧口径一路加码 12288，满额 319s 仍是断 JSON，五轮白烧。
    client, sent = _client([_truncated(normal)])
    try:
        client.chat_json(spec, "sys", "user", {"type": "object"}, attempts=3)
        check(False, "dev 三次都截断应当报错", "没有抛错")
    except OllamaError as exc:
        check("基础上限" in str(exc) and "停止加码" in str(exc),
              "dev 报错说明「基础上限内仍截断、已停止加码」", str(exc)[:110])
    limits = _limits(sent)
    check(limits == [spec.num_predict, spec.num_predict // 2, spec.num_predict],
          "序列 = 6144 → 压到 3072 → 最多回到 6144（绝不升到 12288）", str(limits))
    check("单个任务" in _last_user(sent, 1) and "3072" in _last_user(sent, 1),
          "压短那次明确反馈「单任务写过多、不要整份重写」", _last_user(sent, 1)[-180:])

    print(f"\n通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
