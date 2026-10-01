"""prompt 的占位符渲染（render / render_prompt）与读取失败时的行为。"""

from __future__ import annotations

from datetime import datetime

from bot.prompts import render, render_prompt


def test_render_replaces_the_three_placeholders():
    moment = datetime(2026, 9, 28, 14, 3)                 # 周一
    text = render("现在是 {datetime}（{weekday}）; 日期 {date}", now=moment)
    assert text == "现在是 2026-09-28 14:03 周一（周一）; 日期 2026-09-28"


def test_render_keeps_unknown_placeholders():
    assert render("{nope} 还在", now=datetime(2026, 9, 28)) == "{nope} 还在"


def test_render_weekday_covers_the_week():
    assert render("{weekday}", now=datetime(2026, 9, 28)) == "周一"
    assert render("{weekday}", now=datetime(2026, 9, 27)) == "周日"
    assert render("{weekday}", now=datetime(2026, 10, 3)) == "周六"


def test_render_prompt_reads_and_renders(tmp_path):
    (tmp_path / "p.md").write_text("现在是 {datetime}\n\n正文", encoding="utf-8")
    text = render_prompt(str(tmp_path), "p.md", now=datetime(2026, 9, 28, 8, 5))
    assert text == "现在是 2026-09-28 08:05 周一\n\n正文"


def test_render_prompt_on_missing_file_is_empty(tmp_path):
    # 正常情况下启动时已经用 check_prompt 校验过; 这里只保证读不到不会炸
    assert render_prompt(str(tmp_path), "nope.md") == ""
