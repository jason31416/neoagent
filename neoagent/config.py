"""配置：~/.neoagent/ 下的 yml（人看的）+ channels/*.yml（每频道一个）。

布局（NEOAGENT_HOME 或 --home 可指到别处）：

    ~/.neoagent/
    ├── config.yml        总配置（model / personality / memory / limits / advanced）
    ├── channels/         每频道一个 yml，文件名 = 频道名（时间线 channel 值）
    ├── prompts/          人设等（首启从包内拷入，用户改、升级不冲）
    ├── logs/             日志（排障用，基本没人看）
    └── data/             账本、转录、记忆、工作台、登录态

优先级：环境变量 NEOAGENT_* > config.yml > 默认值。密钥可不落盘。
配置写回（/effort、/connect 等指令）走 ruamel 保注释——改文件和改指令殊途同归。
相对路径一律相对家目录解析。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ruamel.yaml import YAML

# 源码/安装目录：内置 prompts 与配置骨架从这里拷
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent

# 内置频道（控制台），不占 channels/*.yml
BUILTIN_CHANNELS = ("cli",)


def home_dir() -> Path:
    """配置家目录：NEOAGENT_HOME > ~/.neoagent。"""
    env = os.environ.get("NEOAGENT_HOME", "").strip()
    base = Path(env) if env else Path("~/.neoagent")
    return base.expanduser().resolve()


@dataclass
class LLMConfig:
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    model: str = "deepseek-chat"
    # 思考档位（OpenAI 标准参数 reasoning_effort，用户面叫 thinking）：
    # "none" 关思考 | "minimal/low/medium/high/xhigh/ultra/max" 开思考 | "" 不传
    reasoning_effort: str = "medium"


@dataclass
class AgentConfig:
    max_tool_rounds: int = 12
    history_token_budget: int = 250_000  # 时间线 token 预算（成组抛弃，召回不计）
    image_token_estimate: int = 1024     # 每张图估算 token（计数器用）
    identity_file: str = "prompts/messenger.md"   # 人设（相对家目录）
    workspace: str = "data/workspace"    # 工作台：exec 默认 cwd、相对路径基准、产出物落脚点
    # 模型请求上下文落盘（调试用）：置空关闭
    modellog_dir: str = "data/modellog"
    # 气泡连发间隔（秒）：多条消息之间的随机停顿（真实感 + 给阅读时间），0 = 不间隔
    bubble_gap_min: float = 0.5
    bubble_gap_max: float = 1.5


@dataclass
class RecorderConfig:
    """记忆整理：记忆的唯一写者 + 上下文退役的判断者（内部模块，配置面叫 memory）。"""
    memory_dir: str = "data/memory"        # 记忆库：memory.md + 子文件 + old/ 归档
    pause_debounce_seconds: int = 150      # 快整理延迟：任务收工后静默这么久才整理
    slow_interval: int = 86400             # 定期整理的节律
    slow_lookback_days: int = 7            # 定期整理回看起点（天）
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
    allowed_roots: List[str] = field(default_factory=lambda: ["~"])


@dataclass
class ExecConfig:
    timeout_seconds: int = 60
    max_output_bytes: int = 20000
    allowed_roots: List[str] = field(default_factory=lambda: ["~"])
    deny_patterns: List[str] = field(default_factory=list)


@dataclass
class ChannelConfig:
    """一个频道（channels/<name>.yml）：文件名 = 频道名，kind = 通道类型。"""
    name: str
    kind: str = ""
    enabled: bool = False
    params: Dict[str, Any] = field(default_factory=dict)   # 其余键自由（app_id 等）
    path: Optional[Path] = None


@dataclass
class Config:
    home: Path = field(default_factory=home_dir)
    llm: LLMConfig = field(default_factory=LLMConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    recorder: RecorderConfig = field(default_factory=RecorderConfig)
    media: MediaConfig = field(default_factory=MediaConfig)
    asr: AsrConfig = field(default_factory=AsrConfig)
    files: FilesConfig = field(default_factory=FilesConfig)
    exec: ExecConfig = field(default_factory=ExecConfig)
    channels: List[ChannelConfig] = field(default_factory=list)
    fresh_install: bool = False      # 本次启动才生成的骨架（给首启指引用）

    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    def channel_names(self) -> List[str]:
        """模型 channel 属性的白名单：启用的频道 + 内置频道。"""
        return [ch.name for ch in self.channels if ch.enabled] + list(BUILTIN_CHANNELS)


# ---------------------------------------------------------------- yml 读写


def _rt_yaml() -> YAML:
    """round-trip YAML：保注释、保排版——写回用户配置必须用它。"""
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    return y


def _load_yaml(path: Path) -> Any:
    if not path.exists():
        return {}
    data = _rt_yaml().load(path)
    return data if data is not None else {}


def _dump_yaml(path: Path, data: Any) -> None:
    _rt_yaml().dump(data, path)


def _get(data: Any, dotted: str, default: Any = None) -> Any:
    cur = data
    for key in dotted.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _set(data: Any, dotted: str, value: Any) -> None:
    """嵌套赋值（保注释的 CommentedMap 上原地改）。"""
    keys = dotted.split(".")
    cur = data
    for key in keys[:-1]:
        if not isinstance(cur, dict) or key not in cur:
            return
        cur = cur[key]
    if isinstance(cur, dict):
        cur[keys[-1]] = value


def config_path(home: Optional[Path] = None) -> Path:
    return (home or home_dir()) / "config.yml"


def save_config_value(dotted: str, value: Any, home: Optional[Path] = None) -> bool:
    """改 config.yml 里的一个键（保注释）：/effort 等指令的落盘口。

    指令改文件、手改文件两条路殊途同归（生效值单源头）。
    """
    path = config_path(home)
    try:
        data = _load_yaml(path)
        if not isinstance(data, dict):
            return False
        _set(data, dotted, value)
        _dump_yaml(path, data)
        return True
    except Exception:
        return False


def save_channel(ch: ChannelConfig) -> None:
    """把频道配置写回 channels/<name>.yml（保注释）：/connect 等指令的落盘口。"""
    data: Dict[str, Any] = {}
    if ch.path is not None and ch.path.exists():
        loaded = _load_yaml(ch.path)
        if isinstance(loaded, dict):
            data = loaded
    data["kind"] = ch.kind
    data["enabled"] = bool(ch.enabled)
    for key, value in ch.params.items():
        data[key] = value
    if ch.path is None:
        ch.path = home_dir() / "channels" / f"{ch.name}.yml"
    ch.path.parent.mkdir(parents=True, exist_ok=True)
    if ch.path.exists():
        _dump_yaml(ch.path, data)
    else:
        import io
        buf = io.StringIO()
        _rt_yaml().dump(data, buf)
        header = (f"# 频道配置：{ch.name}（kind={ch.kind}）\n"
                  "# enabled 控制起不起；聊天/控制台里 /connect //disconnect 会改这一行\n")
        ch.path.write_text(header + buf.getvalue(), encoding="utf-8")


def load_channels(home: Path) -> List[ChannelConfig]:
    """channels/*.yml → 频道列表；文件名 = 频道名，kind 决定通道类型。"""
    channels: List[ChannelConfig] = []
    dirp = home / "channels"
    if not dirp.is_dir():
        return channels
    for path in sorted(dirp.glob("*.yml")):
        raw = _load_yaml(path)
        if not isinstance(raw, dict):
            continue
        params = {k: v for k, v in raw.items() if k not in ("kind", "enabled")}
        channels.append(ChannelConfig(
            name=path.stem,
            kind=str(raw.get("kind") or path.stem),
            enabled=bool(raw.get("enabled", False)),
            params=params,
            path=path,
        ))
    return channels


# ---------------------------------------------------------------- 首启骨架


def ensure_home(home: Optional[Path] = None) -> bool:
    """没有配置就生成骨架：config.yml + channels/ + prompts/ + data/。

    返回是否是首启（本次才生成骨架）。幂等，已有的文件一律不覆盖。
    """
    home = home or home_dir()
    fresh = not (home / "config.yml").exists()
    home.mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(exist_ok=True)
    (home / "logs").mkdir(exist_ok=True)
    (home / "channels").mkdir(exist_ok=True)
    (home / "prompts").mkdir(exist_ok=True)

    example = PACKAGE_DIR / "config.example.yml"
    cfg_file = home / "config.yml"
    if not cfg_file.exists() and example.exists():
        shutil.copy(example, cfg_file)

    # 人设等：包内拷入 home，用户改自己那份、升级不被冲
    pkg_prompts = PACKAGE_DIR / "prompts"
    if pkg_prompts.is_dir():
        for src in pkg_prompts.glob("*.md"):
            dst = home / "prompts" / src.name
            if not dst.exists():
                shutil.copy(src, dst)
    return fresh


# ---------------------------------------------------------------- 加载


def _expand_roots(roots: List[str]) -> List[str]:
    """把 ~ 展开成绝对路径，后面比较时才不会踩坑。"""
    return [str(Path(r).expanduser().resolve()) for r in roots]


def resolve_path(home: Path, value: str) -> Path:
    """配置里的相对路径一律相对家目录。"""
    p = Path(value).expanduser()
    return p if p.is_absolute() else home / p


def load_config(home: Optional[Path] = None) -> Config:
    """读取 ~/.neoagent/config.yml 与 channels/*.yml，叠加环境变量覆盖。"""
    home = home or home_dir()
    fresh = ensure_home(home)
    cfg = Config(home=home, fresh_install=fresh)
    raw = _load_yaml(home / "config.yml")

    cfg.llm.base_url = str(_get(raw, "model.base_url", cfg.llm.base_url))
    cfg.llm.api_key = str(_get(raw, "model.api_key", cfg.llm.api_key) or "")
    cfg.llm.model = str(_get(raw, "model.name", cfg.llm.model))
    cfg.llm.reasoning_effort = str(_get(raw, "model.thinking", cfg.llm.reasoning_effort))

    cfg.agent.identity_file = str(_get(raw, "personality.prompt", cfg.agent.identity_file))
    cfg.agent.max_tool_rounds = int(_get(raw, "limits.max_tool_rounds", cfg.agent.max_tool_rounds))
    cfg.agent.history_token_budget = int(_get(raw, "limits.history_token_budget",
                                              cfg.agent.history_token_budget))

    cfg.recorder.memory_dir = str(_get(raw, "memory.dir", cfg.recorder.memory_dir))
    cfg.recorder.pause_debounce_seconds = int(_get(raw, "memory.tidy_delay",
                                                   cfg.recorder.pause_debounce_seconds))
    cfg.recorder.slow_interval = int(_get(raw, "memory.tidy_interval", cfg.recorder.slow_interval))
    cfg.recorder.slow_lookback_days = int(_get(raw, "memory.tidy_lookback_days",
                                               cfg.recorder.slow_lookback_days))
    cfg.recorder.reasoning_effort = str(_get(raw, "memory.tidy_thinking",
                                             cfg.recorder.reasoning_effort))
    cfg.recorder.max_tool_rounds = int(_get(raw, "memory.tidy_tool_rounds",
                                            cfg.recorder.max_tool_rounds))

    cfg.agent.workspace = str(_get(raw, "advanced.workspace", cfg.agent.workspace))
    cfg.agent.modellog_dir = str(_get(raw, "advanced.modellog_dir", cfg.agent.modellog_dir))
    cfg.agent.image_token_estimate = int(_get(raw, "advanced.image_token_estimate",
                                              cfg.agent.image_token_estimate))
    gap = _get(raw, "advanced.bubble_gap", [cfg.agent.bubble_gap_min, cfg.agent.bubble_gap_max])
    try:
        cfg.agent.bubble_gap_min = float(gap[0])
        cfg.agent.bubble_gap_max = float(gap[1])
    except (TypeError, ValueError, IndexError):
        pass

    cfg.media.max_bytes = int(_get(raw, "advanced.media.max_bytes", cfg.media.max_bytes))
    cfg.media.max_images_per_message = int(_get(raw, "advanced.media.max_images_per_message",
                                                cfg.media.max_images_per_message))
    cfg.media.download_quoted_media = bool(_get(raw, "advanced.media.download_quoted_media",
                                                cfg.media.download_quoted_media))

    cfg.asr.provider = str(_get(raw, "advanced.asr.provider", cfg.asr.provider))
    cfg.asr.api_key = str(_get(raw, "advanced.asr.api_key", cfg.asr.api_key) or "")
    cfg.asr.base_url = str(_get(raw, "advanced.asr.base_url", cfg.asr.base_url))
    cfg.asr.model = str(_get(raw, "advanced.asr.model", cfg.asr.model))
    cfg.asr.language = str(_get(raw, "advanced.asr.language", cfg.asr.language))
    cfg.asr.max_audio_seconds = int(_get(raw, "advanced.asr.max_audio_seconds",
                                         cfg.asr.max_audio_seconds))

    if _get(raw, "advanced.safety.files_roots") is not None:
        cfg.files.allowed_roots = list(_get(raw, "advanced.safety.files_roots"))
    if _get(raw, "advanced.safety.exec_roots") is not None:
        cfg.exec.allowed_roots = list(_get(raw, "advanced.safety.exec_roots"))
    cfg.exec.timeout_seconds = int(_get(raw, "advanced.safety.exec_timeout",
                                        cfg.exec.timeout_seconds))
    cfg.exec.max_output_bytes = int(_get(raw, "advanced.safety.exec_max_output",
                                         cfg.exec.max_output_bytes))
    if _get(raw, "advanced.safety.deny_patterns") is not None:
        cfg.exec.deny_patterns = list(_get(raw, "advanced.safety.deny_patterns"))

    cfg.channels = load_channels(home)

    # 环境变量覆盖（密钥走这里更安全）
    cfg.llm.api_key = os.environ.get("NEOAGENT_LLM_API_KEY", cfg.llm.api_key)
    cfg.llm.base_url = os.environ.get("NEOAGENT_LLM_BASE_URL", cfg.llm.base_url)
    cfg.llm.model = os.environ.get("NEOAGENT_LLM_MODEL", cfg.llm.model)
    cfg.llm.reasoning_effort = os.environ.get("NEOAGENT_LLM_THINKING", cfg.llm.reasoning_effort)
    cfg.asr.api_key = os.environ.get("NEOAGENT_ASR_API_KEY", cfg.asr.api_key)
    cfg.recorder.reasoning_effort = os.environ.get("NEOAGENT_MEMORY_TIDY_THINKING",
                                                   cfg.recorder.reasoning_effort)
    # 飞书凭据按 kind 覆盖到每个飞书频道（多开也吃得下）
    feishu_env = {k.split("__", 1)[0][len("NEOAGENT_FEISHU_"):].lower(): v
                  for k, v in os.environ.items() if k.startswith("NEOAGENT_FEISHU_")}
    for ch in cfg.channels:
        if ch.kind == "feishu":
            for key in ("app_id", "app_secret"):
                if key in feishu_env:
                    ch.params[key] = feishu_env[key]

    # 相对路径一律相对家目录；根目录统一展开 ~
    cfg.files.allowed_roots = _expand_roots(cfg.files.allowed_roots)
    cfg.exec.allowed_roots = _expand_roots(cfg.exec.allowed_roots)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg


class ReasoningEffortSource:
    """thinking 热调整：config.yml 改了下一轮就生效（按 mtime 重载）。

    每次模型调用前 get()——文件没变零成本；变了就重读（小 yml，毫秒级）。
    配置坏了沿用旧值，绝不炸请求。/effort 指令改的就是这个文件。
    """

    def __init__(self, home: Optional[Path] = None, initial: str = "medium") -> None:
        self._home = Path(home) if home else home_dir()
        self._value = initial
        self._mtime: Optional[float] = None

    def get(self) -> str:
        path = config_path(self._home)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return self._value
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                self._value = load_config(self._home).llm.reasoning_effort
            except Exception:
                pass
        return self._value

    def __call__(self) -> str:
        """effort_provider 契约：可调用、返回当前档位。"""
        return self.get()
