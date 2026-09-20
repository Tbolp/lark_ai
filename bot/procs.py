"""子进程: 收尾与输出解码。

起进程的地方各写各的（谁起谁负责）, 但"怎么把一整棵进程树收干净"和
"子进程输出怎么解码"只有这一份, 免得那个 killpg 的坑被抄成好几份。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal


def decode_bytes(raw: bytes) -> str:
    """子进程输出按 UTF-8 解码。"""
    return raw.decode("utf-8", errors="replace")


def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """收掉子进程及其派生的进程（单独进程组, 避免超时后还留着后台命令）。"""
    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(OSError):
            proc.kill()


async def stop_process(proc: asyncio.subprocess.Process, *, timeout: float = 10.0) -> int | None:
    """强杀子进程组并等它退出, 返回退出码。"""
    _kill_process_tree(proc)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout)
    return proc.returncode
