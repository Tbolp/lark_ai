"""状态库（StateStore）: 关注列表 / 私聊会话表 / 推送清单 / 失败计数。

用真的 SQLite 写在临时目录里（不 mock）: 这一层要保证的正是"写下去真的还在"。
"""

from __future__ import annotations

import sqlite3

import pytest

from bot.store import MAX_WATCH, StateStore


class Clock:
    """可手动推进的假时钟（过期清理、冷却节流都靠它测）。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_store(tmp_path, clock: Clock | None = None) -> StateStore:
    return StateStore(str(tmp_path / "state" / "state.db"), now=clock or Clock())


# ---------- 建库 ----------

def test_creates_directory_and_schema(tmp_path):
    clock = Clock()
    path = tmp_path / "nested" / "state.db"
    StateStore(str(path), now=clock)
    assert path.is_file()
    with sqlite3.connect(path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert version == 1
    assert {"watchlist", "peers", "pushes", "timer_health"} <= tables
    StateStore(str(path), now=clock)          # 幂等: 再建一次不炸


def test_rejects_a_file_that_is_not_a_database(tmp_path):
    path = tmp_path / "state.db"
    path.write_bytes(b"definitely not a sqlite database" * 20)
    with pytest.raises(ValueError) as excinfo:
        StateStore(str(path))
    assert "状态库" in str(excinfo.value)


# ---------- 关注列表 ----------

def test_watch_add_list_remove(tmp_path):
    store = make_store(tmp_path)
    ok, message = store.add_watch("oc_g", "sh600519", "贵州茅台")
    assert ok and "已关注 sh600519 贵州茅台" in message and "共 1 只" in message
    assert store.watch("oc_g") == [{"code": "sh600519", "name": "贵州茅台",
                                    "added_at": 1000.0}]
    assert store.describe("oc_g") == "sh600519 贵州茅台"

    ok, message = store.remove_watch("oc_g", "sh600519")
    assert ok and "已取消关注 sh600519 贵州茅台" in message and "共 0 只" in message
    assert store.watch("oc_g") == []
    assert store.describe("oc_g") == "（还没有关注任何股票）"


def test_watch_is_per_chat(tmp_path):
    store = make_store(tmp_path)
    store.add_watch("oc_group", "sh600519", "贵州茅台")
    store.add_watch("oc_p2p", "sz300750", "宁德时代")
    assert [item["code"] for item in store.watch("oc_group")] == ["sh600519"]
    assert [item["code"] for item in store.watch("oc_p2p")] == ["sz300750"]
    assert store.remove_watch("oc_p2p", "sh600519") == (False, "关注列表里没有 sh600519。")


def test_watch_rejects_bad_codes_and_duplicates(tmp_path):
    store = make_store(tmp_path)
    for bad in ("600519", "sh600519;rm", "SH600519", "", "sh"):
        ok, message = store.add_watch("oc_g", bad, "")
        assert not ok and "不合法" in message, bad
    assert store.add_watch("oc_g", "hk00700", "腾讯控股")[0]
    ok, message = store.add_watch("oc_g", "hk00700", "腾讯控股")
    assert not ok and "已经在关注列表里" in message
    assert store.add_watch("oc_g", "usAAPL.OQ", "苹果")[0]      # 美股代码也认
    assert store.add_watch("oc_g", "pt01801080", "半导体")[0]   # 板块代码也认


def test_watch_caps_each_chat(tmp_path):
    store = make_store(tmp_path)
    for index in range(MAX_WATCH):
        assert store.add_watch("oc_g", f"sh6005{index:02d}", "")[0]
    ok, message = store.add_watch("oc_g", "sh600999", "")
    assert not ok and str(MAX_WATCH) in message
    assert len(store.watch("oc_g")) == MAX_WATCH


def test_watch_name_is_single_line_and_limited(tmp_path):
    store = make_store(tmp_path)
    store.add_watch("oc_g", "sh600519", "贵州\n茅台\t股份  有限公司 とても長い名前です")
    name = store.watch("oc_g")[0]["name"]
    assert "\n" not in name and "\t" not in name and len(name) <= 20


def test_watch_without_chat_id_is_rejected(tmp_path):
    store = make_store(tmp_path)
    assert store.add_watch("", "sh600519", "贵州茅台")[0] is False
    assert store.remove_watch("", "sh600519")[0] is False


# ---------- 私聊会话表 ----------

def test_peers_upsert_and_read(tmp_path):
    store = make_store(tmp_path, Clock(1000.0))
    store.remember_peer("ou_a", "oc_a")
    store.remember_peer("ou_b", "oc_b")
    store.remember_peer("ou_a", "oc_a2")          # 同一个人 -> 更新成新的 chat_id
    assert store.peers() == {"ou_a": "oc_a2", "ou_b": "oc_b"}
    store.remember_peer("", "oc_x")               # 空值直接忽略
    assert len(store.peers()) == 2


# ---------- 推送清单 ----------

def test_pushes_keep_the_latest_five(tmp_path):
    clock = Clock(10_000.0)
    store = make_store(tmp_path, clock)
    for index in range(8):
        store.record_push("oc_g", clock.now, f"第 {index} 条")
        clock.now += 60
    recent = store.recent_pushes("oc_g")
    assert [summary for _, summary in recent] == [
        "第 7 条", "第 6 条", "第 5 条", "第 4 条", "第 3 条"]      # 最新在前
    assert store.recent_pushes("oc_other") == []                    # 按会话分开


def test_pushes_drop_entries_older_than_48_hours(tmp_path):
    clock = Clock(10_000.0)
    store = make_store(tmp_path, clock)
    store.record_push("oc_g", clock.now, "两天前的事")
    clock.now += 49 * 3600
    store.record_push("oc_g", clock.now, "刚刚的事")               # 写新的顺手清旧的
    assert [summary for _, summary in store.recent_pushes("oc_g")] == ["刚刚的事"]


def test_push_summary_is_single_line_and_limited(tmp_path):
    store = make_store(tmp_path)
    store.record_push("oc_g", 1.0, "第一行\n第二行" + "很长" * 100)
    summary = store.recent_pushes("oc_g")[0][1]
    assert "\n" not in summary and len(summary) <= 120


# ---------- 失败计数 ----------

def test_failure_alert_fires_on_the_third_streak_with_cooldown(tmp_path):
    clock = Clock(1000.0)
    store = make_store(tmp_path, clock)
    assert store.note_failure("oc_g") is False
    assert store.note_failure("oc_g") is False
    assert store.note_failure("oc_g") is True            # 连续第 3 次: 该提示了
    store.mark_alerted("oc_g")
    assert store.note_failure("oc_g") is False           # 24 小时内不再提示
    clock.now += 25 * 3600
    assert store.note_failure("oc_g") is True            # 冷却过了, 还在连续失败
    assert store.note_failure("oc_other") is False       # 按目标分开计数


def test_alert_cooldown_only_starts_after_mark_alerted(tmp_path):
    store = make_store(tmp_path)
    for _ in range(3):
        store.note_failure("oc_g")
    assert store.note_failure("oc_g") is True            # 提示没发出去: 下一轮还该提示
    assert store.note_failure("oc_g") is True


def test_clear_failures_resets_the_streak(tmp_path):
    store = make_store(tmp_path)
    for _ in range(3):
        store.note_failure("oc_g")
    store.clear_failures("oc_g")
    assert store.note_failure("oc_g") is False           # 从头数
    assert store.note_failure("oc_g") is False
