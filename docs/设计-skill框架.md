# 设计-skill框架（对外部世界的能力）

让 neoagent 对外部世界有"手"。大多数服务不暴露 MCP，只有 REST/HTTP API；本设计只定**框架**，与具体服务无关。

核心判断：
**工具不随服务涨——固定四个工具；服务是 agent 自己读写维护的 skill；"工具 = 脚本"，靠一份统一契约保证有效性（参数进 env、密钥进 .env、结果出 stdout）。**

对照 Hermes（Nous）的路子：服务→插件/MCP→工具随服务涨，模型侧靠 tool_search 检索收敛。我们连注册侧都不涨；检索式发现对小模型不友好（Hermes 自测检索失败差 26 个点），全量清单 + 参数里选工具对 deepseek-flash 稳得多。

---

## 一、图景

```
~/.neoagent/skills/                  # 用户级，跨项目共用（与 config.yml 同级）
  skill-dev/
    skill.md                         # 默认 skill：教 agent 怎么创建/维护 skill
  mcsm/
    skill.md                         # 语义：这 skill 是干嘛的、工具怎么用、参数含义、坑
    skill.yml                        # 机械：工具声明（唯一执行入口的索引）
    .env                             # 密钥（MCSM_URL=... / MCSM_API_KEY=...），不进 git
    scripts/
      list_instances.sh              # 脚本：curl / python 随便
      restart_instance.sh
```

- **skill 的定义放宽**：目录里有 `skill.md` **或** `skill.yml` 就算一个 skill。不硬分"文档型/工具型"——skill 就是 skill，`skill_list` 靠工具名列表自然区分形态。
- **分工**：语义全在 `skill.md`（frontmatter `name`/`description`）；`skill.yml` 是纯机械声明，只多留**工具级一句话 desc**（清单要用）。
- **skill 目录要对 agent 读写放开**（file_tools 增加这一根目录，读写都放）：能读能写，"自己维护"才成立；`skill_show` 只是看的便利，不是唯一通道。实现时注意路径穿越校验。
- 定时跑 skill 不需要新机制：现有 wake 到点后 neoagent 自己 `skill_call` 就行（note 里写清干什么）。

## 二、固定工具（四个，永不随服务涨）

| 工具 | 干什么 |
|---|---|
| `skill_list()` | 一览：每个 skill 的 name + description + 工具名列表 |
| `skill_show(name)` | 一份 XML：skill.md 全文 + 各工具的结构化描述 + 提示用 `skill_call` |
| `skill_call(id, params)` | 唯一执行入口：跑 skill.yml 声明过的工具 |
| `skill_check(name)` | 校验 skill，agent 自维护的反馈回路 |

人类入口 `/skills`（聊天 / 控制台）：只列 skill 名。

### skill_list

```
mcsm — MCSManager 面板：查实例、开关服、看日志 [list_instances, restart_instance]
skill-dev — 怎么创建和维护 skill；写 skill 前先读这个 []
```

### skill_show

渲染**结构化的工具描述**，不粘 skill.yml 原文；skill.md 原文直接进（CDATA 装，markdown 里的 `<` 不用逃）：

```xml
<skill name="mcsm">
  <description>MCSManager 面板：查实例、开关服、看日志</description>
  <skill_md><![CDATA[
# MCSManager 面板
……skill.md 全文……
]]></skill_md>
  <env requires="MCSM_URL, MCSM_API_KEY"/>
  <tools>
    <tool id="mcsm.list_instances" desc="列出所有实例（服务器）及运行状态" timeout="30">
      <param name="keyword" type="string" required="false"/>
      <run><![CDATA[bash scripts/list_instances.sh]]></run>
    </tool>
    <tool id="mcsm.restart_instance" desc="重启指定实例" timeout="60">
      <param name="instance_id" type="string" required="true"/>
      <run><![CDATA[bash scripts/restart_instance.sh]]></run>
    </tool>
  </tools>
  <hint>以上工具用 skill_call(id, params) 调用；参数以环境变量传入脚本。</hint>
</skill>
```

### skill_call

- id 带命名空间：`mcsm.list_instances`；**只跑声明过的工具**——skill 目录里别的脚本想跑走现有 exec，边界干净；
- 流程：校验参数 → 注入 env（静默加载 `.env`）→ cwd = skill 目录 → 执行 `run` → stdout 截断返回；
- 失败归一：exit≠0 或超时 → 结果里带 exit code + stderr（同样截断）+ 耗时，一段模型看得懂的话，不甩裸异常。

### skill_check

校验清单（反馈给 agent 自己修）：

1. skill.yml 可解析、字段/类型合法；
2. 工具 name 合法、无重复（完整 id = `<skill>.<name>`）；
3. params 无重名、type 在枚举内；参数名大写后不得与 `env:` 声明 / `.env` 键冲突；
4. `env:` 声明的键在 `.env` 里**缺不缺（只查键名，不看值）**→ 缺就列出该补哪些；
5. `run` 非空；`run` 里引用的相对路径脚本文件存在（浅检查）；
6. skill.md 存在与否、frontmatter `name`/`description` 缺失给提示。

## 三、skill.yml schema

```yaml
# ~/.neoagent/skills/mcsm/skill.yml —— 纯机械声明，语义在 skill.md
env: [MCSM_URL, MCSM_API_KEY]        # 需要的密钥键名（不存值）
tools:
  - name: list_instances
    desc: 列出所有实例（服务器）及运行状态   # 工具级一句话，进清单
    params:
      - {name: keyword, type: string, required: false}   # 参数语义写在 skill.md
    run: bash scripts/list_instances.sh
    timeout: 30
  - name: restart_instance
    desc: 重启指定实例
    params:
      - {name: instance_id, type: string, required: true}
    run: bash scripts/restart_instance.sh
```

| 字段 | 说明 |
|---|---|
| `env:` | 顶层。本 skill 需要的环境变量键名清单，值在 `.env`；无顶层 `desc`（与 skill.md 重复） |
| `tools[].name` | 工具名，完整 id = `<skill>.<name>` |
| `tools[].desc` | 工具级一句话描述（唯一保留的 desc） |
| `tools[].params[]` | `name` / `type: string\|number\|bool\|json` / `required` / 可选 `default`；**无 desc**，语义在 skill.md |
| `tools[].run` | shell 文本：内联 curl 一行，或 `bash scripts/x.sh` / `.venv/bin/python tools/x.py` |
| `tools[].timeout` | 秒，默认 60，上限 600（与 exec 一致） |

无 read/write 标签（本期不做审批）。

## 四、脚本契约（工具 = 脚本）

**参数 → 环境变量**

- 变量名 = 参数名大写（`course_id` → `COURSE_ID`）；
- `string`/`number`/`bool` 直接字符串；`json` 传 JSON 文本（脚本里 jq / python 解析）；
- 参数名不得与 `.env` 键同名（`skill_check` 拦）；
- 缺必填参数不跑，报"缺哪个 + 有哪些参数"。

**密钥 → `.env`**

- skill 目录下 `.env`，框架**静默加载**进脚本环境；值不进日志、不进错误信息、模型永远看不到；
- `env:` 只声明键名，所以缺键时报"缺 `MCSM_API_KEY`，让用户补 `.env`"，而不是让 curl 撞个 401。

**出参 → stdout**

- stdout = 结果，**30K 字符截断**并注明截断；脚本职责是自己先瘦身（jq 过滤、取字段），别把几百 KB 原始 JSON 丢给模型；
- exit 0 = 成功；非零 = 失败（stderr + exit code 归一返回）；
- cwd = skill 目录（脚本可相对引用 `./scripts/`、`./.venv/`）；
- 工具结果全局上限（`registry.TOOL_RESULT_MAX`）同步对齐 30K，避免全局截断先把 30K 掐掉一截。

自由度都在脚本里：多步流程（先登录换 token 再调）、分页循环、响应过滤、python 帮手脚本，全是脚本内部的事。**auth 抽象和响应整形不需要框架做**——这是"工具=脚本"最大的红利。

## 五、agent 自维护

### 默认 skill：skill-dev

`~/.neoagent/skills/skill-dev/`（skill.md 指南 + 一个工具，现身说法）：

- **自带工具 `create_template`**（`skill-dev.create_template`）：给 name 和一句话
  description，建出新 skill 骨架（skill.md / skill.yml / scripts/hello.sh / .env 四件套），
  返回文件清单和下一步提示；
- 脚本契约（参数→env、密钥→.env、结果→stdout、30K 截断、超时）；
- skill.yml 字段表 + 一个完整例子（skill.md / skill.yml / 脚本 / .env 四件套）；
- **什么时候该沉淀 skill**：同一服务第 2 次裸调 API、要复用的多步流程、需要管密钥；
- 建 skill 的流程：写文件 → `skill_check` → 挑只读工具 `skill_call` 试跑 → 把学到的坑写回 skill.md。

### 提示词加一段"能力管理"

```
## 能力管理（skill）
你在 ~/.neoagent/skills/ 下有一组自己维护的 skill，是你对外部服务的"手"。接到涉及外部服务的活：
1. 先 skill_list 看有没有现成 skill，有就 skill_show 看细节、skill_call 干活；
2. 头一两次可以直接调 API 探路，但只要你发现自己在重复拼同一个服务的请求，
   就停下来把它沉淀成 skill（照 skill-dev 的写法：skill.md + skill.yml + scripts/）；
3. 服务接口变了、skill 调用坏了，修 skill 本身（skill_check 诊断），不要绕过它继续裸调；
4. skill 是你的长期能力：补全 description、删废工具、把新踩的坑写进 skill.md。
```

## 六、不做清单（本期明确不做）

| 不做 | 为什么 |
|---|---|
| workflow / 流程模板 | 接口本身够用（定案）；要编排时 neoagent 自己串 `skill_call`，或醒来自行跑 |
| read/write 标签、审批 | 后置；契约不为此设障碍，将来想加在 skill.yml 加标签即可 |
| 搜索式发现（tool_search 类） | op 数量级小，全量清单够；小模型检索失败率高 |
| auth provider 抽象、响应整形框架件 | 脚本自己干，框架保持薄 |
| 服务注册/配置向导 | skill 就是文件，agent 写文件 + skill_check 就是注册 |

## 七、设计决策记录

| 决策 | 理由 |
|---|---|
| 工具不随服务涨（固定四个） | 上下文平；反例 Hermes 工具爆炸靠检索兜底 |
| 工具 = 脚本，参数进 env | 自由度（curl/python、多步、登录、分页）+ 有效性（校验后才跑）并得 |
| 密钥进 `.env`，skill.yml 只声明键名 | 模型看不到值；缺键有好报错 |
| skill.yml 无顶层 desc、无参数 desc | 与 skill.md 重复；工具级 desc 留（`skill_list`/`skill_show` 清单用） |
| `skill_show` 渲染工具清单而非粘原文 | 模型直接看到"有哪些工具、什么参数"，并被引向 `skill_call` |
| skill 统一住 `~/.neoagent/skills/` | 用户级、跨项目；file_tools 放开该目录保证 agent 可读写 |
| stdout 30K 截断 | 上下文预算（deepseek-flash，90 轮上限） |
| 兼容只有 skill.md 的 skill | 与常见 skill 生态同构；`skill_show` 一把看全，不逼模型挨个 read |
