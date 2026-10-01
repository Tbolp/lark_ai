"""prompt 的读取、校验与占位符渲染。

prompt 是一段**指令**, 和技能正文拼起来作为会话的 system 消息
（见 skills.build_instructions）。里面可以写三个时间占位符, 由 render() 替换:

    {datetime}   2026-09-28 14:03 周一
    {date}       2026-09-28
    {weekday}    周一

替换发生在**每段新对话开始时**（见 harness.ChatSession._ensure_system）, 所以时间不会
冻在进程启动那一刻; 用户那句话仍然是每轮的输入, 不混进指令里。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

_WEEKDAYS = "周一周二周三周四周五周六周日"


def resolve_prompt_path(prompts_dir: str, name: str) -> str:
    """prompt 的完整路径（name 是绝对路径时原样用）。"""
    return name if os.path.isabs(name) else os.path.join(prompts_dir, name)


def check_prompt(prompts_dir: str, name: str) -> str:
    """启动时校验: prompt 文件得存在、能读、非空, 返回完整路径。

    宁可在启动时炸, 也不要等每条 @ 进来才发现没 prompt 可用。
    返回的路径给调用方打日志用。
    """
    path = resolve_prompt_path(prompts_dir, name)
    try:
        with open(path, encoding="utf-8") as fh:
            template = fh.read()
    except OSError as exc:
        raise ValueError(f"读不到 prompt 文件 {path}: {exc}") from exc
    if not template.strip():
        raise ValueError(f"prompt 文件 {path} 是空的")
    return path


def read_prompt(prompts_dir: str, name: str) -> str:
    """读 prompt 内容（原样返回）。

    会话建立时读一次, 所以改了文件, 下一条新会话就用新的 ——
    已经建立的那条会话不受影响（不热加载; 空闲超时清掉上下文后就等于新会话）。
    启动时已经用 check_prompt 校验过一次, 正常情况下这里不会失败。
    """
    path = resolve_prompt_path(prompts_dir, name)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
    except OSError as exc:
        sys.stderr.write(f"[prompt] 读取 {path} 失败 ({exc})\n")
        return ""
    sys.stderr.write(f"[prompt] 读取 {path}（{len(text)} 字符）\n")
    return text


def render(text: str, *, now: datetime | None = None) -> str:
    """把 prompt 里的时间占位符换成"现在"。

    不认识的 `{…}` 原样留着: 宁可让它显眼地留在正文里, 也不要因为一个没定义的
    占位符就让进程起不来。
    """
    moment = now or datetime.now()
    weekday = _WEEKDAYS[moment.weekday() * 2: moment.weekday() * 2 + 2]
    return (text
            .replace("{datetime}", f"{moment.strftime('%Y-%m-%d %H:%M')} {weekday}")
            .replace("{date}", moment.strftime("%Y-%m-%d"))
            .replace("{weekday}", weekday))


def render_prompt(prompts_dir: str, name: str, *, now: datetime | None = None) -> str:
    """读 prompt 文件并渲染占位符（会话建立时调; 读不到时 read_prompt 自己会记一行）。"""
    return render(read_prompt(prompts_dir, name), now=now)
