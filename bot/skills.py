"""技能: 解析、校验、注入。

一个"技能"就是一份 `SKILL.md`（front matter + 指令正文）加一个可选的 `references/`
目录。这里干三件事:

  load_skills()          配置里的技能名 -> Skill（正文已经读好, 参考文档已登记）
  build_instructions()   把 prompt 文件和各技能正文拼成一段**静态**指令
  Skill.references       references/*.md 的白名单（文件名词干 -> 路径）

和 codex 那套技能机制的区别, 是这次重构的重点:

  codex 每轮把**所有**被发现技能的 name + description + path 塞进 prompt, 正文按需读;
  这里相反 —— 只有 config.json 里点名的技能进 prompt, 而且正文一次性常驻。

代价是改 SKILL.md 得重启进程才生效; 好处是 prompt 里有什么完全由配置决定, 没有
隐藏的发现机制（不会因为用户目录里多装了一个技能就悄悄多出几千 token）。

技能从 `~/.agents/skills`（用户级）或项目下的 `.agents/skills`（仓库级）里找, 按名字
命中第一个; 两处都没有就在启动时报错（宁可现在炸, 也不要每条分析都少一份指令）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable

# 技能搜索目录: 用户级 / 项目级（与 codex 的发现根一致, 便于复用同一批技能包）
SKILL_ROOTS = ("~/.agents/skills", ".agents/skills")

# front matter: 文件开头的 `---\n...\n---`（name / description 那一块）
_FRONT_MATTER = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)


@dataclass(frozen=True)
class Skill:
    """一个已加载的技能: SKILL.md 的正文 + references 白名单。"""

    name: str
    directory: str          # 技能目录
    path: str               # SKILL.md 的完整路径（日志用）
    body: str               # 去掉 front matter 的正文（进 prompt 的就是它）
    references: dict[str, str]   # references/*.md 的词干 -> 完整路径


def _skill_md(name: str, *, project_dir: str) -> str:
    """按名字找 SKILL.md; 找不到抛 ValueError（启动时就炸）。"""
    roots = [os.path.expanduser(SKILL_ROOTS[0]), os.path.join(project_dir, SKILL_ROOTS[1])]
    for root in roots:
        path = os.path.join(root, name, "SKILL.md")
        if os.path.isfile(path):
            return path
    raise ValueError(f"找不到技能 {name}（找过 {'、'.join(roots)}）")


def _references(directory: str) -> dict[str, str]:
    """references/ 下的 markdown: 词干 -> 路径。

    用扫目录而不是写死清单, 技能包加了参考文档就自动可用; 但模型能读到的仍然只有
    这里登记过的文件（read_reference 只认这张表, 路径不来自模型）。
    """
    ref_dir = os.path.join(directory, "references")
    if not os.path.isdir(ref_dir):
        return {}
    found: dict[str, str] = {}
    for entry in sorted(os.listdir(ref_dir)):
        if not entry.endswith(".md"):
            continue
        path = os.path.join(ref_dir, entry)
        if os.path.isfile(path):
            found[entry[:-3]] = path
    return found


def load_skills(names: Iterable[str], *, project_dir: str) -> list[Skill]:
    """把技能名解析成 Skill 列表（读正文、登记参考文档）; 有一个找不到就报错。"""
    skills: list[Skill] = []
    for name in names:
        path = _skill_md(name, project_dir=project_dir)
        directory = os.path.dirname(path)
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            raise ValueError(f"读不到技能 {name} 的 {path}: {exc}") from exc
        body = _FRONT_MATTER.sub("", raw).strip()
        if not body:
            raise ValueError(f"技能 {name} 的 {path} 没有正文（只有 front matter？）")
        skills.append(Skill(name=name, directory=directory, path=path, body=body,
                            references=_references(directory)))
    return skills


def build_instructions(prompt: str, skills: Iterable[Skill]) -> str:
    """prompt 文件 + 各技能正文, 拼成一段静态指令（会话建立时用一次）。

    技能正文带着标题分隔, 免得两段指令首尾相接分不清界限。
    """
    parts = [prompt.strip()]
    for skill in skills:
        parts.append(f"===== 技能：{skill.name} =====\n\n{skill.body}")
    return "\n\n".join(part for part in parts if part)


def reference_table(skills: Iterable[Skill]) -> dict[tuple[str, str], str]:
    """(技能名, 参考文档名) -> 路径。两个技能里有同名文档（ai_usage_guide 等）, 内容不同,
    所以键必须带上技能名 —— 只按文件名索引一定会串。"""
    return {(skill.name, name): path
            for skill in skills
            for name, path in skill.references.items()}
