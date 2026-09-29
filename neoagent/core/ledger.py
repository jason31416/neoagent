"""消息库（SQLite）：**完整上下文存储**——凡是会作为一条独立消息进入模型上下文的东西都在这里。

一行 = 一条协议消息（type 区分种类）：
- user        用户消息（文本行 / 附件行各自独立一行，各拿 msg#）
- assistant   模型发出的 <message> 气泡
- internal    系统内部注入（E/cron/jev，本期恒空）
- tool_call   一轮模型响应里的工具调用数组（一条协议消息，meta.calls）
- tool_result 单个工具的返回（role:"tool" 机器件，meta.call_id 配对）

要点：
- 序号 seq_id 每会话单调递增、**永不回收**（sessions.last_seq 分配器，删行不倒退）；
- LLM 上下文只是渲染时截取的最新 N **行**（见 render.py）；
- 附件/引用等 type 相关载荷放 meta(JSON)；图片字节入 blobs 表（内容寻址 image_sha）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional

from .events import Attachment, ReplyRef

# 行类型
TYPE_USER = "user"
TYPE_ASSISTANT = "assistant"
TYPE_INTERNAL = "internal"
TYPE_TOOL_CALL = "tool_call"
TYPE_TOOL_RESULT = "tool_result"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_key TEXT PRIMARY KEY,
  last_seq    INTEGER NOT NULL DEFAULT 0,
  r_seen_seq  INTEGER NOT NULL DEFAULT 0,   -- Recorder 增量游标：已整理到哪
  cut_seq     INTEGER NOT NULL DEFAULT 0    -- 上下文切点：低于此的行不再渲染（单调）
);
CREATE TABLE IF NOT EXISTS messages (
  seq_id       INTEGER NOT NULL,
  session_key  TEXT NOT NULL,
  message_id   TEXT,
  type         TEXT NOT NULL,          -- user | assistant | internal | tool_call | tool_result
  sender_name  TEXT,
  timestamp    REAL,
  text         TEXT,
  meta         JSON,                   -- type 相关载荷（附件/引用/工具调用数组/配对 id）
  PRIMARY KEY (session_key, seq_id)
);
CREATE TABLE IF NOT EXISTS blobs (
  sha256       TEXT PRIMARY KEY,       -- 内容寻址：同图只存一份
  mime         TEXT,
  bytes        BLOB                    -- 图片字节本体（账持有数据，本地删移不影响还原）
);
CREATE INDEX IF NOT EXISTS idx_messages_msgid ON messages(session_key, message_id);
"""


def _att_to_dict(a: Attachment) -> dict:
    return {
        "kind": a.kind, "filename": a.filename, "mime": a.mime,
        "size": a.size, "local_path": a.local_path, "image_sha": a.image_sha,
    }


def _att_from_dict(d: dict) -> Attachment:
    return Attachment(
        kind=d.get("kind", "file"), filename=d.get("filename", ""),
        mime=d.get("mime", ""), size=int(d.get("size", 0)),
        local_path=d.get("local_path", ""), image_sha=d.get("image_sha", ""),
    )


def _reply_to_dict(ref: ReplyRef) -> dict:
    return {
        "message_id": ref.message_id,
        "sender_name": ref.sender_name,
        "timestamp": ref.timestamp,
        "text": ref.text,
        "attachments": [_att_to_dict(a) for a in ref.attachments],
        "resolved_from": ref.resolved_from,
        "seq_id": ref.seq_id,
    }


def _reply_to_from_dict(d: dict) -> ReplyRef:
    return ReplyRef(
        message_id=d.get("message_id", ""),
        sender_name=d.get("sender_name", ""),
        timestamp=float(d.get("timestamp") or 0),
        text=d.get("text", ""),
        attachments=[_att_from_dict(x) for x in d.get("attachments", [])],
        resolved_from=d.get("resolved_from", "unknown"),
        seq_id=d.get("seq_id"),
    )


class MessageLedger:
    """按会话的上下文消息库（SQLite 持久化）。"""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """老库无损升级：补 sessions 的 Recorder 状态列。"""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(sessions)")}
        for col in ("r_seen_seq", "cut_seq"):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} "
                                   "INTEGER NOT NULL DEFAULT 0")

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
    def add(self, *, session_key: str, type: str, message_id: str = "",
            sender_name: str = "", text: str = "", timestamp: float | None = None,
            attachments: List[Attachment] | None = None,
            reply_to: Optional[ReplyRef] = None,
            calls: Optional[List[dict]] = None,
            call_id: str = "", name: str = "") -> int:
        """记一行，返回 seq_id。

        - user/assistant：attachments / reply_to 进 meta
        - tool_call：calls=[{call_id, name, arguments}, …]（一轮的数组，一条协议消息）
        - tool_result：call_id + name，结果全文放 text
        """
        meta: dict = {}
        if attachments:
            for a in attachments:
                if a.kind == "image" and a.data:
                    if not a.image_sha:
                        a.image_sha = hashlib.sha256(a.data).hexdigest()
                    self._put_blob(a.image_sha, a.mime, a.data)
                    a.data = b""                   # 瞬态字段不进 meta（字节在 blobs）
            meta["attachments"] = [_att_to_dict(a) for a in attachments]
        if reply_to is not None:
            meta["reply_to"] = _reply_to_dict(reply_to)
        if calls:
            meta["calls"] = list(calls)
        if call_id:
            meta["call_id"] = call_id
        if name:
            meta["name"] = name

        seq = self._alloc_seq(session_key)
        self._conn.execute(
            "INSERT INTO messages(seq_id, session_key, message_id, type, sender_name,"
            " timestamp, text, meta) VALUES(?,?,?,?,?,?,?,?)",
            (seq, session_key, message_id, type, sender_name,
             timestamp if timestamp is not None else time.time(), text,
             json.dumps(meta, ensure_ascii=False) if meta else None),
        )
        self._conn.commit()
        return seq

    # ------------------------------------------------------------------
    # Recorder 状态：增量游标 r_seen_seq + 上下文切点 cut_seq
    # ------------------------------------------------------------------
    def get_recorder_state(self, session_key: str) -> tuple:
        """(r_seen_seq, cut_seq)；会话不存在时 (0, 0)。"""
        row = self._conn.execute(
            "SELECT r_seen_seq, cut_seq FROM sessions WHERE session_key = ?",
            (session_key,)).fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    def set_recorder_state(self, session_key: str, *,
                           r_seen_seq: int | None = None,
                           cut_seq: int | None = None) -> None:
        """更新 Recorder 状态。cut_seq 单调只进不退：传小了无视（机器不变量）。"""
        self._conn.execute(
            "INSERT INTO sessions(session_key, last_seq) VALUES(?, 0) "
            "ON CONFLICT(session_key) DO NOTHING", (session_key,))
        if r_seen_seq is not None:
            self._conn.execute("UPDATE sessions SET r_seen_seq = ? WHERE session_key = ?",
                               (int(r_seen_seq), session_key))
        if cut_seq is not None:
            self._conn.execute(
                "UPDATE sessions SET cut_seq = MAX(cut_seq, ?) WHERE session_key = ?",
                (int(cut_seq), session_key))
        self._conn.commit()

    def session_keys(self) -> List[str]:
        """全部会话（慢周期遍历用）。"""
        return [r[0] for r in self._conn.execute(
            "SELECT session_key FROM sessions ORDER BY session_key")]

    def set_message_id(self, session_key: str, seq_id: int, message_id: str) -> None:
        """回填发出消息的平台 id（消息发出后才拿得到；供以后被引用）。"""
        self._conn.execute(
            "UPDATE messages SET message_id = ? WHERE session_key = ? AND seq_id = ?",
            (message_id, session_key, seq_id))
        self._conn.commit()

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def find(self, session_key: str, message_id: str) -> Optional[Any]:
        """按平台消息 id 反查（引用解析主路径）。返回轻量命名元组。"""
        if not message_id:
            return None
        row = self._conn.execute(
            "SELECT seq_id, sender_name, timestamp, text, meta FROM messages"
            " WHERE session_key = ? AND message_id = ? ORDER BY seq_id DESC LIMIT 1",
            (session_key, message_id)).fetchone()
        if row is None:
            return None
        meta = json.loads(row[4]) if row[4] else {}
        return SimpleNamespace(
            seq_id=int(row[0]), sender_name=row[1] or "",
            timestamp=float(row[2] or 0), text=row[3] or "",
            attachments=[_att_from_dict(d) for d in meta.get("attachments", [])],
        )

    def _row_to_entry(self, r) -> Any:
        meta = json.loads(r[6]) if r[6] else {}
        atts = [_att_from_dict(d) for d in meta.get("attachments", [])]
        for a in atts:                              # 图片字节现取（blobs 内容寻址）
            if a.image_sha:
                hit = self.blob(a.image_sha)
                if hit is not None:
                    a.data = hit[1]
        return SimpleNamespace(
            seq_id=int(r[0]), message_id=r[1] or "", type=r[2],
            sender_name=r[3] or "", timestamp=float(r[4] or 0), text=r[5] or "",
            attachments=atts,
            reply_to=_reply_to_from_dict(meta["reply_to"]) if meta.get("reply_to") else None,
            calls=[SimpleNamespace(**c) for c in meta.get("calls", [])],
            call_id=meta.get("call_id", ""),
            name=meta.get("name", ""),
            reasoning=meta.get("reasoning", ""),
        )

    def _put_blob(self, sha: str, mime: str, data: bytes) -> None:
        """图片字节落 blobs（内容寻址，同图只存一份，幂等）。"""
        self._conn.execute(
            "INSERT OR IGNORE INTO blobs(sha256, mime, bytes) VALUES(?,?,?)",
            (sha, mime or "", sqlite3.Binary(data)))
        self._conn.commit()

    def blob(self, sha: str) -> Optional[tuple]:
        """按 sha 取图片字节 (mime, bytes)；没有返回 None。"""
        row = self._conn.execute(
            "SELECT mime, bytes FROM blobs WHERE sha256 = ?", (sha,)).fetchone()
        return (row[0] or "", bytes(row[1])) if row else None

    def attach_reasoning(self, session_key: str, seq_id: int, reasoning: str) -> None:
        """把一轮的思考草稿挂到轮首行（meta.reasoning）。

        reasoning 是"模型响应轮"的产物（一份/轮），账是消息粒度——挂轮首行，
        渲染时附到由它产生的第一条协议消息上（草稿语义：不给用户看，只回传/审计）。"""
        row = self._conn.execute(
            "SELECT meta FROM messages WHERE session_key = ? AND seq_id = ?",
            (session_key, seq_id)).fetchone()
        if row is None:
            return
        meta = json.loads(row[0]) if row[0] else {}
        meta["reasoning"] = reasoning
        self._conn.execute(
            "UPDATE messages SET meta = ? WHERE session_key = ? AND seq_id = ?",
            (json.dumps(meta, ensure_ascii=False), session_key, seq_id))
        self._conn.commit()

    def visible(self, session_key: str, window: int) -> List[Any]:
        """渲染窗口内的行（旧→新）。全量在库里，这里只取最新 window 行。"""
        rows = self._conn.execute(
            "SELECT seq_id, message_id, type, sender_name, timestamp, text, meta"
            " FROM messages WHERE session_key = ? ORDER BY seq_id DESC LIMIT ?",
            (session_key, window)).fetchall()
        return [self._row_to_entry(r) for r in reversed(rows)]

    def since(self, session_key: str, after_seq: int, limit: int = 500) -> List[Any]:
        """游标之后的新行（旧→新），增量渲染用。"""
        rows = self._conn.execute(
            "SELECT seq_id, message_id, type, sender_name, timestamp, text, meta"
            " FROM messages WHERE session_key = ? AND seq_id > ? ORDER BY seq_id LIMIT ?",
            (session_key, after_seq, limit)).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def since_time(self, session_key: str, ts_min: float, limit: int = 5000) -> List[Any]:
        """某时刻之后的行（旧→新，不分类型），Recorder 慢周期回看用。"""
        rows = self._conn.execute(
            "SELECT seq_id, message_id, type, sender_name, timestamp, text, meta"
            " FROM messages WHERE session_key = ? AND timestamp >= ?"
            " ORDER BY seq_id LIMIT ?",
            (session_key, ts_min, limit)).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def tail(self, session_key: str, before_seq: Optional[int] = None,
             limit: int = 500) -> List[Any]:
        """从最新往老翻批（新→旧），冷启动扫预算用。before_seq=None 从最新开始。"""
        if before_seq is None:
            rows = self._conn.execute(
                "SELECT seq_id, message_id, type, sender_name, timestamp, text, meta"
                " FROM messages WHERE session_key = ? ORDER BY seq_id DESC LIMIT ?",
                (session_key, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT seq_id, message_id, type, sender_name, timestamp, text, meta"
                " FROM messages WHERE session_key = ? AND seq_id < ?"
                " ORDER BY seq_id DESC LIMIT ?",
                (session_key, before_seq, limit)).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def by_seqs(self, session_key: str, seq_ids: List[int]) -> List[Any]:
        """按 seq 取行（引用召回用），旧→新。不在库里的 id 静默略过。"""
        ids = sorted({int(s) for s in seq_ids})
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT seq_id, message_id, type, sender_name, timestamp, text, meta"
            f" FROM messages WHERE session_key = ? AND seq_id IN ({ph}) ORDER BY seq_id",
            (session_key, *ids)).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
