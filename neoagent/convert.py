"""老配置迁移：把仓库里的 config.toml + data/ 一次搬进 ~/.neoagent/。

    uv run python -m neoagent.convert
    uv run python -m neoagent.convert --src <老目录> --home <新家目录>

只读不动原件：config.yml 生成（骨架打底、老值覆盖、注释保留）、
channels/*.yml 从 [channels.*] 生成、data/ 是**复制**（确认无误后可自行删旧目录）。
跑一次就够，重复跑会跳过已有的家目录数据。
"""

from __future__ import annotations

import argparse
import shutil
import tomllib
from pathlib import Path
from typing import Any, Dict

from .config import (ChannelConfig, PACKAGE_DIR, PROJECT_ROOT, ensure_home,
                     home_dir, save_channel, save_config_value)


def _effort_of(llm: Dict[str, Any]) -> str:
    """老 [llm] 的思考档位：reasoning_effort 优先，旧字段 thinking 兼容。"""
    if "reasoning_effort" in llm:
        return str(llm["reasoning_effort"])
    t = str(llm.get("thinking", ""))
    return {"disabled": "none", "enabled": "medium"}.get(t, t)


def convert(src: Path, home: Path) -> None:
    toml_path = src / "config.toml"
    if not toml_path.exists():
        raise SystemExit(f"没找到老配置：{toml_path}")
    with open(toml_path, "rb") as f:
        raw = tomllib.load(f)

    ensure_home(home)

    llm = raw.get("llm", {})
    ag = raw.get("agent", {})
    rc = raw.get("recorder", {})
    md = raw.get("media", {})
    ar = raw.get("asr", {})
    fl = raw.get("files", {})
    ex = raw.get("exec", {})

    # 老值覆盖进骨架（骨架的注释和排版保留；没配的键维持默认）
    overlay = {
        "model.base_url": llm.get("base_url"),
        "model.api_key": llm.get("api_key"),
        "model.name": llm.get("model"),
        "model.thinking": _effort_of(llm) if llm else None,
        "personality.prompt": "prompts/messenger.md",
        "memory.dir": rc.get("memory_dir"),
        "memory.tidy_delay": rc.get("pause_debounce_seconds"),
        "memory.tidy_interval": rc.get("slow_interval"),
        "memory.tidy_lookback_days": rc.get("slow_lookback_days"),
        "memory.tidy_thinking": rc.get("reasoning_effort"),
        "memory.tidy_tool_rounds": rc.get("max_tool_rounds"),
        "limits.max_tool_rounds": ag.get("max_tool_rounds"),
        "limits.history_token_budget": ag.get("history_token_budget"),
        "advanced.workspace": ag.get("workspace"),
        "advanced.modellog_dir": ag.get("modellog_dir"),
        "advanced.image_token_estimate": ag.get("image_token_estimate"),
        "advanced.bubble_gap": ([ag["bubble_gap_min"], ag["bubble_gap_max"]]
                                if "bubble_gap_min" in ag and "bubble_gap_max" in ag else None),
        "advanced.media.max_bytes": md.get("max_bytes"),
        "advanced.media.max_images_per_message": md.get("max_images_per_message"),
        "advanced.media.download_quoted_media": md.get("download_quoted_media"),
        "advanced.asr.provider": ar.get("provider"),
        "advanced.asr.api_key": ar.get("api_key"),
        "advanced.asr.base_url": ar.get("base_url"),
        "advanced.asr.model": ar.get("model"),
        "advanced.asr.language": ar.get("language"),
        "advanced.asr.max_audio_seconds": ar.get("max_audio_seconds"),
        "advanced.safety.files_roots": fl.get("allowed_roots"),
        "advanced.safety.exec_roots": ex.get("allowed_roots"),
        "advanced.safety.exec_timeout": ex.get("timeout_seconds"),
        "advanced.safety.exec_max_output": ex.get("max_output_bytes"),
        "advanced.safety.deny_patterns": ex.get("deny_patterns"),
    }
    for key, value in overlay.items():
        if value is not None:
            save_config_value(key, value, home=home)

    # 频道：[channels.*] → channels/<name>.yml（两个内置类型都生成，好找）
    old_ch = raw.get("channels", {})
    for name in ("weixin", "feishu"):
        section = old_ch.get(name, {})
        params: Dict[str, Any] = {"owner": ""}
        if name == "feishu":
            params["app_id"] = str(section.get("app_id", ""))
            params["app_secret"] = str(section.get("app_secret", ""))
        save_channel(ChannelConfig(
            name=name, kind=name,
            enabled=bool(section.get("enabled", False)),
            params=params,
            path=home / "channels" / f"{name}.yml"))

    # 人设：老 identity_file 如果是自定义路径，把它拷成新家的人设
    identity = str(ag.get("identity_file", ""))
    default_identity = str(PACKAGE_DIR / "prompts" / "messenger.md")
    if identity:
        old_identity = Path(identity)
        if not old_identity.is_absolute():
            old_identity = src / old_identity
        if old_identity.exists() and str(old_identity.resolve()) != default_identity:
            shutil.copy(old_identity, home / "prompts" / "messenger.md")
            print(f"人设：{old_identity} → {home / 'prompts' / 'messenger.md'}")

    # 旧记忆文件：一次性迁入记忆库（老 app 也会做，这里兜住老 data 没跑过新代码的情况）
    memory_file = str(ag.get("memory_file", ""))
    if memory_file:
        old_mem = Path(memory_file)
        if not old_mem.is_absolute():
            old_mem = src / old_mem
        new_mem = home / "data" / "memory" / "memory.md"
        if old_mem.exists() and not new_mem.exists():
            new_mem.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(old_mem, new_mem)
            print(f"旧记忆：{old_mem} → {new_mem}")

    # data/：复制（不动原件；新家已有账本就跳过，不覆盖）
    old_data = src / "data"
    new_data = home / "data"
    if old_data.is_dir():
        if (new_data / "neoagent.db").exists():
            print(f"data：{new_data / 'neoagent.db'} 已存在，跳过复制（不覆盖）")
        else:
            shutil.copytree(old_data, new_data, dirs_exist_ok=True)
            print(f"data：{old_data} → {new_data}（复制，原件保留在 {old_data}，确认后可删）")

    print(f"完成。配置在 {home / 'config.yml'}，频道在 {home / 'channels'}。")
    print(f"注意：{toml_path} 原样保留没动；密钥若走环境变量（NEOAGENT_*）则继续有效。")


def main() -> None:
    parser = argparse.ArgumentParser(description="把老 config.toml + data/ 迁入 ~/.neoagent/")
    parser.add_argument("--src", type=Path, default=PROJECT_ROOT, help="老目录（默认仓库根）")
    parser.add_argument("--home", type=Path, default=None, help="新家目录（默认 ~/.neoagent）")
    args = parser.parse_args()
    convert(args.src.resolve(), (args.home or home_dir()).expanduser().resolve())


if __name__ == "__main__":
    main()
