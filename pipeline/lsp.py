"""最小 LSP 客户端：问 pyright「谁引用了这个符号」（JSON-RPC over stdio）。

**为什么必须自己写**：pyright 的 CLI 只输出诊断，``textDocument/references``
只能走 LSP 协议。

**为什么值得写**：本项目的影响面扫描有 ast 版，但 ast 只按**名字**匹配，有两处硬伤：

    u = Unrelated()
    u.move(3)             # ast 记成「Game.move 的上游」—— 同名不同物，纯误报
    from game import Game as G   # ast 匹配不到 G，漏掉这条上游

LSP 靠类型推断能排除前者、补上后者。**注意一个容易想当然的点**（实测纠正）：
ast **能**找到 ``h = make_game(); h.move(2)`` 这种「间接实例」调用 —— 它匹配的是
``.move`` 属性名，不关心 h 的类型；所以 LSP 的增量不是"找到间接调用"，
而是"排除同名误报 + 补别名"。

2026-09-25 实测（5 文件样本）：

    * ``initialize`` 0.3s，首次 ``references`` 命中约 2.9s（轮询第 1~2 轮）；
    * **不需要**逐个 ``didOpen`` —— server 会自己索引工作区，
      只需轮询等它就绪（这一点决定了实现能有多简单）。

**可选增强**：探测不到 langserver、或超出时间预算，就退回 ast 版结果，
绝不让流水线因此失败或长时间挂住。
"""
from __future__ import annotations

import ast
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from . import semantics
from .config import (
    LSP_MAX_SYMBOLS,
    LSP_REFERENCE_BUDGET,
    LSP_REFERENCE_INTERVAL,
    LSP_REFERENCE_ROUNDS,
)

#: Windows 下隐藏子进程窗口
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
#: 单个请求的等待上限（比轮询总时长宽，免得 server 慢一点就被判死）
_REQUEST_TIMEOUT = 30.0


def _frame(obj: dict[str, Any]) -> bytes:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


def _symbol_position(source: str, symbol: str) -> tuple[int, int] | None:
    """用 ast 找某符号**名字**的 (行, 列)，都是 0-based —— LSP 的坐标口径。

    注意不能直接用 ``node.col_offset``：它指向 ``def`` / ``class`` 关键字的开头
    （列 4），而 LSP 要求光标落在**标识符**上（列 8）—— 用错会让 references
    一次都查不到（实测踩过：8 秒轮询完拿回 0 处引用）。

    取不到或同名不止一处时返回 None（引用查找的锚点必须唯一，否则查出来的
    是别人的引用）。
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol:
                hits.append(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == symbol:
                    hits.append(node)
    if len(hits) != 1:
        return None
    node = hits[0]
    lineno = int(getattr(node, "lineno", 0) or 0)
    if lineno <= 0:
        return None
    lines = source.splitlines()
    if lineno > len(lines):
        return None
    # 在该行里定位符号名本身（`def move(` 里的 move），拿不到就退回节点起点
    match = re.search(rf"\b{re.escape(symbol)}\b", lines[lineno - 1])
    char = match.start() if match else int(getattr(node, "col_offset", 0) or 0)
    return lineno - 1, char


def _rel_from_uri(uri: str, root: Path) -> str:
    """把 LSP 的 ``file://`` URI 转成相对 ``root`` 的路径。

    Windows 上 pyright 返回的形如 ``file:///c%3A/Users/ADMINI~1/...`` —— 既做了
    URL 编码、又把用户名压成 8.3 短名，所以 ``Path(...).relative_to(root)`` 必然失败
    （实测就栽在这里，ref_file 直接变成一个 URI 字符串）。

    两条路：先按路径规范化比对；失败就退回「拿 root 最后一段目录名在路径里定位」——
    沙箱目录名（run_id 那层）是唯一的，这个定位可靠。
    """
    try:
        parsed = urlparse(uri)
        if parsed.scheme and parsed.scheme != "file":
            return uri
        raw = url2pathname(unquote(parsed.path or uri))
        path = Path(raw)
    except (ValueError, OSError):
        return uri
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        pass
    text = str(path).replace("\\", "/")
    marker = f"/{root.name}/"
    idx = text.rfind(marker)
    if idx >= 0:
        return text[idx + len(marker):]
    return text


class _Session:
    """一次 LSP server 会话（context manager）。失败时只记 ``error``、不抛。"""

    def __init__(self, entry: Path, cwd: Path, *, root_uri: str) -> None:
        self.entry = entry
        self.cwd = cwd
        self.root_uri = root_uri
        self.proc: subprocess.Popen | None = None
        self.error = ""
        #: 管道断了（server 崩了）：后续请求直接放弃，不再白等超时
        self.broken = False
        self._q: queue.Queue = queue.Queue()
        self._next_id = 1

    # ---------------------------------------------------------------- 生命周期
    def __enter__(self) -> _Session:
        try:
            self.proc = subprocess.Popen(  # noqa: S603
                [str(self.entry), "--stdio"],
                cwd=str(self.cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # stderr 用 DEVNULL：pyright 会往上写日志，没人读的话管道写满会把它卡死
                stderr=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )
        except (OSError, ValueError) as exc:
            self.error = f"启动 langserver 失败：{type(exc).__name__}: {exc}"
            return self
        threading.Thread(target=self._reader, daemon=True).start()
        self._initialize()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                self._send({"jsonrpc": "2.0", "id": self._new_id(), "method": "shutdown",
                            "params": None})
                self._send({"jsonrpc": "2.0", "method": "exit", "params": None})
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.kill()
            except OSError:
                pass
            self.proc = None

    # ---------------------------------------------------------------- 收发
    def _reader(self) -> None:
        stream = self.proc.stdout if self.proc else None
        if stream is None:
            return
        while True:
            length = None
            try:
                while True:
                    line = stream.readline()
                    if not line:
                        self._q.put(None)
                        return
                    line = line.strip()
                    if not line:
                        break
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1].strip())
                if length is None:
                    continue
                data = b""
                while len(data) < length:
                    chunk = stream.read(length - len(data))
                    if not chunk:
                        break
                    data += chunk
                self._q.put(json.loads(data.decode("utf-8")))
            except (OSError, ValueError, json.JSONDecodeError):
                self._q.put(None)
                return

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _send(self, obj: dict[str, Any]) -> None:
        """发一条消息。**失败一律吞掉**：server 中途崩了会抛 BrokenPipeError，
        而这是可选增强 —— 绝不能让它把整条 verify 带崩（实测 pyright 在超大仓库上
        会 OOM 退出，那时管道早已断开）。
        """
        proc = self.proc
        if proc is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(_frame(obj))
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            # 只记错、**不置空 self.proc**：置空会让 close() 拿不到句柄而漏杀进程
            self.error = self.error or f"langserver 管道已断：{type(exc).__name__}"
            self.broken = True

    def _wait(self, msg_id: int, timeout: float) -> dict[str, Any] | None:
        """等指定 id 的响应，跳过服务端主动推送（``window/logMessage`` 等）。"""
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                return None
            try:
                msg = self._q.get(timeout=max(0.05, remain))
            except queue.Empty:
                return None
            if msg is None:
                return None
            if msg.get("id") == msg_id:
                return msg

    def _initialize(self) -> None:
        mid = self._new_id()
        self._send({
            "jsonrpc": "2.0", "id": mid, "method": "initialize",
            "params": {
                "processId": None,
                "rootUri": self.root_uri,
                "capabilities": {},
                "workspaceFolders": [{"uri": self.root_uri, "name": self.cwd.name}],
            },
        })
        if self._wait(mid, _REQUEST_TIMEOUT) is None:
            self.error = self.error or "langserver initialize 超时"
            return
        self._send({"jsonrpc": "2.0", "method": "initialized", "params": {}})

    # ---------------------------------------------------------------- 查询
    def references(self, rel_file: str, line: int, char: int) -> list[dict[str, Any]] | None:
        """查某位置的引用；返回 None 表示「这次没问到」（还可能没索引完）。"""
        if self.proc is None or self.broken:
            return None
        mid = self._new_id()
        self._send({
            "jsonrpc": "2.0", "id": mid, "method": "textDocument/references",
            "params": {
                "textDocument": {"uri": (self.cwd / rel_file).as_uri()},
                "position": {"line": line, "character": char},
                "context": {"includeDeclaration": False},
            },
        })
        resp = self._wait(mid, _REQUEST_TIMEOUT)
        if resp is None or "result" not in resp:
            return None
        result = resp.get("result")
        return result if isinstance(result, list) else None


def find_references(work: Path | str, targets: list[tuple[str, str]]) -> dict[str, Any]:
    """对若干 ``(相对路径, 符号名)`` 查引用。

    一次会话查多个符号（server 启动 + 首次索引是大头，实测约 3s；之后每符号零点几秒）。
    返回结构与 :func:`verify.audit_impact` 对齐，便于合并/对拍。
    """
    out: dict[str, Any] = {
        "available": False,
        "reason": "",
        "queried": 0,
        "references": [],
        "elapsed_s": 0.0,
    }
    root = Path(work)
    entry = semantics.langserver_entry()
    if entry is None:
        out["reason"] = (
            "未找到 pyright-langserver，引用查找回退到 ast"
            "（安装：npm i -g pyright；或设 PIPELINE_LSP=0 显式关闭）"
        )
        return out
    if not root.is_dir():
        out["reason"] = f"目录不存在：{root}"
        return out
    # 必须绝对化：``Path.as_uri()`` 对相对路径直接抛 ValueError，而 orchestrator
    # 传进来的沙箱路径（run_dir / "verify" / "work"）正是相对的 —— 真机跑一次必崩。
    # 单元测试用 mkdtemp 造的是绝对路径，反而照不出这个问题。
    try:
        root = root.resolve()
    except OSError as exc:
        out["reason"] = f"沙箱路径无法解析：{exc}"
        return out
    out["available"] = True

    started = time.time()
    deadline = started + max(5, LSP_REFERENCE_BUDGET)
    refs: list[dict[str, Any]] = []
    # 整段包一层兜底：这是可选增强，任何意外（server 崩、管道断、编码问题）
    # 都只能降级成「没有 LSP 结果」，绝不允许把 verify 带崩。
    try:
        _collect(entry, root, targets, deadline, refs, out)
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"引用查找异常（已降级到 ast）：{type(exc).__name__}: {exc}"
    out["references"] = refs
    out["elapsed_s"] = round(time.time() - started, 2)
    return out


def _collect(
    entry: Path,
    root: Path,
    targets: list[tuple[str, str]],
    deadline: float,
    refs: list[dict[str, Any]],
    out: dict[str, Any],
) -> None:
    """真的去查（拆出来是为了让 find_references 能整体兜异常）。"""
    with _Session(entry, root, root_uri=root.as_uri()) as session:
        if session.error:
            out["reason"] = session.error
            return
        if session.broken:
            out["reason"] = out["reason"] or "langserver 管道已断"
            return
        for rel, symbol in targets[:LSP_MAX_SYMBOLS]:
            if time.time() > deadline:
                out["reason"] = f"超出 {LSP_REFERENCE_BUDGET}s 预算，剩余符号未查"
                break
            source_path = root / rel
            if not source_path.is_file():
                continue
            try:
                source = source_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            pos = _symbol_position(source, symbol)
            if pos is None:
                continue
            line, char = pos
            out["queried"] += 1
            # 索引可能还没好：轮询到有结果为止（实测第 1~2 轮就命中）
            for _ in range(max(1, LSP_REFERENCE_ROUNDS)):
                if time.time() > deadline:
                    break
                locs = session.references(rel, line, char)
                if locs:
                    for loc in locs:
                        ref_rel = _rel_from_uri(str(loc.get("uri") or ""), root)
                        start = (loc.get("range") or {}).get("start") or {}
                        refs.append({
                            "symbol": symbol,
                            "file": rel,
                            "ref_file": ref_rel,
                            # LSP 是 0-based，对外统一成 1-based（和 ast 版一致）
                            "ref_line": int(start.get("line", 0)) + 1,
                        })
                    break
                if not locs and locs is not None:
                    break  # 明确「没有引用」，不必再轮询
                time.sleep(max(0.1, LSP_REFERENCE_INTERVAL))


def summary_line(result: dict[str, Any]) -> str:
    if not result:
        return "引用查找（LSP）：未运行"
    if not result.get("available"):
        return f"引用查找（LSP）：跳过（{result.get('reason') or '不可用'}）"
    if result.get("reason"):
        return f"引用查找（LSP）：{result['reason']}"
    return (
        f"引用查找（LSP）：查了 {result.get('queried', 0)} 个符号，"
        f"拿到 {len(result.get('references') or [])} 处引用，{result.get('elapsed_s', 0)}s"
    )


def _selftest() -> int:
    """直接跑 `python -m pipeline.lsp` 自检（会用临时目录造一个间接调用样本）。"""
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="lsp_selftest_"))
    (root / "game.py").write_text(
        "class Game:\n    def move(self, x):\n        return x\n\n\ndef make_game():\n    return Game()\n",
        encoding="utf-8",
    )
    (root / "use.py").write_text(
        "from game import Game, make_game\n\ng = Game()\ng.move(1)\nh = make_game()\nh.move(2)\n",
        encoding="utf-8",
    )
    print("langserver:", semantics.langserver_entry() or "(未找到)")
    res = find_references(root, [("game.py", "move")])
    print(summary_line(res))
    for item in res.get("references") or []:
        print(f"   {item['symbol']} <- {item['ref_file']}:{item['ref_line']}")
    print("预期：use.py 的 2 处引用，其中一行是 make_game() 返回值的间接调用")
    return 0 if len(res.get("references") or []) >= 2 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_selftest())
