"""配置加载。

优先级：环境变量 NEOAGENT_* > config.toml > 默认值。
密钥放 config.toml（已 gitignore）或环境变量，绝不写死在代码里。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

# 项目根目录（neoagent/ 包的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class LLMConfig:
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-chat"
    thinking: str = "disabled"   # DeepSeek thinking 开关；"" = 不传该参数


@dataclass
class AgentConfig:
    max_tool_rounds: int = 12
    history_token_budget: int = 250_000  # 时间线 token 预算（成组抛弃，召回不计）
    image_token_estimate: int = 1024     # 每张图估算 token（计数器用）
    memory_file: str = "data/MEMORY.md"  # 记忆文件（M 自己编辑，全文注入【记忆】段）
    identity_file: str = "IDENTITY.md"   # 身份文件（【它是谁】段，仓库根）
    workspace: str = "data/workspace"    # 工作台：exec 默认 cwd、相对路径基准、产出物落脚点


@dataclass
class MediaConfig:
    max_bytes: int = 20 * 1024 * 1024        # 20MB
    max_images_per_message: int = 4
    download_quoted_media: bool = False      # 引用里的媒体默认不下载


@dataclass
class VisionConfig:
    provider: str = "deepseek-files"         # 可拆换：inline-base64
    file_expires_seconds: int = 2592000      # 30 天过期（0 = 永久）


@dataclass
class AsrConfig:
    provider: str = "xiaomi-mimo"            # 可拆换
    api_key: str = ""
    base_url: str = "https://api.xiaomimimo.com/v1"
    model: str = "mimo-v2.5-asr"
    language: str = "auto"
    max_audio_seconds: int = 300


@dataclass
class FilesConfig:
    allowed_roots: List[str] = field(default_factory=lambda: [str(PROJECT_ROOT)])


@dataclass
class ExecConfig:
    timeout_seconds: int = 60
    max_output_bytes: int = 20000
    allowed_roots: List[str] = field(default_factory=lambda: [str(PROJECT_ROOT)])
    deny_patterns: List[str] = field(default_factory=list)


@dataclass
class WeixinConfig:
    enabled: bool = False


@dataclass
class FeishuConfig:
    enabled: bool = False
    app_id: str = ""
    app_secret: str = ""


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    files: FilesConfig = field(default_factory=FilesConfig)
    exec: ExecConfig = field(default_factory=ExecConfig)
    weixin: WeixinConfig = field(default_factory=WeixinConfig)
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")


def _expand_roots(roots: List[str]) -> List[str]:
    """把 ~ 展开成绝对路径，后面比较时才不会踩坑。"""
    return [str(Path(r).expanduser().resolve()) for r in roots]


def load_config(path: Path | None = None) -> Config:
    """读取 config.toml（如存在）并叠加环境变量覆盖。"""
    cfg = Config()
    path = path or PROJECT_ROOT / "config.toml"

    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)

        llm = raw.get("llm", {})
        cfg.llm.base_url = llm.get("base_url", cfg.llm.base_url)
        cfg.llm.api_key = llm.get("api_key", cfg.llm.api_key)
        cfg.llm.model = llm.get("model", cfg.llm.model)
        cfg.llm.thinking = str(llm.get("thinking", cfg.llm.thinking))

        ag = raw.get("agent", {})
        cfg.agent.max_tool_rounds = int(ag.get("max_tool_rounds", cfg.agent.max_tool_rounds))
        cfg.agent.history_token_budget = int(ag.get("history_token_budget",
                                                   cfg.agent.history_token_budget))
        cfg.agent.image_token_estimate = int(ag.get("image_token_estimate",
                                                    cfg.agent.image_token_estimate))
        cfg.agent.memory_file = str(ag.get("memory_file", cfg.agent.memory_file))
        cfg.agent.identity_file = str(ag.get("identity_file", cfg.agent.identity_file))
        cfg.agent.workspace = str(ag.get("workspace", cfg.agent.workspace))

        md = raw.get("media", {})
        cfg.media.max_bytes = int(md.get("max_bytes", cfg.media.max_bytes))
        cfg.media.max_images_per_message = int(md.get("max_images_per_message", cfg.media.max_images_per_message))
        cfg.media.download_quoted_media = bool(md.get("download_quoted_media", cfg.media.download_quoted_media))

        vs = raw.get("vision", {})
        cfg.vision.provider = str(vs.get("provider", cfg.vision.provider))
        cfg.vision.file_expires_seconds = int(vs.get("file_expires_seconds", cfg.vision.file_expires_seconds))

        ar = raw.get("asr", {})
        cfg.asr.provider = str(ar.get("provider", cfg.asr.provider))
        cfg.asr.api_key = str(ar.get("api_key", cfg.asr.api_key))
        cfg.asr.base_url = str(ar.get("base_url", cfg.asr.base_url))
        cfg.asr.model = str(ar.get("model", cfg.asr.model))
        cfg.asr.language = str(ar.get("language", cfg.asr.language))
        cfg.asr.max_audio_seconds = int(ar.get("max_audio_seconds", cfg.asr.max_audio_seconds))

        fl = raw.get("files", {})
        if "allowed_roots" in fl:
            cfg.files.allowed_roots = list(fl["allowed_roots"])

        ex = raw.get("exec", {})
        cfg.exec.timeout_seconds = int(ex.get("timeout_seconds", cfg.exec.timeout_seconds))
        cfg.exec.max_output_bytes = int(ex.get("max_output_bytes", cfg.exec.max_output_bytes))
        if "allowed_roots" in ex:
            cfg.exec.allowed_roots = list(ex["allowed_roots"])
        if "deny_patterns" in ex:
            cfg.exec.deny_patterns = list(ex["deny_patterns"])

        wx = raw.get("channels", {}).get("weixin", {})
        cfg.weixin.enabled = bool(wx.get("enabled", False))

        fs = raw.get("channels", {}).get("feishu", {})
        cfg.feishu.enabled = bool(fs.get("enabled", False))
        cfg.feishu.app_id = str(fs.get("app_id", ""))
        cfg.feishu.app_secret = str(fs.get("app_secret", ""))

    # 环境变量覆盖（密钥走这里更安全）
    cfg.llm.api_key = os.environ.get("NEOAGENT_LLM_API_KEY", cfg.llm.api_key)
    cfg.llm.base_url = os.environ.get("NEOAGENT_LLM_BASE_URL", cfg.llm.base_url)
    cfg.llm.model = os.environ.get("NEOAGENT_LLM_MODEL", cfg.llm.model)
    cfg.llm.thinking = os.environ.get("NEOAGENT_LLM_THINKING", cfg.llm.thinking)
    cfg.asr.api_key = os.environ.get("NEOAGENT_ASR_API_KEY", cfg.asr.api_key)
    cfg.feishu.app_id = os.environ.get("NEOAGENT_FEISHU_APP_ID", cfg.feishu.app_id)
    cfg.feishu.app_secret = os.environ.get("NEOAGENT_FEISHU_APP_SECRET", cfg.feishu.app_secret)

    # 根目录统一展开 ~ 并解析为绝对路径
    cfg.files.allowed_roots = _expand_roots(cfg.files.allowed_roots)
    cfg.exec.allowed_roots = _expand_roots(cfg.exec.allowed_roots)

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
