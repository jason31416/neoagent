"""QQ 通道适配器：把 OneBot 11 协议层接进 ChannelAdapter 抽象。

职责边界（保持薄）：
- 收：事件流 → 去自说/去重 → 消息段归一 → MessageEvent → 丢给核心
- 发：核心的回复 → send_group_msg / send_private_msg（引用挂 reply 段）
不做任何 agent 逻辑。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import aiohttp

from ...core.channel import ChannelAdapter
from ...core.events import Attachment, Conversation, MessageEvent, ReplyRef, SendResult, Sender
from ...core.frames import short_id
from .onebot import OneBotClient, message_text, normalize_message

logger = logging.getLogger(__name__)

TEXT_SPLIT_MAX = 1500     # 单条消息文本切条长度（QQ 单条有长度上限）


def _safe_filename(name: str) -> str:
    """文件名安全化：去掉路径分隔符和控制字符。"""
    name = str(name).replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name or "unnamed"


def _seg_filename(d: dict, fallback: str) -> str:
    """媒体段的文件名：file_name/name 优先，file 字段的尾段（形如 xx.image）兜底。"""
    for key in ("file_name", "name", "file"):
        v = _safe_filename(str(d.get(key) or ""))
        if v and v != "unnamed":
            return v[:80]
    return fallback


def _split_text(text: str, max_len: int) -> List[str]:
    """按长度切条，尽量在换行处断开。"""
    if len(text) <= max_len:
        return [text]
    chunks, rest = [], text
    while rest:
        if len(rest) <= max_len:
            chunks.append(rest)
            break
        cut = rest.rfind("\n", 0, max_len)
        if cut < max_len // 2:
            cut = max_len
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    return chunks


class QQAdapter(ChannelAdapter):
    name = "qq"

    def __init__(self, ws_url: str, token: str = "", data_dir=None,
                 max_bytes: int = 20 * 1024 * 1024) -> None:
        super().__init__()
        self._ws_url = ws_url
        self._token = token
        self._data_dir = data_dir
        self._max_bytes = max_bytes
        self._client: Optional[OneBotClient] = None
        self._session = None            # 附件下载用 aiohttp（connect 时建）
        self._self_id = ""
        self._group_names: Dict[str, str] = {}   # 群名缓存（事件不带群名，得查 API）
        self._seen: Dict[str, float] = {}   # message_id 去重（重连/上游重发兜底）

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        if not self._ws_url:
            raise RuntimeError("qq 通道未配置 ws_url（napcat 的 OneBot 11 WebSocket 地址）")
        # 下载 session 先建：事件在首连那一刻就会涌进来，附件下载不能踩空。
        # 连接失败要把 session 收干净（否则泄漏到 GC 才报 Unclosed client session）
        self._session = aiohttp.ClientSession(trust_env=True)
        try:
            self._client = OneBotClient(self._ws_url, self._token,
                                        on_event=self._on_event)
            await self._client.start()
        except Exception:
            await self._session.close()
            self._session = None
            raise
        try:
            info = await self._client.call("get_login_info")
            self._self_id = str(info.get("user_id") or "") or self._client.self_id
        except Exception as exc:
            self._self_id = self._client.self_id
            logger.warning("[qq] get_login_info 失败（不影响收消息）：%s", exc)
        await self._refresh_group_names()
        logger.info("[qq] 已连接，self_id=%s，群名缓存 %d 个",
                    self._self_id or "未知", len(self._group_names))

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._session is not None and not self._session.closed:
            await self._session.close()
        logger.info("[qq] 已断开")

    async def _refresh_group_names(self) -> None:
        """群名缓存：get_group_list 一把拉全；失败不碍事（记录里显示群 id）。"""
        if self._client is None:
            return
        try:
            groups = await self._client.call("get_group_list")
            for g in groups or []:
                gid = str(g.get("group_id") or "")
                name = str(g.get("group_name") or "").strip()
                if gid and name:
                    self._group_names[gid] = name
        except Exception as exc:
            logger.warning("[qq] 拉群名失败（不影响收消息）：%s", exc)

    def _group_title(self, group_id: str) -> str:
        """群名：缓存里有就带上（群记录/list_groups 显示群名）；没有现查一次。"""
        name = self._group_names.get(group_id, "")
        if not name and self._client is not None:
            asyncio.get_event_loop().create_task(self._fetch_group_name(group_id))
        return name

    async def _fetch_group_name(self, group_id: str) -> None:
        try:
            info = await self._client.call("get_group_info",
                                           {"group_id": _num(group_id)})
            name = str(info.get("group_name") or "").strip()
            if name:
                self._group_names[group_id] = name
        except Exception as exc:
            logger.debug("[qq] 查群名失败 %s：%s", group_id, exc)

    # ------------------------------------------------------------------
    # 收消息：事件 → MessageEvent
    # ------------------------------------------------------------------
    async def _on_event(self, evt: Dict[str, Any]) -> None:
        if evt.get("post_type") != "message":
            return                       # meta_event/notice/request 不收
        # self_id 事件帧里就有：先认下来，自己回流的过滤才灵（get_login_info 晚于事件）
        if evt.get("self_id"):
            self._self_id = self._self_id or str(evt["self_id"])
        sender = evt.get("sender") or {}
        sender_id = str(sender.get("user_id") or "")
        if not sender_id or sender_id == self._self_id:
            return                       # 自己发的（reportSelfMessage 打开时会回流）
        message_id = str(evt.get("message_id") or "")
        if message_id and self._is_duplicate(message_id):
            return

        segments = normalize_message(evt.get("message"), str(evt.get("raw_message") or ""))
        text, at_labels, at_bot, atts, resources, reply = self._parse_segments(segments)
        if not text and not atts:
            return

        group_id = str(evt.get("group_id") or "")
        is_group = bool(group_id) and evt.get("message_type") == "group"
        event = MessageEvent(
            id=message_id or f"qq-{int(time.time() * 1000)}",
            channel=self.name,
            sender=Sender(id=sender_id, name=short_id(sender_id)),
            conversation=Conversation(
                id=group_id if is_group else sender_id,
                type="group" if is_group else "direct",
                title=self._group_title(group_id) if is_group else ""),
            text=text,
            timestamp=float(evt.get("time") or time.time()),
            attachments=atts,
            reply_to=reply,
            metadata={"message_id": message_id, "user_id": sender_id,
                      "group_id": group_id if is_group else "",
                      "at_bot": at_bot, "at": at_labels},
        )
        # 按需下载：download_gate 说不（休眠群/非主人私聊）就零下载
        if self.download_gate is None or self.download_gate(event):
            await self._download_resources(message_id or event.id, resources)
        await self._dispatch(event)

    def _is_duplicate(self, message_id: str) -> bool:
        now = time.time()
        for k in [k for k, t in self._seen.items() if now - t > 300]:
            self._seen.pop(k, None)
        if message_id in self._seen:
            return True
        self._seen[message_id] = now
        return False

    def _parse_segments(self, segments: List[Dict[str, Any]]) -> tuple:
        """消息段 → (正文, 被@名单, 是否@机器人, 附件, 待下载资源, 引用)。

        at 段判定 @机器人是精确的（qq == self_id）；self_id 还没拿到时
        降级"有 at 即算 @"。图片/语音/视频/文件都出附件（字节按需下载）。"""
        parts: List[str] = []
        labels: List[str] = []
        atts: List[Attachment] = []
        resources: List[Tuple[Attachment, str]] = []   # (att, 下载 URL)
        reply: Optional[ReplyRef] = None
        at_bot = False

        for seg in segments:
            t, d = seg["type"], seg["data"]
            if t == "text":
                parts.append(str(d.get("text") or ""))
            elif t == "at":
                qq = str(d.get("qq") or "")
                label = str(d.get("name") or "").strip() or short_id(qq or "at")
                parts.append(f"@{label}")
                labels.append(label)
                if not self._self_id or qq == self._self_id or qq == "all":
                    at_bot = True
            elif t == "reply":
                rid = str(d.get("id") or "")
                if rid:
                    reply = ReplyRef(message_id=rid, resolved_from="unknown")
            elif t == "image":
                att = Attachment(kind="image",
                                 filename=_seg_filename(d, "image.img"),
                                 mime="image/*")
                url = str(d.get("url") or "")
                if not url and str(d.get("file") or "").startswith("http"):
                    url = str(d["file"])
                if url:
                    resources.append((att, url))
                atts.append(att)
                parts.append("（图）")
            elif t == "record":
                att = Attachment(kind="audio",
                                 filename=_seg_filename(d, "voice.silk"),
                                 mime=str(d.get("mime") or "audio/*"))
                url = str(d.get("url") or "") or (
                    str(d.get("file") or "")
                    if str(d.get("file") or "").startswith("http") else "")
                if url:
                    resources.append((att, url))
                atts.append(att)
            elif t == "video":
                att = Attachment(kind="video",
                                 filename=_seg_filename(d, "video.mp4"),
                                 mime=str(d.get("mime") or "video/*"))
                url = str(d.get("url") or "") or (
                    str(d.get("file") or "")
                    if str(d.get("file") or "").startswith("http") else "")
                if url:
                    resources.append((att, url))
                atts.append(att)
            elif t == "file":
                att = Attachment(kind="file",
                                 filename=_seg_filename(d, "file.bin"))
                url = str(d.get("url") or "") or (
                    str(d.get("file") or "")
                    if str(d.get("file") or "").startswith("http") else "")
                if url:
                    resources.append((att, url))
                atts.append(att)
                parts.append(f"（文件 {att.filename}）")
            elif t == "face":
                parts.append("（表情）")
            elif t in ("json", "xml"):
                parts.append("（卡片消息）")
            elif t == "forward":
                parts.append("（合并转发）")
            else:
                logger.debug("[qq] 忽略消息段 %s", t)
        return "".join(parts).strip(), labels, at_bot, atts, resources, reply

    # ------------------------------------------------------------------
    # 附件下载
    # ------------------------------------------------------------------
    async def _download_resources(self, message_id: str,
                                  resources: List[Tuple[Attachment, str]]) -> None:
        for att, url in resources:
            try:
                parsed = urlparse(url)
                if parsed.scheme not in ("http", "https"):
                    logger.warning("[qq] 附件 URL 协议不认，降级占位：%s", url[:80])
                    continue
                data = await self._fetch(url)
                if len(data) > self._max_bytes:
                    logger.warning("[qq] 附件过大 %dB，降级占位：%s", len(data), att.filename)
                    continue
                path = self._save_media(data, message_id, att.filename)
                att.size = len(data)
                att.local_path = str(path)
                logger.info("[qq] 附件已落盘 %s（%d 字节）", path.name, len(data))
            except Exception as exc:
                logger.warning("[qq] 附件下载失败 %s：%s", att.filename, exc)

    async def _fetch(self, url: str) -> bytes:
        assert self._session is not None

        async def _do() -> bytes:
            async with self._session.get(
                    url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                resp.raise_for_status()
                return await resp.read()

        return await asyncio.wait_for(_do(), timeout=60)

    def _save_media(self, data: bytes, message_id: str, filename: str) -> Path:
        d = Path(self._data_dir) / "media" / "qq" / _safe_filename(message_id)
        d.mkdir(parents=True, exist_ok=True)
        p = d / filename
        p.write_bytes(data)
        return p

    # ------------------------------------------------------------------
    # 发消息
    # ------------------------------------------------------------------
    def _target(self, event: MessageEvent) -> Tuple[str, Dict[str, Any]]:
        """事件 → (action, 目标参数)：群里发群、私聊发人。"""
        gid = str(event.metadata.get("group_id") or "")
        if event.conversation.type == "group" or gid:
            gid = gid or str(event.conversation.id)
            return "send_group_msg", {"group_id": _num(gid)}
        uid = str(event.metadata.get("user_id") or event.conversation.id)
        return "send_private_msg", {"user_id": _num(uid)}

    async def send_reply(self, event: MessageEvent, text: str,
                         quote_to: Optional[str] = None) -> SendResult:
        assert self._client is not None
        action, target = self._target(event)
        sent_id = None
        try:
            for i, chunk in enumerate(_split_text(text or "", TEXT_SPLIT_MAX)):
                msg: List[dict] = []
                if quote_to and i == 0:
                    msg.append({"type": "reply", "data": {"id": str(quote_to)}})
                msg.append({"type": "text", "data": {"text": chunk}})
                data = await self._client.call(action, {**target, "message": msg})
                sent_id = str(data.get("message_id") or "") or sent_id
            return SendResult(ok=True, message_id=sent_id)
        except Exception as exc:
            logger.warning("[qq] 发送失败：%s", exc)
            return SendResult(ok=False, error=str(exc), message_id=sent_id)

    async def send_media(self, event: MessageEvent, path: str,
                         quote_to: Optional[str] = None) -> SendResult:
        """发图片/文件为独立消息：字节 base64 进消息段（不依赖 napcat 本地路径）。"""
        assert self._client is not None
        action, target = self._target(event)
        p = Path(path)
        mime = mimetypes.guess_type(str(p))[0] or ""
        try:
            blob = p.read_bytes()
        except OSError as exc:
            return SendResult(ok=False, error=f"读文件失败：{exc}")
        file_val = "base64://" + base64.b64encode(blob).decode()
        seg_type = "image" if mime.startswith("image/") else "file"
        seg: Dict[str, Any] = {"type": seg_type, "data": {"file": file_val}}
        if seg_type == "file":
            seg["data"]["name"] = p.name
        msg: List[dict] = []
        if quote_to:
            msg.append({"type": "reply", "data": {"id": str(quote_to)}})
        msg.append(seg)
        try:
            data = await self._client.call(action, {**target, "message": msg})
            return SendResult(ok=True,
                              message_id=str(data.get("message_id") or "") or None)
        except Exception as exc:
            logger.warning("[qq] 媒体发送失败：%s", exc)
            return SendResult(ok=False, error=str(exc))

    # ------------------------------------------------------------------
    # 引用兜底：按 id 取消息文本（核心查库未命中时才调用）
    # ------------------------------------------------------------------
    async def fetch_message_text(self, message_id: str) -> Optional[str]:
        assert self._client is not None
        try:
            data = await self._client.call("get_msg", {"message_id": _num(message_id)})
            return message_text(data.get("message"), str(data.get("raw_message") or "")) or None
        except Exception as exc:
            logger.warning("[qq] get_msg 失败 %s：%s", message_id, exc)
            return None


def _num(v: str):
    """OneBot 的 id 字段是数字；能转就转，转不了原样（宽容）。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return v
