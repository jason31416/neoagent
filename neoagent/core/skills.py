"""skill 内核：neoagent 对外部服务的"手"。

skill = ~/.neoagent/skills/<name>/ 一个目录：
- skill.md   语义（frontmatter name/description + 全文说明，给人和 neoagent 看）
- skill.yml  机械声明（env 键名 + tools[]：name/desc/params/run/timeout）
- .env       密钥（KEY=VALUE），执行时静默注入脚本环境；值不进日志、不进结果
- scripts/   脚本本体（bash 直接 curl，或 .venv/bin/python xxx.py，随你）

契约（工具 = 脚本，全文见 docs/设计-skill框架.md）：
参数以环境变量注入（参数名大写；json 类型传 JSON 文本）→ bash -c run（cwd = skill 目录）
→ stdout 即结果（30K 截断）；exit≠0 / 超时归一成可读失败。密钥从 .env 静默加载，
skill.yml 的 env: 只声明键名——缺键先报"补 .env"，不放脚本去撞 401。

本模块只管 skill 的发现/解析/校验/执行（纯逻辑，不碰注册表）；
给模型的 XML 信封在 tools/skill_tools.py。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .proc import kill_process_group

logger = logging.getLogger(__name__)

_YAML = YAML(typ="safe")   # skill.yml / frontmatter 都是配置，永远走 safe

SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")     # 禁点：点是 id 的命名空间分隔
TOOL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")
PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")   # 要当环境变量名用
ENV_KEY_RE = PARAM_NAME_RE
PARAM_TYPES = ("string", "number", "bool", "json")

CALL_STDOUT_MAX = 30000   # stdout 进结果的字符上限（与工具结果全局上限一致）
CALL_STDERR_MAX = 4000    # stderr 只做诊断，给一小段就够
TIMEOUT_DEFAULT = 60      # 每工具默认超时（秒）
TIMEOUT_MAX = 600         # 超时硬上限（与 exec 一致）


# ----------------------------------------------------------------------
# 数据模型
# ----------------------------------------------------------------------

@dataclass
class SkillParam:
    name: str
    type: str = "string"           # PARAM_TYPES 里的一个
    required: bool = False
    default: Any = None


@dataclass
class SkillTool:
    name: str
    desc: str = ""
    params: List[SkillParam] = field(default_factory=list)
    run: str = ""
    timeout: int = TIMEOUT_DEFAULT


@dataclass
class Skill:
    name: str
    directory: Path
    md_path: Optional[Path] = None
    yml_path: Optional[Path] = None
    description: str = ""
    env_keys: List[str] = field(default_factory=list)
    tools: List[SkillTool] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)   # 解析/schema 问题（skill_check 喂给自修）

    def tool(self, name: str) -> Optional[SkillTool]:
        for t in self.tools:
            if t.name == name:
                return t
        return None


@dataclass
class CallOutcome:
    """一次 skill_call 的执行结果（成功与否看 error / exit）。"""
    error: str = ""                 # 失败的可读说明（非空 = 没跑成）
    error_kind: str = "bad_param"   # 失败类别：bad_param / timeout / exec_failed
    exit: Optional[int] = None
    elapsed: float = 0.0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    timeout: int = TIMEOUT_DEFAULT


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------

def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """skill.md 的 frontmatter（--- 包夹的 YAML）→ (meta, 正文)。没有就整篇当正文。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            head = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1:])
            try:
                meta = _YAML.load(head) or {}
            except YAMLError:
                return {}, text
            return (meta if isinstance(meta, dict) else {}), body
    return {}, text


def _load_yaml(path: Path, problems: List[str]) -> Any:
    try:
        return _YAML.load(path.read_text(encoding="utf-8"))
    except YAMLError as e:
        problems.append(f"skill.yml 解析失败：{e}")
    except OSError as e:
        problems.append(f"skill.yml 读取失败：{e}")
    return None


def _parse_tool(raw: Any, problems: List[str]) -> Optional[SkillTool]:
    """skill.yml 里的一项工具声明 → SkillTool（宽松解析，问题记进 problems）。"""
    if not isinstance(raw, dict):
        problems.append(f"tools 里有一项不是对象：{raw!r}")
        return None
    unknown = set(raw) - {"name", "desc", "params", "run", "timeout"}
    if unknown:
        problems.append(f"工具 {raw.get('name', '?')} 有未知字段：{sorted(unknown)}")
    name = str(raw.get("name", "")).strip()
    params: List[SkillParam] = []
    for p in (raw.get("params") or []):
        if not isinstance(p, dict):
            problems.append(f"工具 {name or '?'} 有参数不是对象：{p!r}")
            continue
        pu = set(p) - {"name", "type", "required", "default"}
        if pu:
            problems.append(f"工具 {name or '?'} 的参数 {p.get('name', '?')} 有未知字段：{sorted(pu)}")
        params.append(SkillParam(
            name=str(p.get("name", "")).strip(),
            type=str(p.get("type", "string")).strip(),
            required=bool(p.get("required", False)),
            default=p.get("default")))
    timeout = raw.get("timeout", TIMEOUT_DEFAULT)
    try:
        timeout = max(1, min(int(timeout), TIMEOUT_MAX))
    except (TypeError, ValueError):
        problems.append(f"工具 {name or '?'} 的 timeout 不是整数秒：{raw.get('timeout')!r}")
        timeout = TIMEOUT_DEFAULT
    return SkillTool(name=name, desc=str(raw.get("desc", "")).strip(),
                     params=params, run=str(raw.get("run", "")).strip(), timeout=timeout)


def load_skill(directory: Path) -> Skill:
    """读一个 skill 目录（skill.md 或 skill.yml 存在即算）。解析问题进 skill.problems。"""
    skill = Skill(name=directory.name, directory=directory)
    md = directory / "skill.md"
    yml = directory / "skill.yml"
    if md.is_file():
        skill.md_path = md
        try:
            meta, _ = _parse_frontmatter(md.read_text(encoding="utf-8"))
            skill.description = str(meta.get("description", "")).strip()
        except OSError as e:
            skill.problems.append(f"skill.md 读取失败：{e}")
    if yml.is_file():
        skill.yml_path = yml
        data = _load_yaml(yml, skill.problems)
        if data is not None:
            if not isinstance(data, dict):
                skill.problems.append("skill.yml 顶层必须是对象（env/tools）")
            else:
                unknown = set(data) - {"env", "tools"}
                if unknown:
                    skill.problems.append(f"skill.yml 有未知字段：{sorted(unknown)}")
                env = data.get("env") or []
                if not isinstance(env, list):
                    skill.problems.append("env 必须是键名列表")
                else:
                    skill.env_keys = [str(k).strip() for k in env]
                for raw in (data.get("tools") or []):
                    t = _parse_tool(raw, skill.problems)
                    if t is not None:
                        skill.tools.append(t)
    return skill


def discover(skills_dir: Path) -> List[Skill]:
    """扫 skills 目录，按名字排序返回全部 skill。"""
    if not skills_dir.is_dir():
        return []
    out: List[Skill] = []
    for d in sorted(skills_dir.iterdir(), key=lambda p: p.name):
        if d.is_dir() and ((d / "skill.md").is_file() or (d / "skill.yml").is_file()):
            out.append(load_skill(d))
    return out


def find_skill(skills_dir: Path, name: str) -> Optional[Skill]:
    d = skills_dir / name
    if d.is_dir() and ((d / "skill.md").is_file() or (d / "skill.yml").is_file()):
        return load_skill(d)
    return None


def list_names(skills_dir: Path) -> List[str]:
    """skill 名清单（/skills 指令用：只给名字）。"""
    return [s.name for s in discover(skills_dir)]


# ----------------------------------------------------------------------
# 校验（skill_check 的六条 + 名字规则）
# ----------------------------------------------------------------------

def _env_file(skill: Skill) -> Dict[str, str]:
    return load_env_file(skill.directory / ".env")


def load_env_file(path: Path) -> Dict[str, str]:
    """.env（KEY=VALUE 行）→ 字典。坏行忽略（不炸，check 会给提示）；值原样去引号。"""
    env: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return env
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if not ENV_KEY_RE.fullmatch(k):
            continue
        env[k] = v.strip().strip('"').strip("'")
    return env


def check_skill(skill: Skill) -> List[Tuple[bool, str]]:
    """逐条校验，返回 (ok, 说明)。给 neoagent 自维护用，措辞是修法不是责备。"""
    rows: List[Tuple[bool, str]] = []

    # 0. 解析遗留问题
    for p in skill.problems:
        rows.append((False, p))

    # 1. 名字规则（skill 名禁点：点是 id 的命名空间分隔）
    if not SKILL_NAME_RE.fullmatch(skill.name):
        rows.append((False, f"skill 名不合法：{skill.name}（只允许小写字母/数字/-/_，且不含点）"))
    else:
        rows.append((True, "skill 名合法"))

    # 2. skill.md / frontmatter
    if skill.md_path is None:
        rows.append((False, "缺 skill.md（语义说明：这 skill 是干嘛的、参数什么含义、坑）"))
    else:
        meta, _ = _parse_frontmatter(skill.md_path.read_text(encoding="utf-8"))
        missing = [k for k in ("name", "description") if not str(meta.get(k, "")).strip()]
        if missing:
            rows.append((False, f"skill.md frontmatter 缺：{'/'.join(missing)}（skill_list 的简介靠它）"))
        else:
            rows.append((True, "skill.md 与 frontmatter 齐全"))

    # 3. skill.yml 结构（没有 yml = 只有文档，不算错）
    if skill.yml_path is None:
        rows.append((True, "无 skill.yml（纯文档 skill，skill_call 不适用，正常）"))
        return rows
    if skill.problems and any(p.startswith("skill.yml") for p in skill.problems):
        return rows    # yml 都没解析出来，后面的都是空谈

    # 4. 工具名
    names = [t.name for t in skill.tools]
    bad = [n for n in names if not TOOL_NAME_RE.fullmatch(n)]
    dup = {n for n in names if names.count(n) > 1}
    if bad:
        rows.append((False, f"工具名不合法：{bad}（小写字母/数字/_，不含点）"))
    if dup:
        rows.append((False, f"工具名重复：{sorted(dup)}"))
    if not bad and not dup:
        rows.append((True, f"工具名合法（{len(names)} 个）" if names else "没有声明工具"))

    # 5. env 键名 + .env 缺不缺（只查键名，不看值）
    env_file = _env_file(skill)
    bad_env = [k for k in skill.env_keys if not ENV_KEY_RE.fullmatch(k)]
    if bad_env:
        rows.append((False, f"env 键名不合法：{bad_env}"))
    missing_env = [k for k in skill.env_keys if k not in env_file]
    if missing_env:
        rows.append((False, f"缺密钥：{missing_env}（补 {skill.directory / '.env'}，KEY=VALUE 一行一个）"))
    elif skill.env_keys:
        rows.append((True, f"密钥键名齐全（{len(skill.env_keys)} 个）"))

    # 6. 逐工具：参数表 + run
    for t in skill.tools:
        if not t.run:
            rows.append((False, f"工具 {t.name} 的 run 为空（执行什么脚本要写）"))
        else:
            rows.extend(_check_run_refs(skill, t))
        pnames = [p.name for p in t.params]
        for p in t.params:
            if not PARAM_NAME_RE.fullmatch(p.name):
                rows.append((False, f"工具 {t.name} 参数名不合法：{p.name!r}（要当环境变量名用）"))
            elif p.type not in PARAM_TYPES:
                rows.append((False, f"工具 {t.name} 参数 {p.name} 类型不合法：{p.type}（可选 {PARAM_TYPES}）"))
            elif p.name.upper() in set(skill.env_keys) | set(env_file):
                rows.append((False, f"工具 {t.name} 参数 {p.name} 与 .env 键 {p.name.upper()} 撞名（改参数名）"))
        pdup = {n for n in pnames if pnames.count(n) > 1}
        if pdup:
            rows.append((False, f"工具 {t.name} 参数重名：{sorted(pdup)}"))
    if names and all(r[0] for r in rows):
        rows.append((True, "工具声明齐全"))
    return rows


def _check_run_refs(skill: Skill, tool: SkillTool) -> List[Tuple[bool, str]]:
    """浅检查：run 里引用的相对脚本文件得存在。"""
    tokens = re.split(r"[\s;|&<>()\"']+", tool.run)
    missing = []
    for tok in tokens:
        tok = tok.strip()
        if not tok or tok.startswith("-") or "://" in tok or "/" not in tok:
            continue
        if not tok.endswith((".sh", ".py")):
            continue
        p = Path(tok)
        if not p.is_absolute():
            p = skill.directory / p
        if not p.is_file():
            missing.append(tok)
    if missing:
        return [(False, f"工具 {tool.name} 的 run 引用的脚本不存在：{missing}")]
    return [(True, f"工具 {tool.name} 的 run 与脚本文件对得上")]


# ----------------------------------------------------------------------
# 执行（skill_call）
# ----------------------------------------------------------------------

def _param_env_value(p: SkillParam, v: Any) -> str:
    """参数值 → 环境变量值（简单类型字符串化；json 传 JSON 文本）。"""
    if p.type == "json":
        if isinstance(v, str):
            json.loads(v)   # 先验是合法 JSON 文本（ValueError 由调用方归一）
            return v
        return json.dumps(v, ensure_ascii=False)
    if p.type == "number":
        float(v)   # 校验（ValueError 上抛由调用方归一）
        return str(v)
    if p.type == "bool":
        if isinstance(v, bool):
            return "true" if v else "false"
        s = str(v).strip().lower()
        if s in ("true", "1", "yes"):
            return "true"
        if s in ("false", "0", "no"):
            return "false"
        raise ValueError(f"bool 参数要 true/false，收到 {v!r}")
    return str(v)


async def execute_tool(skill: Skill, tool: SkillTool,
                       params: Dict[str, Any]) -> CallOutcome:
    """预检（参数/密钥）→ 注入 env → bash -c run（cwd = skill 目录）→ 截断出结果。"""
    params = params or {}
    errs: List[str] = []

    # 预检 1：参数（未知/缺必填/类型）
    known = {p.name: p for p in tool.params}
    unknown = [k for k in params if k not in known]
    if unknown:
        errs.append(f"未知参数：{unknown}（{tool.name} 接受：{list(known) or '（无参数）'}）")
    env_file = _env_file(skill)
    used = set(skill.env_keys) | set(env_file)
    param_env: Dict[str, str] = {}
    for name, p in known.items():
        v = params.get(name, p.default)
        if v is None:
            if p.required:
                errs.append(f"缺必填参数：{name}")
            continue
        try:
            val = _param_env_value(p, v)
        except (ValueError, TypeError) as e:
            errs.append(f"参数 {name} 不合法：{e}")
            continue
        key = name.upper()
        if key in used:
            errs.append(f"参数 {name} 与密钥键 {key} 撞名（改 skill.yml 的参数名）")
            continue
        param_env[key] = val

    # 预检 2：密钥缺不缺（只看键名；缺了先补 .env，不放脚本去撞 401）
    missing_env = [k for k in skill.env_keys if k not in env_file]
    if missing_env:
        errs.append(f"缺密钥：{missing_env}（让用户提供，补 {skill.directory / '.env'}）")
    if not tool.run:
        errs.append(f"工具 {tool.name} 的 run 为空")
    if errs:
        return CallOutcome(error="；".join(errs), timeout=tool.timeout)

    # 跑（独立进程组：超时整棵杀；env = 继承 + .env + 参数）
    env = {**os.environ, **env_file, **param_env}
    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", tool.run,
            cwd=str(skill.directory),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=tool.timeout)
        except asyncio.TimeoutError:
            # 同 exec：进程组整棵杀 + 限时收尸，两步都不能省
            kill_process_group(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                logger.warning("[skill] 杀进程组后 5s 仍未收尸（pgid=%s），放弃等待", proc.pid)
            return CallOutcome(error=f"脚本跑到 {tool.timeout}s 被终止：{tool.run}",
                               error_kind="timeout",
                               elapsed=time.monotonic() - t0,
                               timed_out=True, timeout=tool.timeout)
    except OSError as e:
        return CallOutcome(error=f"脚本启动失败：{e}", error_kind="exec_failed",
                           timeout=tool.timeout)

    out = stdout.decode("utf-8", errors="replace").rstrip("\n")
    err = stderr.decode("utf-8", errors="replace").rstrip("\n")
    if len(out) > CALL_STDOUT_MAX:
        out = out[:CALL_STDOUT_MAX] + "\n…（输出过长已截断，脚本请先自行瘦身：jq 过滤、取字段）"
    if len(err) > CALL_STDERR_MAX:
        err = err[:CALL_STDERR_MAX] + "\n…（stderr 过长已截断）"
    return CallOutcome(exit=proc.returncode, elapsed=time.monotonic() - t0,
                       stdout=out, stderr=err, timeout=tool.timeout)


# ----------------------------------------------------------------------
# skill-dev 种子（教 neoagent 自己创建/维护 skill 的默认 skill）
# ----------------------------------------------------------------------

def ensure_skill_dev(skills_dir: Path, seed_dir: Path) -> bool:
    """skill-dev 缺失才从包内种子拷贝；已存在绝不覆盖（用户可能改过）。"""
    target = skills_dir / "skill-dev"
    if target.exists() or not seed_dir.is_dir():
        return False
    skills_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(seed_dir, target)
    logger.info("已植入默认 skill：%s", target)
    return True
