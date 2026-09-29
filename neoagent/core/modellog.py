"""modellog：把每次发给模型的上下文原样落盘（调试用）。

data/modellog/ 下一个请求一个 JSON 文件，文件名时间有序：

    20260929-221103-4821_m_feishu_oc_xxx_r3-a1b2.json   M 的第 3 轮请求
    20260929-221105-0017_r_fast_feishu_oc_xxx_r0-c3d4.json  R 一次整理的第 0 轮

文件内容 = 真实请求载荷（model / reasoning_effort / tools / messages），
可以原样重放对着 API 打——"当时到底发了什么"以它为准（日志只有摘要）。
请求**发出前**落盘：400 之类的翻车现场也留得下证据。

- 默认不落盘（setup() 之前是空转），app 组装时按配置开启；
- 超过 keep 个自动删最老的——250k 上下文一个文件能有几 MB，不封顶会撑爆磁盘；
- 落盘失败绝不影响请求（内部全吞，只留一行 warning）。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

_DIR: Optional[Path] = None
_KEEP = 200
_SLUG_RE = re.compile(r"[^\w.-]")


def setup(directory: Path, keep: int = 200) -> None:
    """开启落盘（app 组装时调一次）。传 None / 空路径 = 关闭。"""
    global _DIR, _KEEP
    if not directory:
        _DIR = None
        return
    _DIR = Path(directory)
    _DIR.mkdir(parents=True, exist_ok=True)
    _KEEP = max(int(keep), 1)


def enabled() -> bool:
    return _DIR is not None


def dump(kind: str, session: str, payload: Dict[str, Any]) -> Optional[Path]:
    """落一份请求载荷。kind = "m"（M）/ "r"（R）；payload 里是请求的全部字段。

    返回落盘路径（未开启/失败为 None）。绝不抛异常。"""
    if _DIR is None:
        return None
    try:
        now = time.time()
        slug = _SLUG_RE.sub("_", session or "")[:40]
        ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        ms = int((now % 1) * 1000)
        cycle = payload.get("cycle")
        name = (f"{ts}-{ms:03d}_{kind}"
                f"{'_' + cycle if cycle else ''}_{slug}"
                f"_r{payload.get('round', 0)}-{uuid4().hex[:4]}.json")
        doc = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "kind": kind,
            "session": session,
            **payload,
        }
        path = _DIR / name
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        _prune()
        return path
    except Exception:
        logger.warning("modellog 落盘失败（不影响请求）", exc_info=True)
        return None


def _prune() -> None:
    """只留最新 _KEEP 个（文件名时间前缀有序，排序即时间序）。"""
    try:
        files = sorted(p for p in _DIR.iterdir()
                       if p.is_file() and p.suffix == ".json")
        for p in files[:max(len(files) - _KEEP, 0)]:
            p.unlink(missing_ok=True)
    except Exception:
        pass
