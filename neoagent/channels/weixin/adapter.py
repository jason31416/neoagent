"""微信通道适配器：把 iLink 协议层接进 ChannelAdapter 抽象。

职责边界（保持薄）：
- 收：getupdates 长轮询 → 去重 → 归一化成 MessageEvent → 丢给核心
- 发：核心的回复 → send_text（回带 context_token）→ 发回微信
不做任何 agent 逻辑。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import secrets
import time
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import quote

import aiohttp

from ...core.channel import ChannelAdapter
from ...core.events import Attachment, Conversation, MessageEvent, ReplyRef, SendResult, Sender
from . import ilink

logger = logging.getLogger(__name__)


def _content_fingerprint(text: str, item_list: list) -> str:
    """内容指纹：文本 + 每个 item 的身份（类型/文件名/大小/媒体键）。
    不同的图/文件指纹必不同；同一内容重发指纹相同（上游换 message_id 重发时拦）。"""
    ident = []
    for item in item_list:
        t = item.get("type")
        if t == ilink.ITEM_TEXT:
            ident.append(["text", (item.get("text_item") or {}).get("text", "")])
            continue
        payload = {}
        for key in ("image_item", "voice_item", "file_item", "video_item"):
            if item.get(key):
                payload = item[key] or {}
                break
        media = payload.get("media") or {}
        ident.append([
            t,
            str(payload.get("file_name") or ""),
            str(payload.get("len") or payload.get("mid_size") or ""),
            str(media.get("encrypt_query_param") or media.get("full_url") or ""),
        ])
    raw = json.dumps([text, ident], ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()


def _safe_filename(name: str) -> str:
    """文件名安全化：去掉路径分隔符和控制字符，防 ../../ 注入。"""
    import re
    name = str(name).replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name or "unnamed"


class _Dedup:
    """消息去重：iLink 偶尔会重发（换 message_id 重发同内容），双键拦。"""

    def __init__(self, ttl_seconds: float = 300.0):
        self._seen: Dict[str, float] = {}
        self._ttl = ttl_seconds

    def is_duplicate(self, key: str) -> bool:
        now = time.time()
        # 顺手清过期键，防止长期运行膨胀
        for k in [k for k, t in self._seen.items() if now - t > self._ttl]:
            self._seen.pop(k, None)
        if key in self._seen:
            return True
        self._seen[key] = now
        return False


class WeixinAdapter(ChannelAdapter):
    name = "weixin"

    def __init__(self, data_dir, max_bytes: int = 20 * 1024 * 1024) -> None:
        super().__init__()
        self._data_dir = data_dir
        self._max_bytes = max_bytes
        self._session: Optional[aiohttp.ClientSession] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._running = False
        self._dedup = _Dedup()                       # message_id 去重（300s，拦上游重发）
        self._content_dedup = _Dedup(ttl_seconds=10.0)  # 内容去重（10s：人类重复发要放行）
        self._tokens = ilink.ContextTokenStore(data_dir)

        # 登录后拿到的凭据
        self._account_id = ""
        self._token = ""
        self._base_url = ilink.ILINK_BASE_URL

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """加载已有登录态（没有就引导扫码），然后开长轮询。"""
        saved = ilink.latest_account(self._data_dir)
        if saved is None:
            print("未找到微信登录态，进入扫码登录：")
            creds = await ilink.qr_login(self._data_dir)
            if creds is None:
                raise RuntimeError("微信扫码登录失败，无法启动 weixin 通道")
            self._account_id = creds["account_id"]
            self._token = creds["token"]
            self._base_url = creds["base_url"] or ilink.ILINK_BASE_URL
        else:
            self._account_id, data = saved
            self._token = data["token"]
            self._base_url = data.get("base_url") or ilink.ILINK_BASE_URL

        self._tokens.restore(self._account_id)
        self._session = aiohttp.ClientSession(trust_env=True)
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("[weixin] 已连接，account_id=%s", self._account_id[:8])

    async def disconnect(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("[weixin] 已断开")

    # ------------------------------------------------------------------
    # 收消息：长轮询
    # ------------------------------------------------------------------
    async def _poll_loop(self) -> None:
        assert self._session is not None
        sync_buf = ilink._load_sync_buf(self._data_dir, self._account_id)
        timeout_ms = ilink.LONG_POLL_TIMEOUT_MS
        failures = 0

        while self._running:
            try:
                resp = await ilink.get_updates(self._session, base_url=self._base_url,
                                               token=self._token, sync_buf=sync_buf,
                                               timeout_ms=timeout_ms)
                # 服务端建议的轮询超时优先
                suggested = resp.get("longpolling_timeout_ms")
                if isinstance(suggested, int) and suggested > 0:
                    timeout_ms = suggested

                ret, errcode = resp.get("ret", 0), resp.get("errcode", 0)
                if ret not in {0, None} or errcode not in {0, None}:
                    if ilink.SESSION_EXPIRED_ERRCODE in (ret, errcode):
                        logger.error("[weixin] 会话过期，10 分钟后重试（可重新扫码登录）")
                        await asyncio.sleep(600)
                        continue
                    failures += 1
                    logger.warning("[weixin] getupdates 失败 ret=%s errcode=%s (%d/3)",
                                   ret, errcode, failures)
                    if failures >= 3:
                        await asyncio.sleep(30)
                        failures = 0
                    else:
                        await asyncio.sleep(2)
                    continue
                failures = 0

                # 先派发再存游标：游标推进了但消息没处理就崩了 = 丢消息
                for message in resp.get("msgs") or []:
                    asyncio.create_task(self._process_message_safe(message))

                new_buf = str(resp.get("get_updates_buf") or "")
                if new_buf and new_buf != sync_buf:
                    sync_buf = new_buf
                    ilink._save_sync_buf(self._data_dir, self._account_id, sync_buf)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                failures += 1
                logger.error("[weixin] 轮询出错 (%d/3): %s", failures, exc)
                await asyncio.sleep(5 if failures < 3 else 30)
                if failures >= 3:
                    failures = 0

    async def _process_message_safe(self, message: dict) -> None:
        try:
            await self._process_message(message)
        except Exception:
            logger.exception("[weixin] 处理消息出错")

    async def _process_message(self, message: dict) -> None:
        sender_id = str(message.get("from_user_id") or "").strip()
        message_id = str(message.get("message_id") or "").strip()
        # 跳过：没发件人 / 自己发的 / 去重命中
        if not sender_id or sender_id == self._account_id:
            return
        if message_id and self._dedup.is_duplicate(message_id):
            return

        item_list = message.get("item_list") or []
        text = ilink.extract_text(item_list, fold_ref=False)

        # 内容指纹去重（上游会换 message_id 重发同样内容）。
        # 指纹必须纳入**附件身份**（文件名/大小/媒体键）——只按"附件个数"算
        # 会让两张不同的图/文件撞指纹被误杀（实测 P0-2）。
        # 而且要在**下载之前**算，别为去重命中的消息白下载。
        # 人类重复发消息是正常行为（"在吗"×2），上游重发的特征是**同内容同时间戳**
        # ——指纹纳入消息时间字段；没有时间字段就靠 10s 短 TTL 兜底。
        msg_time = str(message.get("create_time_ms") or message.get("create_time")
                       or message.get("msg_time") or "")
        fingerprint = f"content:{sender_id}:{msg_time}:{_content_fingerprint(text, item_list)}"
        if self._content_dedup.is_duplicate(fingerprint):
            logger.info("[weixin] 内容去重命中（上游重发），丢弃 %s", fingerprint[:24])
            return

        # 媒体附件：下载 + AES 解密 + 落盘（图片/文件/语音/视频）
        attachments = await self._collect_attachments(item_list, message_id or f"wx-{int(time.time()*1000)}")

        if not text and not attachments:
            return

        # 引用回复：ref_msg 内联就是被引用消息的原文（零 API 零下载）
        reply_to = self._parse_ref(item_list)

        # 记下对方最新的 context_token——回消息必须回带它
        context_token = str(message.get("context_token") or "").strip()
        if context_token:
            self._tokens.set(self._account_id, sender_id, context_token)

        # 群聊：room_id 非空算群；demo 简化为都按会话 id 处理
        room_id = str(message.get("room_id") or message.get("chat_room_id") or "").strip()
        chat_type = "group" if room_id else "direct"
        chat_id = room_id or sender_id

        event = MessageEvent(
            id=message_id or f"wx-{int(time.time() * 1000)}",
            channel=self.name,
            sender=Sender(id=sender_id, name=sender_id),
            conversation=Conversation(id=chat_id, type=chat_type),
            text=text,
            timestamp=time.time(),
            attachments=attachments,
            reply_to=reply_to,
            metadata={
                # send_reply 需要的私有信息
                "to_user_id": sender_id,
                "context_token": context_token or None,
            },
        )
        att_names = ",".join(a.filename for a in attachments) or "-"
        logger.debug("[weixin] 收到消息 from=%s chat=%s att=%d[%s] ref=%s",
                     sender_id[:8], chat_id[:8], len(attachments), att_names,
                     bool(reply_to))
        await self._dispatch(event)

    # ------------------------------------------------------------------
    # 附件与引用
    # ------------------------------------------------------------------
    # item type → (kind, item_key, 默认扩展名, mime)；对应 ilink 的 ITEM_* 常量
    _MEDIA_SPEC = {
        ilink.ITEM_IMAGE: ("image", "image_item", ".jpg", "image/jpeg"),
        ilink.ITEM_VOICE: ("audio", "voice_item", ".silk", "audio/silk"),
        ilink.ITEM_FILE: ("file", "file_item", "", ""),
        ilink.ITEM_VIDEO: ("video", "video_item", ".mp4", "video/mp4"),
    }

    async def _collect_attachments(self, item_list: list, message_id: str) -> list:
        """把 item_list 里的媒体 item 下载落盘成 Attachment。
        超过 max_bytes 或下载失败 → 降级为元数据占位（不带 local_path）。"""
        atts = []
        for item in item_list:
            spec = self._MEDIA_SPEC.get(item.get("type"))
            if spec is None:
                continue
            kind, item_key, ext, mime = spec
            payload = item.get(item_key) or {}
            media = payload.get("media") or {}
            filename = str(payload.get("file_name") or "") or f"{kind}{ext}"
            filename = _safe_filename(filename)

            att = Attachment(kind=kind, filename=filename, mime=mime)
            try:
                aes_key_b64 = media.get("aes_key")
                if item_key == "image_item" and payload.get("aeskey"):
                    # image_item 可能带裸 hex aeskey（besides media.aes_key）
                    import base64 as _b64
                    aes_key_b64 = _b64.b64encode(
                        bytes.fromhex(str(payload["aeskey"]))).decode() or aes_key_b64
                data = await ilink.download_media(
                    self._session,
                    encrypted_query_param=media.get("encrypt_query_param"),
                    aes_key_b64=aes_key_b64,
                    full_url=media.get("full_url"),
                    timeout_seconds=120.0,
                )
                if len(data) > self._max_bytes:
                    logger.warning("[weixin] 附件过大 %dB，降级占位：%s", len(data), filename)
                    continue  # 只留元数据占位
                path = self._save_media(data, message_id, filename)
                att.size = len(data)
                att.local_path = str(path)
                logger.info("[weixin] 附件已落盘 %s（%d 字符）", path.name, len(data))
            except Exception as exc:
                logger.warning("[weixin] 附件下载失败 %s：%s", filename, exc)
            atts.append(att)
        return atts

    def _save_media(self, data: bytes, message_id: str, filename: str):
        from pathlib import Path
        d = Path(self._data_dir) / "media" / "weixin" / _safe_filename(message_id)
        d.mkdir(parents=True, exist_ok=True)
        p = d / filename
        p.write_bytes(data)
        return p

    def _parse_ref(self, item_list: list):
        """引用回复：ref_msg 内联原文 → ReplyRef（resolved_from="inline"）。
        引用里的媒体只出元数据占位，不下载。"""
        for item in item_list:
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            if not ref and not ref_item:
                continue
            # 实测定案：ref_msg = {"message_item": {...}}——只给**指针**不给原文：
            # msg_id 在 message_item 里（不叫 message_id），带 create_time_ms，
            # 正文不在（text_item 缺席）。原文靠 msg_id 查消息库。
            logger.debug("[weixin] ref_msg 载荷 %.300s",
                         json.dumps(ref, ensure_ascii=False)[:300])
            msg_id = str(ref_item.get("msg_id") or ref.get("message_id")
                         or ref.get("svrid") or "")
            ts_ms = ref_item.get("create_time_ms") or ref.get("create_time_ms")
            try:
                timestamp = float(ts_ms) / 1000 if ts_ms else 0.0
            except (TypeError, ValueError):
                timestamp = 0.0
            # 理论上 type=ITEM_TEXT 的 ref_item 才内联文本，绝大多数情况为空
            text = ilink.extract_text([ref_item]) if ref_item else ""
            atts = []
            spec = self._MEDIA_SPEC.get(ref_item.get("type"))
            if spec is not None:
                kind, _key, _ext, _mime = spec
                atts.append(Attachment(kind=kind, filename=str(ref.get("title") or kind)))
            return ReplyRef(
                message_id=msg_id,
                sender_name=str(ref.get("title") or ""),
                timestamp=timestamp,
                text=text,
                attachments=atts,
                resolved_from="inline" if text else "unknown",
            )
        return None

    # ------------------------------------------------------------------
    # 发消息
    # ------------------------------------------------------------------
    async def send_reply(self, event: MessageEvent, text: str,
                         quote_to: Optional[str] = None) -> SendResult:
        assert self._session is not None
        to = str(event.metadata.get("to_user_id") or event.conversation.id)
        # 优先用事件里的 token，拿不到回退到 token 缓存（重启后靠它）
        context_token = event.metadata.get("context_token") \
            or self._tokens.get(self._account_id, to)

        try:
            sent_id = None
            # 微信单条消息别太长，超长按 1500 字符切条发；引用只挂在第一条
            for i, chunk in enumerate(_split_text(text, 1500)):
                resp = await ilink.send_text(self._session, base_url=self._base_url,
                                             token=self._token, to=to, text=chunk,
                                             context_token=context_token,
                                             ref_msg_id=quote_to if i == 0 else None)
                ret = resp.get("ret", 0)
                if ret not in (0, None):
                    err = f"sendmessage ret={ret} errcode={resp.get('errcode')} errmsg={resp.get('errmsg')}"
                    logger.error("[weixin] %s", err)
                    return SendResult(ok=False, error=err, message_id=sent_id)
                sent_id = ilink.sent_msg_id(resp) or sent_id
                # 探针：看 sendmessage 响应载荷（sent_msg_id 键名靠它定案）
                if not getattr(self, "_send_resp_logged", False):
                    self._send_resp_logged = True
                    logger.info("[weixin] sendmessage 响应载荷 %.300s",
                                json.dumps(resp, ensure_ascii=False)[:300])
            return SendResult(ok=True, message_id=sent_id)
        except Exception as exc:
            logger.exception("[weixin] 发送失败")
            return SendResult(ok=False, error=str(exc))

    async def send_media(self, event: MessageEvent, path: str,
                         quote_to: Optional[str] = None) -> SendResult:
        """发图片/文件为独立消息（hermes 同款链路）：
        getuploadurl → AES-128-ECB 加密 → CDN POST → 带媒体 item 的 sendmessage。"""
        assert self._session is not None
        to = str(event.metadata.get("to_user_id") or event.conversation.id)
        context_token = event.metadata.get("context_token") \
            or self._tokens.get(self._account_id, to)
        try:
            plaintext = Path(path).read_bytes()
            media_type, item_type = _outbound_media_type(path)
            filekey, aes_key = secrets.token_hex(16), secrets.token_bytes(16)
            rawsize, rawfilemd5 = len(plaintext), hashlib.md5(plaintext).hexdigest()

            resp = await ilink.get_upload_url(
                self._session, base_url=self._base_url, token=self._token, to_user_id=to,
                media_type=media_type, filekey=filekey, rawsize=rawsize,
                rawfilemd5=rawfilemd5, filesize=((rawsize + 16) // 16) * 16,
                aeskey_hex=aes_key.hex())
            ciphertext = ilink._aes128_ecb_encrypt(plaintext, aes_key)
            upload_param = str(resp.get("upload_param") or "")
            # 优先 upload_full_url（CDN 直传），否则拼 upload_param；hermes 实测 POST，PUT 404
            upload_url = str(resp.get("upload_full_url") or "") or (upload_param and (
                f"{ilink.WEIXIN_CDN_BASE_URL.rstrip('/')}/upload"
                f"?encrypted_query_param={quote(upload_param, safe='')}&filekey={quote(filekey, safe='')}"))
            if not upload_url:
                return SendResult(ok=False, error="getuploadurl 既没回 upload_param 也没回 upload_full_url")
            enc_param = await ilink.upload_ciphertext(self._session, ciphertext=ciphertext,
                                                      upload_url=upload_url)
            item = ilink.media_item(
                item_type,
                encrypt_query_param=enc_param,
                # iLink 期望 aes_key = base64(hex 字符串)；给原始字节会渲染成灰图（hermes 实测）
                aes_key_for_api=base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii"),
                ciphertext_size=len(ciphertext), plaintext_size=rawsize,
                filename=Path(path).name, rawfilemd5=rawfilemd5)
            resp = await ilink.send_items(self._session, base_url=self._base_url,
                                          token=self._token, to=to, item_list=[item],
                                          context_token=context_token, ref_msg_id=quote_to)
            ret = resp.get("ret", 0)
            if ret not in (0, None):
                err = f"sendmessage ret={ret} errcode={resp.get('errcode')} errmsg={resp.get('errmsg')}"
                logger.error("[weixin] 媒体发送 %s", err)
                return SendResult(ok=False, error=err)
            return SendResult(ok=True, message_id=ilink.sent_msg_id(resp))
        except Exception as exc:
            logger.exception("[weixin] 媒体发送失败")
            return SendResult(ok=False, error=str(exc))


def _outbound_media_type(path: str) -> Tuple[int, int]:
    """出向媒体归类：图→IMAGE、视频→VIDEO，其余（含音频）都当 FILE 发。"""
    mime = mimetypes.guess_type(path)[0] or ""
    if mime.startswith("image/"):
        return ilink.MEDIA_IMAGE, ilink.ITEM_IMAGE
    if mime.startswith("video/"):
        return ilink.MEDIA_VIDEO, ilink.ITEM_VIDEO
    return ilink.MEDIA_FILE, ilink.ITEM_FILE


def _split_text(text: str, max_len: int) -> list[str]:
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
