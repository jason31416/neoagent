"""进程小工具：exec / skill_call 共用的进程组强杀。"""

from __future__ import annotations

import asyncio
import os
import signal


def kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """把整棵进程组杀掉（start_new_session=True 后 pgid == proc.pid）。

    只 proc.kill() 杀得了 bash，杀不了它生的孙进程；孙进程活着一天，
    管道不断开，proc.wait() 就一天不返回。杀组是唯一治本的招。"""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()                  # 组没了就退化成单杀（进程可能已死）
        except ProcessLookupError:
            pass
