# neoagent · 最简样例

技术栈可行性 demo：**接入微信 / 飞书，能收消息、调工具、回消息**。
产品文档中的 M/E/R、记忆、cron/proactive 等均不在本样例范围内（见 `最简样例-实施计划.md`）。

## 它能做什么

- 在**微信**里和它说话（腾讯官方 iLink Bot API，扫码登录，无封号风险）
- 在**飞书**里和它说话（官方 lark-oapi SDK，WebSocket 长连接）
- 它能调用工具干活：`read_file`（读文件）/ `write_file`（写文件）/ `exec`（跑命令）
- 干完把结果用聊天消息回给你（OpenAI 兼容 tool calling 循环，当前接 DeepSeek）

## 快速开始

```bash
uv sync                        # 装依赖（飞书另需 uv sync --extra feishu）
cp config.example.toml config.toml   # 填 LLM api_key 等

# 1. 微信扫码登录（凭据落在 data/weixin/，之后不用再扫）
uv run python -m neoagent.channels.weixin.login

# 2. 启动服务（按 config.toml 里 channels.*.enabled 起通道）
uv run python -m neoagent.run

# 也可以先在终端里试（不接 IM）
uv run python -m neoagent.cli
```

## 架构

```
微信用户 ──iLink Bot API(官方)──► weixin-adapter ─┐
飞书用户 ──lark-oapi SDK(WS)───► feishu-adapter ─┼─► MessageEvent 归一化
CLI      ───────────────────────────────────────┘        │
                                                          ▼
                                                   agent 循环（DeepSeek / OpenAI 兼容）
                                                          │
                                                   工具：read_file / write_file / exec
                                                          ▼
                                                   回复按原通道返回
```

- **通道与核心解耦**：适配器只做"消息 ↔ MessageEvent"，agent 核心不知道自己挂在哪个 IM 上（同一核心、多通道）。将来产品文档里的 M（唯一的声音）就落在核心层。
- **工具是注册制**：JSON Schema + handler，加新工具不需要动 agent 循环。
- **会话历史是内存态**：按 `通道:会话` 维度维护，进程重启即清空（本期不做持久化）。

## 安全边界（demo 也必须有）

IM 里的 LLM 等于拿着一台 shell，三道防线都在 `config.toml` 里可调：

| 工具 | 防线 |
|---|---|
| `read_file` / `write_file` | `files.allowed_roots` 路径白名单（防越界，实测会拦 + 模型自纠） |
| `exec` | cwd 白名单 + 危险命令正则拒绝 + 超时强杀（60s）+ 输出截断（20KB） |

⚠️ `exec.deny_patterns` 只是兜底，真正的边界是把 `allowed_roots` 限制在你的工作目录里。

## 目录结构

```
neoagent/
├── core/          MessageEvent / ChannelAdapter / LLM 客户端 / agent 循环
├── channels/
│   ├── weixin/    iLink Bot API 协议层 + 适配器 + 扫码登录
│   └── feishu/    lark-oapi 适配器
├── tools/         工具注册表 + read_file / write_file / exec
├── app.py         组装（配置 → 工具 → agent）
├── run.py         服务入口
└── cli.py         终端入口（M0 验证载体）
```

## 参考实现

微信 iLink 协议移植自 [nousresearch/hermes-agent](https://github.com/nousresearch/hermes-agent)（MIT）
的 `gateway/platforms/weixin.py`；通道抽象、工具注册制参考其 `BasePlatformAdapter` / `tools/registry.py`。
