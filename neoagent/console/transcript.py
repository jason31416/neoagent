"""控制台转录：用户在控制台看到过什么（消息/通知/工作行/指令回执）。

与账本分工：账本是模型上下文的真值，转录是 UI 的记录——退出重进靠它回放，
不在场的输出（比如回复发到 cli 频道时人不在）也进这里。

**不落盘**：只活在服务进程的内存里（环形缓冲，最多 1 万行），服务重启即清空；
旧版本落进 sqlite 的 console_lines 表由 drop_stored 一次性删掉。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Dict, List

_MAX_ROWS = 10_000    # 环形上限：长跑服务也不膨胀


class ConsoleTranscript:
    def __init__(self) -> None:
        self._rows: List[Dict] = []
        self._next_id = 1

    def append(self, ev: Dict) -> Dict:
        """记一条事件（补 ts），返回带 id 的事件。"""
        ev = dict(ev)
        ev.setdefault("ts", time.time())
        ev["id"] = self._next_id
        self._next_id += 1
        self._rows.append(ev)
        if len(self._rows) > _MAX_ROWS:
            del self._rows[: len(self._rows) - _MAX_ROWS]
        return ev

    def recent(self, n: int = 100) -> List[Dict]:
        """最近 n 条（按时间正序）。"""
        n = max(1, int(n or 100))
        return self._rows[-n:]

    @staticmethod
    def drop_stored(db_path: Path) -> None:
        """一次性清理旧版落盘的转录表（console_lines）。"""
        try:
            if not Path(db_path).exists():
                return
            conn = sqlite3.connect(str(db_path))
            try:
                conn.execute("DROP TABLE IF EXISTS console_lines")
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            pass
