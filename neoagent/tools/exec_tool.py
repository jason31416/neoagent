"""exec 工具：命令执行（IM 里的 LLM 拿到的是一台 shell，安全边界必须有）。

防线分四层（demo 够用，不是生产级）：
1. cwd 只能在 exec.allowed_roots 内
2. 命令命中 deny_patterns 正则直接拒绝（弱防线，挡住最蠢的事故）
3. 超时强杀（默认 60s）
4. 输出截断（默认 20KB），防止把 LLM 上下文撑爆

⚠️ 不做交互式确认（IM 场景等不起），所以 deny_patterns 只是最后一道兜底，
真正的边界是 allowed_roots：把工作目录限制在 demo 目录里。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List

from .registry import ToolRegistry


class ExecPolicy:
    """exec 的安全策略配置（由启动入口注入）。"""

    def __init__(self, timeout_seconds: int = 60, max_output_bytes: int = 20000,
                 allowed_roots: List[str] | None = None,
                 deny_patterns: List[str] | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.allowed_roots = allowed_roots or []
        # 预编译正则，命中任一即拒绝
        self.deny_res = [re.compile(p) for p in (deny_patterns or [])]


_POLICY: ExecPolicy | None = None


def set_exec_policy(policy: ExecPolicy) -> None:
    """由启动入口注入 exec 安全策略。"""
    global _POLICY
    _POLICY = policy


async def exec_command(args: Dict[str, Any]) -> str:
    """exec 工具实现：在受限环境里跑一条 bash 命令。"""
    policy = _POLICY or ExecPolicy()
    command = str(args.get("command", "")).strip()
    if not command:
        return "缺少 command 参数"

    # 1. 危险命令正则拦截
    for dre in policy.deny_res:
        if dre.search(command):
            return f"命令被安全策略拒绝（命中禁止模式 {dre.pattern}）：{command}"

    # 2. cwd 校验（默认落在项目根目录）
    cwd = str(args.get("cwd", "")) or (policy.allowed_roots[0] if policy.allowed_roots else ".")
    from pathlib import Path
    cwd_p = Path(cwd).expanduser().resolve()
    ok = any(cwd_p == Path(r).resolve() or Path(r).resolve() in cwd_p.parents
             for r in policy.allowed_roots)
    if not ok:
        return f"cwd 超出允许范围（allowed_roots={policy.allowed_roots}）：{cwd_p}"

    # 3. 跑命令（bash -c 支持管道/重定向；超时强杀）
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", command,
            cwd=str(cwd_p),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=policy.timeout_seconds)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"命令超时（>{policy.timeout_seconds}s），已终止：{command}"
    except OSError as e:
        return f"命令启动失败：{e}"

    # 4. 输出拼装 + 截断
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    parts = []
    if out:
        parts.append(out)
    if err:
        parts.append(f"[stderr]\n{err}")
    parts.append(f"[exit code: {proc.returncode}]")
    result = "\n".join(parts)

    if len(result) > policy.max_output_bytes:
        result = result[:policy.max_output_bytes] + "\n…（输出过长已截断）"
    return result


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
            "cwd": {"type": "string", "description": "工作目录（可选，默认项目目录；必须在允许范围内）"},
        },
        "required": ["command"],
    },
}


def register_exec_tool(registry: ToolRegistry, policy: ExecPolicy) -> None:
    """把 exec 工具注册进 registry。"""
    set_exec_policy(policy)
    registry.register(EXEC_SCHEMA["name"], EXEC_SCHEMA["description"],
                      EXEC_SCHEMA["parameters"], exec_command)
