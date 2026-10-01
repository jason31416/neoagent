"""iLink Bot API 协议层（移植自 hermes weixin.py，MIT）。

只做协议：HTTP 封装、登录、收发、消息解析。
不涉及 agent / 工具 / 会话——那是 adapter.py 和核心的事。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 输出口：扫码流程的提示（二维码/进度）。默认 print（终端直用）；
# 服务形态 set_output 成"推进控制台转录"的函数，二维码就流进控制台。
# ----------------------------------------------------------------------
_out: Callable[..., None] = print


def set_output(fn: Optional[Callable[..., None]]) -> None:
    global _out
    _out = fn or print


def _dot() -> None:
    """等待扫码的小数点（只在终端直用模式打；推控制台时静默不刷屏）。"""
    if _out is print:
        print(".", end="", flush=True)

# ----------------------------------------------------------------------
# 协议常量（来自 hermes 对 iLink 的逆向实现，腾讯无公开文档）
# ----------------------------------------------------------------------
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
QR_TIMEOUT_MS = 35_000

# item_list 条目类型：文本最重要，其余本期只识别不下载
ITEM_TEXT, ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO = 1, 2, 3, 4, 5
MSG_TYPE_BOT, MSG_STATE_FINISH = 2, 2

# iLink 的会话过期 / 限频错误码（hermes 踩坑总结）
SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2


def _headers(token: Optional[str], body: str) -> Dict[str, str]:
    """iLink 请求头。X-WECHAT-UIN 是个随机 base64 uin（协议要求，每次请求都变）。"""
    uin = base64.b64encode(str(int.from_bytes(secrets.token_bytes(4), "big")).encode()).decode()
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": uin,
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        **({"Authorization": f"Bearer {token}"} if token else {}),
    }


async def _api_post(session: aiohttp.ClientSession, *, base_url: str, endpoint: str,
                    payload: Dict[str, Any], token: Optional[str], timeout_ms: int) -> Dict[str, Any]:
    """POST + 统一包壳：所有请求都要带 base_info.channel_version。"""
    body = json.dumps({**payload, "base_info": {"channel_version": CHANNEL_VERSION}},
                      ensure_ascii=False, separators=(",", ":"))
    async def _do() -> Dict[str, Any]:
        async with session.post(f"{base_url.rstrip('/')}/{endpoint}",
                                headers=_headers(token, body), data=body) as resp:
            raw = await resp.text()
            if not resp.ok:
                raise RuntimeError(f"iLink POST {endpoint} HTTP {resp.status}: {raw[:200]}")
            return json.loads(raw)
    return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)


async def _api_get(session: aiohttp.ClientSession, *, base_url: str, endpoint: str,
                   timeout_ms: int) -> Dict[str, Any]:
    headers = {"iLink-App-Id": ILINK_APP_ID, "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION)}
    async def _do() -> Dict[str, Any]:
        async with session.get(f"{base_url.rstrip('/')}/{endpoint}", headers=headers) as resp:
            raw = await resp.text()
            if not resp.ok:
                raise RuntimeError(f"iLink GET {endpoint} HTTP {resp.status}: {raw[:200]}")
            return json.loads(raw)
    return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)


# ----------------------------------------------------------------------
# 凭据 / 游标 / context_token 持久化（重启不丢登录态和消息游标）
# ----------------------------------------------------------------------
def _account_dir(data_dir: Path) -> Path:
    p = data_dir / "weixin"
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_account(data_dir: Path, *, account_id: str, token: str, base_url: str, user_id: str = "") -> None:
    path = _account_dir(data_dir) / f"{account_id}.json"
    path.write_text(json.dumps({"token": token, "base_url": base_url,
                                "user_id": user_id, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}),
                    encoding="utf-8")
    try:
        path.chmod(0o600)  # 凭据文件仅属主可读
    except OSError:
        pass


def load_account(data_dir: Path, account_id: str) -> Optional[Dict[str, Any]]:
    path = _account_dir(data_dir) / f"{account_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def latest_account(data_dir: Path) -> Optional[Tuple[str, Dict[str, Any]]]:
    """取最近保存的账号（demo 只用一个微信账号）。"""
    files = sorted(_account_dir(data_dir).glob("*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files:
        if f.name.endswith(".context-tokens.json") or f.name.endswith(".sync.json"):
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("token"):
                return f.stem, data
        except Exception:
            continue
    return None


def _load_sync_buf(data_dir: Path, account_id: str) -> str:
    path = _account_dir(data_dir) / f"{account_id}.sync.json"
    if not path.exists():
        return ""
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("get_updates_buf", ""))
    except Exception:
        return ""


def _save_sync_buf(data_dir: Path, account_id: str, sync_buf: str) -> None:
    (_account_dir(data_dir) / f"{account_id}.sync.json").write_text(
        json.dumps({"get_updates_buf": sync_buf}), encoding="utf-8")


class ContextTokenStore:
    """context_token 缓存：key = account_id:peer_id。
    回消息必须回带对方最新的 context_token，且重启后也要能发，所以落盘。"""

    def __init__(self, data_dir: Path):
        self._dir = _account_dir(data_dir)
        self._cache: Dict[str, str] = {}

    def restore(self, account_id: str) -> None:
        path = self._dir / f"{account_id}.context-tokens.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self._cache.update({f"{account_id}:{k}": v for k, v in data.items() if v})
        except Exception as exc:
            logger.warning("恢复 context_token 失败 %s: %s", account_id, exc)

    def get(self, account_id: str, peer_id: str) -> Optional[str]:
        return self._cache.get(f"{account_id}:{peer_id}")

    def set(self, account_id: str, peer_id: str, token: str) -> None:
        self._cache[f"{account_id}:{peer_id}"] = token
        prefix = f"{account_id}:"
        payload = {k[len(prefix):]: v for k, v in self._cache.items() if k.startswith(prefix)}
        try:
            (self._dir / f"{account_id}.context-tokens.json").write_text(
                json.dumps(payload), encoding="utf-8")
        except Exception as exc:
            logger.warning("持久化 context_token 失败: %s", exc)


# ----------------------------------------------------------------------
# 收发
# ----------------------------------------------------------------------
async def get_updates(session: aiohttp.ClientSession, *, base_url: str, token: str,
                      sync_buf: str, timeout_ms: int) -> Dict[str, Any]:
    """长轮询收消息。超时不算错，回空批次（长轮询的正常形态）。"""
    try:
        return await _api_post(session, base_url=base_url, endpoint=EP_GET_UPDATES,
                               payload={"get_updates_buf": sync_buf}, token=token,
                               timeout_ms=timeout_ms)
    except asyncio.TimeoutError:
        return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}


def sent_msg_id(resp: Dict[str, Any]) -> Optional[str]:
    """从 sendmessage 响应里掏我们发出消息的 id（键名未定案，多试几处；
    探针日志确认后收紧）。拿不到就 None——引用自己的回复依赖它。"""
    for key in ("msg_id", "svrid", "message_id"):
        v = resp.get(key)
        if v:
            return str(v)
    msg = resp.get("msg") or {}
    if isinstance(msg, dict):
        for key in ("msg_id", "svrid", "message_id"):
            v = msg.get(key)
            if v:
                return str(v)
    return None


async def send_items(session: aiohttp.ClientSession, *, base_url: str, token: str,
                     to: str, item_list: List[Dict[str, Any]], context_token: Optional[str],
                     ref_msg_id: Optional[str] = None) -> Dict[str, Any]:
    """发一条消息（item_list 可含文本/媒体项）。ref_msg_id：引用同会话某条消息。"""
    items = list(item_list)
    if ref_msg_id and items:
        items[0] = {**items[0],
                    "ref_msg": {"message_item": {"type": ITEM_TEXT, "msg_id": str(ref_msg_id)}}}
    message: Dict[str, Any] = {
        "from_user_id": "", "to_user_id": to,
        "client_id": secrets.token_hex(8),       # 幂等去重用，随机即可
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": items,
    }
    if context_token:
        message["context_token"] = context_token
    return await _api_post(session, base_url=base_url, endpoint=EP_SEND_MESSAGE,
                           payload={"msg": message}, token=token, timeout_ms=API_TIMEOUT_MS)


async def send_text(session: aiohttp.ClientSession, *, base_url: str, token: str,
                    to: str, text: str, context_token: Optional[str],
                    ref_msg_id: Optional[str] = None) -> Dict[str, Any]:
    """回一条文本消息。context_token 必带（拿不到就多半发不出去）。
    ref_msg_id：引用（回复）同会话里的某条消息——协议对称（收包 ref_msg 同构），
    服务端不认就当普通消息发出去，不炸。"""
    return await send_items(
        session, base_url=base_url, token=token, to=to,
        item_list=[{"type": ITEM_TEXT, "text_item": {"text": text}}],
        context_token=context_token, ref_msg_id=ref_msg_id)


def extract_text(item_list: List[Dict[str, Any]], fold_ref: bool = True) -> str:
    """从 item_list 里抽纯文本。

    fold_ref=True：引用消息折叠进文本（[引用: …] 前缀，兼容旧用法）；
    fold_ref=False：只取用户自己的正文——引用由 ReplyRef 单独承载，不折叠。
    """
    for item in item_list:
        if item.get("type") == ITEM_TEXT:
            text = str((item.get("text_item") or {}).get("text") or "")
            if not fold_ref:
                return text
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            if ref_item:
                title = str(ref.get("title") or "")
                quoted = extract_text([ref_item]) if ref_item.get("type") == ITEM_TEXT else "[媒体]"
                return f"[引用: {title or quoted}] {text}".strip()
            return text
    return ""


# ----------------------------------------------------------------------
# 扫码登录
# ----------------------------------------------------------------------
async def fetch_qr(session: aiohttp.ClientSession, bot_type: str = "3") -> Tuple[str, str]:
    """拿二维码。返回 (qrcode 值, 二维码内容链接)。
    注意：微信要扫的是 qrcode_img_content 那个链接（liteapp 链接），不是裸 token。"""
    resp = await _api_get(session, base_url=ILINK_BASE_URL,
                          endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}", timeout_ms=QR_TIMEOUT_MS)
    return str(resp.get("qrcode") or ""), str(resp.get("qrcode_img_content") or "")


def print_qr(qrcode_value: str, qrcode_url: str) -> None:
    """输出二维码：优先 ASCII 渲染，渲染不了就给链接（走输出口，服务形态进控制台）。"""
    if qrcode_url:
        _out(f"二维码链接（打不开就直接用微信扫它）：\n{qrcode_url}")
    try:
        import qrcode  # 可选依赖，没装就只给链接
        qr = qrcode.QRCode()
        qr.add_data(qrcode_url or qrcode_value)
        qr.make(fit=True)
        buf = io.StringIO()
        qr.print_ascii(invert=True, out=buf)
        _out(buf.getvalue().rstrip("\n"))
    except Exception as exc:
        _out(f"（终端二维码渲染失败：{exc}）")


async def qr_login(data_dir: Path, *, bot_type: str = "3", timeout_seconds: int = 480) -> Optional[Dict[str, str]]:
    """扫码登录全流程：取码 → 等扫码 → 等确认 → 存凭据。
    成功返回凭据 dict，失败返回 None。"""
    async with aiohttp.ClientSession(trust_env=True) as session:
        try:
            qrcode_value, qrcode_url = await fetch_qr(session, bot_type)
        except Exception as exc:
            logger.error("获取二维码失败: %s", exc)
            return None
        if not qrcode_value:
            logger.error("二维码响应缺少 qrcode 字段")
            return None

        _out("请使用微信扫描以下二维码：")
        print_qr(qrcode_value, qrcode_url)

        deadline = time.monotonic() + timeout_seconds
        current_base_url = ILINK_BASE_URL
        while time.monotonic() < deadline:
            try:
                resp = await _api_get(session, base_url=current_base_url,
                                      endpoint=f"{EP_GET_QR_STATUS}?qrcode={qrcode_value}",
                                      timeout_ms=QR_TIMEOUT_MS)
            except Exception as exc:
                if not isinstance(exc, asyncio.TimeoutError):
                    logger.warning("轮询二维码状态出错: %s", exc)
                await asyncio.sleep(1)
                continue

            status = str(resp.get("status") or "wait")
            if status == "wait":
                _dot()
            elif status == "scaned":
                _out("已扫码，请在微信里确认登录...")
            elif status == "scaned_but_redirect" and resp.get("redirect_host"):
                # 集群重定向：换 base_url 继续轮询
                current_base_url = f"https://{resp['redirect_host']}"
            elif status == "expired":
                # 不自动刷新：过期就停，由用户重新发起登录取新码
                _out("二维码已过期。重新 /connect weixin 取个新的。")
                return None
            elif status == "confirmed":
                creds = {
                    "account_id": str(resp.get("ilink_bot_id") or ""),
                    "token": str(resp.get("bot_token") or ""),
                    "base_url": str(resp.get("baseurl") or ILINK_BASE_URL),
                    "user_id": str(resp.get("ilink_user_id") or ""),
                }
                if not creds["account_id"] or not creds["token"]:
                    logger.error("扫码确认但凭据不完整：%s", resp)
                    return None
                save_account(data_dir, **creds)
                _out(f"微信连接成功，account_id={creds['account_id']}")
                return creds
            await asyncio.sleep(1)

        _out("微信登录超时。")
        return None


# ----------------------------------------------------------------------
# 媒体下载（移植自 hermes weixin.py 的 _download_and_decrypt_media）
# 微信媒体走 AES-128-ECB 加密 CDN，下载后要解密才能拿到明文文件。
# ----------------------------------------------------------------------
WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

# SSRF 防护：只允许从白名单 CDN host 拉媒体（hermes 同款名单）
WEIXIN_CDN_ALLOWLIST = frozenset({
    "novac2c.cdn.weixin.qq.com", "ilinkai.weixin.qq.com", "wx.qlogo.cn",
    "thirdwx.qlogo.cn", "res.wx.qq.com", "mmbiz.qpic.cn", "mmbiz.qlogo.cn",
})


def assert_cdn_url(url: str) -> None:
    """媒体 URL 白名单校验：只认 http(s) + 白名单 host（防 SSRF）。"""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        scheme, host = parsed.scheme.lower(), parsed.hostname or ""
    except Exception as exc:
        raise ValueError(f"无法解析媒体 URL: {url!r}") from exc
    if scheme not in {"http", "https"}:
        raise ValueError(f"媒体 URL 协议不允许: {scheme!r}")
    if host not in WEIXIN_CDN_ALLOWLIST:
        raise ValueError(f"媒体 URL host 不在白名单（防 SSRF 拒绝）: {host!r}")


def _parse_aes_key(aes_key_b64: str) -> bytes:
    """aes_key 兼容两种形态：直接 16 字节，或 32 字符 hex 的 base64。"""
    import base64 as _b64
    decoded = _b64.b64decode(aes_key_b64)
    if len(decoded) == 16:
        return decoded
    text = decoded.decode("ascii", errors="ignore") if len(decoded) == 32 else ""
    if text and all(ch in "0123456789abcdefABCDEF" for ch in text):
        return bytes.fromhex(text)
    raise ValueError(f"意外的 aes_key 格式（decoded {len(decoded)} 字节）")


def _aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """AES-128-ECB 解密 + 去 PKCS7 填充（填充不规范时回退原始输出）。"""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    decryptor = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend()).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    pad_len = padded[-1] if padded else 0
    if 1 <= pad_len <= 16 and padded.endswith(bytes([pad_len]) * pad_len):
        return padded[:-pad_len]
    return padded


async def download_media(session: "aiohttp.ClientSession", *, encrypted_query_param: str | None,
                         aes_key_b64: str | None, full_url: str | None,
                         timeout_seconds: float = 60.0) -> bytes:
    """下载（并解密）一个媒体 item 的内容。
    优先 encrypt_query_param（拼 CDN download 接口），否则 full_url（先过白名单）。"""
    from urllib.parse import quote

    if encrypted_query_param:
        url = (f"{WEIXIN_CDN_BASE_URL.rstrip('/')}/download"
               f"?encrypted_query_param={quote(encrypted_query_param, safe='')}")
    elif full_url:
        assert_cdn_url(full_url)
        url = full_url
    else:
        raise RuntimeError("媒体 item 既没有 encrypt_query_param 也没有 full_url")

    async def _do() -> bytes:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()

    raw = await asyncio.wait_for(_do(), timeout=timeout_seconds)
    return _aes128_ecb_decrypt(raw, _parse_aes_key(aes_key_b64)) if aes_key_b64 else raw


# ----------------------------------------------------------------------
# 媒体上传/发送（移植自 hermes weixin.py 的 _send_file 链路）
# getuploadurl → AES-128-ECB 加密 → CDN POST → x-encrypted-param → 带媒体 item 的 sendmessage
# ----------------------------------------------------------------------
MEDIA_IMAGE, MEDIA_VIDEO, MEDIA_FILE, MEDIA_VOICE = 1, 2, 3, 4   # getuploadurl media_type
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """AES-128-ECB 加密 + PKCS7 填充（与下载解密对称）。"""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    encryptor = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend()).encryptor()
    return encryptor.update(_pkcs7_pad(plaintext)) + encryptor.finalize()


# 出向媒体 item 构造：item type → (item key, 附加字段)
_ITEM_SHAPES: Dict[int, Any] = {
    ITEM_FILE: ("file_item", lambda kw: {"file_name": kw["filename"], "len": str(kw["plaintext_size"])}),
    ITEM_IMAGE: ("image_item", lambda kw: {"mid_size": kw["ciphertext_size"]}),
    ITEM_VIDEO: ("video_item", lambda kw: {
        "video_size": kw["ciphertext_size"], "play_length": kw.get("play_length", 0),
        "video_md5": kw.get("rawfilemd5", "")}),
    ITEM_VOICE: ("voice_item", lambda kw: {
        "encode_type": kw.get("encode_type"), "bits_per_sample": kw.get("bits_per_sample"),
        "sample_rate": kw.get("sample_rate"), "playtime": kw.get("playtime", 0)}),
}


def media_item(item_type: int, **kw: Any) -> Dict[str, Any]:
    """出向媒体 item（收包同构的 media 块 + 各类型附加字段）。"""
    key, fields = _ITEM_SHAPES[item_type]
    media = {"encrypt_query_param": kw["encrypt_query_param"],
             "aes_key": kw["aes_key_for_api"], "encrypt_type": 1}
    return {"type": item_type, key: {"media": media, **fields(kw)}}


async def get_upload_url(session: aiohttp.ClientSession, *, base_url: str, token: str,
                         to_user_id: str, media_type: int, filekey: str,
                         rawsize: int, rawfilemd5: str, filesize: int,
                         aeskey_hex: str) -> Dict[str, Any]:
    """向 iLink 要上传地址。filesize 是 PKCS7 补齐后的密文尺寸（16 的倍数）。"""
    payload = {"filekey": filekey, "media_type": media_type, "to_user_id": to_user_id,
               "rawsize": rawsize, "rawfilemd5": rawfilemd5, "filesize": filesize,
               "no_need_thumb": True, "aeskey": aeskey_hex}
    return await _api_post(session, base_url=base_url, endpoint=EP_GET_UPLOAD_URL,
                           payload=payload, token=token, timeout_ms=API_TIMEOUT_MS)


async def upload_ciphertext(session: aiohttp.ClientSession, *, ciphertext: bytes,
                            upload_url: str) -> str:
    """密文 POST 到 CDN，返回响应头 x-encrypted-param（放进媒体 item 用）。"""

    async def _do() -> str:
        async with session.post(upload_url, data=ciphertext,
                                headers={"Content-Type": "application/octet-stream"}) as resp:
            param = resp.headers.get("x-encrypted-param") if resp.status == 200 else None
            if param:
                await resp.read()
                return param
            raw = (await resp.text())[:200]
            raise RuntimeError(f"CDN 上传缺 x-encrypted-param 头: {raw}"
                               if resp.status == 200 else f"CDN 上传 HTTP {resp.status}: {raw}")
    return await asyncio.wait_for(_do(), timeout=120)
