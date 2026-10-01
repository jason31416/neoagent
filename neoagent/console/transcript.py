"""控制台转录：用户在控制台看到过什么（消息/通知/工作行/指令回执）。

与账本分工：账本是模型上下文的真值，转录是 UI 的记录——退出重进靠它回放，
不在场的输出（比如回复发到 cli 频道时人不在）也落这里。
存在账本同一个 sqlite 文件里（备份一份就把运行状态带全）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional


class ConsoleTranscript:
    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS console_lines ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts REAL NOT NULL,"
            " kind TEXT NOT NULL,"
            " data TEXT NOT NULL)")
        self._conn.commit()

    def append(self, ev: Dict) -> Dict:
        """落一条事件（补 ts），返回带 id 的事件。"""
        ev = dict(ev)
        ev.setdefault("ts", time.time())
        cur = self._conn.execute(
            "INSERT INTO console_lines (ts, kind, data) VALUES (?, ?, ?)",
            (float(ev["ts"]), str(ev.get("kind", "")), json.dumps(ev, ensure_ascii=False)))
        self._conn.commit()
        ev["id"] = cur.lastrowid
        return ev

    def recent(self, n: int = 100) -> List[Dict]:
        """最近 n 条（按时间正序）。"""
        n = max(1, int(n or 100))
        rows = self._conn.execute(
            "SELECT data FROM console_lines ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]

    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM console_lines").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
