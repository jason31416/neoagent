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
    # 思考档位（OpenAI 标准参数 reasoning_effort）：
    # "none" 关思考 | "minimal/low/medium/high/xhigh/ultra/max" 开思考 | "" = 不传（用服务端默认）
    reasoning_effort: str = "medium"


@dataclass
class AgentConfig:
    max_tool_rounds: int = 12
    history_token_budget: int = 250_000  # 时间线 token 预算（成组抛弃，召回不计）
    image_token_estimate: int = 1024     # 每张图估算 token（计数器用）
    memory_file: str = "data/MEMORY.md"  # 旧记忆文件（仅用于一次性迁移到 recorder 的 memory_dir）
    identity_file: str = "IDENTITY.md"   # 身份文件（【它是谁】段，仓库根）
    workspace: str = "data/workspace"    # 工作台：exec 默认 cwd、相对路径基准、产出物落脚点
    # 模型请求上下文落盘（M/R 共用，调试用）：每会话一份 md、只留最新一次请求，
    # 置空关闭
    modellog_dir: str = "data/modellog"


@dataclass
class RecorderConfig:
    """R（Recorder）——记忆整理模块：记忆的唯一写者 + 上下文退役的判断者。"""
    memory_dir: str = "data/memory"        # 记忆库：memory.md（注入 M）+ 子文件 + old/ 归档
    pause_debounce_seconds: int = 150      # 停顿判定（末条用户消息后多久算对话停顿）
    slow_interval_hours: int = 24          # 慢周期整理的节律
    slow_lookback_days: int = 7            # 慢周期回看起点（天）
    max_tool_rounds: int = 12              # 一次整理跑的工具轮上限
    reasoning_effort: str = "medium"       # 整理用的思考档位（快慢同档）


@dataclass
class MediaConfig:
    max_bytes: int = 20 * 1024 * 1024        # 20MB
    max_images_per_message: int = 4
    download_quoted_media: bool = False      # 引用里的媒体默认不下载


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
    recorder: RecorderConfig = field(default_factory=RecorderConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
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
        cfg.llm.reasoning_effort = str(llm.get("reasoning_effort", cfg.llm.reasoning_effort))
        # 旧字段 thinking 兼容映射（只在没配 reasoning_effort 时生效）：
        # "disabled"→"none"，"enabled"→"medium"，""→不传
        if "reasoning_effort" not in llm and "thinking" in llm:
            t = str(llm.get("thinking", ""))
            cfg.llm.reasoning_effort = {"disabled": "none", "enabled": "medium"}.get(t, t)

        ag = raw.get("agent", {})
        cfg.agent.max_tool_rounds = int(ag.get("max_tool_rounds", cfg.agent.max_tool_rounds))
        cfg.agent.history_token_budget = int(ag.get("history_token_budget",
                                                   cfg.agent.history_token_budget))
        cfg.agent.image_token_estimate = int(ag.get("image_token_estimate",
                                                    cfg.agent.image_token_estimate))
        cfg.agent.memory_file = str(ag.get("memory_file", cfg.agent.memory_file))
        cfg.agent.identity_file = str(ag.get("identity_file", cfg.agent.identity_file))
        cfg.agent.workspace = str(ag.get("workspace", cfg.agent.workspace))
        cfg.agent.modellog_dir = str(ag.get("modellog_dir", cfg.agent.modellog_dir))

        rc = raw.get("recorder", {})
        cfg.recorder.memory_dir = str(rc.get("memory_dir", cfg.recorder.memory_dir))
        cfg.recorder.pause_debounce_seconds = int(rc.get("pause_debounce_seconds",
                                                         cfg.recorder.pause_debounce_seconds))
        cfg.recorder.slow_interval_hours = int(rc.get("slow_interval_hours",
                                                      cfg.recorder.slow_interval_hours))
        cfg.recorder.slow_lookback_days = int(rc.get("slow_lookback_days",
                                                     cfg.recorder.slow_lookback_days))
        cfg.recorder.max_tool_rounds = int(rc.get("max_tool_rounds",
                                                  cfg.recorder.max_tool_rounds))
        cfg.recorder.reasoning_effort = str(rc.get("reasoning_effort",
                                                   cfg.recorder.reasoning_effort))

        md = raw.get("media", {})
        cfg.media.max_bytes = int(md.get("max_bytes", cfg.media.max_bytes))
        cfg.media.max_images_per_message = int(md.get("max_images_per_message", cfg.media.max_images_per_message))
        cfg.media.download_quoted_media = bool(md.get("download_quoted_media", cfg.media.download_quoted_media))

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
    cfg.llm.reasoning_effort = os.environ.get("NEOAGENT_LLM_REASONING_EFFORT",
                                             cfg.llm.reasoning_effort)
    cfg.asr.api_key = os.environ.get("NEOAGENT_ASR_API_KEY", cfg.asr.api_key)
    cfg.recorder.reasoning_effort = os.environ.get("NEOAGENT_RECORDER_REASONING_EFFORT",
                                                   cfg.recorder.reasoning_effort)
    cfg.feishu.app_id = os.environ.get("NEOAGENT_FEISHU_APP_ID", cfg.feishu.app_id)
    cfg.feishu.app_secret = os.environ.get("NEOAGENT_FEISHU_APP_SECRET", cfg.feishu.app_secret)

    # 根目录统一展开 ~ 并解析为绝对路径
    cfg.files.allowed_roots = _expand_roots(cfg.files.allowed_roots)
    cfg.exec.allowed_roots = _expand_roots(cfg.exec.allowed_roots)

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg


class ReasoningEffortSource:
    """reasoning_effort 热调整：config.toml 改了下一轮就生效（按 mtime 重载）。

    每次模型调用前 get()——文件没变零成本；变了就重读（小 toml，毫秒级）。
    配置坏了沿用旧值，绝不炸请求。将来"聊天指令调档"可替换本类接入。"""

    def __init__(self, path: Path | None = None, initial: str = "medium") -> None:
        self._path = Path(path) if path else PROJECT_ROOT / "config.toml"
        self._value = initial
        self._mtime: float | None = None

    def get(self) -> str:
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return self._value
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                self._value = load_config(self._path).llm.reasoning_effort
            except Exception:
                pass
        return self._value

    def __call__(self) -> str:
        """effort_provider 契约：可调用、返回当前档位。"""
        return self.get()
