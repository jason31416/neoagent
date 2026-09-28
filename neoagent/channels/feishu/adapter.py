"""飞书通道适配器：官方 lark-oapi SDK（WebSocket 长连接，无需公网回调地址）。

参考 hermes 的 plugins/platforms/feishu 砍到最小集：
- 收：P2ImMessageReceiveV1 事件（文本 / 图片 / 文件 / 语音 / 视频）
- DM 直接回，群聊仅在 @机器人 时回（否则群里会被刷屏）
- 引用：parent_id → 先查消息库（核心层），库没有才 message.get 拉文本
- 回：create 接口发普通消息到会话（不做消息级引用）

用法前置：飞书开放平台建自建应用，开通 im:message 收发权限
（群聊引用解析还需 im:message.group_msg），把 App ID / Secret 填进 config.toml。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import List, Optional, Tuple

from ...core.channel import ChannelAdapter
from ...core.events import Attachment, Conversation, MessageEvent, ReplyRef, SendResult, Sender

logger = logging.getLogger(__name__)

# lark-oapi 懒加载：没装依赖时 import 本模块不炸，connect 时再报错
try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        CreateFileRequest, CreateFileRequestBody,
        CreateImageRequest, CreateImageRequestBody,
        GetMessageRequest, GetMessageResourceRequest,
        CreateMessageRequest, CreateMessageRequestBody,
        P2ImMessageReceiveV1, ReplyMessageRequest, ReplyMessageRequestBody,
    )
    LARK_AVAILABLE = True
except ImportError:
    LARK_AVAILABLE = False


def _ms_to_sec(value) -> float:
    """飞书 create_time 是毫秒时间戳字符串（偶尔是 int），统一转秒。"""
    try:
        return int(value) / 1000
    except (TypeError, ValueError):
        return time.time()


def _parse_post(content: dict, resources: list) -> List[str]:
    """post 富文本 → 段落文本列表；内嵌图追加进 resources（(att, image_key, "image")）。

    形态：{"title":…, "content":[[{tag,text,href,user_id,image_key…},…],…]}；
    部分客户端带语言包裹 {"zh_cn":{…}}。未知 tag 降级占位，不丢段落。
    """
    body = content
    if "content" not in body:
        for v in content.values():
            if isinstance(v, dict) and "content" in v:
                body = v
                break
    lines: List[str] = []
    title = str(body.get("title") or "").strip()
    if title:
        lines.append(title)
    for para in body.get("content") or []:
        parts: List[str] = []
        for run in para if isinstance(para, list) else [para]:
            if not isinstance(run, dict):
                continue
            tag = str(run.get("tag") or "")
            if tag in ("text", "md", "code_block"):
                parts.append(str(run.get("text") or ""))
            elif tag == "a":
                label = str(run.get("text") or run.get("href") or "")
                href = str(run.get("href") or "")
                parts.append(f"{label}（{href}）" if href and href != label else label)
            elif tag == "at":
                name = str(run.get("user_name") or "")
                uid = str(run.get("user_id") or "")
                parts.append(f"@{name or uid}" if (name or uid) else "")
            elif tag in ("img", "media"):
                key = str(run.get("image_key") or run.get("file_key") or "")
                if key:
                    att = Attachment(kind="image", filename=f"{key}.img", mime="image/*")
                    resources.append((att, key, "image"))
                parts.append("（图）" if tag == "img" else "（媒体）")
            elif tag == "emotion":
                parts.append("（表情）")
            elif tag == "hr":
                parts.append("———")
            elif tag == "plain_text":
                parts.append(str(run.get("text") or ""))
            else:
                txt = str(run.get("text") or "")
                parts.append(txt or "（不支持的内容）")
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return lines


def _safe_filename(name: str) -> str:
    """文件名安全化：去掉路径分隔符和控制字符。"""
    import re
    name = str(name).replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name or "unnamed"


class FeishuAdapter(ChannelAdapter):
    name = "feishu"

    def __init__(self, app_id: str, app_secret: str, data_dir=None,
                 max_bytes: int = 20 * 1024 * 1024) -> None:
        super().__init__()
        if not LARK_AVAILABLE:
            raise RuntimeError("缺少依赖 lark-oapi：uv sync --extra feishu")
        self.app_id = app_id
        self.app_secret = app_secret
        self._data_dir = data_dir
        self._max_bytes = max_bytes
        self._client = None          # lark REST 客户端（发消息/下载资源用）
        self._ws_client = None       # lark WS 客户端（收消息用）
        self._loop = None            # 主事件循环（connect 时记录）
        self._ws_loop = None         # WS 线程专用事件循环（disconnect 时停它）

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        if not self.app_id or not self.app_secret:
            raise RuntimeError("feishu 通道未配置 app_id/app_secret")

        self._client = (lark.Client.builder()
                        .app_id(self.app_id)
                        .app_secret(self.app_secret)
                        .log_level(lark.LogLevel.WARNING)
                        .build())

        # WS 长连接：事件处理器（同步签名是 SDK 要求，处理里再开 task）
        handler = (lark.EventDispatcherHandler.builder("", "")
                   .register_p2_im_message_receive_v1(self._on_message)
                   .build())
        self._ws_client = lark.ws.Client(
            self.app_id, self.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.WARNING,
        )

        # SDK 的 start() 会阻塞且自带模块级事件循环（lark_oapi.ws.client.loop）。
        # 坑：SDK 是在我们的事件循环里被导入的，它抓到的 loop 就是我们正在跑的
        # 那个，直接 start() 会报 "This event loop is already running"。
        # 解法：在专用线程里给 SDK 换一个全新的 loop 再 start。
        # 线程必须是 daemon 且可停：start() 卡在 run_until_complete(_select())（无限 sleep），
        # 停法 = 停它专用的 loop（run_until_complete 随之抛 RuntimeError 收场）。
        self._loop = asyncio.get_running_loop()
        self._ws_loop: Optional[asyncio.AbstractEventLoop] = None

        def _run_ws() -> None:
            import lark_oapi.ws.client as lark_ws_client_mod
            ws_loop = asyncio.new_event_loop()
            lark_ws_client_mod.loop = ws_loop  # SDK 模块级 loop 指到本线程专用
            self._ws_loop = ws_loop
            try:
                self._ws_client.start()       # 内部 run_until_complete，阻塞到 loop 停
            except RuntimeError:
                logger.info("[feishu] WS 线程收尾退出")   # loop 被停是预期收场
            except Exception:
                logger.exception("[feishu] WS 线程异常退出")

        threading.Thread(target=_run_ws, name="feishu-ws", daemon=True).start()
        logger.info("[feishu] WS 长连接启动")

    async def disconnect(self) -> None:
        """停掉 WS：先尽量关 socket，再停它专用的事件循环。
        线程是 daemon——就算这里没停干净，进程退出也不会被它拖住。"""
        ws_loop = self._ws_loop
        if ws_loop is None:
            return
        try:
            if self._ws_client is not None and hasattr(self._ws_client, "_disconnect"):
                fut = asyncio.run_coroutine_threadsafe(self._ws_client._disconnect(), ws_loop)
                fut.result(timeout=2)
        except Exception:
            pass                        # 关不掉就算了，loop 一停 socket 也活不了
        try:
            ws_loop.call_soon_threadsafe(ws_loop.stop)
        except RuntimeError:
            pass
        logger.info("[feishu] 断开")

    # ------------------------------------------------------------------
    # 收消息
    # ------------------------------------------------------------------
    def _on_message(self, data) -> None:
        """SDK 事件回调（在 SDK 线程里跑）。解析后送回主循环处理。"""
        try:
            event, resources = self._parse_event(data)
        except Exception:
            logger.exception("[feishu] 解析消息出错")
            return
        if event is None:
            return
        asyncio.run_coroutine_threadsafe(self._process(event, resources), self._loop)

    async def _process(self, event: MessageEvent,
                       resources: List[Tuple[Attachment, str, str]]) -> None:
        """主循环里跑：先下载资源落盘，再交给核心。"""
        await self._download_resources(event.id, resources)
        await self._dispatch(event)

    def _parse_event(self, data) -> Tuple[Optional[MessageEvent], list]:
        """把 P2ImMessageReceiveV1 归一化成 MessageEvent + 待下载资源清单。"""
        try:
            evt = data.event
            msg = evt.message
            sender = evt.sender

            content = {}
            try:
                content = json.loads(msg.content or "{}")
            except Exception:
                pass

            text = ""
            resources: List[Tuple[Attachment, str, str]] = []  # (att, file_key, res_type)
            mtype = msg.message_type

            if mtype == "text":
                text = str(content.get("text", "")).strip()
                # 群聊里只有 @机器人 才响应；去掉 @_user_N 占位符
                if msg.chat_type == "group":
                    if not (msg.mentions or []):
                        return None, []
                    for m in msg.mentions:
                        text = text.replace(f"@_user_{m.key}", "").strip()
            elif mtype == "post":
                # 富文本：逐段展开成纯文本；内嵌图当图片资源下载
                text = ""
                for para in _parse_post(content, resources):
                    text += para + "\n"
                text = text.strip()
                for m in msg.mentions or []:
                    text = text.replace(f"@_user_{m.key}", "").strip()
                if msg.chat_type == "group" and not (msg.mentions or []):
                    return None, []
            elif mtype == "image":
                key = str(content.get("image_key", ""))
                if key:
                    att = Attachment(kind="image", filename=f"{key}.img", mime="image/*")
                    resources.append((att, key, "image"))
            elif mtype == "file":
                key = str(content.get("file_key", ""))
                name = _safe_filename(content.get("file_name") or f"{key}.bin")
                if key:
                    att = Attachment(kind="file", filename=name)
                    resources.append((att, key, "file"))
            elif mtype == "audio":
                key = str(content.get("file_key", ""))
                if key:
                    att = Attachment(kind="audio", filename=f"{key}.audio", mime="audio/*")
                    resources.append((att, key, "file"))
            elif mtype == "video":
                key = str(content.get("file_key", ""))
                if key:
                    att = Attachment(kind="video", filename=f"{key}.video", mime="video/*")
                    resources.append((att, key, "file"))
            else:
                logger.info("[feishu] 忽略消息类型 %s", mtype)
                return None, []

            if not text and not resources:
                return None, []

            # 引用：只有 parent_id，原文靠消息库/兜底拉取（resolved_from 由核心判定）
            reply_to = None
            if msg.parent_id:
                reply_to = ReplyRef(message_id=str(msg.parent_id), resolved_from="unknown")

            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            event = MessageEvent(
                id=msg.message_id,
                channel=self.name,
                sender=Sender(id=sender_id or "unknown", name=sender_id or ""),
                conversation=Conversation(
                    id=msg.chat_id,
                    type="group" if msg.chat_type == "group" else "direct"),
                text=text,
                timestamp=_ms_to_sec(msg.create_time),
                attachments=[a for a, _k, _t in resources],
                reply_to=reply_to,
                metadata={"message_id": msg.message_id, "chat_id": msg.chat_id},
            )
            return event, resources
        except Exception:
            logger.exception("[feishu] 解析事件失败")
            return None, []

    # ------------------------------------------------------------------
    # 资源下载（im.v1.message_resource.get）
    # ------------------------------------------------------------------
    async def _download_resources(self, message_id: str,
                                  resources: List[Tuple[Attachment, str, str]]) -> None:
        for att, file_key, res_type in resources:
            try:
                data = await asyncio.to_thread(self._get_resource, message_id, file_key, res_type)
                if len(data) > self._max_bytes:
                    logger.warning("[feishu] 附件过大 %dB，降级占位：%s", len(data), att.filename)
                    continue
                path = self._save_media(data, message_id, att.filename)
                att.size = len(data)
                att.local_path = str(path)
                logger.info("[feishu] 附件已落盘 %s（%d 字节）", path.name, len(data))
            except Exception as exc:
                logger.warning("[feishu] 资源下载失败 %s：%s", att.filename, exc)

    def _get_resource(self, message_id: str, file_key: str, res_type: str) -> bytes:
        """同步下载一个消息资源（SDK 是同步 API，外面包了 to_thread）。"""
        request = (GetMessageResourceRequest.builder()
                   .message_id(message_id).file_key(file_key).type(res_type)
                   .build())
        resp = self._client.im.v1.message_resource.get(request)
        if not resp.success():
            raise RuntimeError(f"message_resource.get 失败 code={resp.code} msg={resp.msg}")
        return resp.file.read()

    def _save_media(self, data: bytes, message_id: str, filename: str):
        from pathlib import Path
        d = Path(self._data_dir) / "media" / "feishu" / _safe_filename(message_id)
        d.mkdir(parents=True, exist_ok=True)
        p = d / filename
        p.write_bytes(data)
        return p

    # ------------------------------------------------------------------
    # 引用兜底：按 id 取消息文本（核心查库未命中时才调用）
    # ------------------------------------------------------------------
    async def fetch_message_text(self, message_id: str) -> Optional[str]:
        """im.v1.message.get 拉被引用消息的文本。
        群聊消息需要 im:message.group_msg 权限；撤回消息拿不到 content。"""
        try:
            data = await asyncio.to_thread(self._get_message, message_id)
            return data
        except Exception as exc:
            logger.warning("[feishu] message.get 失败 %s：%s", message_id, exc)
            return None

    def _get_message(self, message_id: str) -> Optional[str]:
        request = GetMessageRequest.builder().message_id(message_id).build()
        resp = self._client.im.v1.message.get(request)
        if not resp.success():
            raise RuntimeError(f"message.get 失败 code={resp.code} msg={resp.msg}")
        items = (resp.data.items if resp.data else None) or []
        if not items:
            return None
        m = items[0]
        if getattr(m, "deleted", False):
            return "（该消息已被撤回）"
        try:
            content = json.loads(m.body.content if m.body else "{}")
            if m.msg_type == "text":
                return str(content.get("text", ""))
            return f"[{m.msg_type} 消息]"
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 发消息
    # ------------------------------------------------------------------
    async def send_reply(self, event: MessageEvent, text: str,
                         quote_to: Optional[str] = None) -> SendResult:
        """发消息到会话。quote_to 有值且可用时走 reply 接口挂真引用（M 的 replyTo
        落到通道 UI），否则 create 发普通消息。"""
        try:
            content = json.dumps({"text": text}, ensure_ascii=False)
            if quote_to:
                body = (ReplyMessageRequestBody.builder()
                        .content(content)
                        .msg_type("text")
                        .build())
                request = (ReplyMessageRequest.builder()
                           .message_id(quote_to)
                           .request_body(body)
                           .build())
                response = self._client.im.v1.message.reply(request)
            else:
                body = (CreateMessageRequestBody.builder()
                        .receive_id(event.metadata.get("chat_id") or event.conversation.id)
                        .msg_type("text")
                        .content(content)
                        .build())
                request = (CreateMessageRequest.builder()
                           .receive_id_type("chat_id")
                           .request_body(body)
                           .build())
                response = self._client.im.v1.message.create(request)
            if not response.success():
                err = f"code={response.code} msg={response.msg}"
                logger.error("[feishu] 发送失败 %s", err)
                return SendResult(ok=False, error=err)
            sent_id = getattr(getattr(response, "data", None), "message_id", None)
            return SendResult(ok=True, message_id=sent_id)
        except Exception as exc:
            logger.exception("[feishu] 发送异常")
            return SendResult(ok=False, error=str(exc))

    async def send_media(self, event: MessageEvent, path: str,
                         quote_to: Optional[str] = None) -> SendResult:
        """发图片/文件为**独立消息**：上传拿 image_key/file_key → create/reply。
        svg 等 image/* 但非栅格的走 file 通道（飞书图片上传只收栅格图）。"""
        import mimetypes
        from pathlib import Path
        try:
            p = Path(path)
            mime = mimetypes.guess_type(str(p))[0] or ""
            if mime in ("image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"):
                with open(p, "rb") as f:
                    body = (CreateImageRequestBody.builder()
                            .image_type("message").image(f).build())
                    resp = self._client.im.v1.image.create(
                        CreateImageRequest.builder().request_body(body).build())
                if not resp.success():
                    err = f"图片上传失败 code={resp.code} msg={resp.msg}"
                    logger.error("[feishu] %s", err)
                    return SendResult(ok=False, error=err)
                content = json.dumps({"image_key": resp.data.image_key})
                msg_type = "image"
            else:
                with open(p, "rb") as f:
                    body = (CreateFileRequestBody.builder()
                            .file_type("stream").file_name(p.name).file(f).build())
                    resp = self._client.im.v1.file.create(
                        CreateFileRequest.builder().request_body(body).build())
                if not resp.success():
                    err = f"文件上传失败 code={resp.code} msg={resp.msg}"
                    logger.error("[feishu] %s", err)
                    return SendResult(ok=False, error=err)
                content = json.dumps({"file_key": resp.data.file_key})
                msg_type = "file"

            if quote_to:
                rbody = (ReplyMessageRequestBody.builder()
                         .content(content).msg_type(msg_type).build())
                request = (ReplyMessageRequest.builder()
                           .message_id(quote_to).request_body(rbody).build())
                response = self._client.im.v1.message.reply(request)
            else:
                sbody = (CreateMessageRequestBody.builder()
                         .receive_id(event.metadata.get("chat_id") or event.conversation.id)
                         .msg_type(msg_type).content(content).build())
                request = (CreateMessageRequest.builder()
                           .receive_id_type("chat_id").request_body(sbody).build())
                response = self._client.im.v1.message.create(request)
            if not response.success():
                err = f"code={response.code} msg={response.msg}"
                logger.error("[feishu] 媒体发送失败 %s", err)
                return SendResult(ok=False, error=err)
            sent_id = getattr(getattr(response, "data", None), "message_id", None)
            return SendResult(ok=True, message_id=sent_id)
        except Exception as exc:
            logger.exception("[feishu] 媒体发送异常")
            return SendResult(ok=False, error=str(exc))
