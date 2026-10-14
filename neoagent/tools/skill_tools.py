"""skill 工具：skill_list / skill_show / skill_call / skill_check（固定四个，不随服务涨）。

skill 本体是 ~/.neoagent/skills/ 下的文件（契约见 docs/设计-skill框架.md），由 neoagent
自己读写维护（file 工具放开该目录）；这里只做发现/展示/校验/执行的工具信封，
逻辑全在 core/skills.py。skill_call 是唯一执行入口——skill 目录里没声明的脚本
想跑就走 exec，边界干净。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..core import skills as core
from .format import ToolOutput, error, root
from .registry import ToolRegistry


def _cdata(text: str) -> str:
    """CDATA 装原文（markdown/脚本里的 < 不用逃）；内容自带 ]]> 就拆段。"""
    return "<![CDATA[" + (text or "").replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _q(v: Any) -> str:
    """属性值引号安全（与 format._attrs_str 同规）。"""
    return str(v).replace('"', "'")


def _fmt_elapsed(elapsed: float) -> str:
    return f"{elapsed * 1000:.0f}ms" if elapsed < 0.1 else f"{elapsed:.1f}s"


# ----------------------------------------------------------------------
# 渲染
# ----------------------------------------------------------------------

def render_list(items) -> str:
    if not items:
        return root("skills", {"count": 0},
                    "（还没有 skill；skill-dev 教怎么创建，先 skill_show skill-dev）")
    body = "\n".join(
        f'<skill name="{_q(s.name)}" desc="{_q(s.description or "（无描述）")}" '
        f'tools="{_q(", ".join(t.name for t in s.tools) or "（无）")}"/>'
        for s in items)
    return root("skills", {"count": len(items)}, body)


def render_show(skill: core.Skill) -> str:
    parts = [f'<description>{_q(skill.description or "（无描述）")}</description>']
    if skill.md_path is not None:
        parts.append(f'<skill_md>{_cdata(skill.md_path.read_text(encoding="utf-8"))}</skill_md>')
    else:
        parts.append("<skill_md>（缺 skill.md：语义说明没了，跑 skill_check 看详情）</skill_md>")
    if skill.env_keys:
        parts.append(f'<env requires="{_q(", ".join(skill.env_keys))}"/>')
    if skill.tools:
        tool_blocks = []
        for t in skill.tools:
            params = "\n".join(
                f'<param name="{_q(p.name)}" type="{_q(p.type)}" '
                f'required="{str(p.required).lower()}"'
                + (f' default="{_q(p.default)}"' if p.default is not None else "")
                + "/>"
                for p in t.params)
            head = (f'<tool id="{_q(skill.name)}.{_q(t.name)}" '
                    f'desc="{_q(t.desc or "（无描述）")}" timeout="{t.timeout}">')
            tool_blocks.append(
                f'{head}\n{params}\n<run>{_cdata(t.run)}</run>\n</tool>' if params
                else f'{head}\n<run>{_cdata(t.run)}</run>\n</tool>')
        parts.append("<tools>\n" + "\n".join(tool_blocks) + "\n</tools>")
    else:
        parts.append("<tools>（无工具声明：照 skill.md 的说明用通用工具干活）</tools>")
    if skill.problems:
        parts.append("<problems>" + _cdata("\n".join(skill.problems)) +
                     "</problems>")
    parts.append("<hint>以上工具用 skill_call(id, params) 调用；参数以环境变量传入脚本，"
                 "密钥放本 skill 目录的 .env（skill.yml 的 env: 声明键名）。</hint>")
    return root("skill", {"name": skill.name}, "\n".join(parts))


def render_check(skill: core.Skill, rows) -> str:
    ok = all(r[0] for r in rows)
    body = "\n".join(("✔ " if r[0] else "✘ ") + r[1] for r in rows)
    return ToolOutput(text=root("skill_check", {"name": skill.name,
                                                "ok": str(ok).lower()}, body),
                      stat="全过" if ok else f"{sum(1 for r in rows if not r[0])} 条待修")


# ----------------------------------------------------------------------
# 工具实现
# ----------------------------------------------------------------------

class SkillKit:
    """一套 skill 工具；skills_dir 是 skill 根目录（~/.neoagent/skills/）。

    allow：skill 白名单（None = 不设限；项 = "skill名" 或 "skill名.工具名"）。
    群 agent 用它做能力收口：白名单外的连名字都看不到（防探路）。"""

    def __init__(self, skills_dir: Path,
                 allow: Optional[Sequence[str]] = None) -> None:
        self.skills_dir = skills_dir
        self.allow = None if allow is None else [str(a).strip() for a in allow]

    def _skill_ok(self, name: str) -> bool:
        return self.allow is None or any(
            a == name or a.startswith(name + ".") for a in self.allow)

    def _tool_ok(self, name: str, tool: str) -> bool:
        return self.allow is None or any(
            a == name or a == f"{name}.{tool}" for a in self.allow)

    def _visible(self, skill: core.Skill) -> core.Skill:
        """白名单过滤后的视图（工具表也收口）；discover/find 每次新建对象，直接改安全。"""
        skill.tools = [t for t in skill.tools if self._tool_ok(skill.name, t.name)]
        return skill

    def _names(self) -> List[str]:
        """这个 kit 眼里可用的 skill 名（not_found 提示用，不泄白名单外的名字）。"""
        return [n for n in core.list_names(self.skills_dir) if self._skill_ok(n)]

    # -- skill_list -----------------------------------------------------
    async def skill_list(self, args: Dict[str, Any]) -> ToolOutput:
        items = [self._visible(s) for s in core.discover(self.skills_dir)
                 if self._skill_ok(s.name)]
        return ToolOutput(text=render_list(items), stat=f"{len(items)} 个 skill")

    # -- skill_show -----------------------------------------------------
    async def skill_show(self, args: Dict[str, Any]) -> ToolOutput:
        name = str(args.get("name", "")).strip()
        skill = core.find_skill(self.skills_dir, name) if name else None
        if skill is None or not self._skill_ok(skill.name):
            return error("not_found", f"没有这个 skill：{name or '（缺 name 参数）'}，"
                                      f"可用：{', '.join(self._names()) or '（无）'}")
        skill = self._visible(skill)
        return ToolOutput(text=render_show(skill),
                          stat=f"{len(skill.tools)} 个工具")

    # -- skill_call -----------------------------------------------------
    async def skill_call(self, args: Dict[str, Any]) -> ToolOutput:
        sid = str(args.get("id", "")).strip()
        if "." not in sid:
            return error("bad_param", 'id 形如 "skill名.工具名"（skill_show 里有清单），'
                                      f"收到：{sid or '（缺 id 参数）'}")
        sname, tname = sid.rsplit(".", 1)
        skill = core.find_skill(self.skills_dir, sname)
        if skill is None or not self._skill_ok(skill.name):
            return error("not_found", f"没有这个 skill：{sname}，"
                                      f"可用：{', '.join(self._names()) or '（无）'}")
        tool = skill.tool(tname)
        if tool is None or not self._tool_ok(sname, tname):
            return error("not_found", f"{sname} 没有工具 {tname}，"
                                      f"可用：{', '.join(t.name for t in skill.tools if self._tool_ok(sname, t.name)) or '（无）'}")
        params = args.get("params") or {}
        if not isinstance(params, dict):
            return error("bad_param", "params 必须是对象（键 = skill_show 里的 param 名）")
        outcome = await core.execute_tool(skill, tool, params)
        if outcome.error:
            return error(outcome.error_kind, outcome.error)
        t = _fmt_elapsed(outcome.elapsed)
        body = ""
        if outcome.stdout:
            body += root("stdout", {}, outcome.stdout)
        if outcome.stderr:
            body += ("\n" if body else "") + root("stderr", {}, outcome.stderr)
        return ToolOutput(
            text=root("skill_call", {"id": sid, "exit": outcome.exit,
                                     "elapsed": t}, body),
            stat=f"exit {outcome.exit} {t}",
            fields=[("time", t), ("exit", str(outcome.exit))])

    # -- skill_check ----------------------------------------------------
    async def skill_check(self, args: Dict[str, Any]) -> ToolOutput:
        name = str(args.get("name", "")).strip()
        skill = core.find_skill(self.skills_dir, name) if name else None
        if skill is None:
            return error("not_found", f"没有这个 skill：{name or '（缺 name 参数）'}，"
                                      f"可用：{', '.join(self._names()) or '（无）'}")
        return render_check(skill, core.check_skill(skill))


# ----------------------------------------------------------------------
# 注册
# ----------------------------------------------------------------------

def register_skill_tools(registry: ToolRegistry, skills_dir: Path, *,
                         allow: Optional[Sequence[str]] = None,
                         with_check: bool = True) -> None:
    """注册 skill 工具。allow 给 SkillKit 做白名单（群 agent 的能力收口）；
    with_check=False 不注册 skill_check（自修入口，只给主 agent）。"""
    kit = SkillKit(skills_dir, allow=allow)
    registry.register(
        "skill_list",
        "列出全部 skill——neoagent 对外部服务的能力包（住 ~/.neoagent/skills/，"
        "每个 skill 是一组声明式工具 + 说明文档）。每项给 name、简介、工具名。"
        "接到外部服务的活先看这里：有现成 skill 就 skill_show 看细节、skill_call 干活；"
        "没有再考虑自己调 API，并记得把重复用的调用沉淀成 skill。",
        {"type": "object", "properties": {}}, kit.skill_list)
    registry.register(
        "skill_show",
        "看一个 skill 的完整内容：skill.md 全文 + 各工具的结构化说明（id/参数/超时/脚本）"
        "+ 需要的密钥键名。调工具前先看这里，参数名照 <param> 传给 skill_call。",
        {"type": "object",
         "properties": {"name": {"type": "string", "description": "skill 名（skill_list 里有）"}},
         "required": ["name"]}, kit.skill_show)
    registry.register(
        "skill_call",
        '执行 skill 声明的工具（唯一执行入口）。id 形如 "mcsm.list_instances"'
        "（skill_show 里有清单）；params 的键按 <param> 名传，参数以环境变量注入脚本，"
        "结果在 stdout。缺必填参数/缺密钥会拒并说明怎么补；脚本失败给 exit code 和 stderr。",
        {"type": "object",
         "properties": {"id": {"type": "string",
                               "description": '工具 id："skill名.工具名"'},
                        "params": {"type": "object",
                                   "description": "参数（键 = param 名）；无参数的工具传空对象或不传",
                                   "additionalProperties": True}},
         "required": ["id"]}, kit.skill_call)
    if with_check:
        registry.register(
            "skill_check",
            "校验一个 skill（声明结构、参数表、密钥键名、脚本引用）。改完 skill.yml / 脚本后"
            "跑一遍，✘ 项按提示修——skill 是 neoagent 自己维护的长期能力。",
            {"type": "object",
             "properties": {"name": {"type": "string", "description": "skill 名（skill_list 里有）"}},
             "required": ["name"]}, kit.skill_check)
