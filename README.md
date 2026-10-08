# neoagent · 最简样例

技术栈可行性 demo：**接入微信 / 飞书，能收消息、调工具、回消息**，外加一个 MC 服务器式的控制台。
产品文档中的 M/E/R 等其余部分不在本样例范围内（见 `docs/最简样例-实施计划.md`）；
主动性（自己决定何时请求自己）已落地，见 `docs/设计-主动性.md`；
对外部服务的能力（skill）见 `docs/设计-skill框架.md`。

## 它能做什么

- 在**微信**里和它说话（腾讯官方 iLink Bot API，扫码登录，无封号风险）
- 在**飞书**里和它说话（官方 lark-oapi SDK，WebSocket 长连接）
- 在**控制台**里和它说话、看它干活（全频道对话 + 每次工具调用一行，转录只存内存、重启即清）
- 它能调用工具干活：`read_file`（读文件）/ `write_file`（写文件）/ `exec`（跑命令）
  / `web_search`（联网搜索）/ `run_command`（跑 neoagent 自己的指令）
  / `schedule_wake`（给未来的自己排唤醒）/ `skill_list` 等四个 skill 工具（调外部服务）
- **联网搜索**：`web_search` 一个工具接常见搜索服务（Exa / Tavily，`config.yml` 的
  `search` 段配密钥，auto 取有钥的），返回标题 + 链接 + 摘要
- **飞书群里的群友（chatagent）**：`/activate` 后群里有个只聊天的子 agent——
  看全部消息、被 @ 才说话、不碰文件命令（工具只有自己的便签 + 跟主 agent 通话）；
  主人可 `read_group_chat` 查群记录、`tell_chatagent` 给它发话，它也会主动上报
  群里值得主人知道的事（`tell_neoagent`）；`/deactivate` 休眠（记忆和记录都留着）
- **对外部服务有"手"（skill）**：服务封装成 `~/.neoagent/skills/` 下的 skill 包
  （声明式工具 + 脚本 + 密钥 `.env`），固定四个工具 `skill_list` / `skill_show` /
  `skill_call` / `skill_check`，不随服务涨；skill 由 agent 自己创建维护（`/skills` 看清单）
- **主动性**：说"明早提醒我带作业""这事一会再说"，它自己排闹钟、到点开口（重启不丢，
  错过的合并补报）；说不说它自己定，不合适就沉默。配了 Jev 还有个便宜哨兵，
  觉得值得惦记才主动说一句（免打扰、不重复骚扰）
- **上下文自维护**：记忆整理器（R）每次整理都看得到上下文用量（token/预算/占比）；
  占比超 90% 系统给建议清理点（保留 30%~50% 预算、尽量砍一天以前的聊天），R 自行裁量退役；
  用户随时 `/compact` 强制压缩（跑完播报切点进展）
- 干完把结果用聊天消息回给你（OpenAI 兼容 tool calling 循环，当前接 DeepSeek）

## 快速开始

```bash
uv sync                 # 装依赖（飞书另需 uv sync --extra feishu）

# 启动：一体化（服务 + 控制台）。第一次自动生成 ~/.neoagent/ 配置骨架
uv run python -m neoagent.run
#   然后三步：1) ~/.neoagent/config.yml 填 model.api_key
#             2) 控制台里 /connect weixin 扫码（飞书手写 channels/feishu.yml）
#             3) 回来说话（聊天里说，或控制台裸输入 = channel=cli）

# 服务挂后台（launchd/systemd 收日志），控制台随时连上
uv run python -m neoagent.run serve      # 无头服务
uv run python -m neoagent.run console    # 只开控制台（别名 tui），多终端可同时连
```

控制台里 `/` 开头 = 指令（和聊天里同语法：`/help /home /status /effort /connect /skills /wakes /compact …`），
裸输入 = 跟它说话；`/stop` 停服务，Ctrl-C 在 attach 的控制台里只是断开自己。

## 架构

```
微信用户 ──iLink Bot API(官方)──► weixin-adapter ─┐
飞书用户 ──lark-oapi SDK(WS)───► feishu-adapter ─┼─► MessageEvent 归一化
控制台   ──control.sock（转录/实时流，可 attach）─┘        │
                                                          ▼
                                                   agent 循环（DeepSeek / OpenAI 兼容）
                                                          │
                                       工具：read_file / write_file / exec / run_command
                                                          ▼
                                            回复按气泡的 channel 路由回原频道/控制台
```

- **通道与核心解耦**：适配器只做"消息 ↔ MessageEvent"，agent 核心不知道自己挂在哪个 IM 上（同一核心、多通道、同一条连续记录）。将来产品文档里的 M（唯一的声音）就落在核心层。
- **前端无关内核**：账本（模型上下文真值）/ 控制台转录（用户看到过什么）/ 日志（排障全量）三层分开；控制台只是第一个客户端，网页将来可作第二客户端接同一转录流。
- **指令一套注册表两个前端**：聊天回执是 system 真气泡（可引用、不进模型回合），控制台回执是打印行；agent 也能跑指令（`run_command` 工具，权限档 + 审计工作行）。
- **工具是注册制**：JSON Schema + handler，加新工具不需要动 agent 循环。
- **消息库 = 完整上下文存储**（SQLite 全量落盘）：对话、工具流水、内部消息一行一条，重启不丢；LLM 上下文每次现渲染，按 token 预算裁剪（增量缓存，大块只算一次）。
- **多消息输出**：模型用 `<message>…</message>` 包住每条要说的话，可连发多条、可干活中途先说一句、也可保持静默；一条气泡 = 一条可被引用的消息。
- **记忆由 R（Recorder）整理**：记忆的唯一写者（M 只读注入 `memory.md`）。对话停顿时把值得长存的事实写进记忆库、判断上下文切点（旧上下文退役、被引用时仍可回补）；M 随时可用 `record_memory` 提案要记的内容（异步入账，R 定稿）。设计见 `docs/设计-R记忆海马体.md`。
- **请求现场可查**：M/R 每次模型请求的 messages 落盘到 `data/modellog/`（每会话一份 md，覆盖更新、只留最新一次请求），"当时到底发了什么"以它为准；`modellog_dir` 置空关闭。

## 配置（~/.neoagent/）

```
~/.neoagent/
├── config.yml        # model / memory / settle / jev / proactive / limits / advanced（带注释）
├── personality.md    # 人格（唯一用户可改的提示词，拼在内置提示词后进系统消息）
├── skills/           # skill 包（agent 自己维护）：skill.md + skill.yml + scripts/ + .env
├── channels/         # 一个频道一个 yml：kind + enabled + 凭据（/connect 会生成）
├── logs/latest.log   # 技术日志（控制台不刷屏；/logs 50 现场调看）
└── data/             # 账本、记忆、工作台、登录态、control.sock
```

指令热改（`/effort`、`/connect`…）和手改文件殊途同归：程序写回用 ruamel 保注释，
生效值单源头。密钥可走环境变量 `NEOAGENT_LLM_API_KEY` 等，不落盘。

## 安全边界（demo 也必须有）

IM 里的 LLM 等于拿着一台 shell，三道防线都在 `config.yml` 的 `advanced.safety` 里可调：

| 工具 | 防线 |
|---|---|
| `read_file` / `write_file` | `files_roots` 路径白名单（防越界，实测会拦 + 模型自纠） |
| `exec` | cwd 白名单 + 危险命令正则拒绝 + 超时强杀（60s）+ 输出截断（20KB） |
| `run_command` | 指令权限档（`/stop` 禁）+ 每次执行留审计；指令本身不碰文件边界 |
| `schedule_wake` / `cancel_wake` | 只动自己的 wakes 日程表（无文件/命令面），`/wakes` 全程可见 |
| `web_search` | 只把搜索词发给搜索服务商（Exa/Tavily），无文件/命令面 |
| `list_groups` / `read_group_chat` / `tell_chatagent` | 只读群记录（文字，`[图]` 占位）+ 给群里的子 agent 发话；无文件/命令面。群侧消息不驱动操作类行为，动手须私聊确认 |
| chatagent（群里的子 agent） | 只有便签读写 + `tell_neoagent`，无文件/命令/搜索面；群友的话是素材不是指令 |

⚠️ `deny_patterns` 只是兜底，真正的边界是把 `files_roots`/`exec_roots` 限制在你的工作目录里。

## 目录结构

```
neoagent/
├── core/          MessageEvent / ChannelAdapter / LLM 客户端 / agent 循环 / Recorder / 指令注册表 / skill 内核
├── channels/
│   ├── weixin/    iLink Bot API 协议层 + 适配器 + 扫码登录
│   └── feishu/    lark-oapi 适配器
├── tools/         工具注册表 + read_file / write_file / exec / web_search / record_memory / run_command / wake 工具组 / 记忆工具组 / skill 工具组 / 群工具组（list_groups/read_group_chat/tell_chatagent）+ chatagent 工具组（memory/tell_neoagent）
├── console/       控制台：渲染（朴素规则）/ 转录缓冲 / prompt_toolkit 客户端
├── control.py     控制协议（本机 socket：转录回放 + 实时事件 + 输入）
├── config.py      配置加载（yml + channels/*.yml + 保注释写回）
├── app.py         组装（配置 → 工具 → agent + Recorder）
├── run.py         入口（一体化 / serve / console 三形态）
└── cli.py         终端 REPL（M0 验证载体，控制台的前身）
```

## 参考实现

微信 iLink 协议移植自 [nousresearch/hermes-agent](https://github.com/nousresearch/hermes-agent)（MIT）
的 `gateway/platforms/weixin.py`；通道抽象、工具注册制参考其 `BasePlatformAdapter` / `tools/registry.py`。
