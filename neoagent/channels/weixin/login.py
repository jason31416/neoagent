"""微信扫码登录入口。

    uv run python -m neoagent.channels.weixin.login

跑起来后用微信扫码确认，凭据落到 data/weixin/。
之后启动 neoagent（run.py）会自动复用登录态。
"""

from __future__ import annotations

import asyncio

from ...config import load_config
from . import ilink


def main() -> None:
    cfg = load_config()
    creds = asyncio.run(ilink.qr_login(cfg.data_dir))
    if creds is None:
        raise SystemExit("登录失败")
    print("登录完成。现在可以跑 `uv run python -m neoagent.run` 启动服务。")


if __name__ == "__main__":
    main()
