"""单实例文件锁：一个状态库同时只允许一个守护进程在跑。

**为什么必须有**：回写是本产品**唯一改变真实业务状态**的路径（药师确认后写 HIS
审方复核接口）。两台守护进程对着同一个状态库跑，会各自发起回写、推送两遍卡片、
并互相踩批次状态——即使回写幂等键能挡住同一份请求，进程内各自维护的「已回写」
状态仍会让药师端出现重复的复核记录。单机部署时最容易踩的是「运维以为守护已经
停了，用同一条命令又拉起一个」，两个进程对着同一个 HIS 各回写一遍。

**怎么做**：用 ``fcntl.flock`` 对状态库旁的锁文件加**独占非阻塞**锁。

* 锁由**内核**持有：进程退出（含 ``kill -9``、机器断电）时自动释放，
  因此不存在「机器重启留下死锁、谁来都起不来」的老问题；
* 第二个实例拿不到锁就**显式报错退出**，并打印占用进程的信息
  （pid / 主机 / 启动时间 / 命令行），让运维一眼看出「已经有一个在跑了」；
* 互斥语义只依赖 ``flock``，**不依赖锁文件里的内容**——内容被截断、被改成
  乱码都不影响互斥（内容只给人看）。

**源产品能力对应**（spec.md §7 / §8）：源仓库
``backend/src/advocate/connection.py`` 的 ``validating_create_connection()``
——**先校验、再动作**：目标地址不合法就不发请求。本轮把同一纪律用在这一步：
「这个状态库是不是已经有别人在用」没校验过，**根本不起步**，而不是起了再发现
重复回写。

**本轮新建**（源产品无对应实现）：源产品是 Web 服务，靠 gunicorn / Celery 的
worker 数控制并发，没有「单机单实例锁」这个概念；本轮交付形态是**常驻守护
进程**，必须自己守住「一个状态库一个实例」这条线。
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import socket
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = [
    "DaemonLockedError",
    "LockHolder",
    "SingleInstanceLock",
    "holder_of",
    "lock_path_for",
    "read_holder",
]


def lock_path_for(state_db: str | Path) -> Path:
    """状态库路径 → 锁文件路径（``<state_db>.lock``）。

    锁文件跟着状态库走：换状态库 = 换一把锁，不会误判成「别的实例在跑」。
    """
    return Path(f"{state_db}.lock")


@dataclass(frozen=True)
class LockHolder:
    """锁的占用者信息（**给人看的**，互斥不依赖它）。"""

    pid: int = 0
    host: str = ""
    started_at: str = ""
    command: str = ""
    state_db: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 化的字典。"""
        return {
            "pid": self.pid,
            "host": self.host,
            "started_at": self.started_at,
            "command": self.command,
            "state_db": self.state_db,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "LockHolder":
        """从锁文件里的 JSON 还原占用者信息（内容坏了就当不知道）。"""
        if not isinstance(payload, dict):
            return cls()
        try:
            pid = int(payload.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        return cls(
            pid=pid,
            host=str(payload.get("host") or ""),
            started_at=str(payload.get("started_at") or ""),
            command=str(payload.get("command") or ""),
            state_db=str(payload.get("state_db") or ""),
        )

    def describe(self) -> str:
        """一行可读的占用者描述（打印到 stderr）。"""
        bits = [f"pid {self.pid}"]
        if self.host:
            bits.append(f"主机 {self.host}")
        if self.started_at:
            bits.append(f"启动于 {self.started_at}")
        if self.command:
            bits.append(f"命令行 {self.command}")
        return " · ".join(bits)


class DaemonLockedError(RuntimeError):
    """已有另一个守护进程占着这个状态库。"""

    def __init__(self, path: str | Path, holder: LockHolder | None = None) -> None:
        self.path = Path(path)
        self.holder = holder or LockHolder()
        detail = self.holder.describe() if self.holder.pid else "占用进程信息未知"
        super().__init__(
            f"状态库 {self.path} 已被另一个守护进程占用（{detail}）："
            "本进程拒绝启动，避免两个实例对着同一个 HIS 重复回写。"
        )


def read_holder(path: str | Path) -> LockHolder:
    """读锁文件里的占用者信息（**不**加锁；文件不存在或内容坏了就返回空）。"""
    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return LockHolder()
    if not raw:
        return LockHolder()
    try:
        payload = json.loads(raw)
    except ValueError:
        return LockHolder()
    return LockHolder.from_payload(payload)


def holder_of(path: str | Path) -> LockHolder | None:
    """当前锁的占用者；**没人占着**就返回 ``None``。

    探测方式：试拿一把**共享**锁——拿不到说明有人持着独占锁（守护在跑），
    拿得到说明没人占（立刻放掉）。探测窗口只有一次系统调用，不影响正常启动。
    """
    target = Path(path)
    if not target.exists():
        return None
    try:
        fd = os.open(str(target), os.O_RDWR)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return read_holder(target)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        try:
            os.close(fd)
        except OSError:  # pragma: no cover - 关闭失败不影响判断结果
            pass


class SingleInstanceLock:
    """状态库单实例锁（``flock`` 独占非阻塞）。

    用法::

        lock = SingleInstanceLock(lock_path_for(config.state_db))
        lock.acquire()          # 拿不到就抛 DaemonLockedError
        ...
        lock.release()
    """

    def __init__(self, path: str | Path, *, state_db: str | Path = "") -> None:
        self.path = Path(path)
        self.state_db = str(state_db)
        self._fd: int | None = None
        self._holder: LockHolder | None = None

    # -- 状态 ---------------------------------------------------------------

    @property
    def holder(self) -> LockHolder | None:
        """本进程写入的占用者信息（未加锁时为 ``None``）。"""
        return self._holder

    @property
    def acquired(self) -> bool:
        """是否已持有这把锁。"""
        return self._fd is not None

    # -- 加锁 / 解锁 --------------------------------------------------------

    def acquire(self) -> LockHolder:
        """加锁；已被别人占着时抛 :class:`DaemonLockedError`（带占用者信息）。"""
        if self._fd is not None:
            return self._holder or LockHolder()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = read_holder(self.path)
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise DaemonLockedError(self.path, holder) from None
            raise
        holder = LockHolder(
            pid=os.getpid(),
            host=socket.gethostname(),
            started_at=datetime.now().isoformat(timespec="seconds"),
            command=" ".join(sys.argv),
            state_db=self.state_db,
        )
        payload = json.dumps(holder.to_dict(), ensure_ascii=False).encode("utf-8")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload)
        try:
            os.fsync(fd)
        except OSError:  # pragma: no cover - 某些文件系统不支持 fsync
            pass
        self._fd = fd
        self._holder = holder
        return holder

    def release(self) -> None:
        """放锁（幂等；没加过锁时什么都不做）。"""
        fd = self._fd
        self._fd = None
        self._holder = None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - 解锁失败不影响退出
            pass
        try:
            os.close(fd)
        except OSError:  # pragma: no cover
            pass

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
