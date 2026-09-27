"""消息库（SQLite）：账本的最终形态——全量落盘的唯一事实源。

产品文档的"对话自带账本"落成一张 messages 表：
- 所有消息（user / assistant / internal）+ 附件 + 引用 + 工具轨迹全量入库，重启不丢；
- 序号 seq_id 每会话单调递增、**永不回收**（sessions.last_seq 分配器，删行不倒退）；
- LLM 上下文只是渲染时截取的最新 N 条（见 render.py）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional

from .events import Attachment, ReplyRef


@dataclass
class ToolCallRecord:
    """一轮工具调用的事实记录（渲染时展开成协议机器件）。"""
    name: str
    arguments: str          # JSON 字符串
    result: str             # 已截断的工具输出

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_key TEXT PRIMARY KEY,
  last_seq    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
  seq_id       INTEGER NOT NULL,
  session_key  TEXT NOT NULL,
  message_id   TEXT,
  role         TEXT NOT NULL,          -- user | assistant | internal
  sender_name  TEXT,
  timestamp    REAL,
  text         TEXT,
  attachments  JSON,
  reply_to     JSON,
  tool_trace   JSON,
  PRIMARY KEY (session_key, seq_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_msgid ON messages(session_key, message_id);
"""


def _dump_provider_ref(ref: Any) -> Any:
    """provider_ref 是不透明句柄（VisionRef 等）——入库时转纯数据。"""
    if ref is None:
        return None
    if is_dataclass(ref):
        return asdict(ref)
    if isinstance(ref, dict):
        return ref
    return str(ref)


def _load_provider_ref(data: Any) -> Any:
    """出库时还原成带属性访问的对象（SimpleNamespace 足够 content_part 用）。"""
    if data is None:
        return None
    if isinstance(data, dict):
        return SimpleNamespace(**data)
    return data


def _att_to_dict(a: Attachment) -> dict:
    return {
        "kind": a.kind, "filename": a.filename, "mime": a.mime,
        "size": a.size, "local_path": a.local_path,
        "provider_ref": _dump_provider_ref(a.provider_ref),
    }


def _att_from_dict(d: dict) -> Attachment:
    return Attachment(
        kind=d.get("kind", "file"), filename=d.get("filename", ""),
        mime=d.get("mime", ""), size=int(d.get("size", 0)),
        local_path=d.get("local_path", ""),
        provider_ref=_load_provider_ref(d.get("provider_ref")),
    )


class MessageLedger:
    """按会话的消息账本（SQLite 持久化）。"""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------
    # 序号分配：每会话单调递增、永不回收
    # ------------------------------------------------------------------
    def _alloc_seq(self, session_key: str) -> int:
        self._conn.execute(
            "INSERT INTO sessions(session_key, last_seq) VALUES(?, 0) "
            "ON CONFLICT(session_key) DO NOTHING", (session_key,))
        row = self._conn.execute(
            "UPDATE sessions SET last_seq = last_seq + 1 WHERE session_key = ? "
            "RETURNING last_seq", (session_key,)).fetchone()
        self._conn.commit()
        return int(row[0])

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def add(self, *, session_key: str, role: str, message_id: str = "",
            sender_name: str = "", text: str = "", timestamp: float | None = None,
            attachments: List[Attachment] | None = None,
            reply_to: Optional[ReplyRef] = None,
            tool_trace: List[Any] | None = None) -> int:
        """记一条账，返回 seq_id。tool_trace 传 ToolCallRecord 列表。"""
        seq = self._alloc_seq(session_key)
        reply_dict = None
        if reply_to is not None:
            reply_dict = {
                "message_id": reply_to.message_id,
                "sender_name": reply_to.sender_name,
                "timestamp": reply_to.timestamp,
                "text": reply_to.text,
                "attachments": [_att_to_dict(a) for a in reply_to.attachments],
                "resolved_from": reply_to.resolved_from,
                "seq_id": reply_to.seq_id,
            }
        trace_dict = None
        if tool_trace:
            trace_dict = [asdict(t) if is_dataclass(t) else dict(t) for t in tool_trace]
        self._conn.execute(
            "INSERT INTO messages(seq_id, session_key, message_id, role, sender_name,"
            " timestamp, text, attachments, reply_to, tool_trace)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (seq, session_key, message_id, role, sender_name,
             timestamp if timestamp is not None else time.time(), text,
             json.dumps([_att_to_dict(a) for a in (attachments or [])], ensure_ascii=False),
             json.dumps(reply_dict, ensure_ascii=False) if reply_dict else None,
             json.dumps(trace_dict, ensure_ascii=False) if trace_dict else None),
        )
        self._conn.commit()
        return seq

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def find(self, session_key: str, message_id: str) -> Optional[Any]:
        """按平台消息 id 反查（引用解析主路径）。返回轻量命名元组。"""
        if not message_id:
            return None
        row = self._conn.execute(
            "SELECT seq_id, sender_name, timestamp, text, attachments FROM messages"
            " WHERE session_key = ? AND message_id = ? ORDER BY seq_id DESC LIMIT 1",
            (session_key, message_id)).fetchone()
        if row is None:
            return None
        return SimpleNamespace(
            seq_id=int(row[0]), sender_name=row[1] or "",
            timestamp=float(row[2] or 0), text=row[3] or "",
            attachments=[_att_from_dict(d) for d in (json.loads(row[4]) if row[4] else [])],
        )

    def visible(self, session_key: str, window: int) -> List[Any]:
        """渲染窗口内的条目（旧→新）。全量在库里，这里只取最新 window 条。"""
        rows = self._conn.execute(
            "SELECT seq_id, message_id, role, sender_name, timestamp, text,"
            " attachments, reply_to, tool_trace FROM messages"
            " WHERE session_key = ? ORDER BY seq_id DESC LIMIT ?",
            (session_key, window)).fetchall()
        entries = []
        for r in reversed(rows):
            reply_to = None
            if r[7]:
                d = json.loads(r[7])
                reply_to = ReplyRef(
                    message_id=d.get("message_id", ""),
                    sender_name=d.get("sender_name", ""),
                    timestamp=float(d.get("timestamp") or 0),
                    text=d.get("text", ""),
                    attachments=[_att_from_dict(x) for x in d.get("attachments", [])],
                    resolved_from=d.get("resolved_from", "unknown"),
                    seq_id=d.get("seq_id"),
                )
            entries.append(SimpleNamespace(
                seq_id=int(r[0]), message_id=r[1] or "", role=r[2],
                sender_name=r[3] or "", timestamp=float(r[4] or 0), text=r[5] or "",
                attachments=[_att_from_dict(x) for x in (json.loads(r[6]) if r[6] else [])],
                reply_to=reply_to,
                tool_trace=[SimpleNamespace(**t) for t in (json.loads(r[8]) if r[8] else [])],
            ))
        return entries

    def close(self) -> None:
        self._conn.close()
