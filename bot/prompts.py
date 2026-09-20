"""prompt 的读取与启动校验。

prompt 是一段**纯静态的指令**, 原样作为 codex 会话的 developer instructions
（相当于 system prompt）: 里面不填占位符 —— 现在的日期时间 codex 自己知道,
用户那句话则是每轮的输入, 都不该混进指令里。
"""

from __future__ import annotations

import os
import sys


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

    每个 codex 会话建立时读一次, 所以改了文件, 下一条新会话就用新的 ——
    已经建立的那条会话不受影响（不热加载）。
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
