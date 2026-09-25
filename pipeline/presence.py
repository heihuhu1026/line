"""进程「在场」标记：让「某个运行是否真的在跑」不再只依赖服务端的内存注册表。

问题（真机踩到，2026-09-25）
--------------------------
``server._any_running()`` 只遍历内存里的 ``_JOBS``。而跑流水线的是**独立子进程**，
服务端一重启（比如换代码加载），子进程还活着、注册表却空了。于是：

* 页面把这个运行显示成「已中断」、续跑按钮是亮的 —— 点下去会起**第二个**进程，
  两个同时写同一个运行目录，还都抢显存；
* ``busy_with`` 为空 → 「已有运行在进行中」的并发防护失效，能再起一个运行；
* 裁决参谋的显存互斥判断也跟着失真（同一个 ``busy_with``）。

修法
----
跑流水线的**子进程自己**在运行目录写一份在场标记（pid + 心跳），服务端读到且判定存活
才算「在跑」。**由子进程写而不是服务端写**是关键：服务端会重启、子进程不会 —— 这正是
要覆盖的场景。

存活判定 = **pid 还活着** 且 **心跳足够新**。
  * pid 检查让「进程被杀」立刻被发现（不用等心跳过期）；
  * 心跳让「pid 被系统复用给了别的进程」不会误判成活（复用者不会维护这份心跳）；
  * 心跳由 daemon 线程每 ``BEAT_S`` 秒刷一次，与模型调用无关（模型调用只阻塞主线程）。

标记文件放在运行目录下的 ``.running.json``：它不匹配 ``runstore.STAGE_FILE_RE``
（那要求文件名以数字开头），所以不会被当成阶段快照、不影响产物读取。
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from . import runstore

#: 标记文件名（隐藏 + 数字开头才能当快照，二者都满足「不会混进产物」）
NAME = ".running.json"
#: 心跳间隔（秒）
BEAT_S = 5.0
#: 心跳容忍上限（秒）。留得足够宽松：只用来兜「pid 复用」与「机器卡顿」，
#: 真正判定「进程没了」靠 pid 检查，不依赖这个值。
TTL_S = 120.0
#: 扫描层数上限：``runs/`` → 运行目录 / `_jobs/` → 作业目录 → 作业下的模块目录。
#: 不做全树递归 —— ``scan`` 会被页面轮询调到（``/api/runs`` 每 2.5s），
#: 而运行目录里可能躺着 verify 沙箱这类大目录，全树 rglob 会随文件数线性变慢。
MAX_DEPTH = 3
#: 扫描时最多看多少个目录（防御异常目录结构，避免一次轮询卡住）
MAX_DIRS = 300
#: 作业目录名（与 ``gateway.JOBS_DIRNAME`` 一致；这里不 import gateway，
#: 避免 gateway → orchestrator → presence 的循环依赖）
JOBS_DIRNAME = "_jobs"


def path(run_dir: str | Path) -> Path:
    return Path(run_dir) / NAME


# --------------------------------------------------------------------- 进程探活
def pid_alive(pid: int) -> bool:
    """该 pid 是否对应一个**还活着**的进程。

    ⚠️ Windows 上**绝不能用** ``os.kill(pid, 0)`` 探活：CPython 在 Windows 对
    ``os.kill`` 的实现是「除 CTRL_C_EVENT / CTRL_BREAK_EVENT 之外，一律调
    TerminateProcess」—— 传 0 会把目标进程**直接杀掉**，探活变成误杀。
    这里用 OpenProcess + GetExitCodeProcess（只查询、无副作用）。
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # 进程存在，只是不属于当前用户
        except OSError:
            return False
        return True

    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def terminate(pid: int) -> bool:
    """按 pid 结束进程（用于「服务端不认识这个孩子」时的中断）。返回是否成功。"""
    if not pid_alive(pid):
        return False
    if os.name != "nt":
        import signal

        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except OSError:
            return False

    import ctypes

    PROCESS_TERMINATE = 0x0001
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = k32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not handle:
        return False
    try:
        return bool(k32.TerminateProcess(handle, 1))
    finally:
        k32.CloseHandle(handle)


# --------------------------------------------------------------------- 读写
def write(run_dir: str | Path, stage: str = "", run_id: str | None = None,
          pid: int | None = None) -> dict[str, Any]:
    """写一份新的在场标记（当前进程即「跑流水线的那个进程」）。"""
    run_dir = Path(run_dir)
    now = time.time()
    payload: dict[str, Any] = {
        "pid": int(pid if pid is not None else os.getpid()),
        "run_id": str(run_id or run_dir.name),
        "stage": str(stage or ""),
        "started_at": now,
        "heartbeat": now,
    }
    runstore.write_json(path(run_dir), payload)
    return payload


def touch(run_dir: str | Path, stage: str | None = None) -> None:
    """刷心跳（保留 pid 与 started_at）。标记不存在时补写一份。"""
    run_dir = Path(run_dir)
    cur = runstore.read_json_if_exists(path(run_dir))
    if not isinstance(cur, dict) or not cur.get("pid"):
        write(run_dir, stage=stage or "")
        return
    cur["heartbeat"] = time.time()
    if stage is not None:
        cur["stage"] = str(stage)
    runstore.write_json(path(run_dir), cur)


def read(run_dir: str | Path) -> dict[str, Any] | None:
    """读在场标记；**只有判定为「真的还在跑」时才返回**，否则返回 None。

    判定：pid 还活着 且 心跳在 ``TTL_S`` 内。任一不满足都当成「没有在跑」——
    残留的标记文件（进程被强杀时留下的）不该让人以为还在跑。
    """
    return _live(Path(run_dir))


def _live(run_dir: Path) -> dict[str, Any] | None:
    payload = runstore.read_json_if_exists(path(run_dir))
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    beat = payload.get("heartbeat")
    if not isinstance(pid, int) or not isinstance(beat, (int, float)):
        return None
    if time.time() - float(beat) > TTL_S:
        return None
    if not pid_alive(pid):
        return None
    return payload


def clear(run_dir: str | Path, pid: int | None = None) -> None:
    """删掉标记（默认只删**当前进程**写的那份，避免误删别人的）。"""
    run_dir = Path(run_dir)
    target = path(run_dir)
    try:
        payload = runstore.read_json_if_exists(target)
        if isinstance(payload, dict) and pid is not None and payload.get("pid") != pid:
            return
        target.unlink(missing_ok=True)
    except OSError:
        pass


def scan(root: str | Path) -> dict[str, dict[str, Any]]:
    """扫描根目录下**所有活着的**在场标记，返回 ``{run_id: payload}``。

    只走固定几层（见 ``MAX_DEPTH``）：运行目录、``_jobs/<作业>``、``_jobs/<作业>/<模块>``。
    """
    out: dict[str, dict[str, Any]] = {}
    root = Path(root)
    if not root.exists():
        return out
    visited = 0
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            entries = list(cur.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            visited += 1
            if visited > MAX_DIRS:
                return out
            found = _live(entry)
            if found:
                out[str(found.get("run_id") or entry.name)] = found
            if depth + 1 < MAX_DEPTH:
                stack.append((entry, depth + 1))
    return out


def describe(payload: dict[str, Any] | None) -> str:
    """给日志/提示用的一句话，如 ``外部进程 pid=6376（阶段 dev）``。"""
    if not payload:
        return ""
    stage = payload.get("stage") or "?"
    return f"pid={payload.get('pid')}（阶段 {stage}）"


# --------------------------------------------------------------------- 心跳维持
class Guard:
    """运行期间维持在场标记：起一次心跳线程，退出时清掉。

    用法（``orchestrator._execute`` 里）::

        guard = presence.Guard(self.run_dir, stage=self.cursor).start()
        ...
        guard.stage = self.cursor     # 阶段推进时更新，页面能看到「在跑哪个阶段」
        guard.stop()

    心跳线程是 daemon：进程被强杀时不会拖住退出；标记文件残留也不影响判定
    （``read()`` 会校验 pid 与心跳）。
    """

    def __init__(self, run_dir: str | Path, stage: str = "", interval: float = BEAT_S) -> None:
        self.run_dir = Path(run_dir)
        self.interval = max(1.0, float(interval))
        self._stage = str(stage or "")
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self.payload: dict[str, Any] | None = None

    def start(self) -> Guard:
        try:
            self.payload = write(self.run_dir, stage=self._stage)
        except OSError:
            return self  # 标记写不出来不该让流水线跑不起来
        self._thread = threading.Thread(target=self._beat, name="presence-heartbeat", daemon=True)
        self._thread.start()
        return self

    def _beat(self) -> None:
        while not self._halt.wait(self.interval):
            try:
                touch(self.run_dir, self._stage)
            except OSError:
                pass

    @property
    def stage(self) -> str:
        return self._stage

    @stage.setter
    def stage(self, value: Any) -> None:
        self._stage = str(value or "")
        try:
            touch(self.run_dir, self._stage)
        except OSError:
            pass

    def stop(self) -> None:
        self._halt.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        # 只删自己写的那份：万一同目录里有别的 Guard（不该有，但别误删）
        clear(self.run_dir, pid=os.getpid())


__all__ = [
    "NAME", "BEAT_S", "TTL_S", "Guard", "clear", "describe", "path", "pid_alive",
    "read", "scan", "terminate", "touch", "write",
]
