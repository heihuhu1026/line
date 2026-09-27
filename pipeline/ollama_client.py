"""Ollama 客户端：结构化输出（format=schema）+ 契约校验重试 + 严格单驻留调度。

- chat_json：服务端用 JSON Schema 强约束输出，客户端再校验一次；失败则带错误反馈重试。
  输出撞 ``num_predict`` 被**截断**时单独识别并抬高上限重试 —— 见 chat_json 内的注释。
- ensure_exclusive：任何一次调用前，把非目标模型从显存卸载（10GB 卡的硬约束，决策 3）。
- MockClient：不触碰真实模型，按 schema 合成占位产物，用于跑通编排与回流逻辑。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any

from .budget import estimate_tokens
from .config import ModelSpec
from .schemas import ASSESSMENT, GLOBAL_ARCHITECTURE, validate


class OllamaError(RuntimeError):
    pass


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

#: ollama 在「撞到 num_predict 上限、输出被切断」时把 done_reason 置为该值。
#: 与之相对的是 "stop"（模型自己写完了）。这个区别很关键：截断是**确定性失败**，
#: 同 prompt 同上限重试必然在同一处再断，而「模型写错 JSON」重试才有意义。
_DONE_REASON_LENGTH = "length"

#: 抬高输出上限时给 num_ctx 留的余量（token）。prompt 已占掉一部分，上限只能取剩下的再减它 ——
#: 不留余量的话请求会顶到 ctx，ollama 会按 ctx 静默截断 prompt，症状比截断输出更难查。
_CTX_SAFETY_MARGIN = 256


def _parse_json(content: str) -> Any:
    text = _FENCE_RE.sub("", content.strip())
    return json.loads(text)


class OllamaClient:
    def __init__(self, host: str, timeout: int = 1800) -> None:
        self.host = host.rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------------ 底层
    def _request(self, path: str, payload: dict | None, method: str = "POST", timeout: int | None = None) -> dict:
        url = self.host + path
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:  # 把服务端 body 带出来，便于定位
            body = exc.read().decode("utf-8", "replace")
            raise OllamaError(f"{method} {path} -> HTTP {exc.code}: {body[:500]}") from exc
        except Exception as exc:  # noqa: BLE001
            raise OllamaError(f"{method} {path} 失败: {exc}") from exc

    def ps(self) -> list[dict]:
        return self._request("/api/ps", None, method="GET", timeout=30).get("models", [])

    def unload(self, tag: str, wait: float = 1.5) -> None:
        self._request("/api/generate", {"model": tag, "keep_alive": 0}, timeout=120)
        time.sleep(wait)

    # -------------------------------------------------------- 单驻留调度（决策 3）
    def ensure_exclusive(self, tag: str) -> dict:
        """保证显存里只有 tag 这一个模型，返回本步骤的调度信息（供埋点）。"""
        loaded = self.ps()
        names = [m.get("name", "") for m in loaded]
        others = [n for n in names if n and not n.startswith(tag)]
        for name in others:
            self.unload(name)
        already = any(n.startswith(tag) for n in names)
        return {
            "resident_before": names,
            "unloaded": others,
            "switched": bool(others),
            "kept_warm": already,
        }

    # ------------------------------------------------------------------ 结构化调用
    def chat_json(
        self,
        spec: ModelSpec,
        system: str,
        user: str,
        schema: dict,
        num_predict: int | None = None,
        # 3 次而不是 2 次：真机 run snake-detailed（2026-09-26）里 test 阶段**两次**都写出
        # 畸形 JSON（`Unterminated string starting at char 475`），直接抛错把整个运行干掉 ——
        # 而续跑后第 2 次就成功了，说明单次成功率大概只有五成上下。
        # 两次尝试下「整轮跑崩」的概率约 25%，三次降到约 12%。
        # 代价只有在真失败时才付（多一次 ~60s 的调用），而崩一次的代价是丢掉整轮
        # 十几分钟的算力 + 全部中间态。这笔账明显划算。
        attempts: int = 3,
    ) -> tuple[Any, dict]:
        base_user = user
        last_errors: list[str] = []
        failed: list[dict] = []  # 未通过契约的那些原始输出也要留档（诊断提示词问题时最关键）
        # 本次实际使用的输出上限。撞到它被截断时会**调高**再试 —— 同上限重试是确定性白费。
        limit = num_predict or spec.num_predict
        for attempt in range(1, attempts + 1):
            prompt = base_user
            if attempt > 1:
                prompt = (
                    base_user
                    + "\n\n【上次输出不合契约，必须修正】\n"
                    + "\n".join(f"- {e}" for e in last_errors[:12])
                    + "\n请只输出符合 schema 的 JSON，不要解释、不要 markdown 代码块。"
                )
            payload: dict[str, Any] = {
                "model": spec.tag,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "format": schema,
                "options": {
                    "num_ctx": spec.num_ctx,
                    "temperature": spec.temperature,
                    "num_predict": limit,
                },
            }
            if spec.think is not None:
                payload["think"] = spec.think

            t0 = time.time()
            resp = self._request("/api/chat", payload)
            wall = time.time() - t0

            message = resp.get("message", {}) or {}
            content = message.get("content", "") or ""
            thinking = message.get("thinking", "") or ""
            # 「为什么停了」：stop = 模型自己写完了 / length = 撞 num_predict 被切断。
            # 以前完全没看这个字段 —— 截断与「模型写坏 JSON」在日志里长得一模一样，
            # 排查方向会直接跑偏（真机 2026-09-26 就踩了）。
            done_reason = str(resp.get("done_reason") or "")
            prompt_tokens = int(resp.get("prompt_eval_count") or 0)

            meta = {
                "tag": spec.tag,
                "role": spec.role,
                "num_ctx": spec.num_ctx,
                "think": spec.think,
                "attempt": attempt,
                "wall_s": round(wall, 2),
                "load_s": round(resp.get("load_duration", 0) / 1e9, 2),
                "prompt_tokens": prompt_tokens,
                "output_tokens": resp.get("eval_count", 0),
                "prompt_s": round(resp.get("prompt_eval_duration", 0) / 1e9, 2),
                "eval_s": round(resp.get("eval_duration", 0) / 1e9, 2),
                "thinking_chars": len(thinking),
                "prompt_est_tokens": estimate_tokens(prompt),
                "done_reason": done_reason,
                "num_predict": limit,
            }

            if done_reason == _DONE_REASON_LENGTH:
                # ---- 输出被截断：确定性失败，必须换条件重试 ----
                # 真机教训（2026-09-26 run snake-detailed）：test 阶段的 7B 输出被砍在字符串中间，
                # 两次尝试都报「Unterminated string starting at ...」—— 同一 prompt、同一上限
                # 必然在同一处再断，于是两次重试全废，**整个运行直接崩掉、连产物都没留下**。
                # 所以这里不能当普通契约失败处理，要抬高上限再试。
                detail = (
                    f"输出被截断：撞 num_predict={limit} 上限，JSON 在中间被切断"
                    "（解析必然失败，不是模型写错了内容）"
                )
                meta["schema_errors"] = [detail]
                failed.append({"attempt": attempt, "errors": [detail], "raw": content[-3000:]})
                last_errors = [detail]
                room = spec.num_ctx - prompt_tokens - _CTX_SAFETY_MARGIN
                raised = min(max(limit * 2, limit + 2048), room)
                if raised <= limit:
                    raise OllamaError(
                        f"{spec.tag} 输出被截断，且没有上下文余量可抬高上限："
                        f"prompt 已占 {prompt_tokens} tok / num_ctx={spec.num_ctx} / "
                        f"输出上限 {limit} tok（可用余量仅 {room} tok）。"
                        "该阶段的产物对当前 num_ctx 来说太长了 —— 请调大该阶段的 num_ctx，"
                        "或让产物更短（拆分任务、减少条目）。"
                    )
                limit = raised
                continue

            try:
                data = _parse_json(content)
            except Exception as exc:  # noqa: BLE001
                last_errors = [f"输出不是合法 JSON: {exc}"]
                meta["schema_errors"] = last_errors
                failed.append({"attempt": attempt, "errors": last_errors, "raw": content[:4000]})
                continue

            errors = validate(data, schema)
            meta["schema_errors"] = errors
            if not errors:
                # 下划线开头的键是「记录用素材」，编排器会取走写进 traces.jsonl，不进 llm-calls.jsonl
                meta["_raw_text"] = content
                meta["_raw_thinking"] = thinking
                meta["_failed_attempts"] = failed
                return data, meta
            last_errors = errors
            failed.append({"attempt": attempt, "errors": errors, "raw": content[:4000]})

        # 报错时把**最后一次的原始输出**也带出来。以前只留 error 文本，
        # 「模型到底写成了什么样、是截断还是畸形」只能靠猜 —— 真机 2026-09-26 崩在
        # test 阶段时就卡在这上面：日志只有一句「Unterminated string starting at char 475」，
        # 既看不到原文，也分不清是撞上限还是模型真写坏了。
        tail = str((failed[-1].get("raw") if failed else "") or "")[-700:]
        raise OllamaError(
            f"{spec.tag} 连续 {attempts} 次未通过契约校验: {last_errors}"
            + (f"\n最后一次原始输出尾部：\n{tail}" if tail else "")
        )


class MockClient:
    """离线跑通编排用：按 schema 合成占位产物，不加载任何模型。

    rework_first=N 时，前 N 次评审判定返回 rework_dev，用于验证回流边是否真的会触发。
    单驻留调度按 tag 变化模拟（真实环境下 8B→14B→coder→14B 共 3 次切换）。
    """

    def __init__(self, rework_first: int = 0) -> None:
        self.message = "mock: 未加载真实模型"
        self.rework_first = rework_first
        self._review_calls = 0
        self._resident: str | None = None

    def ps(self) -> list[dict]:
        return [{"name": self._resident}] if self._resident else []

    def unload(self, tag: str, wait: float = 0.0) -> None:  # noqa: ARG002
        if self._resident == tag:
            self._resident = None

    def ensure_exclusive(self, tag: str) -> dict:
        before = [self._resident] if self._resident else []
        switched = bool(before) and self._resident != tag
        self._resident = tag
        return {
            "resident_before": before,
            "unloaded": before if switched else [],
            "switched": switched,
            "kept_warm": bool(before) and not switched,
        }

    def chat_json(
        self,
        spec: ModelSpec,
        system: str,  # noqa: ARG002
        user: str,
        schema: dict,
        num_predict: int | None = None,  # noqa: ARG002
        attempts: int = 2,  # noqa: ARG002
    ) -> tuple[Any, dict]:
        data = _synthesize(schema)
        if schema is ASSESSMENT:
            # 没有真实代码片段时，架构师评估的 modules 就该是空数组（否则会触发事实接地的重试噪声）
            data["modules"] = []
        if schema is GLOBAL_ARCHITECTURE:
            # 与真实契约对齐：一份**自洽**的两模块拆分。必须自洽 ——
            # gateway.check_self_consistency 会查「执行顺序是否覆盖全部模块且是合法拓扑序」，
            # 合成一份不自洽的样本会让 mock 永远走不到 large 分支，等于这条链路没被测过。
            data = {
                "project_summary": "mock：把需求拆成两个可独立交付的模块",
                "modules": [
                    {
                        "module_id": "M-01",
                        "module_name": "核心逻辑",
                        "responsibility": "承载需求主流程与数据模型调整",
                        "scope_in": ["主流程实现", "数据模型调整"],
                        "scope_out": ["入口接线", "展示层适配"],
                        "risk_level": "high",
                        "depends_on": [],
                    },
                    {
                        "module_id": "M-02",
                        "module_name": "接入与展示",
                        "responsibility": "在既有入口暴露新能力并做展示适配",
                        "scope_in": ["入口接线", "展示层适配"],
                        "scope_out": ["核心算法", "数据模型"],
                        "risk_level": "medium",
                        "depends_on": ["M-01"],
                    },
                ],
                "interface_contracts": [
                    {
                        "interface_id": "IF-01",
                        "from_module": "M-01",
                        "to_module": "M-02",
                        "interface_name": "核心能力查询接口",
                        "input_format": "查询条件字典",
                        "output_format": "结果列表",
                        "error_codes": ["NOT_FOUND=目标不存在"],
                    }
                ],
                "global_constraints": {
                    "forbidden_paths": [],
                    "naming_rules": "沿用仓库现有命名风格",
                    "compatibility_rules": "不得改变既有对外接口签名",
                    "dependency_versions": "沿用仓库现有依赖版本",
                },
                "execution_order": ["M-01", "M-02"],
                "integration_checkpoints": [
                    {
                        "checkpoint": "端到端主流程可用",
                        "verification_method": "跑既有回归用例 + 新增用例",
                    }
                ],
                "uncertainties": [
                    {
                        "issue": "存量目录细节未提供",
                        "assumption": "按顶层目录职责划分模块边界",
                        "impact": "可能需人工微调边界",
                    }
                ],
            }
            # 把 prompt 里「已知禁区」那一段原样回填 —— 这样 mock 也能验证
            # 「禁区真的流进了 GA 的输入」，而不是只测了一个空数组。
            # 必须**锚定到禁区块**：用户消息里其它段落也有 "  - " 开头的行（约束列表），
            # 不锚定就会把约束文字当成路径。
            block = re.search(
                r"已知禁区（forbidden_paths[^）]*）[^\n]*\n((?:  - .+\n?)*)", user
            )
            if block:
                paths = re.findall(r"^  - (.+)$", block.group(1), re.MULTILINE)
                if paths:
                    data["global_constraints"]["forbidden_paths"] = paths[:2]
        if spec.role.startswith("开发"):
            # 与真实契约对齐：锚定补丁 + covers_tasks（覆盖审计要能机械核对通过）
            task_ids = re.findall(r'"id"\s*:\s*"([^"]+)"', user) or ["mock_task"]
            data["edits"] = [
                {
                    "path": "src/example.py",
                    "change_type": "modify",
                    "target_symbol": "mock_symbol",
                    "anchor": "def mock_symbol(conn):",
                    "patch_mode": "insert_after",
                    "patch": '@@ -1,3 +1,4 @@\n def mock_symbol(conn):\n-    return 1\n+    conn.commit()\n+    return 2',
                    "covers_tasks": [task_ids[0]],
                    "rationale": "mock",
                }
            ]
            data["not_implemented"] = []
        if spec.role == "评审":
            self._review_calls += 1
            data["blockers"] = []
            data["residual_risks"] = []
            if self._review_calls <= self.rework_first:
                # 固定文案：便于验证「同一个问题跨轮次复发」是否被识别出来
                fix = "mock 要求返工：补测后端导出接口存在性"
                data["verdict"] = "rework_dev"
                data["required_fixes"] = [fix]
                data["required_fixes_detail"] = [
                    {"fix": fix, "scope": "in_material", "why": "本轮材料内可改"}
                ]
            else:
                data["verdict"] = "pass"
                data["required_fixes"] = []
                data["required_fixes_detail"] = []
        meta = {
            "tag": spec.tag,
            "role": spec.role,
            "num_ctx": spec.num_ctx,
            "think": spec.think,
            "attempt": 1,
            "wall_s": 0.0,
            "load_s": 0.0,
            "prompt_tokens": estimate_tokens(user),
            "output_tokens": 0,
            "prompt_s": 0.0,
            "eval_s": 0.0,
            "thinking_chars": 0,
            "prompt_est_tokens": estimate_tokens(user),
            "schema_errors": [],
            # 与真机埋点保持同列：mock 不会截断，所以恒为 stop / 满额上限
            "done_reason": "stop",
            "num_predict": spec.num_predict,
            "mock": True,
            "_raw_text": json.dumps(data, ensure_ascii=False),
            "_raw_thinking": "",
            "_failed_attempts": [],
        }
        return data, meta


def _synthesize(schema: dict, key: str = "") -> Any:
    """按 schema 造一个通过校验的最小样本。"""
    if "enum" in schema:
        return schema["enum"][0]
    want = schema.get("type")
    if want == "object":
        out: dict[str, Any] = {}
        for name, sub in schema.get("properties", {}).items():
            out[name] = _synthesize(sub, name)
        return out
    if want == "array":
        return [_synthesize(schema.get("items", {"type": "string"}), key)] * max(schema.get("minItems", 1), 1)
    if want == "integer":
        return int(schema.get("minimum", 0))
    if want == "number":
        return float(schema.get("minimum", 0))
    if want == "boolean":
        return True
    return f"<{key or 'text'}>"
