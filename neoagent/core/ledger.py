"""上下文库（SQLite）：两张表两个世界。

- **turns（主表）**：上下文的真相，一行 = 一条协议消息，存的都是**定稿**
  （入库前已完成属性净化、气泡编号、recall 快照固化——见 frames.py）：
  · user        用户消息（<message …> 标签定稿，图片字节在 blobs）
  · assistant   模型一次完整输出（气泡标签 + 标签外碎碎念，原样；calls=tool 调用数组；
                reasoning=思考草稿——协议回传要，模型看不到）
  · internal    系统内部注入（指令回执等）
  · tool_result 单个工具的返回（XML 原文，call_id 配对）
  · wake        自我唤醒（<wake> 块：闹钟到点/哨兵叫醒/补漏清单）
  装配（render.py）几乎零加工：按 seq 顺序抄进 messages 数组即可。

- **bubbles（平台消息表）**：投递的真相，一行 = 平台上真实出现的一个气泡：
  平台/聊天/平台消息 id ↔ 气泡编号 msg#N ↔ 所属主表行。发送回执、真引用挂靠、
  recall 快照来源都在这层，与上下文互不污染。

编号两套（语义分家）：
- seq_id   主表流水号（一切记录都有；游标/切点用），单调递增、永不回收；
- msg#N    气泡编号（只发给"聊天里的消息"，tool 行不占号），连续、持久化、不回收。

协议组（group_id）：assistant 行 + 它的 tool_result 行同组，是裁剪/切点的最小
原子——tool 链绝不从中间断开。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional

from .events import Attachment

# 主表行类型
TYPE_USER = "user"
TYPE_ASSISTANT = "assistant"
TYPE_INTERNAL = "internal"
TYPE_TOOL_RESULT = "tool_result"
TYPE_WAKE = "wake"        # 自我唤醒（<wake> 块；渲染 role=user 的系统消息）

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_key TEXT PRIMARY KEY,
  last_seq    INTEGER NOT NULL DEFAULT 0,   -- 主表流水号分配器
  last_bubble INTEGER NOT NULL DEFAULT 0,   -- 气泡编号 msg#N 分配器
  r_seen_seq  INTEGER NOT NULL DEFAULT 0,   -- Recorder 增量游标：已整理到哪
  cut_seq     INTEGER NOT NULL DEFAULT 0    -- 上下文切点：低于此的行不再装配（单调）
);
CREATE TABLE IF NOT EXISTS turns (
  seq_id      INTEGER NOT NULL,
  session_key TEXT NOT NULL,
  type        TEXT NOT NULL,          -- user | assistant | internal | tool_result | wake
  text        TEXT,                   -- 定稿文本（净化/编号/recall 均已固化）
  reasoning   TEXT,                   -- assistant 思考草稿（回传用）
  calls       JSON,                   -- assistant 的 tool_calls 数组
  call_id     TEXT,                   -- tool_result 配对 id
  tool_name   TEXT,
  meta        JSON,                   -- 附件引用（图片字节在 blobs）
  group_id    INTEGER NOT NULL,       -- 协议组（tool 链原子），= 组首行 seq
  ts          REAL,
  PRIMARY KEY (session_key, seq_id)
);
CREATE TABLE IF NOT EXISTS bubbles (
  session_key TEXT NOT NULL,
  bubble_id   INTEGER NOT NULL,       -- msg#N（模型面气泡编号）
  seq_id      INTEGER NOT NULL,       -- 所属主表行（turnid）
  direction   TEXT NOT NULL,          -- in | out
  platform    TEXT,
  chat_id     TEXT,
  message_id  TEXT,                   -- 平台消息 id（发出回执后回填）
  kind        TEXT NOT NULL,          -- text | image | file
  content     TEXT,                   -- 气泡纯文本（recall 快照取这里）
  sender      TEXT,
  meta        JSON,
  ts          REAL,
  PRIMARY KEY (session_key, bubble_id)
);
CREATE TABLE IF NOT EXISTS blobs (
  sha256       TEXT PRIMARY KEY,       -- 内容寻址：同图只存一份
  mime         TEXT,
  bytes        BLOB
);
CREATE TABLE IF NOT EXISTS kv (
  key   TEXT PRIMARY KEY,              -- 运行时设置（home 频道等），重启不丢
  value TEXT
);
CREATE TABLE IF NOT EXISTS group_messages (
  chat_id    TEXT NOT NULL,            -- 群 id（平台局部）
  message_id TEXT,                     -- 平台消息 id（引用解析用）
  sender     TEXT NOT NULL DEFAULT '', -- 发送者 id（身份只认 id）
  is_owner   INTEGER NOT NULL DEFAULT 0, -- 主人标记（代码层比对后落这）
  kind       TEXT NOT NULL DEFAULT 'text', -- text | image | file | audio
  text       TEXT NOT NULL DEFAULT '', -- 文字；非文字为占位（[图] 等）
  ts         REAL
);
CREATE INDEX IF NOT EXISTS idx_group_messages_ts ON group_messages(chat_id, ts);
CREATE TABLE IF NOT EXISTS wakes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 对外 w<N>
  session_key TEXT NOT NULL,
  at          REAL NOT NULL,           -- 下次触发时刻（重复项触发后重算）
  every       TEXT NOT NULL DEFAULT '',-- cron 表达式；空 = 一次性
  note        TEXT NOT NULL,           -- M 给自己的便利贴（到点的理由）
  status      TEXT NOT NULL DEFAULT 'pending',   -- pending | fired | cancelled
  source      TEXT NOT NULL DEFAULT 'alarm',     -- alarm | probe | event
  created_ts  REAL NOT NULL,
  last_fired  REAL,
  fired_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bubbles_msgid ON bubbles(session_key, message_id);
CREATE INDEX IF NOT EXISTS idx_turns_group ON turns(session_key, group_id);
CREATE INDEX IF NOT EXISTS idx_wakes_due ON wakes(status, at);
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


class MessageLedger:
    """按会话的上下文库（SQLite 持久化）。"""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------
    # 分配器：seq（主表流水）/ msg#N（气泡），都单调、永不回收
    # ------------------------------------------------------------------
    def _ensure_session(self, session_key: str) -> None:
        self._conn.execute(
            "INSERT INTO sessions(session_key) VALUES(?) "
            "ON CONFLICT(session_key) DO NOTHING", (session_key,))

    def _alloc_seq(self, session_key: str) -> int:
        self._ensure_session(session_key)
        row = self._conn.execute(
            "UPDATE sessions SET last_seq = last_seq + 1 WHERE session_key = ? "
            "RETURNING last_seq", (session_key,)).fetchone()
        self._conn.commit()
        return int(row[0])

    def alloc_bubbles(self, session_key: str, n: int) -> List[int]:
        """预分配 n 个气泡编号（入库定稿时要写进标签，须先拿号）。"""
        if n <= 0:
            return []
        self._ensure_session(session_key)
        row = self._conn.execute(
            "UPDATE sessions SET last_bubble = last_bubble + ? WHERE session_key = ? "
            "RETURNING last_bubble", (int(n), session_key)).fetchone()
        self._conn.commit()
        last = int(row[0])
        return list(range(last - n + 1, last + 1))

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def add_turn(self, *, session_key: str, type: str, text: str = "",
                 reasoning: Optional[str] = None,
                 calls: Optional[List[dict]] = None,
                 call_id: str = "", tool_name: str = "",
                 attachments: Optional[List[Attachment]] = None,
                 group_id: Optional[int] = None,
                 timestamp: Optional[float] = None) -> int:
        """记一条协议消息（主表），返回 seq_id。

        group_id：协议组（tool 链原子）。不传 = 自成一组（组首行的 seq）。
        图片字节落 blobs（内容寻址），meta 只留引用。
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
        seq = self._alloc_seq(session_key)
        self._conn.execute(
            "INSERT INTO turns(seq_id, session_key, type, text, reasoning, calls,"
            " call_id, tool_name, meta, group_id, ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (seq, session_key, type, text, reasoning or "",
             json.dumps(list(calls), ensure_ascii=False) if calls else None,
             call_id, tool_name,
             json.dumps(meta, ensure_ascii=False) if meta else None,
             int(group_id) if group_id else seq,
             timestamp if timestamp is not None else time.time()),
        )
        self._conn.commit()
        return seq

    def add_bubble(self, *, session_key: str, bubble_id: int, seq_id: int,
                   direction: str, kind: str = "text", content: str = "",
                   platform: str = "", chat_id: str = "", message_id: str = "",
                   sender: str = "", meta: Optional[dict] = None,
                   timestamp: Optional[float] = None) -> None:
        """记一个平台气泡（投递层）。message_id 发出回执后可用 set_bubble_message_id 补。"""
        self._ensure_session(session_key)
        self._conn.execute(
            "INSERT INTO bubbles(session_key, bubble_id, seq_id, direction, platform,"
            " chat_id, message_id, kind, content, sender, meta, ts)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_key, int(bubble_id), int(seq_id), direction, platform,
             chat_id, message_id or "", kind, content, sender,
             json.dumps(meta, ensure_ascii=False) if meta else None,
             timestamp if timestamp is not None else time.time()),
        )
        self._conn.commit()

    def set_bubble_message_id(self, session_key: str, bubble_id: int,
                              message_id: str) -> None:
        """回填气泡的平台消息 id（发出后才拿得到；供以后被真引用）。"""
        self._conn.execute(
            "UPDATE bubbles SET message_id = ? WHERE session_key = ? AND bubble_id = ?",
            (message_id, session_key, int(bubble_id)))
        self._conn.commit()

    def set_bubble_meta(self, session_key: str, bubble_id: int, meta: dict) -> None:
        """补记气泡的投递元数据（发送失败原因等）。"""
        self._conn.execute(
            "UPDATE bubbles SET meta = ? WHERE session_key = ? AND bubble_id = ?",
            (json.dumps(meta, ensure_ascii=False), session_key, int(bubble_id)))
        self._conn.commit()

    def _put_blob(self, sha: str, mime: str, data: bytes) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO blobs(sha256, mime, bytes) VALUES(?,?,?)",
            (sha, mime or "", sqlite3.Binary(data)))
        self._conn.commit()

    def blob(self, sha: str) -> Optional[tuple]:
        """按 sha 取图片字节 (mime, bytes)；没有返回 None。"""
        row = self._conn.execute(
            "SELECT mime, bytes FROM blobs WHERE sha256 = ?", (sha,)).fetchone()
        return (row[0] or "", bytes(row[1])) if row else None

    # ------------------------------------------------------------------
    # 气泡读取（recall 快照 / 引用解析）
    # ------------------------------------------------------------------
    _BCOLS = ("bubble_id, seq_id, direction, platform, chat_id, message_id,"
              " kind, content, sender, ts, meta")

    def get_bubble(self, session_key: str, bubble_id: int) -> Optional[Any]:
        row = self._conn.execute(
            f"SELECT {self._BCOLS} FROM bubbles"
            " WHERE session_key = ? AND bubble_id = ?",
            (session_key, int(bubble_id))).fetchone()
        return self._bubble(row) if row else None

    def find_bubble(self, session_key: str, message_id: str,
                    channel: str = "") -> Optional[Any]:
        """按平台消息 id 反查气泡（入站引用解析主路径）。

        平台消息 id 是**平台局部**的（两平台可能撞 id），channel 非空时
        只在该频道的气泡里找——跨频道的引用本来就不成立。"""
        if not message_id:
            return None
        sql = (f"SELECT {self._BCOLS} FROM bubbles"
               " WHERE session_key = ? AND message_id = ?")
        args: list = [session_key, message_id]
        if channel:
            sql += " AND platform = ?"
            args.append(channel)
        row = self._conn.execute(
            sql + " ORDER BY bubble_id DESC LIMIT 1", args).fetchone()
        return self._bubble(row) if row else None

    def last_inbound(self, session_key: str,
                     channel: str = "") -> Optional[Any]:
        """最新一条入站气泡（direction=in）；channel 非空时限定该频道。

        用途：出口路由的默认频道（"用户最后说话的频道"）、出站气泡的 chat 归属。"""
        sql = (f"SELECT {self._BCOLS} FROM bubbles"
               " WHERE session_key = ? AND direction = 'in'")
        args: list = [session_key]
        if channel:
            sql += " AND platform = ?"
            args.append(channel)
        row = self._conn.execute(
            sql + " ORDER BY bubble_id DESC LIMIT 1", args).fetchone()
        return self._bubble(row) if row else None

    def first_inbound(self, session_key: str) -> Optional[Any]:
        """最早一条入站气泡（home 未设置时的默认 = 第一条用户消息的频道）。"""
        row = self._conn.execute(
            f"SELECT {self._BCOLS} FROM bubbles"
            " WHERE session_key = ? AND direction = 'in'"
            " ORDER BY bubble_id ASC LIMIT 1", (session_key,)).fetchone()
        return self._bubble(row) if row else None

    @staticmethod
    def _bubble(r) -> Any:
        meta = {}
        try:
            meta = json.loads(r[10]) if len(r) > 10 and r[10] else {}
        except Exception:
            meta = {}
        return SimpleNamespace(
            bubble_id=int(r[0]), seq_id=int(r[1]), direction=r[2] or "",
            platform=r[3] or "", chat_id=r[4] or "", message_id=r[5] or "",
            kind=r[6] or "text", content=r[7] or "", sender=r[8] or "",
            timestamp=float(r[9] or 0), is_owner=bool(meta.get("owner")),
        )

    # ------------------------------------------------------------------
    # Recorder 状态：增量游标 r_seen_seq + 上下文切点 cut_seq（切点由压缩机械推进）
    # ------------------------------------------------------------------
    def get_recorder_state(self, session_key: str) -> tuple:
        """(r_seen_seq, cut_seq)；会话不存在时 (0, 0)。"""
        row = self._conn.execute(
            "SELECT r_seen_seq, cut_seq FROM sessions WHERE session_key = ?",
            (session_key,)).fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    def set_recorder_state(self, session_key: str, *,
                           r_seen_seq: Optional[int] = None,
                           cut_seq: Optional[int] = None) -> None:
        """更新 Recorder 状态。

        cut_seq 两个机器不变量：①单调只进不退；②落点吸附到协议组边界
        （tool 链绝不从中间断开）——传进来的值会下吸附到所在组的组首。
        """
        self._ensure_session(session_key)
        if r_seen_seq is not None:
            self._conn.execute("UPDATE sessions SET r_seen_seq = ? WHERE session_key = ?",
                               (int(r_seen_seq), session_key))
        if cut_seq is not None:
            cut = self.group_start(session_key, int(cut_seq))
            self._conn.execute(
                "UPDATE sessions SET cut_seq = MAX(cut_seq, ?) WHERE session_key = ?",
                (cut, session_key))
        self._conn.commit()

    def group_start(self, session_key: str, seq_id: int) -> int:
        """seq 所在协议组的组首 seq（切点吸附用）；行不存在就原样返回。"""
        row = self._conn.execute(
            "SELECT group_id FROM turns WHERE session_key = ? AND seq_id = ?",
            (session_key, int(seq_id))).fetchone()
        return int(row[0]) if row else int(seq_id)

    def session_keys(self) -> List[str]:
        """全部会话（慢周期遍历用）。"""
        return [r[0] for r in self._conn.execute(
            "SELECT session_key FROM sessions ORDER BY session_key")]

    # ------------------------------------------------------------------
    # kv 运行时设置（home 频道等；指令层写、路由层读，重启不丢）
    # ------------------------------------------------------------------
    def get_setting(self, key: str, default: str = "") -> str:
        row = self._conn.execute(
            "SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)))
        self._conn.commit()

    # ------------------------------------------------------------------
    # 群记录（group_messages）：所有群消息的流水，谁都能查（read_group_chat），
    # 与任何 agent 的上下文无关——30 天保留，只存文字（图 = [图] 占位）。
    # ------------------------------------------------------------------
    _GCOLS = ("chat_id, message_id, sender, is_owner, kind, text, ts")

    @staticmethod
    def _group_msg(r) -> Any:
        return SimpleNamespace(
            chat_id=r[0] or "", message_id=r[1] or "", sender=r[2] or "",
            is_owner=bool(r[3]), kind=r[4] or "text", text=r[5] or "",
            timestamp=float(r[6] or 0),
        )

    def log_group_message(self, *, chat_id: str, sender: str, text: str,
                          kind: str = "text", is_owner: bool = False,
                          message_id: str = "",
                          timestamp: Optional[float] = None) -> None:
        """落一条群记录（图片等非文字传 kind + 占位 text）。"""
        self._conn.execute(
            "INSERT INTO group_messages(chat_id, message_id, sender, is_owner,"
            " kind, text, ts) VALUES(?,?,?,?,?,?,?)",
            (chat_id, message_id or "", sender or "", 1 if is_owner else 0,
             kind or "text", text or "",
             timestamp if timestamp is not None else time.time()))
        self._conn.commit()

    def group_log_range(self, chat_id: str, *, start: float = 0.0,
                        end: float = 0.0, query: str = "",
                        limit: int = 200, desc: bool = False) -> List[Any]:
        """[start, end] 区间的群记录（默认旧→新；desc=True 新→旧）。"""
        sql = f"SELECT {self._GCOLS} FROM group_messages WHERE chat_id = ?"
        args: list = [chat_id]
        if start > 0:
            sql += " AND ts >= ?"
            args.append(float(start))
        if end > 0:
            sql += " AND ts <= ?"
            args.append(float(end))
        if query:
            sql += " AND text LIKE ?"
            args.append(f"%{query}%")
        sql += " ORDER BY ts " + ("DESC" if desc else "ASC") + " LIMIT ?"
        args.append(max(1, int(limit)))
        return [self._group_msg(r) for r in self._conn.execute(sql, args)]

    def group_log_count(self, chat_id: str, *, start: float = 0.0,
                        end: float = 0.0, query: str = "") -> int:
        """区间内总条数（头尾截断时算"中间省略 N 条"用）。"""
        sql = "SELECT COUNT(*) FROM group_messages WHERE chat_id = ?"
        args: list = [chat_id]
        if start > 0:
            sql += " AND ts >= ?"
            args.append(float(start))
        if end > 0:
            sql += " AND ts <= ?"
            args.append(float(end))
        if query:
            sql += " AND text LIKE ?"
            args.append(f"%{query}%")
        return int(self._conn.execute(sql, args).fetchone()[0])

    def group_log_chats(self) -> List[str]:
        """有记录的群 id 清单（list_groups 用）。"""
        return [r[0] for r in self._conn.execute(
            "SELECT DISTINCT chat_id FROM group_messages ORDER BY chat_id")]

    def gc_group_log(self, before_ts: float) -> int:
        """删 before_ts 之前的群记录（保留期清扫），返回删除条数。"""
        cur = self._conn.execute(
            "DELETE FROM group_messages WHERE ts < ?", (float(before_ts),))
        self._conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------------
    # blob 回收：删"只被该会话切点以下的行"引用的图（压缩即销毁）。
    # 内容寻址跨会话共享——有任何其他行（别的会话、或本会话切点以上）引用
    # 就不删，主 agent 在上下文里的图不受影响。
    # ------------------------------------------------------------------
    def reclaim_blobs(self, session_key: str) -> int:
        """回收该会话已裁掉的图；返回删除个数。"""
        _, cut = self.get_recorder_state(session_key)
        rows = self._conn.execute(
            "SELECT seq_id, meta FROM turns WHERE session_key = ?"
            " AND seq_id < ? AND meta LIKE '%image_sha%'",
            (session_key, int(cut))).fetchall()
        gone = 0
        for seq_id, meta in rows:
            try:
                atts = (json.loads(meta) or {}).get("attachments", [])
            except Exception:
                continue
            for a in atts:
                sha = a.get("image_sha")
                if not sha:
                    continue
                # 谁还在引用它？排除"本会话切点以下"的引用，其余都算活引用
                refs = self._conn.execute(
                    "SELECT t.seq_id, t.session_key, s.cut_seq FROM turns t"
                    " JOIN sessions s ON s.session_key = t.session_key"
                    " WHERE t.meta LIKE ?", (f"%{sha}%",)).fetchall()
                alive = any(not (sk == session_key and sq < cut)
                            for sq, sk, _c in refs)
                if not alive:
                    self._conn.execute("DELETE FROM blobs WHERE sha256 = ?", (sha,))
                    gone += 1
        if gone:
            self._conn.commit()
        return gone

    # ------------------------------------------------------------------
    # wakes（自我唤醒表）：M 排的闹钟、调度器响，重启不丢
    # ------------------------------------------------------------------
    _WCOLS = ("id, session_key, at, every, note, status, source,"
              " created_ts, last_fired, fired_count")

    @staticmethod
    def _wake(r) -> Any:
        return SimpleNamespace(
            id=int(r[0]), session_key=r[1], at=float(r[2] or 0), every=r[3] or "",
            note=r[4] or "", status=r[5] or "", source=r[6] or "",
            created_ts=float(r[7] or 0),
            last_fired=float(r[8]) if r[8] else None,
            fired_count=int(r[9] or 0))

    def add_wake(self, *, session_key: str, at: float, note: str,
                 every: str = "", source: str = "alarm") -> int:
        """排一个唤醒（once 或 cron 重复），返回 w<N> 的 N。"""
        cur = self._conn.execute(
            "INSERT INTO wakes(session_key, at, every, note, status, source, created_ts)"
            " VALUES(?,?,?,?, 'pending', ?, ?)",
            (session_key, float(at), every or "", note, source, time.time()))
        self._conn.commit()
        return int(cur.lastrowid)

    def get_wake(self, wake_id: int) -> Optional[Any]:
        row = self._conn.execute(
            f"SELECT {self._WCOLS} FROM wakes WHERE id = ?", (int(wake_id),)).fetchone()
        return self._wake(row) if row else None

    def list_wakes(self, session_key: str, status: str = "") -> List[Any]:
        """该会话的唤醒清单（按 at 升序）；status 空 = 全部。"""
        sql = f"SELECT {self._WCOLS} FROM wakes WHERE session_key = ?"
        args: list = [session_key]
        if status:
            sql += " AND status = ?"
            args.append(status)
        return [self._wake(r) for r in self._conn.execute(
            sql + " ORDER BY at", args).fetchall()]

    def due_wakes(self, now: Optional[float] = None) -> List[Any]:
        """到点（含过点未补）的 pending 唤醒，按 at 升序。"""
        ts = time.time() if now is None else float(now)
        return [self._wake(r) for r in self._conn.execute(
            f"SELECT {self._WCOLS} FROM wakes WHERE status = 'pending' AND at <= ?"
            " ORDER BY at", (ts,)).fetchall()]

    def mark_fired(self, wake_id: int, *, fired_at: Optional[float] = None,
                   next_at: Optional[float] = None) -> None:
        """触发记账：fired_count+1；重复项给 next_at 就续排，否则转 fired 回收。"""
        ts = time.time() if fired_at is None else float(fired_at)
        if next_at is not None:
            self._conn.execute(
                "UPDATE wakes SET fired_count = fired_count + 1, last_fired = ?, at = ?"
                " WHERE id = ?", (ts, float(next_at), int(wake_id)))
        else:
            self._conn.execute(
                "UPDATE wakes SET fired_count = fired_count + 1, last_fired = ?,"
                " status = 'fired' WHERE id = ?", (ts, int(wake_id)))
        self._conn.commit()

    def reschedule_wake(self, wake_id: int, at: float) -> None:
        """改期（pending 项挪到新的时刻）。"""
        self._conn.execute("UPDATE wakes SET at = ? WHERE id = ?",
                           (float(at), int(wake_id)))
        self._conn.commit()

    def cancel_wake(self, wake_id: int) -> str:
        """作废一个唤醒；返回状态说明（幂等，好回给 M）。"""
        row = self._conn.execute(
            "SELECT status FROM wakes WHERE id = ?", (int(wake_id),)).fetchone()
        if row is None:
            return "没有这个唤醒"
        if row[0] != "pending":
            return f"已经是 {row[0]} 状态了，不能再取消"
        self._conn.execute("UPDATE wakes SET status = 'cancelled' WHERE id = ?",
                           (int(wake_id),))
        self._conn.commit()
        return "cancelled"

    # ------------------------------------------------------------------
    # 主表读取
    # ------------------------------------------------------------------
    _COLS = ("seq_id, type, text, reasoning, calls, call_id, tool_name,"
             " meta, group_id, ts")

    def _row_to_entry(self, r) -> Any:
        meta = json.loads(r[7]) if r[7] else {}
        atts = [_att_from_dict(d) for d in meta.get("attachments", [])]
        for a in atts:                              # 图片字节现取（blobs 内容寻址）
            if a.image_sha:
                hit = self.blob(a.image_sha)
                if hit is not None:
                    a.data = hit[1]
        return SimpleNamespace(
            seq_id=int(r[0]), type=r[1], text=r[2] or "",
            reasoning=r[3] or "",
            calls=[SimpleNamespace(**c) for c in (json.loads(r[4]) if r[4] else [])],
            call_id=r[5] or "", tool_name=r[6] or "",
            group_id=int(r[8]), timestamp=float(r[9] or 0),
            attachments=atts,
        )

    def since(self, session_key: str, after_seq: int, limit: int = 500) -> List[Any]:
        """游标之后的新行（旧→新）。"""
        rows = self._conn.execute(
            f"SELECT {self._COLS} FROM turns WHERE session_key = ? AND seq_id > ?"
            " ORDER BY seq_id LIMIT ?",
            (session_key, after_seq, limit)).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def since_time(self, session_key: str, ts_min: float, limit: int = 5000) -> List[Any]:
        """某时刻之后的行（旧→新），Recorder 慢周期回看用。"""
        rows = self._conn.execute(
            f"SELECT {self._COLS} FROM turns WHERE session_key = ? AND ts >= ?"
            " ORDER BY seq_id LIMIT ?",
            (session_key, ts_min, limit)).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def tail(self, session_key: str, before_seq: Optional[int] = None,
             limit: int = 500) -> List[Any]:
        """从最新往老翻批（新→旧），装配扫预算用。"""
        if before_seq is None:
            rows = self._conn.execute(
                f"SELECT {self._COLS} FROM turns WHERE session_key = ?"
                " ORDER BY seq_id DESC LIMIT ?",
                (session_key, limit)).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT {self._COLS} FROM turns WHERE session_key = ? AND seq_id < ?"
                " ORDER BY seq_id DESC LIMIT ?",
                (session_key, before_seq, limit)).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def visible(self, session_key: str, window: int) -> List[Any]:
        """最新 window 行（旧→新）。"""
        rows = self._conn.execute(
            f"SELECT {self._COLS} FROM turns WHERE session_key = ?"
            " ORDER BY seq_id DESC LIMIT ?",
            (session_key, window)).fetchall()
        return [self._row_to_entry(r) for r in reversed(rows)]

    def by_seqs(self, session_key: str, seq_ids: List[int]) -> List[Any]:
        """按 seq 取行（旧→新）。不在库里的 id 静默略过。"""
        ids = sorted({int(s) for s in seq_ids})
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT {self._COLS} FROM turns WHERE session_key = ?"
            f" AND seq_id IN ({ph}) ORDER BY seq_id",
            (session_key, *ids)).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
