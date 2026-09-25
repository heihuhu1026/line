"""本地配置覆盖：`pipeline/config.local.json` 的读写。

为什么要有它
------------
操作页面会把流水线跑成**子进程**（`server._spawn` → `python -m pipeline.cli`），
模型和提示词是在那个子进程里被消费的。因此页面上的改动必须**落盘**，
由 `config.py` / `prompts.py` 在模块初始化末尾读取应用 ——
这样任何 `from .config import X` 都拿到覆盖后的值，无需调用方配合。

反过来：**不要在别处替换配置对象**。`orchestrator` 是用
`from .config import (DEV_TWO_PASS, MAX_REWORK_ROUNDS, ...)` 绑定值的，
晚一步执行覆盖，它们手里那份就还是代码默认值（踩过这类坑，务必注意）。

设计约定
--------
* 键缺失 = 沿用代码默认值：升级流水线新增参数后，旧配置文件不会失效。
* 保存是**局部合并**：只覆盖传入的键，其余保留；值为 `None` 表示删除该覆盖。
* 读失败一律降级为「空覆盖」：配置文件写坏了不能导致流水线起不来。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PATH = Path(os.getenv("PIPELINE_LOCAL_CONFIG", str(Path(__file__).resolve().with_name("config.local.json"))))

VERSION = 1
SCOPES = ("models", "prompts", "runtime", "guard")

#: 入口总闸（gateway）的默认值：键缺失即取这里。
#: ``mode``   —— auto（先做零模型调用的预判，疑似大型才调 GA）/ always / off
#: ``scale``  —— 人工强制 small|large（"" = 不强制），用于判错时纠正而不必改代码
#: ``forbidden_paths`` —— 全局禁区清单（GA 的输入来源；见 gateway.forbidden_paths）
GUARD_DEFAULTS: dict[str, Any] = {"mode": "auto", "scale": "", "forbidden_paths": []}

_CACHE: dict[str, Any] | None = None


def path() -> Path:
    return PATH


def load(force: bool = False) -> dict[str, Any]:
    """读取覆盖内容。文件缺失或损坏时返回空覆盖（降级到代码默认值）。"""
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    data: dict[str, Any] = {}
    try:
        parsed = json.loads(PATH.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            data = parsed
        else:
            print(f"[local_config] {PATH} 内容不是对象，已忽略")
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as exc:
        # 配置坏了不能让流水线起不来，降级 + 留痕即可
        print(f"[local_config] 读取 {PATH} 失败，本次使用代码默认值：{exc}")
        data = {}
    _CACHE = data
    return data


def _node(scope: str) -> dict[str, Any]:
    value = load().get(scope)
    return value if isinstance(value, dict) else {}


def models() -> dict[str, dict[str, Any]]:
    """按阶段覆盖的模型参数：{"pm": {"tag": ..., "num_ctx": ...}, ...}"""
    return {k: v for k, v in _node("models").items() if isinstance(v, dict)}


def prompts() -> dict[str, str]:
    """按角色覆盖的系统提示词：{"pm": "完整提示词", ...}"""
    return {k: v for k, v in _node("prompts").items() if isinstance(v, str)}


def runtime() -> dict[str, Any]:
    """运行时参数覆盖：{"review_every": 1, "max_rework": 3, ...}"""
    return dict(_node("runtime"))


def guard() -> dict[str, Any]:
    """入口总闸（gateway）配置：{"mode": "auto", "scale": "", "forbidden_paths": [...]}。

    逐键合并默认值：缺键 = 沿用 ``GUARD_DEFAULTS``，所以升级新增键不会让旧配置失效。
    """
    merged = dict(GUARD_DEFAULTS)
    saved = _node("guard")
    for key, value in saved.items():
        merged[key] = value
    if not isinstance(merged.get("forbidden_paths"), list):
        merged["forbidden_paths"] = []
    return merged


def save(patch: dict[str, Any]) -> dict[str, Any]:
    """局部合并保存。传 `None` 表示移除该项覆盖（退回代码默认值）。"""
    current = copy.deepcopy(load())
    for scope in SCOPES:
        incoming = patch.get(scope)
        if not isinstance(incoming, dict) or not incoming:
            continue
        node = current.get(scope)
        if not isinstance(node, dict):
            node = {}
            current[scope] = node
        for key, value in incoming.items():
            if value is None:
                # None = 删除该项覆盖，退回代码默认值
                node.pop(key, None)
            elif isinstance(value, dict):
                # 二级合并（models.<stage>.<field> / budgets.<name>.<stage>）：
                # 字段级 None 表示「该字段无覆盖，删除它」。
                # 注意目标不存在时也要走这条分支 —— 否则首次保存会把整块 {字段: None}
                # 原样落盘，留下一个全是 null 的空壳（点击保存就凭空产生覆盖）。
                target = node.get(key)
                if not isinstance(target, dict):
                    target = {}
                    node[key] = target
                for sub_key, sub_value in value.items():
                    if sub_value is None:
                        target.pop(sub_key, None)
                    else:
                        target[sub_key] = sub_value
                if not target:
                    node.pop(key, None)  # 该层已无覆盖，整个键删掉
            else:
                node[key] = value
    # 清掉空壳：全部回退到默认值后不该在文件里留下 {}
    for name in SCOPES:
        if name in current and not current[name]:
            current.pop(name)
    current["version"] = VERSION
    _atomic_write(current)
    global _CACHE
    _CACHE = current
    return current


def clear(scope: str | None = None) -> dict[str, Any]:
    """清空全部覆盖，或只清空指定 scope（models / prompts / runtime）。"""
    current = copy.deepcopy(load())
    if scope:
        current.pop(scope, None)
    else:
        for name in SCOPES:
            current.pop(name, None)
    current["version"] = VERSION
    _atomic_write(current)
    global _CACHE
    _CACHE = current
    return current


def _atomic_write(data: dict[str, Any]) -> None:
    """先写临时文件再 replace：写一半崩了不会留下截断的配置。"""
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_name(PATH.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, PATH)  # Windows 上 os.replace 覆盖已有文件是允许的
