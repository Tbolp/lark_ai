"""把 stderr 复制到按天滚动的日志文件（顺带在每行前补时间戳）。"""

from __future__ import annotations

import os
import sys
from datetime import datetime


class DailyLogTee:
    """把 stderr 输出复制到按天滚动的日志文件, 并在每行前补时间戳。

    （整个程序只有一个线程在写 stderr, 这里的锁是顺手保个险。）
    """

    def __init__(self, stream, log_file):
        self.stream = stream
        self.log_file = log_file
        self.at_line_start = True
        self.current_date: str | None = None
        self.file_obj = None

    def _path_for(self, now: datetime) -> str:
        date_text = now.strftime("%Y-%m-%d")
        if "{date}" in self.log_file:
            return self.log_file.format(date=date_text)
        base, ext = os.path.splitext(self.log_file)
        return f"{base}-{date_text}{ext}"

    def _open_for_today(self) -> None:
        now = datetime.now()
        date_text = now.strftime("%Y-%m-%d")
        if self.current_date == date_text and self.file_obj is not None:
            return
        if self.file_obj is not None:
            try:
                self.file_obj.close()
            except OSError:
                pass
        self.current_date = date_text
        try:
            path = self._path_for(now)
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            self.file_obj = open(path, "a", encoding="utf-8")
        except OSError as exc:
            self.stream.write(f"[bot] 打开日志文件失败 ({exc}), 仅输出到终端\n")
            self.file_obj = None
            self.current_date = None

    def write(self, text: str) -> int:
        if not text:
            return 0
        self._open_for_today()
        rendered = []
        idx = 0
        while idx < len(text):
            if self.at_line_start and text[idx] != "\n":
                rendered.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S "))
                self.at_line_start = False
            nl = text.find("\n", idx)
            if nl == -1:
                rendered.append(text[idx:])
                self.at_line_start = False
                break
            rendered.append(text[idx:nl + 1])
            self.at_line_start = True
            idx = nl + 1
        line = "".join(rendered)
        self.stream.write(line)
        if self.file_obj is not None:
            self.file_obj.write(line)
            self.file_obj.flush()
        self.stream.flush()
        return len(text)

    def flush(self) -> None:
        self.stream.flush()
        if self.file_obj is not None:
            self.file_obj.flush()


def setup_logging(log_file: str | None) -> None:
    """把 stderr 复制到按天滚动的日志文件（空字符串可关闭）。"""
    if not log_file:
        return
    sys.stderr = DailyLogTee(sys.stderr, log_file)
