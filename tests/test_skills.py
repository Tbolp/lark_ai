"""技能解析（load_skills / build_instructions / reference_table）。"""

from __future__ import annotations

import os

import pytest

from bot.skills import build_instructions, load_skills, reference_table


def make_skill(root, name: str, *, body: str = "# 技能正文", refs: dict | None = None):
    """在项目级技能根下造一个技能目录。"""
    directory = root / ".agents" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 这是给发现机制看的\n---\n\n{body}\n",
        encoding="utf-8")
    for ref_name, text in (refs or {}).items():
        ref_dir = directory / "references"
        ref_dir.mkdir(exist_ok=True)
        (ref_dir / f"{ref_name}.md").write_text(text, encoding="utf-8")
    return str(directory / "SKILL.md")


def test_load_skills_reads_body_and_strips_front_matter(tmp_path):
    path = make_skill(tmp_path, "demo", body="# 正文\n\n用 westock 取数。")
    skill, = load_skills(["demo"], project_dir=str(tmp_path))
    assert skill.name == "demo"
    assert skill.path == path
    assert skill.body.startswith("# 正文")
    assert "description" not in skill.body          # front matter 不进 prompt


def test_load_skills_registers_references(tmp_path):
    make_skill(tmp_path, "demo", refs={"commands": "命令清单", "routing-guide": "路由"})
    skill, = load_skills(["demo"], project_dir=str(tmp_path))
    assert set(skill.references) == {"commands", "routing-guide"}
    assert all(os.path.isfile(p) for p in skill.references.values())


def test_load_skills_without_references_is_fine(tmp_path):
    make_skill(tmp_path, "demo")
    skill, = load_skills(["demo"], project_dir=str(tmp_path))
    assert skill.references == {}


def test_load_skills_errors_when_missing(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        load_skills(["nope"], project_dir=str(tmp_path))
    assert "nope" in str(excinfo.value)


def test_load_skills_errors_on_empty_body(tmp_path):
    directory = tmp_path / ".agents" / "skills" / "blank"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("---\nname: blank\n---\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_skills(["blank"], project_dir=str(tmp_path))


def test_build_instructions_puts_each_skill_under_a_header(tmp_path):
    make_skill(tmp_path, "one", body="第一份指令")
    make_skill(tmp_path, "two", body="第二份指令")
    skills = load_skills(["one", "two"], project_dir=str(tmp_path))
    text = build_instructions("机器人指令", skills)
    assert text.index("机器人指令") < text.index("技能：one") < text.index("技能：two")
    assert "第一份指令" in text and "第二份指令" in text


def test_reference_table_is_namespaced_by_skill(tmp_path):
    """同名参考文档（ai_usage_guide 之类）在两份技能里内容不同, 键必须带上技能名。"""
    make_skill(tmp_path, "one", refs={"guide": "one 的说明"})
    make_skill(tmp_path, "two", refs={"guide": "two 的说明"})
    skills = load_skills(["one", "two"], project_dir=str(tmp_path))
    table = reference_table(skills)
    assert set(table) == {("one", "guide"), ("two", "guide")}
    assert open(table[("one", "guide")], encoding="utf-8").read() == "one 的说明"
    assert open(table[("two", "guide")], encoding="utf-8").read() == "two 的说明"
