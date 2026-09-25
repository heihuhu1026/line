"""本地操作页面（仅标准库，无第三方依赖，纯离线）。

功能：
- 列出 runs/ 下所有运行（状态 / 判定 / 游标 / 轮次 / 耗时）
- 查看每次运行的分阶段产物、调用埋点、输入预览、待人工确认清单、运行日志
- 直接编辑某阶段的 artifact 并保存（续跑时以文件为准，编辑立即生效）
- 通过 / 打回重跑某阶段 / 带人工意见续跑 / 中断正在跑的进程
- 新建运行（需求文本 + 仓库路径 + 人工闸门）

启动::

    python -m pipeline.server --port 8787

页面只在 127.0.0.1 上监听；流水线子进程与页面共用同一份 runs/ 目录。
注意：显存单驻留，同一时刻只允许一个流水线进程（接口层强制）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import string
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import advice as advice_mod
from . import config as config_mod
from . import flow
from . import gateway
from . import issues as issues_mod
from . import local_config
from . import prd as prd_mod
from . import presence
from . import prompts as prompts_mod
from . import runstore
from . import verify as verify_mod
from .config import RUNS_DIR
from .ollama_client import MockClient
from .orchestrator import ONLY_STAGES, feedback_target
from .schemas import STAGE_SCHEMAS, validate

ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = Path(__file__).resolve().with_name("console.html")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-\.]{0,80}$")
# --pause-after 只接受模型阶段；human_review 由流程在评审通过后自动触发，不能作为人工闸门传入
PAUSE_STAGES = [s for s in ONLY_STAGES if s != "human_review"]

_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


# --------------------------------------------------------------------- 子进程
def _job(run_id: str) -> dict[str, Any] | None:
    with _JOBS_LOCK:
        job = _JOBS.get(run_id)
        if job and job["proc"].poll() is not None:
            job["exit_code"] = job["proc"].returncode
            job["running"] = False
            return job
        return job


def _runs_root() -> Path:
    """当前服务的 runs 目录（模块级函数没有 self，只能从 Handler 上取）。"""
    return Path(getattr(Handler, "runs_dir", RUNS_DIR))


def _run_live(run_id: str, run_dir: Path | None = None) -> tuple[bool, dict[str, Any] | None]:
    """这个运行是否**真的在跑**，以及它的在场标记（没有则 None）。

    两个来源，缺一不可：
      * 内存注册表 —— 本服务起的孩子。最可靠，还能直接 terminate；
      * 在场标记 —— 服务端**重启过**之后仍在跑的孩子只在这里看得到
        （真机踩到：重启换代码后，正在跑的孩子成了「隐身人」，页面显示成已中断、
        续跑按钮是亮的，点下去会起第二个进程抢显存）。见 pipeline/presence.py。
    """
    job = _job(run_id)
    if job and job.get("running"):
        return True, None
    mark = presence.read(run_dir if run_dir is not None else _runs_root() / run_id)
    return (mark is not None), mark


def _any_running() -> str | None:
    with _JOBS_LOCK:
        for run_id, job in _JOBS.items():
            if job["proc"].poll() is None:
                return run_id
    live = presence.scan(_runs_root())
    if live:
        # 单驻留下本不该有第二个在跑；真出现多个就取最早开始的，好歹报一个出来
        return min(live, key=lambda rid: float(live[rid].get("started_at") or 0))
    return None


def _spawn(run_id: str, args: list[str], log_path: Path) -> dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PIPELINE_RUNS_DIR", str(RUNS_DIR))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = log_path.open("a", encoding="utf-8")
    creation = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    proc = subprocess.Popen(  # noqa: S603
        args, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT, env=env, creationflags=creation
    )
    job = {"proc": proc, "log": log_path, "started": time.time(), "argv": args, "running": True}
    with _JOBS_LOCK:
        _JOBS[run_id] = job
    return job


def shutdown_jobs(grace: float = 1.0) -> None:
    """终止所有仍在跑的子进程（服务退出时清理用，避免遗留进程占着显存）。"""
    with _JOBS_LOCK:
        jobs = list(_JOBS.values())
    for job in jobs:
        if job["proc"].poll() is None:
            job["proc"].terminate()
    if jobs and grace:
        time.sleep(grace)


def _log_tail(path: Path, lines: int = 400) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # noqa: BLE001
        return f"<读取日志失败: {exc}>"
    return "\n".join(text.splitlines()[-lines:])


# --------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "pipeline-console"
    runs_dir: Path = RUNS_DIR

    # ---------------------------------------------------------------- 工具
    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
        return

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: Any) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _err(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _path_parts(self) -> list[str]:
        return [part for part in urlparse(self.path).path.split("/") if part]

    def _run_dir(self, run_id: str) -> Path | None:
        if not RUN_ID_RE.match(run_id):
            return None
        run_dir = self.runs_dir / run_id
        return run_dir if run_dir.is_dir() else None

    # ---------------------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802
        parts = self._path_parts()
        try:
            if not parts or parts[0] in ("index.html", "console.html"):
                if not HTML_PATH.exists():
                    return self._err(500, f"缺少前端文件: {HTML_PATH}")
                return self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
            if parts[0] != "api":
                return self._err(404, "not found")
            if len(parts) == 1 and parts[0] == "api":
                return self._json(200, self._list_runs())
            if parts[1] == "runs" and len(parts) == 2:
                return self._json(200, self._list_runs())
            if parts[1] == "report":
                report = issues_mod.build_report(self.runs_dir)
                return self._json(200, {"report": report, "markdown": issues_mod.report_markdown(report)})
            if parts[1] == "kinds":
                return self._json(200, {
                    "kinds": [{"kind": k, "label": issues_mod.KIND_CN.get(k, k)} for k in issues_mod.KINDS]
                })
            if parts[1] == "flow" and len(parts) == 2:
                return self._json(200, self._flow_view())
            if parts[1] == "jobs" and len(parts) == 2:
                # 全局架构作业（runs/_jobs/）：作业目录 "_" 开头，不在运行列表里，单独一栏
                return self._json(
                    200,
                    {
                        "jobs": gateway.list_jobs(self.runs_dir),
                        "mode": gateway.local_config.guard().get("mode"),
                        "forbidden": gateway.forbidden_paths(),
                    },
                )
            if parts[1] == "jobs" and len(parts) == 3:
                job_id = parts[2]
                if not RUN_ID_RE.match(job_id):
                    return self._err(400, "非法 job_id")
                view = gateway.job_view(self.runs_dir, job_id)
                if view is None:
                    return self._err(404, f"找不到作业: {job_id}")
                return self._json(200, view)
            if parts[1] == "config" and len(parts) == 2:
                return self._json(200, self._config_view())
            if parts[1] == "config" and len(parts) == 3 and parts[2] == "defaults":
                return self._json(200, {
                    "models": config_mod.CODE_DEFAULTS.get("models") or {},
                    "runtime": config_mod.CODE_DEFAULTS.get("runtime") or {},
                    "budgets": config_mod.CODE_DEFAULTS.get("budgets") or {},
                    "prompts": prompts_mod.PROMPT_DEFAULTS,
                })
            if parts[1] == "ollama" and len(parts) == 3 and parts[2] == "tags":
                return self._json(200, self._ollama_tags())
            if parts[1] == "fs" and len(parts) >= 3 and parts[2] == "browse":
                query = parse_qs(urlparse(self.path).query)
                return self._json(200, self._fs_browse((query.get("path") or [""])[0]))
            if parts[1] == "runs" and len(parts) >= 3:
                run_id = parts[2]
                if not RUN_ID_RE.match(run_id):
                    return self._err(400, "非法 run_id")
                run_dir = self._run_dir(run_id)
                if run_dir is None:
                    return self._err(404, f"找不到运行: {run_id}")
                if len(parts) == 4 and parts[3] == "patch":
                    query = parse_qs(urlparse(self.path).query)
                    name = (query.get("file") or [""])[0]
                    target = (run_dir / "patches" / Path(name).name).resolve()
                    if not str(target).startswith(str((run_dir / "patches").resolve())) or not target.exists():
                        return self._err(404, "找不到该补丁文件")
                    return self._send(200, target.read_bytes(), "text/plain; charset=utf-8")
                if len(parts) == 4 and parts[3] == "log":
                    query = parse_qs(urlparse(self.path).query)
                    lines = int((query.get("lines") or ["400"])[0])
                    stage = (query.get("stage") or [""])[0].strip()
                    if stage:
                        # 按阶段切分日志（流程图节点用）：阶段名先校验，避免把任意字符串当过滤条件
                        if stage not in flow.NODES:
                            return self._err(400, f"未知阶段: {stage}")
                        return self._json(
                            200,
                            {"run_id": run_id, **runstore.stage_log(run_dir, stage, max_lines=max(1, lines))},
                        )
                    return self._send(
                        200,
                        _log_tail(run_dir / runstore.LOG_NAME, lines).encode("utf-8"),
                        "text/plain; charset=utf-8",
                    )
                if len(parts) == 4 and parts[3] == "advice":
                    # 裁决参谋的问答线程 + 该阶段待确认项（只读）：页面进闸门时拉一次
                    return self._advice_view(run_id, run_dir)
                if len(parts) == 4 and parts[3] == "demo":
                    # 演示视图：沙箱里有什么、能跑哪条命令（不执行任何东西）
                    return self._json(200, self._demo_view(run_id, run_dir))
                if len(parts) == 4 and parts[3] == "checkpoints":
                    # 检查点时间线（含 superseded 归档）：页面的「回放到此处」用它做选择器
                    return self._json(
                        200, {"run_id": run_id, "checkpoints": runstore.checkpoints(run_dir)}
                    )
                if len(parts) == 3:
                    return self._json(200, self._detail(run_id, run_dir))
            return self._err(404, "not found")
        except Exception as exc:  # noqa: BLE001
            return self._err(500, f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        parts = self._path_parts()
        try:
            if parts[:1] != ["api"]:
                return self._err(404, "not found")
            if parts[1:2] == ["config"] and len(parts) == 2:
                return self._save_config()
            if parts[1:2] == ["config"] and len(parts) == 3 and parts[2] == "reset":
                return self._reset_config()
            if parts[1:2] == ["runs"] and len(parts) == 2:
                return self._create_run()
            if parts[1:2] == ["jobs"] and len(parts) == 4 and parts[3] == "resume":
                return self._resume_gateway_job(parts[2])
            if parts[1:2] == ["runs"] and len(parts) == 4:
                run_id = parts[2]
                if not RUN_ID_RE.match(run_id):
                    return self._err(400, "非法 run_id")
                run_dir = self._run_dir(run_id)
                if run_dir is None:
                    return self._err(404, f"找不到运行: {run_id}")
                action = parts[3]
                if action == "artifact":
                    return self._save_artifact(run_id, run_dir)
                if action == "resume":
                    return self._resume(run_id, run_dir)
                if action == "stop":
                    return self._stop(run_id, run_dir)
                if action == "intake-decisions":
                    return self._save_intake_decisions(run_id, run_dir)
                if action == "pm-decisions":
                    return self._save_pm_decisions(run_id, run_dir)
                if action == "advice":
                    return self._advice_ask(run_id, run_dir)
                if action == "prd":
                    return self._save_prd(run_id, run_dir)
                if action == "replay":
                    return self._replay_checkpoint(run_id, run_dir)
                if action == "demo":
                    return self._run_demo(run_id, run_dir)
            if parts[1:2] == ["runs"] and len(parts) == 3 and parts[2] == "delete":
                return self._delete_runs_batch()
            return self._err(404, "not found")
        except ValueError as exc:
            return self._err(400, str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._err(500, f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- DELETE
    def do_DELETE(self) -> None:  # noqa: N802
        """删除单次运行：DELETE /api/runs/<run_id>（批量走 POST /api/runs/delete）。"""
        parts = self._path_parts()
        try:
            if not (parts[:2] == ["api", "runs"] and len(parts) == 3):
                return self._err(404, "not found")
            run_id = parts[2]
            if not RUN_ID_RE.match(run_id):
                return self._err(400, "非法 run_id")
            run_dir = self._run_dir(run_id)
            if run_dir is None:
                return self._err(404, f"找不到运行: {run_id}")
            job = _job(run_id)
            if job and job.get("running"):
                return self._err(409, f"{run_id} 正在运行中，请先「中断」再删除")
            ok, err = self._remove_run_dir(run_id, run_dir)
            if not ok:
                return self._err(500, err)
            return self._json(200, {"ok": True, "run_id": run_id, "removed": [run_id], "failed": []})
        except Exception as exc:  # noqa: BLE001
            return self._err(500, f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- 接口实现
    def _list_runs(self) -> dict:
        rows = runstore.list_runs(self.runs_dir)
        for row in rows:
            live, mark = _run_live(row["run_id"], self.runs_dir / row["run_id"])
            row["running"] = live
            # 异常中断：state 写着 running、却没有任何活进程（子进程被强杀 / 机器断电）。
            # 列表与详情必须给同一个判据 —— 只给详情算的话，列表里这一行会退化成
            # 原始英文 `running`，看着像还在跑。
            row["orphaned"] = bool(not live and str(row.get("status") or "") == "running")
            if mark:
                # 注册表里没有、但进程还活着（服务端重启过）：页面据此说明真相，
                # 否则会显示成「已中断」并亮出续跑按钮 —— 点下去就是第二个进程。
                row["presence"] = mark
            # 实时重算问题记录（issues.json 是派生视图，暂停/结束时才落盘）
            state, source = issues_mod.state_of(self.runs_dir / row["run_id"])
            row["issues"] = issues_mod.summarize(issues_mod.collect_issues(state, row["run_id"]))
            row["record_source"] = source
        return {"runs": rows, "runs_dir": str(self.runs_dir), "busy_with": _any_running()}

    # ------------------------------------------------------------------ 配置（页面可调）
    def _config_view(self) -> dict:
        """当前生效的配置 + 代码默认值 + 来源标记，供「配置」页渲染。"""
        over = local_config.load()
        over_models = over.get("models") if isinstance(over.get("models"), dict) else {}
        over_prompts = over.get("prompts") if isinstance(over.get("prompts"), dict) else {}
        over_runtime = over.get("runtime") if isinstance(over.get("runtime"), dict) else {}

        models: dict[str, Any] = {}
        for stage, spec in config_mod.STAGE_MODELS.items():
            models[stage] = {
                "role": spec.role,
                "tag": spec.tag,
                "num_ctx": spec.num_ctx,
                "prompt_token_budget": spec.prompt_token_budget,
                "think": spec.think,
                "temperature": spec.temperature,
                "num_predict": spec.num_predict,
                "default": (config_mod.CODE_DEFAULTS.get("models") or {}).get(stage) or {},
                "source": "override" if stage in over_models else "default",
            }

        prompts_view: dict[str, Any] = {}
        for stage, text in prompts_mod.SYSTEM.items():
            prompts_view[stage] = {
                "text": text,
                "chars": len(text),
                "default": prompts_mod.PROMPT_DEFAULTS.get(stage, ""),
                "source": "override" if stage in over_prompts else "default",
            }

        defaults_runtime = config_mod.CODE_DEFAULTS.get("runtime") or {}
        runtime: dict[str, Any] = {}
        for key, attr in config_mod.RUNTIME_SCALARS.items():
            runtime[key] = {
                "value": getattr(config_mod, attr),
                "default": defaults_runtime.get(key),
                "type": "number",
                "source": "override" if key in over_runtime else "default",
            }
        for key, attr in config_mod.RUNTIME_FLAGS.items():
            runtime[key] = {
                "value": bool(getattr(config_mod, attr)),
                "default": defaults_runtime.get(key),
                "type": "bool",
                "source": "override" if key in over_runtime else "default",
            }

        defaults_budgets = config_mod.CODE_DEFAULTS.get("budgets") or {}
        budgets: dict[str, Any] = {}
        for name in config_mod.BUDGET_DICTS:
            budgets[name] = {
                "value": dict(getattr(config_mod, name)),
                "default": dict(defaults_budgets.get(name) or {}),
                "source": "override" if name in over_runtime else "default",
            }

        return {
            "models": models,
            "prompts": prompts_view,
            "runtime": runtime,
            "budgets": budgets,
            # 只用 STAGE_MODELS 的阶段：ONLY_STAGES 还含 human_review（人工闸门，非模型阶段），
            # 列进去会给页面多渲染一行空模型配置。
            "stages": list(config_mod.STAGE_MODELS),
            "ollama": self._ollama_tags(),
            "path": str(local_config.path()),
            "has_override": bool(over_models or over_prompts or over_runtime),
        }

    def _ollama_tags(self) -> dict:
        """代理 Ollama 的 /api/tags。

        页面在 8787、Ollama 在 11434，浏览器直连会被 CORS 拦，必须由服务端转发。
        """
        host = str(config_mod.OLLAMA_HOST or "http://localhost:11434").rstrip("/")
        try:
            with urllib.request.urlopen(host + "/api/tags", timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            return {
                "reachable": False,
                "host": host,
                "models": [],
                "error": f"{type(exc).__name__}: {exc}",
                "hint": (
                    "Ollama 不可达。若刚冷启动过，请用 models\\start_ollama.ps1 启动 —— 它会带上 "
                    "OLLAMA_MODELS=D:\\AI\\Models，否则模型列表是空的（历史坑，见 CONTEXT.md）。"
                ),
            }
        rows: list[dict[str, Any]] = []
        for item in data.get("models") or []:
            details = item.get("details") or {}
            rows.append({
                "name": item.get("name") or item.get("model") or "",
                "size": item.get("size"),
                "parameter_size": details.get("parameter_size"),
                "quantization_level": details.get("quantization_level"),
                "family": details.get("family"),
                "context_length": details.get("context_length"),
                "capabilities": item.get("capabilities") or [],
            })
        rows.sort(key=lambda r: str(r.get("name")))
        return {"reachable": True, "host": host, "models": rows}

    def _save_config(self) -> None:
        payload = self._body()
        patch = {k: v for k, v in payload.items() if k in local_config.SCOPES and isinstance(v, dict)}
        if not patch:
            return self._err(400, "请求体需包含 models / prompts / runtime 中至少一个对象")

        model_fields = {
            "role", "tag", "num_ctx", "prompt_token_budget",
            "think", "temperature", "num_predict",
        }
        for stage, fields in (patch.get("models") or {}).items():
            if stage not in config_mod.STAGE_MODELS:
                return self._err(400, f"未知阶段: {stage}")
            if fields is None:  # None = 删除该项覆盖
                continue
            if not isinstance(fields, dict):
                return self._err(400, f"models.{stage} 必须是对象")
            unknown = set(fields) - model_fields
            if unknown:
                return self._err(400, f"models.{stage} 含未知字段 {sorted(unknown)}")
        for stage in (patch.get("prompts") or {}):
            if stage not in prompts_mod.SYSTEM:
                return self._err(400, f"未知角色: {stage}")
        known_runtime = (
            set(config_mod.RUNTIME_SCALARS)
            | set(config_mod.RUNTIME_FLAGS)
            | set(config_mod.BUDGET_DICTS)
        )
        for key in (patch.get("runtime") or {}):
            if key not in known_runtime:
                return self._err(400, f"未知运行时参数: {key}")

        saved = local_config.save(patch)
        self._reload_overrides()
        return self._json(200, {"ok": True, "saved": saved, "view": self._config_view()})

    def _reset_config(self) -> None:
        payload = self._body()
        scope = str(payload.get("scope") or "").strip() or None
        if scope and scope not in local_config.SCOPES:
            return self._err(400, f"未知 scope: {scope}，可选 {list(local_config.SCOPES)}")
        local_config.clear(scope)
        self._reload_overrides()
        return self._json(200, {"ok": True, "view": self._config_view()})

    @staticmethod
    def _reload_overrides() -> None:
        """保存/还原后让本进程内的 config / prompts 立刻反映新值。

        只影响 server 自己与**之后新起**的流水线子进程：正在跑的子进程在启动时就已
        加载完毕，不会被改写（页面提示语要说明这一点，避免误解）。
        """
        local_config.load(force=True)
        config_mod.apply_overrides()
        prompts_mod.apply_overrides()

    # --------------------------------------------------- 文件系统浏览（目录选择器）
    def _fs_browse(self, path: str) -> dict:
        """本地目录选择器后端：列出给定目录的下一级，或盘符列表。

        仅在本机 127.0.0.1 上使用，不涉及任何上传。路径越界（盘符之上）用
        哨兵 __computer__ 表示「此电脑」。
        """
        COMPUTER = "__computer__"
        if not path or path == COMPUTER:
            if os.name == "nt":
                drives = [{"name": f"{d}:/", "is_dir": True}
                          for d in string.ascii_uppercase if os.path.exists(f"{d}:/")]
            else:
                drives = [{"name": "/", "is_dir": True}]
            return {"path": COMPUTER, "parent": None, "entries": drives, "computer": True}

        p = Path(path).expanduser()
        if not p.exists():
            p = p.parent
        try:
            p = p.resolve(strict=False)
        except OSError:
            p = p.resolve()
        entries = []
        try:
            for child in p.iterdir():
                if child.name.startswith("."):
                    continue
                entries.append({"name": child.name, "is_dir": child.is_dir()})
        except OSError:
            entries = []
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        parent = COMPUTER if p.parent == p else str(p.parent)
        return {"path": str(p), "parent": parent, "entries": entries, "computer": False}

    def _detail(self, run_id: str, run_dir: Path) -> dict:
        detail = runstore.run_detail(run_dir)
        job = _job(run_id)
        # 「在跑」= 本服务起的孩子**或**还在维持心跳的外部进程（服务端重启后的场景）
        live, mark = _run_live(run_id, run_dir)
        detail["running"] = bool(live)
        # 外部进程（服务端注册表里没有）：页面要能说清「谁在跑」——否则「运行中」却
        # 没有可用的中断句柄，人会以为卡死了。
        detail["presence"] = mark if not (job and job.get("running")) else None
        # 孤儿状态：state 里写着 running，但**没有任何活进程**（典型成因：子进程被强杀、
        # 或机器断电）。不标出来的话页面会永远显示「运行中」、续跑按钮一直置灰，
        # 人只能干瞪眼 —— 实际上它是可以续跑的。
        detail["orphaned"] = bool(
            not detail["running"] and str((detail.get("state") or {}).get("status") or "") == "running"
        )
        detail["started_at"] = (
            (job.get("started") if job else None)
            or (mark or {}).get("started_at")
        )
        detail["busy_with"] = _any_running()
        detail["stages_order"] = ONLY_STAGES
        detail["current_cursor"] = (detail.get("state") or {}).get("cursor")
        # 问题记录：按当前 state 实时算，页面永远看到最新的（本轮之前的老运行退回 summary.json）
        state_view, record_source = issues_mod.state_of(run_dir)
        collected = issues_mod.collect_issues(state_view, run_id)
        detail["record_source"] = record_source
        detail["issues"] = [i.to_dict() for i in collected]
        detail["issue_summary"] = issues_mod.summarize(collected)
        detail["issue_kinds"] = [
            {"kind": kind, "label": issues_mod.KIND_CN.get(kind, kind)} for kind in issues_mod.KINDS
        ]
        detail["log"] = _log_tail(run_dir / runstore.LOG_NAME, 200 if detail["running"] else 120)
        # 项目类型优先取 state 里的真值（流水线实际加载的），.inbox 文件只作兜底：
        # CLI 直接跑的 run 没有 .inbox/*.ptype，只看文件会显示「未记录」，
        # 而 state 里明明记着实际用的是哪套提示词。
        ptype_file = self.runs_dir / ".inbox" / f"{run_id}.ptype"
        fallback = ptype_file.read_text(encoding="utf-8").strip() if ptype_file.exists() else None
        detail["project_type"] = (detail.get("state") or {}).get("project_type") or fallback
        # 若这次运行被入口总闸拆成了作业，详情页要能看到「东西去哪了」
        detail["gateway"] = gateway.read_link(self.runs_dir, run_id)
        return detail

    def _create_run(self) -> None:
        payload = self._body()
        requirement = (payload.get("requirement") or "").strip()
        if not requirement:
            return self._err(400, "需求文本不能为空")
        busy = _any_running()
        if busy and not payload.get("force"):
            return self._err(409, f"已有运行在进行中（{busy}）—— 显存单驻留，请先等它结束或中断它")

        # 先校验人工闸门（在写任何文件之前），非法阶段直接 400，避免落一堆孤儿 inbox 文件
        gates = payload.get("pause_after") or []
        bad = [g for g in gates if g not in PAUSE_STAGES]
        if bad:
            return self._err(
                400,
                f"人工闸门含非法阶段 {bad}，只支持: {PAUSE_STAGES}"
                "（「人工审核」由流程在评审通过后自动触发，无需勾选）",
            )

        # 入口总闸参数一并先校验（同样在写任何文件之前），避免落一堆孤儿 inbox 文件
        gw_flags: list[str] = []
        mode = (payload.get("gateway") or "").strip()
        if mode:
            if mode not in gateway.MODES:
                return self._err(400, f"未知 gateway 模式 {mode}，只支持 {list(gateway.MODES)}")
            gw_flags += ["--gateway", mode]
        scale = (payload.get("scale") or "").strip()
        if scale:
            if scale not in gateway.SCALES:
                return self._err(400, f"未知 scale {scale}，只支持 {list(gateway.SCALES)}")
            gw_flags += ["--scale", scale]
        forbidden = payload.get("forbidden") or []
        if isinstance(forbidden, list):
            picked = [str(x).strip() for x in forbidden if str(x).strip()]
            if picked:
                gw_flags += ["--forbidden", ",".join(picked)]

        run_id = time.strftime("%Y%m%d-%H%M%S")
        if (self.runs_dir / run_id).exists():
            run_id = f"{run_id}-{int(time.time()) % 1000}"
        inbox = self.runs_dir / ".inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        req_file = inbox / f"{run_id}.md"
        req_file.write_text(requirement, encoding="utf-8")
        # 项目类型（新建项目 / 二次开发）：落盘到 inbox，详情页用于展示与区分
        ptype = (payload.get("project_type") or "secondary").strip() or "secondary"
        (inbox / f"{run_id}.ptype").write_text(ptype, encoding="utf-8")

        args = [
            sys.executable,
            "-m",
            "pipeline.cli",
            "--requirement-file",
            str(req_file),
            "--run-id",
            run_id,
            "--out",
            str(self.runs_dir),
        ]
        repo = (payload.get("repo") or "").strip()
        if repo:
            args += ["--repo", repo]
        if gates:
            args += ["--pause-after", ",".join(str(g) for g in gates)]
        if payload.get("review_every"):
            args += ["--review-every", str(int(payload["review_every"]))]
        if payload.get("max_rework") is not None and payload.get("max_rework") != "":
            args += ["--max-rework", str(int(payload["max_rework"]))]
        if payload.get("mock"):
            args += ["--mock"]
        # 项目类型必须传给流水线：它决定用哪套系统提示词、以及是否跳过存量代码评估。
        # 仅仅写进 .inbox/*.ptype 供页面展示是不够的（那是改造前的行为）。
        args += ["--project-type", ptype]
        # 入口总闸：页面可显式指定模式/规模/禁区；不传则沿用 config.local.json 的 guard.*
        args += gw_flags
        _spawn(run_id, args, self.runs_dir / run_id / runstore.LOG_NAME)
        self._json(200, {"ok": True, "run_id": run_id, "argv": args})

    def _save_artifact(self, run_id: str, run_dir: Path) -> None:
        payload = self._body()
        stage = str(payload.get("stage") or "")
        if stage not in STAGE_SCHEMAS:
            return self._err(400, f"未知阶段: {stage}")
        artifact = payload.get("artifact")
        warnings = validate(artifact, STAGE_SCHEMAS[stage])
        if not runstore.save_artifact(run_dir, stage, artifact, note="console-edit"):
            return self._err(409, f"{stage} 还没有落盘产物，无法保存")
        self._append_human_action(
            run_dir,
            action="human_edit",
            stage=stage,
            text=(payload.get("note") or "页面修改产物").strip(),
            kind=str(payload.get("issue_kind") or ""),
        )
        self._json(200, {"ok": True, "run_id": run_id, "warnings": warnings})

    def _append_human_action(self, run_dir: Path, action: str, stage: str | None, text: str = "", kind: str = "") -> None:
        """把人工动作写进 state.human_actions，续跑时由编排器接管（问题记录的数据源之一）。"""
        state = runstore.read_state(run_dir)
        if not state:
            return
        state.setdefault("human_actions", []).append(
            {
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "action": action,
                "stage": stage,
                "text": text,
                "kind": kind,
                "attempt": state.get("attempt"),
            }
        )
        runstore.write_state(run_dir, state)

    def _resume(self, run_id: str, run_dir: Path) -> None:
        payload = self._body()
        job = _job(run_id)
        if job and job.get("running"):
            return self._err(409, f"{run_id} 正在运行中")
        busy = _any_running()
        if busy:
            return self._err(409, f"已有运行在进行中（{busy}），请先等它结束")

        state = runstore.read_state(run_dir)
        if not state:
            return self._err(400, "该运行没有 state.json（--only 运行或未开始），无法续跑")

        from_stage = payload.get("from") or None
        if from_stage and from_stage not in ONLY_STAGES:
            return self._err(400, f"未知阶段: {from_stage}")
        feedback = (payload.get("feedback") or "").strip()
        if feedback:
            target = feedback_target(state.get("cursor"), from_stage)
            feedback_map = state.setdefault("human_feedback", {})
            feedback_map.setdefault(target, []).append(feedback)
            runstore.write_state(run_dir, state)

        args = [sys.executable, "-m", "pipeline.cli", "--resume", run_id, "--out", str(self.runs_dir)]
        if from_stage:
            args += ["--from", from_stage]
        issue_kind = str(payload.get("issue_kind") or "").strip()
        if issue_kind and issue_kind in issues_mod.KINDS:
            args += ["--issue-kind", issue_kind]
        if payload.get("pause_after") is not None:
            gates = payload["pause_after"] or []
            bad = [g for g in gates if g not in PAUSE_STAGES]
            if bad:
                return self._err(
                    400,
                    f"人工闸门含非法阶段 {bad}，只支持: {PAUSE_STAGES}"
                    "（「人工审核」由流程在评审通过后自动触发，无需勾选）",
                )
            args += ["--pause-after", ",".join(str(g) for g in gates)] if gates else ["--no-pause"]
        if payload.get("review_every"):
            args += ["--review-every", str(int(payload["review_every"]))]
        # 回流上限：页面能放宽预算，人工介入才真的能救回一个已触顶的运行
        if payload.get("max_rework") is not None and payload.get("max_rework") != "":
            args += ["--max-rework", str(int(payload["max_rework"]))]
        _spawn(run_id, args, run_dir / runstore.LOG_NAME)
        self._json(200, {"ok": True, "run_id": run_id, "argv": args})

    def _replay_checkpoint(self, run_id: str, run_dir: Path) -> None:
        """回放到指定检查点：``POST /api/runs/<run_id>/replay``，body ``{seq:int}``。

        检查点即阶段快照（``NN-<stage>.json``），``seq`` 即其 checkpoint id。回放会作废该
        检查点之后的全部产物（归档到 ``superseded/``）并把游标归位，随后**立即续跑** ——
        走的是与页面「打回重跑」同一条子进程链路，单驻留约束照样成立。
        """
        payload = self._body()
        seq = payload.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            return self._err(400, "seq 必须是整数（检查点序号）")
        job = _job(run_id)
        if job and job.get("running"):
            return self._err(409, f"{run_id} 正在运行中")
        busy = _any_running()
        if busy:
            return self._err(409, f"已有运行在进行中（{busy}），请先等它结束")
        state = runstore.read_state(run_dir)
        if not state:
            return self._err(400, "该运行没有 state.json（--only 运行或未开始），无法回放")
        live = {c["seq"] for c in runstore.checkpoints(run_dir) if not c["superseded"]}
        if seq not in live:
            return self._err(404, f"找不到在存检查点 seq={seq}")

        # 人工意见沿用「写进 state 让编排器 _restore 读」的路径（与 _resume 一致，
        # 避免同时走 --feedback 参数造成两份重复注入）
        feedback = (payload.get("feedback") or "").strip()
        if feedback:
            target = feedback_target(state.get("cursor"))
            state.setdefault("human_feedback", {}).setdefault(target, []).append(feedback)
            runstore.write_state(run_dir, state)

        args = [
            sys.executable,
            "-m",
            "pipeline.cli",
            "--resume",
            run_id,
            "--out",
            str(self.runs_dir),
            "--from-checkpoint",
            str(seq),
        ]
        _spawn(run_id, args, run_dir / runstore.LOG_NAME)
        self._json(200, {"ok": True, "run_id": run_id, "seq": seq, "argv": args})

    def _resume_gateway_job(self, job_id: str) -> None:
        """续跑作业：``POST /api/jobs/<job_id>/resume``（body 可选 ``pause_after``）。

        与 ``_resume`` 同一条子进程链路：单驻留约束照旧，作业期间不允许别的运行插队。
        """
        if not RUN_ID_RE.match(job_id):
            return self._err(400, "非法 job_id")
        if gateway.read_job(self.runs_dir, job_id) is None:
            return self._err(404, f"找不到作业: {job_id}")
        job = _job(job_id)
        if job and job.get("running"):
            return self._err(409, f"{job_id} 正在运行中")
        busy = _any_running()
        if busy:
            return self._err(409, f"已有运行在进行中（{busy}），请先等它结束")

        payload = self._body()
        gates = payload.get("pause_after") or []
        bad = [g for g in gates if g not in PAUSE_STAGES]
        if bad:
            return self._err(400, f"人工闸门含非法阶段 {bad}，只支持: {PAUSE_STAGES}")

        args = [
            sys.executable,
            "-m",
            "pipeline.cli",
            "--resume-job",
            job_id,
            "--out",
            str(self.runs_dir),
        ]
        args += ["--pause-after", ",".join(str(g) for g in gates)] if gates else ["--no-pause"]
        _spawn(job_id, args, gateway.job_dir(self.runs_dir, job_id) / "console.log")
        self._json(200, {"ok": True, "job_id": job_id, "argv": args})

    def _flow_view(self) -> dict:
        """流定义视图（``GET /api/flow``）：拓扑 + 一致性校验结果。纯离线只读。"""
        problems = flow.validate()
        return {
            "mermaid": flow.mermaid(),
            "nodes": flow.NODES,
            "model_nodes": flow.MODEL_NODES + flow.PRE_NODES,
            "pausable_nodes": flow.PAUSABLE_NODES,
            "pre_nodes": flow.PRE_NODES,
            "pre_edges": flow.PRE_EDGES,
            "gateway": {
                "modes": list(gateway.MODES),
                "scales": list(gateway.SCALES),
                "mode": gateway.local_config.guard().get("mode"),
                "forbidden": gateway.forbidden_paths(),
            },
            "gates": [{"stage": g.stage, "kind": g.kind, "title": g.title} for g in flow.GATE_SPECS],
            "linear": {k: v for k, v in flow.LINEAR_EDGES.items() if v},
            "conditional": flow.CONDITIONAL_EDGES,
            "loop": [{"from": a, "to": b} for a, b in flow.LOOP_EDGES],
            "problems": problems,
            "ok": not problems,
        }

    # ------------------------------------------------------------ 演示 / 运行
    def _demo_sandbox(self, run_dir: Path) -> Path | None:
        """verify 阶段物化出的沙箱副本 —— 演示只在它里面跑，绝不碰用户目录。"""
        work = run_dir / "verify" / "work"
        return work if work.is_dir() else None

    def _demo_files(self, work: Path) -> list[str]:
        return sorted(
            str(p.relative_to(work)).replace("\\", "/")
            for p in work.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        )

    def _demo_view(self, run_id: str, run_dir: Path) -> dict:
        work = self._demo_sandbox(run_dir)
        if work is None:
            return {"run_id": run_id, "sandbox": None, "files": [], "entries": [],
                    "command": None, "runnable": False,
                    "note": "还没有沙箱（该运行没跑过 verify 阶段），无法演示"}
        files = self._demo_files(work)
        py = [f for f in files if f.endswith(".py")]
        command = verify_mod.demo_command(work, py)
        return {
            "run_id": run_id,
            "sandbox": str(work),
            "files": files,
            "entries": verify_mod.entry_files(work, py),
            "command": command,
            "runnable": command is not None,
            "note": "" if command else (
                "交付物没有可执行入口（没有 `if __name__ == '__main__':`，也没有 "
                + " / ".join(verify_mod.ENTRY_NAMES)
                + "）—— 无法演示，需要让开发补入口"
            ),
        }

    def _run_demo(self, run_id: str, run_dir: Path) -> None:
        """在沙箱里跑一条命令（控制台「演示」）。

        安全约定与 verify 完全同一套（白名单程序 / 危险片段 / 超时 / 洗掉凭据 / 无显示环境），
        cwd 固定为沙箱副本 —— 这是它敢做成一键按钮的原因。
        超时取比 verify 更短的默认值：游戏主循环这类不会自己退出的程序不能把页面挂住。
        """
        work = self._demo_sandbox(run_dir)
        if work is None:
            return self._err(404, "还没有沙箱（该运行没跑过 verify 阶段），无法演示")
        payload = self._body()
        files = self._demo_files(work)
        py = [f for f in files if f.endswith(".py")]
        command = str(payload.get("command") or "").strip() or verify_mod.demo_command(work, py)
        if not command:
            return self._err(400, "交付物没有可执行入口，无法演示")
        timeout = max(1, min(int(payload.get("timeout") or 20), 120))
        res = verify_mod.run_command(
            {"command": command, "source": "demo", "display": "控制台演示"},
            cwd=work,
            timeout=timeout,
            allowed_bins=config_mod.VERIFY_ALLOWED_BINS,
            deny_patterns=config_mod.VERIFY_DENY_PATTERNS,
        )
        return self._json(200, {"run_id": run_id, "sandbox": str(work), **res})

    @staticmethod
    def _write_stop_note(run_dir: Path | None, text: str) -> None:
        """把中断动作写进该运行的 console.log（与既有行为一致，便于事后归因）。"""
        if run_dir is None:
            return
        try:
            with (run_dir / runstore.LOG_NAME).open("a", encoding="utf-8") as fh:
                fh.write(f"\n{text}（{time.strftime('%H:%M:%S')}）\n")
        except OSError:
            pass

    def _stop(self, run_id: str, run_dir: Path | None = None) -> None:
        """中断一个运行。

        两条路：注册表里的孩子（本服务起的，直接 terminate）；以及**服务端不认识、
        但进程还活着**的外部孩子 —— 服务端重启后注册表就空了，只能按在场标记里的 pid
        结束它。没有第二条路的话，页面上的「中断」对这种情况就是个哑按钮，
        人只能去任务管理器手动杀。
        """
        job = _job(run_id)
        if job and job.get("running"):
            job["proc"].terminate()
            self._write_stop_note(run_dir, f"[console] 已请求中断 {run_id}")
            return self._json(200, {"ok": True, "via": "registry"})

        mark = presence.read(run_dir) if run_dir is not None else None
        pid = (mark or {}).get("pid")
        if isinstance(pid, int) and presence.terminate(pid):
            self._write_stop_note(
                run_dir,
                f"[console] 已按在场标记中断外部进程 pid={pid}（服务端没有它的子进程句柄）",
            )
            return self._json(200, {"ok": True, "via": "pid", "pid": pid})
        return self._json(200, {"ok": True, "already_stopped": True})

    # ---------------------------------------------------------------- 裁决参谋
    def _advice_stage(self, run_dir: Path, requested: Any) -> str:
        """裁决参谋服务于哪个阶段。

        页面传哪个就用哪个；没传就取「最后一次暂停的阶段」（人就在那个闸门上），
        再退到 cursor。**必须落到一个真实阶段** —— 否则取不到产物，提示词给出的
        上下文是空的，模型只能瞎答。
        """
        name = str(requested or "").strip()
        if name in STAGE_SCHEMAS:
            return name
        state = runstore.read_state(run_dir) or {}
        for candidate in (state.get("paused_after"), state.get("cursor")):
            if str(candidate or "") in STAGE_SCHEMAS:
                return str(candidate)
        return "intake"

    def _advice_view(self, run_id: str, run_dir: Path) -> None:
        """裁决参谋的问答线程 + 该阶段待确认项（只读）。"""
        query = parse_qs(urlparse(self.path).query)
        stage = self._advice_stage(run_dir, (query.get("stage") or [""])[0])
        state = runstore.read_state(run_dir) or {}
        artifact = (runstore.latest_artifacts(run_dir) or {}).get(stage)
        self._json(
            200,
            {
                "run_id": run_id,
                "stage": stage,
                "enabled": bool(config_mod.ADVICE_ENABLED),
                # 单驻留：有运行在跑时不能问（会抢显存），页面据此禁用入口
                "busy": bool(_any_running()),
                "mock": bool(state.get("mock")),
                "model": advice_mod.spec_for(stage).tag,
                "pending": advice_mod.pending_items(stage, artifact, state),
                "turns": advice_mod.read_thread(run_dir, stage),
            },
        )

    def _advice_ask(self, run_id: str, run_dir: Path) -> None:
        """问一次裁决参谋。

        这是整条流水线里**唯一**为了「人工提问」去调模型的地方。因为是旁路，最容易
        踩的两个坑是抢显存与拖住页面，所以两道门：单驻留检查 + 调用超时
        （``config.ADVICE_TIMEOUT``）。
        """
        if not config_mod.ADVICE_ENABLED:
            return self._err(403, "裁决参谋已关闭（PIPELINE_ADVICE=0）")
        if _any_running():
            return self._err(
                409, "有运行正在进行中：单驻留，等它结束或先「中断」再问（否则会抢显存）"
            )
        payload = self._body()
        stage = self._advice_stage(run_dir, payload.get("stage"))
        question = str(payload.get("question") or "").strip()
        if not question:
            return self._err(400, "question 不能为空")
        state = runstore.read_state(run_dir) or {}
        artifact = (runstore.latest_artifacts(run_dir) or {}).get(stage)
        try:
            turn = advice_mod.ask(
                run_dir,
                requirement=str(runstore.run_detail(run_dir).get("requirement") or ""),
                stage=stage,
                artifact=artifact,
                state=state,
                question=question[:2000],
                focus=str(payload.get("focus") or "")[:300],
                # mock 运行不该偷偷去调真模型（与流水线一致）
                client=MockClient() if state.get("mock") else None,
            )
        except Exception as exc:  # noqa: BLE001
            return self._err(500, f"裁决参谋调用失败：{type(exc).__name__}: {exc}")
        self._json(200, {"ok": True, "run_id": run_id, "stage": stage, "turn": turn})

    def _save_intake_decisions(self, run_id: str, run_dir: Path) -> None:
        """保存人工对补强阶段「待确认问题」的裁决。

        补强给出的是**默认假设 / 建议答案**；人工在这里逐条裁决后，它们才成为已知前提：
        - 结构化存进 `state.intake_decisions`（页面回显 + 编排器 `_restore` 读取）；
        - 每条再往 `human_actions` 追加一条 `intake_decision` 留痕（供问题记录统计）；
        - 编排器把它们并入【人工已确认的事实与指令】注入下游，并显式注入 PM。
        """
        payload = self._body()
        decisions = payload.get("decisions")
        if not isinstance(decisions, list):
            return self._err(400, "decisions 必须是数组")
        # 「用默认 / 用建议」＝人工**采纳**该默认值，带入下游时要去掉「默认 / 建议」这类前缀：
        # 否则下游看到的是「默认假设使用方向键控制」——仍是猜测口吻，等于没裁决（真机教训 20260924-135801）。
        lead = re.compile(r"^\s*(?:默认假设|默认建议|默认|建议答案|建议)\s*[:：]?\s*")
        cleaned: list[dict] = []
        for item in decisions:
            if not isinstance(item, dict):
                continue
            decision = lead.sub("", str(item.get("decision") or "").strip()).strip()
            if not decision:
                continue  # 没填裁决的条目不落盘，避免下游看到一堆空结论
            cleaned.append(
                {
                    "kind": str(item.get("kind") or ""),
                    "ref": str(item.get("ref") or "")[:300],
                    "decision": decision[:1000],
                }
            )
        # 裁决**并回补强产物本身**：NN-intake.json 从「初稿」变成「已裁决终稿」，
        # 下游（PM）只看这一份即可，不必再额外接收一份平行的裁决清单。
        artifact = (runstore.latest_artifacts(run_dir) or {}).get("intake")
        merged = 0
        if isinstance(artifact, dict):
            # 待确认项已合并成**一条列表**：不再区分 kind，一律按主题（element）匹配。
            # `intake_items` 两种形状都认 —— 旧运行的 missing_elements +
            # clarifying_questions 会被就地合并进来，前端仍按旧的两类 kind 提交也能对上。
            items = prompts_mod.intake_items(artifact)
            by_ref = {str(row.get("element") or ""): row for row in items}
            for item in cleaned:
                row = by_ref.get(item["ref"])
                if row is None:
                    continue  # 对不上的引用不落盘，也绝不新建条目
                row["final_decision"] = item["decision"]
                row["confirmed"] = True
                merged += 1
            if merged:
                # 落盘时统一成新形状，并收掉旧字段 —— 两种形状并存会让下游读旧字段时
                # 绕过去重与合并。
                artifact["pending_items"] = items
                artifact.pop("missing_elements", None)
                artifact.pop("clarifying_questions", None)
                # 已裁决条目整理成**陈述式**的已确认事实：下游直接当确定结论用，
                # 不再以「问 + 建议答案 + 裁决」的问答形式往下游流转。
                facts = [
                    f"{row.get('element')}：{row.get('final_decision')}"
                    for row in items
                    if row.get("final_decision")
                ]
                if facts:
                    artifact["confirmed_facts"] = facts
                runstore.save_artifact(run_dir, "intake", artifact, note="intake-decisions")
        state = runstore.read_state(run_dir)
        if not state:
            return self._err(409, "该运行没有 state.json，裁决无法保存（请用 --pause-after intake 先停住）")
        # state 里留一份结构化副本：页面回显用，也让编排器续跑时能做「人工已确认事实」的传导
        state["intake_decisions"] = cleaned
        runstore.write_state(run_dir, state)
        for item in cleaned:
            self._append_human_action(
                run_dir,
                action="intake_decision",
                stage="intake",
                text=f"{item['ref']} → {item['decision']}" if item["ref"] else item["decision"],
                kind="human_directive",
            )
        return self._json(200, {"ok": True, "run_id": run_id, "decisions": cleaned, "merged": merged})

    def _save_pm_decisions(self, run_id: str, run_dir: Path) -> None:
        """保存人工对 **PM 未决项**（open_questions）的裁决 —— 与需求补强裁决同一套逻辑。

        - 按 `question` 定位条目，写入 `final_decision` / `confirmed`；
        - 整理成陈述式的 `confirmed_facts`，回写 `NN-pm.json`（该产物对下游即「已裁决终稿」）；
        - 「用建议」＝采纳 PM 给的 `assumed_answer`，同样要剥掉「默认 / 建议」前缀。
        """
        payload = self._body()
        decisions = payload.get("decisions")
        if not isinstance(decisions, list):
            return self._err(400, "decisions 必须是数组")
        lead = re.compile(r"^\s*(?:默认假设|默认建议|默认|建议答案|建议|本次默认按此执行|默认按此执行)\s*[:：]?\s*")
        cleaned: list[dict] = []
        for item in decisions:
            if not isinstance(item, dict):
                continue
            decision = lead.sub("", str(item.get("decision") or "").strip()).strip()
            if not decision:
                continue
            cleaned.append({"ref": str(item.get("ref") or "")[:300], "decision": decision[:1000]})

        artifact = (runstore.latest_artifacts(run_dir) or {}).get("pm")
        merged = 0
        if isinstance(artifact, dict):
            for item in cleaned:
                for entry in artifact.get("open_questions") or []:
                    if isinstance(entry, dict) and str(entry.get("question") or "") == item["ref"]:
                        entry["final_decision"] = item["decision"]
                        entry["confirmed"] = True
                        merged += 1
                        break
            if merged:
                facts = [
                    f"{e.get('question')} → {e.get('final_decision')}"
                    for e in (artifact.get("open_questions") or [])
                    if isinstance(e, dict) and e.get("final_decision")
                ]
                if facts:
                    artifact["confirmed_facts"] = facts
                runstore.save_artifact(run_dir, "pm", artifact, note="pm-decisions")
        state = runstore.read_state(run_dir)
        if not state:
            return self._err(409, "该运行没有 state.json，裁决无法保存")
        state["pm_decisions"] = cleaned
        runstore.write_state(run_dir, state)
        # 裁决结果必须**立刻**体现在 PRD 里：否则人点完保存，回头读到的还是那份写着
        # 「人工尚未确认」的旧文档，会以为没保存成功（真机反馈就是这句）。
        # 这里只做纯渲染、不调模型；人工改写过的（`prd.human`）**不覆盖** ——
        # `prd.write` 自带这道判断，返回 None 即表示没写。要强制回到产物驱动，
        # 走页面的「按产物重新生成」按钮。
        detail = runstore.run_detail(run_dir)
        refreshed = prd_mod.write(
            run_dir,
            str(detail.get("requirement") or ""),
            self._prd_scope(run_dir),
            run_id=run_id,
            repo=str((detail.get("env") or {}).get("repo") or ""),
        )
        for item in cleaned:
            self._append_human_action(
                run_dir, action="pm_decision", stage="pm",
                text=f"{item['ref']} → {item['decision']}" if item["ref"] else item["decision"],
                kind="human_directive",
            )
        return self._json(200, {
            "ok": True, "run_id": run_id, "decisions": cleaned, "merged": merged,
            "prd_refreshed": bool(refreshed),
        })

    def _prd_scope(self, run_dir: Path) -> dict:
        """渲染 PRD 用的 scope：以最新 PM 产物为准，再把 state 里的裁决并一次。

        为什么读取时还要并一遍：`_save_pm_decisions` 是**写入时**并回产物的，那一步只要
        因为任何原因没生效（引用对不上是最常见的一种），裁决就只留在 `state.pm_decisions`
        里。裁决的真源是 state、产物只是它的载体 —— 与补强那边 `apply_intake_decisions`
        是同一种加固思路。
        """
        artifact = (runstore.latest_artifacts(run_dir) or {}).get("pm") or {}
        state = runstore.read_state(run_dir) or {}
        scope = prompts_mod.apply_pm_decisions(artifact, state.get("pm_decisions") or [])
        if not isinstance(scope, dict) or not scope:
            # 产物缺失（产物被人工删过 / 旧运行）时退回 state 里那份
            scope = state.get("scope") or {}
        return scope if isinstance(scope, dict) else {}

    def _save_prd(self, run_id: str, run_dir: Path) -> None:
        """PRD 查看/编辑：`prd.md` 是给人看、也给下游用的完整输出文件。

        - `content` 非空 → 人工改写落盘，并放一个 `prd.human` 标记，
          避免流水线后续 `_persist` 又用产物把它覆盖回去（人工白改）；
        - `regenerate` → **当场**按产物重新渲染。

        关于 `regenerate` 的语义变更（真机反馈）：旧实现只删掉 `prd.human` 标记，真正的
        重渲染留给「下次持久化」—— 可是运行一旦暂停或结束，就**再也不会持久化**了，于是
        这个按钮等于一个永远不生效的空操作：人工在页面上逐条裁决完 PM 未决项、点保存，
        PRD 里一个字都没变，人还以为没保存成功。现在直接在服务端渲染落盘（纯渲染，不调模型）。
        """
        payload = self._body()
        if payload.get("regenerate"):
            flag = run_dir / runstore.PRD_HUMAN_FLAG
            if flag.exists():
                flag.unlink()
            detail = runstore.run_detail(run_dir)
            scope = self._prd_scope(run_dir)
            if not scope:
                return self._err(409, "还没有 PM 产物（scope 为空），无法按产物生成 PRD")
            text = prd_mod.write(
                run_dir,
                str(detail.get("requirement") or ""),
                scope,
                run_id=run_id,
                repo=str((detail.get("env") or {}).get("repo") or ""),
                force=True,   # 人工明确点了「按产物重新生成」，此刻覆盖是被要求的
            )
            decided, pending = prd_mod.decided_pending_count(scope)
            self._append_human_action(
                run_dir,
                action="human_edit",
                stage="pm",
                text=f"按产物重新生成 prd.md（已裁决 {decided} 条 / 待裁决 {pending} 条）",
            )
            return self._json(200, {
                "ok": True, "run_id": run_id, "regenerate": True,
                "wrote": bool(text), "decided": decided, "pending": pending,
            })
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            return self._err(400, "content 必须是非空字符串")
        (run_dir / runstore.PRD_NAME).write_text(content, encoding="utf-8")
        (run_dir / runstore.PRD_HUMAN_FLAG).write_text(
            time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8"
        )
        self._append_human_action(run_dir, action="human_edit", stage="pm", text="人工改写 prd.md")
        return self._json(200, {"ok": True, "run_id": run_id, "edited": True})

    # ------------------------------------------------------------------ 删除运行
    def _remove_run_dir(self, run_id: str, run_dir: Path) -> tuple[bool, str]:
        """删除单个运行目录（含 .inbox 孤儿文件）。

        安全约束：只允许删除 `runs_dir` 的**直接子目录**，防止路径穿越误删。
        调用方负责先校验 run_id 合法性、存在性与运行态（运行中不允许删）。
        """
        if run_dir.parent.resolve() != self.runs_dir.resolve():
            return False, "只允许删除 runs 目录下的运行"
        try:
            shutil.rmtree(run_dir)
        except OSError as exc:
            return False, f"删除失败: {exc}"
        # 清理 .inbox 里的孤儿文件（需求原文 .md 与项目类型 .ptype）：
        # 它们是 _create_run 写的、与运行目录同名的伴随文件，不跟着删会越积越多。
        inbox = self.runs_dir / ".inbox"
        for suffix in (".md", ".ptype"):
            extra = inbox / f"{run_id}{suffix}"
            if extra.exists():
                try:
                    extra.unlink()
                except OSError:
                    pass
        with _JOBS_LOCK:
            _JOBS.pop(run_id, None)
        return True, ""

    def _delete_runs_batch(self) -> None:
        """批量删除：POST /api/runs/delete，body {run_ids:[...]}。

        逐条独立处理并回报结果 —— 某条失败（不存在 / 正在运行）不影响其余删除，
        页面据此提示"删了几个、哪几个没删掉"。
        """
        payload = self._body()
        ids = payload.get("run_ids")
        if not isinstance(ids, list) or not ids:
            return self._err(400, "run_ids 必须是非空数组")
        removed: list[str] = []
        failed: list[dict[str, str]] = []
        for raw in ids:
            run_id = str(raw)
            if not RUN_ID_RE.match(run_id):
                failed.append({"run_id": run_id, "error": "非法 run_id"})
                continue
            run_dir = self._run_dir(run_id)
            if run_dir is None:
                failed.append({"run_id": run_id, "error": "找不到该运行"})
                continue
            job = _job(run_id)
            if job and job.get("running"):
                failed.append({"run_id": run_id, "error": "正在运行中，请先中断"})
                continue
            ok, err = self._remove_run_dir(run_id, run_dir)
            if ok:
                removed.append(run_id)
            else:
                failed.append({"run_id": run_id, "error": err})
        return self._json(200, {"ok": not failed, "removed": removed, "failed": failed})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="二次开发需求流水线 · 本地操作页面")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1（仅本机）")
    parser.add_argument("--port", type=int, default=8787, help="监听端口，默认 8787")
    parser.add_argument("--runs-dir", default=str(RUNS_DIR), help=f"runs 目录，默认 {RUNS_DIR}")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # 启动期校验流定义：阶段漏登记（历史真机故障）在这里直接报错，而不是等跑挂
    flow.assert_valid()
    Handler.runs_dir = Path(args.runs_dir)
    Handler.runs_dir.mkdir(parents=True, exist_ok=True)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    rows = runstore.list_runs(Handler.runs_dir)
    print(f"操作页面: {url}")
    print(f"runs 目录: {Handler.runs_dir}（已有 {len(rows)} 次运行）")
    print("Ctrl+C 停止。注意：显存单驻留，同一时刻只跑一个流水线进程。")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
        if _any_running():
            print("正在终止仍在跑的流水线子进程…")
            shutdown_jobs()
    return 0


if __name__ == "__main__":
    sys.exit(main())
