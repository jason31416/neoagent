"""exec 工具：命令执行（IM 里的 LLM 拿到的是一台 shell，安全边界必须有）。

防线分四层（demo 够用，不是生产级）：
1. cwd 只能在 exec.allowed_roots 内
2. 命令命中 deny_patterns 正则直接拒绝（弱防线，挡住最蠢的事故）
3. 超时强杀（默认 60s）——按进程组整棵杀（bash 的孙进程会攥着管道拖死收尸）
4. 输出截断（默认 20KB），防止把 LLM 上下文撑爆

⚠️ 不做交互式确认（IM 场景等不起），所以 deny_patterns 只是最后一道兜底，
真正的边界是 allowed_roots：把工作目录限制在 demo 目录里。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List

from ..core.proc import kill_process_group
from .format import ToolOutput, error, root
from .registry import ToolRegistry

logger = logging.getLogger(__name__)

MAX_TIMEOUT_SECONDS = 600   # 模型自己申请也顶不过这个硬上限


class ExecPolicy:
    """exec 的安全策略配置（由启动入口注入）。"""

    def __init__(self, timeout_seconds: int = 60, max_output_bytes: int = 20000,
                 allowed_roots: List[str] | None = None,
                 deny_patterns: List[str] | None = None,
                 workspace: str = "") -> None:
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.allowed_roots = allowed_roots or []
        self.workspace = workspace                  # 默认 cwd（工作台）
        # 预编译正则，命中任一即拒绝
        self.deny_res = [re.compile(p) for p in (deny_patterns or [])]


_POLICY: ExecPolicy | None = None


def set_exec_policy(policy: ExecPolicy) -> None:
    """由启动入口注入 exec 安全策略。"""
    global _POLICY
    _POLICY = policy


async def exec_command(args: Dict[str, Any]) -> str:
    """exec 工具实现：在受限环境里跑一条 bash 命令。timeout 由模型按需申请（封顶）。"""
    policy = _POLICY or ExecPolicy()
    command = str(args.get("command", "")).strip()
    if not command:
        return error("bad_param", "缺少 command 参数")
    raw_timeout = args.get("timeout")
    try:
        timeout_s = policy.timeout_seconds if raw_timeout is None else int(raw_timeout)
    except (TypeError, ValueError):
        return error("bad_param", "timeout 必须是整数秒")
    timeout_s = max(1, min(timeout_s, MAX_TIMEOUT_SECONDS))

    # 1. 危险命令正则拦截
    for dre in policy.deny_res:
        if dre.search(command):
            return error("denied", f"命令被安全策略拒绝（命中禁止模式 {dre.pattern}）：{command}")

    # 2. cwd 校验（默认落在工作台；显式 cwd 必须在允许范围内）
    cwd = str(args.get("cwd", "")) or policy.workspace \
        or (policy.allowed_roots[0] if policy.allowed_roots else ".")
    from pathlib import Path
    cwd_p = Path(cwd).expanduser().resolve()
    ok = any(cwd_p == Path(r).resolve() or Path(r).resolve() in cwd_p.parents
             for r in policy.allowed_roots)
    if not ok:
        return error("out_of_scope", f"cwd 超出允许范围（allowed_roots={policy.allowed_roots}）：{cwd_p}")

    # 3. 跑命令（bash -c 支持管道/重定向；超时按进程组整棵强杀）
    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", command,
            cwd=str(cwd_p),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,      # 独立进程组：超时时能连孙进程一起杀
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            # 为什么不能只 proc.kill()：bash 里生出的孙进程会继承 stdout/stderr 管道，
            # 而 asyncio 的 proc.wait() 要等**所有管道断开**才返回（CPython
            # base_subprocess 语义）——杀掉 bash 后收尸照样挂几分钟，直到孙进程自己死。
            # 所以：按进程组整棵杀 + 限时收尸，两步都必须有（2026-09-29 实测挂 8 分钟）。
            kill_process_group(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning("[exec] 杀进程组后 5s 仍未收尸（pgid=%s），放弃等待", proc.pid)
            logger.warning("[exec] 命令超时（>%ds），已杀进程组 pgid=%s：%s",
                           timeout_s, proc.pid, command[:200])
            echo = command if len(command) <= 200 else command[:200] + "…（命令过长截断）"
            return error("timeout", f"命令跑到 {timeout_s}s 被终止：{echo}")
    except OSError as e:
        return error("exec_failed", f"命令启动失败：{e}")

    # 4. 输出拼装（stdout/stderr 两个真实内容块）+ 截断标记
    elapsed = time.monotonic() - t0
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    body_parts = []
    if out:
        body_parts.append(root("stdout", {}, out.rstrip("\n")))
    if err:
        body_parts.append(root("stderr", {}, err.rstrip("\n")))
    body = "\n".join(body_parts)
    truncated = "false"
    if len(body) > policy.max_output_bytes:
        body = body[:policy.max_output_bytes]
        truncated = "true"
    t = f"{elapsed * 1000:.0f}ms" if elapsed < 0.1 else f"{elapsed:.1f}s"
    return ToolOutput(
        text=root("exec", {"exit": proc.returncode, "elapsed": f"{elapsed:.1f}s",
                           "truncated": truncated}, body),
        stat=f"exit {proc.returncode} {t}",
        fields=[("time", t), ("exit", str(proc.returncode))])


EXEC_SCHEMA = {
    "name": "exec",
    "description": (
        "在受限环境里执行一条 bash 命令（支持管道、重定向），返回 stdout/stderr 和退出码。"
        "有超时和输出截断。简单文件操作请优先用 read_file / write_file。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 bash 命令"},
            "cwd": {"type": "string", "description": "工作目录（可选，默认工作台；必须在允许范围内）"},
            "timeout": {"type": "integer", "minimum": 1, "maximum": MAX_TIMEOUT_SECONDS,
                        "description": f"本次允许的最长执行秒数（不传取配置默认，硬上限 {MAX_TIMEOUT_SECONDS}）"},
        },
        "required": ["command"],
    },
}


def register_exec_tool(registry: ToolRegistry, policy: ExecPolicy) -> None:
    """把 exec 工具注册进 registry。"""
    set_exec_policy(policy)
    registry.register(EXEC_SCHEMA["name"], EXEC_SCHEMA["description"],
                      EXEC_SCHEMA["parameters"], exec_command)
