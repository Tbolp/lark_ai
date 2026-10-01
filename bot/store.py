"""状态存储: 一个 SQLite 文件装四张表。

  watchlist     每个会话关注的股票（按 chat_id 分开, 群和私聊各一份）
  peers         白名单私聊的 open_id -> chat_id（整点播报要知道往哪发）
  pushes        每个会话最近推送过的消息（跨重启去重）
  timer_health  每个会话连续失败了几轮、上次失败提示是什么时候

为什么用 SQLite 而不是 JSON: 关注列表是用户自己攒出来的东西, 写过就得在; 事务、并发
写、以后要加"推送历史统计"这类查询都不用重写。`sqlite3` 是标准库, 不引依赖。

库里时间统一存 epoch 秒（排序、过期判断都靠它）。想人读就:

    sqlite3 logs/state.db 'select code, name, datetime(added_at, "unixepoch", "localtime") from watchlist'

两条纪律:

  - **启动期**失败（建不了目录、文件不是合法数据库）抛 ValueError —— 带着坏状态跑
    比不跑更糟, 和 config / prompt 的校验是同一个道理;
  - **运行期**单次读写失败只写一行 `[store]` 日志并降级（读当空、写放弃）—— 一次磁盘
    抖动不该让问答或盯盘炸掉。

校验写在这里而不是提示词里: 代码格式、每会话上限、名称/摘要的单行化都是代码的约束,
提示词那层只是"请求"（和 tools.py 里的安全边界同一个思路）。
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from typing import Any, Callable

# 一个会话最多关注多少只: 整点播报每轮要把它们逐个过一遍新闻, 太多了单次工具输出会被
# tool_output_limit 截断, 反而漏看。
MAX_WATCH = 20

# 去重清单: 每个会话只留最近这么多条, 且不超过这么多小时。
PUSH_KEEP = 5
PUSH_MAX_AGE_HOURS = 48

# 连续失败多少轮才提示、提示之后多久内不再提示（免得模型欠费那种情况刷屏）。
FAIL_STREAK_ALERT = 3
ALERT_COOLDOWN_HOURS = 24

# 名称 / 摘要的展示上限: 这两个值会被拼进定时消息, 不能无限长。
NAME_LIMIT = 20
SUMMARY_LIMIT = 120

# 代码: 市场前缀 + 剩余字符集。前缀就是 westock-data 铁律 6 里那套
# （A股 sh/sz/bj、港股 hk、美股 us、日韩 t/ks/kq、板块 pt、期货 fu/hf、外汇 fx、港股期指 r_hd）。
CODE_PREFIXES = ("sh", "sz", "bj", "hk", "us", "t", "ks", "kq", "pt", "fu", "hf", "fx", "r_hd")
_CODE = re.compile(
    r"\A(?:" + "|".join(sorted(CODE_PREFIXES, key=len, reverse=True)) + r")[0-9A-Za-z._]{1,14}\Z"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    chat_id  TEXT NOT NULL,
    code     TEXT NOT NULL,
    name     TEXT NOT NULL DEFAULT '',
    added_at REAL NOT NULL,
    PRIMARY KEY (chat_id, code)
);
CREATE TABLE IF NOT EXISTS peers (
    open_id  TEXT PRIMARY KEY,
    chat_id  TEXT NOT NULL,
    seen_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pushes (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id  TEXT NOT NULL,
    ts       REAL NOT NULL,
    summary  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pushes_chat_ts ON pushes (chat_id, ts);
CREATE TABLE IF NOT EXISTS timer_health (
    chat_id    TEXT PRIMARY KEY,
    streak     INTEGER NOT NULL DEFAULT 0,
    alerted_at REAL
);
"""

SCHEMA_VERSION = 1


def single_line(text: Any, limit: int) -> str:
    """把一段可能是多行/带控制字符的文本收敛成单行 + 限长。

    模型给的名称、推送摘要都会进定时消息（模型下一轮的输入）; 换行会破坏消息结构,
    超长会挤掉正文。统一在这里收口, 而不是指望提示词自律。
    """
    if not isinstance(text, str):
        return ""
    flat = " ".join(text.split())                      # 换行 / 连续空白折成单空格
    flat = "".join(ch for ch in flat if ch.isprintable())
    return flat[:limit]


class StateStore:
    """一个 SQLite 文件上的全部状态。方法都是同步的（微秒级）, 调用点不要夹 await。"""

    def __init__(self, path: str, *, now: Callable[[], float] = time.time) -> None:
        self.path = path
        self.now = now
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with self._connect() as conn:
                self._migrate(conn)
        except (OSError, sqlite3.Error) as exc:
            raise ValueError(f"建不了状态库 {path}: {exc}") from exc

    # ---------- 连接与建表 ----------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """按 user_version 建表 / 迁移; 重复调用是幂等的。"""
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # ---------- 关注列表 ----------

    def watch(self, chat_id: str) -> list[dict]:
        """这个会话关注的股票（按加入时间排; 读不到就当空）。"""
        rows = self._read(
            "SELECT code, name, added_at FROM watchlist WHERE chat_id = ?"
            " ORDER BY added_at, code",
            (chat_id,), ())
        return [{"code": row["code"], "name": row["name"], "added_at": row["added_at"]}
                for row in rows]

    def describe(self, chat_id: str) -> str:
        """关注列表的单行描述（定时消息用; 空列表也各给一句话）。"""
        items = self.watch(chat_id)
        if not items:
            return "（还没有关注任何股票）"
        return "、".join(f"{item['code']} {item['name']}".strip() for item in items)

    def add_watch(self, chat_id: str, code: str, name: str = "") -> tuple[bool, str]:
        """加一只: 返回 (成功?, 给模型看的人话)。格式、去重、上限都在这里管。"""
        if not chat_id:
            return False, "这个会话没有 chat_id，改不了关注列表。"
        code = (code or "").strip()
        if not _CODE.match(code):
            return False, (f"代码 {code!r} 不合法：得带市场前缀，"
                           "例如 sh600519、sz300750、hk00700、usAAPL.OQ。")
        label = f"{code} {single_line(name, NAME_LIMIT)}".strip()
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT name FROM watchlist WHERE chat_id = ? AND code = ?",
                    (chat_id, code)).fetchone()
                if row is not None:
                    return False, f"{code} 已经在关注列表里了。"
                count = conn.execute(
                    "SELECT COUNT(*) FROM watchlist WHERE chat_id = ?", (chat_id,)).fetchone()[0]
                if count >= MAX_WATCH:
                    return False, f"一个会话最多关注 {MAX_WATCH} 只，先去掉几只再加。"
                conn.execute(
                    "INSERT INTO watchlist (chat_id, code, name, added_at) VALUES (?, ?, ?, ?)",
                    (chat_id, code, single_line(name, NAME_LIMIT), self.now()))
        except sqlite3.Error as exc:
            return False, self._fail("add_watch", exc)
        return True, f"已关注 {label}，当前共 {count + 1} 只。"

    def remove_watch(self, chat_id: str, code: str) -> tuple[bool, str]:
        """去掉一只: 返回 (成功?, 给模型看的人话)。"""
        if not chat_id:
            return False, "这个会话没有 chat_id，改不了关注列表。"
        code = (code or "").strip()
        if not _CODE.match(code):
            return False, (f"代码 {code!r} 不合法：得带市场前缀，"
                           "例如 sh600519、sz300750、hk00700、usAAPL.OQ。")
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT name FROM watchlist WHERE chat_id = ? AND code = ?",
                    (chat_id, code)).fetchone()
                if row is None:
                    return False, f"关注列表里没有 {code}。"
                conn.execute("DELETE FROM watchlist WHERE chat_id = ? AND code = ?",
                             (chat_id, code))
                left = conn.execute(
                    "SELECT COUNT(*) FROM watchlist WHERE chat_id = ?", (chat_id,)).fetchone()[0]
        except sqlite3.Error as exc:
            return False, self._fail("remove_watch", exc)
        label = f"{code} {row['name']}".strip()
        return True, f"已取消关注 {label}，当前共 {left} 只。"

    # ---------- 私聊会话表 ----------

    def remember_peer(self, open_id: str, chat_id: str) -> None:
        """记下"这个白名单用户私聊时的 chat_id"（整点播报的私聊目标就靠它）。"""
        if not open_id or not chat_id:
            return
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO peers (open_id, chat_id, seen_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(open_id) DO UPDATE SET"
                    " chat_id = excluded.chat_id, seen_at = excluded.seen_at",
                    (open_id, chat_id, self.now()))
        except sqlite3.Error as exc:
            self._log("remember_peer", exc)

    def peers(self) -> dict[str, str]:
        """open_id -> 私聊 chat_id（读不到就当空）。"""
        rows = self._read("SELECT open_id, chat_id FROM peers", (), ())
        return {row["open_id"]: row["chat_id"] for row in rows}

    # ---------- 推送清单 ----------

    def recent_pushes(self, chat_id: str, limit: int = PUSH_KEEP) -> list[tuple[float, str]]:
        """这个会话最近推送过什么: [(epoch, 一句话结论)]。"""
        rows = self._read(
            "SELECT ts, summary FROM pushes WHERE chat_id = ?"
            " ORDER BY ts DESC, id DESC LIMIT ?",
            (chat_id, limit), ())
        return [(row["ts"], row["summary"]) for row in rows]

    def record_push(self, chat_id: str, ts: float, summary: str) -> None:
        """记一条推送, 顺手把超龄和超量的清掉（只清这个会话的）。"""
        try:
            with self._connect() as conn:
                conn.execute("INSERT INTO pushes (chat_id, ts, summary) VALUES (?, ?, ?)",
                             (chat_id, ts, single_line(summary, SUMMARY_LIMIT)))
                cutoff = self.now() - PUSH_MAX_AGE_HOURS * 3600
                conn.execute("DELETE FROM pushes WHERE chat_id = ? AND ts < ?",
                             (chat_id, cutoff))
                conn.execute(
                    "DELETE FROM pushes WHERE chat_id = ? AND id NOT IN ("
                    " SELECT id FROM pushes WHERE chat_id = ? ORDER BY ts DESC, id DESC LIMIT ?)",
                    (chat_id, chat_id, PUSH_KEEP))
        except sqlite3.Error as exc:
            self._log("record_push", exc)

    # ---------- 失败计数 ----------

    def note_failure(self, chat_id: str) -> bool:
        """记一轮失败, 返回"现在该不该给这个目标发失败提示"。

        提示是否真的发出去了由调用方 mark_alerted 落库 —— 提示自己发失败时不该把
        冷却时间也一起用掉。
        """
        now = self.now()
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT streak, alerted_at FROM timer_health WHERE chat_id = ?",
                    (chat_id,)).fetchone()
                streak = (row["streak"] if row is not None else 0) + 1
                alerted_at = row["alerted_at"] if row is not None else None
                conn.execute(
                    "INSERT INTO timer_health (chat_id, streak, alerted_at) VALUES (?, ?, ?)"
                    " ON CONFLICT(chat_id) DO UPDATE SET streak = excluded.streak",
                    (chat_id, streak, alerted_at))
        except sqlite3.Error as exc:
            self._log("note_failure", exc)
            return False
        if streak < FAIL_STREAK_ALERT:
            return False
        return alerted_at is None or now - alerted_at >= ALERT_COOLDOWN_HOURS * 3600

    def mark_alerted(self, chat_id: str) -> None:
        """提示真的发出去之后才调: 记下时间, 冷却从这一刻起算。"""
        try:
            with self._connect() as conn:
                conn.execute("UPDATE timer_health SET alerted_at = ? WHERE chat_id = ?",
                             (self.now(), chat_id))
        except sqlite3.Error as exc:
            self._log("mark_alerted", exc)

    def clear_failures(self, chat_id: str) -> None:
        """这一轮是好的（推了 / 或明确判断无需推送）: 连续失败计数归零。"""
        try:
            with self._connect() as conn:
                conn.execute("UPDATE timer_health SET streak = 0 WHERE chat_id = ? AND streak != 0",
                             (chat_id,))
        except sqlite3.Error as exc:
            self._log("clear_failures", exc)

    # ---------- 内部 ----------

    def _read(self, sql: str, params: tuple, default):
        try:
            with self._connect() as conn:
                return conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            self._log("query", exc)
            return default

    @staticmethod
    def _log(what: str, exc: Exception) -> None:
        sys.stderr.write(f"[store] {what} 失败: {exc!r}\n")

    def _fail(self, what: str, exc: Exception) -> str:
        self._log(what, exc)
        return f"关注列表写入失败（{exc}），已记进日志，请稍后再试。"
