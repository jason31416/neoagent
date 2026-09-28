"""应用组装：把配置 → 工具注册表 → providers → 消息库 → M（agent 核心）串起来。
CLI / 微信 / 飞书入口都走这里，保证"同一核心挂多通道"。
"""

from __future__ import annotations

from pathlib import Path

from .config import Config, PROJECT_ROOT
from .core.agent_loop import AgentLoop
from .core.ledger import MessageLedger
from .core.llm import LLMClient
from .providers.asr import make_transcriber
from .providers.vision import make_vision
from .tools.exec_tool import ExecPolicy, register_exec_tool
from .tools.file_tools import register_file_tools
from .tools.registry import ToolRegistry


def build_agent(cfg: Config) -> AgentLoop:
    """按配置组装 M（agent 核心）：LLM + 工具 + providers + 消息库。"""
    if not cfg.llm.api_key:
        raise SystemExit("缺少 LLM api_key：请在 config.toml 或 NEOAGENT_LLM_API_KEY 配置")

    # 工具（本期变体：M 直接持有工具——产品文档里这些是 E 的活）
    registry = ToolRegistry()
    register_file_tools(registry, cfg.files.allowed_roots)
    register_exec_tool(registry, ExecPolicy(
        timeout_seconds=cfg.exec.timeout_seconds,
        max_output_bytes=cfg.exec.max_output_bytes,
        allowed_roots=cfg.exec.allowed_roots,
        deny_patterns=cfg.exec.deny_patterns,
    ))

    # providers（供应商适配全部藏在 providers/ 后面）
    vision = make_vision(
        cfg.vision.provider, cfg.llm.base_url, cfg.llm.api_key,
        cfg.vision.file_expires_seconds or None)
    transcriber = make_transcriber(
        cfg.asr.provider, cfg.asr.base_url, cfg.asr.api_key, cfg.asr.model,
        cfg.asr.language, cfg.asr.max_audio_seconds)

    # 消息库（SQLite，全量落盘）+ 记忆文件 + 身份文件
    ledger = MessageLedger(cfg.data_dir / "neoagent.db")
    memory_file = Path(cfg.agent.memory_file)
    if not memory_file.is_absolute():
        memory_file = PROJECT_ROOT / memory_file
    identity_file = Path(cfg.agent.identity_file)
    if not identity_file.is_absolute():
        identity_file = PROJECT_ROOT / identity_file

    llm = LLMClient(cfg.llm.base_url, cfg.llm.api_key, cfg.llm.model,
                    thinking=cfg.llm.thinking)
    return AgentLoop(llm, registry, ledger, memory_file,
                     identity_file=identity_file,
                     vision=vision, transcriber=transcriber,
                     max_tool_rounds=cfg.agent.max_tool_rounds,
                     history_limit=cfg.agent.history_limit,
                     max_images=cfg.media.max_images_per_message)
