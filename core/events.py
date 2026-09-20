"""事件注册与发射: 消费者用 on() 注册, 生产者用 emit() 触发。

语义:
  - 同步 handler: 在 emit 的调用点当场执行, emit 返回时它一定已经跑完;
  - 异步 handler（协程函数）: 只被调度起来, emit 不等它;
  - 两种 handler 按注册顺序被调用; 但异步的完成顺序不保证;
  - 每个 handler 单独隔离异常: 一个坏了不影响同一事件的其他 handler, 也不影响 emit;
  - 异步 handler 由这里保活（持有引用、异常不吞、退出时统一收尾）。

框架自己会发的事件: "task.done" —— 异步 handler 结束时（payload 是 Done）。
框架保留 "task.*" 这个名字空间, 业务事件名随便取, 框架不解释。

靠调用方遵守的两条约定（框架不强制）:
  - 同步 handler 里绝对不要阻塞: 它是当场执行的, 一个 time.sleep 就能把整个
    事件循环卡住;
  - 注册集中在一处（启动时）, 用 registered() 可以把"事件 -> handler"打出来。
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable

# 框架自己发的事件名
TASK_DONE = "task.done"

_HANDLERS: dict[str, list[Callable[[Any], Any]]] = {}
_TASKS: set[asyncio.Task] = set()


@dataclass(frozen=True)
class Done:
    """一个异步 handler 的结局。"""

    name: str
    status: str          # "ok" | "failed" | "cancelled"
    elapsed: float
    error: BaseException | None = None


# ---------- 注册与发射 ----------

def on(name: str, handler: Callable[[Any], Any]) -> None:
    """注册一个 handler。同步函数当场执行, 协程函数被调度起来（不等它）。"""
    _HANDLERS.setdefault(name, []).append(handler)


def emit(name: str, payload: Any = None) -> None:
    """触发一个事件（同步函数, 可以在协程里直接调）。"""
    for handler in list(_HANDLERS.get(name) or ()):
        try:
            result = handler(payload)
        except Exception as exc:            # 一个 handler 坏了不影响别人
            _log(f"处理 {name} 的 {_name_of(handler)} 抛异常: {exc!r}")
            continue
        if inspect.isawaitable(result):
            _start(result, name=f"{name}:{_name_of(handler)}")


def registered() -> dict[str, list[str]]:
    """现在注册了哪些事件、各有几个 handler（启动时打一行日志用）。"""
    return {name: [_name_of(h) for h in handlers]
            for name, handlers in _HANDLERS.items()}


# ---------- 后台协程的生命周期 ----------

async def cancel_all() -> None:
    """取消所有还在跑的协程, 并等它们各自收尾（进程退出前调用）。

    调用方的 finally 会照常执行（比如杀掉自己起的子进程）。
    主进程被 SIGKILL 时这里根本没机会跑, 不在保证范围内。
    """
    pending = list(_TASKS)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# ---------- 内部 ----------

def _start(coro, *, name: str) -> None:
    started = time.monotonic()
    task = asyncio.create_task(coro, name=name)
    _TASKS.add(task)
    task.add_done_callback(lambda t: _finish(name, started, t))


def _finish(name: str, started: float, task: asyncio.Task) -> None:
    _TASKS.discard(task)
    if task.cancelled():
        status, error = "cancelled", None
    elif (exc := task.exception()) is not None:
        status, error = "failed", exc
        _log(f"{name} 异常退出: {exc!r}")
    else:
        status, error = "ok", None
    emit(TASK_DONE, Done(name=name, status=status,
                         elapsed=time.monotonic() - started, error=error))


def _name_of(handler) -> str:
    if isinstance(handler, functools.partial):        # 用 partial 绑了上下文的情况
        return _name_of(handler.func)
    return (getattr(handler, "__name__", None)                # 函数: 短名字最好读
            or getattr(type(handler), "__qualname__", None)    # 可调用对象: 类名
            or repr(handler))


def _log(text: str) -> None:
    sys.stderr.write(f"[core] {text}\n")
