---
name: skill-dev
description: 怎么创建和维护 skill（neoagent 对外部服务的能力包）；写 skill 前先读这个
---

# skill 开发指南

skill 是 neoagent 对外部服务的"手"：一个目录 = 一份能力包，住在
~/.neoagent/skills/<name>/。大部分服务没有 MCP，只有 REST/HTTP API——
把调用封装成 skill，neoagent 就有了长期的手，不用每次裸调 API。

## 目录四件套

- skill.md   语义：这个 skill 是干嘛的、工具有哪些、参数什么含义、坑（frontmatter 的
  name/description 是 skill_list 的简介）
- skill.yml  机械声明：env 键名 + 工具表（name/desc/params/run/timeout）
- .env       密钥（KEY=VALUE 一行一个），不进 git、不进任何输出
- scripts/   脚本本体：bash 直接 curl，或 .venv/bin/python xxx.py，随你

## 脚本契约（工具 = 脚本）

- 参数 → 环境变量：skill_call 的参数名大写后注入（course_id → COURSE_ID）。
  string/number/bool 直接字符串，json 传 JSON 文本（脚本里用 jq / python 解析）；
  参数名不要和 .env 键撞名
- 密钥 → .env：框架静默加载进脚本环境，值不会出现在结果和日志里；
  skill.yml 的 env: 只声明键名，缺了会直接报"补 .env"，不会放脚本去撞 401
- 结果 → stdout（30K 截断）：先 jq 瘦身再输出，别把原始大 JSON 丢出来；
  exit 0 成功，非零退出的 stderr 会原样带回
- 工作目录 = skill 目录（脚本可以 ./scripts/ 相对引用）；默认超时 60s，
  可在 skill.yml 里改（上限 600s）

## skill.yml 字段

```yaml
env: [MCSM_URL, MCSM_API_KEY]        # 需要的密钥键名（值放 .env）
tools:
  - name: list_instances
    desc: 列出所有实例（服务器）及运行状态   # 工具级一句话，进 skill_list 清单
    params:
      - {name: keyword, type: string, required: false}   # 参数语义写在本文件正文
    run: bash scripts/list_instances.sh
    timeout: 30
```

- 顶层只有 env / tools，不写 desc（描述在 skill.md，别重复）
- params 的 type：string / number / bool / json；required 和 default 二选一按需
- run 就是一段 shell：简单的内联 curl 一行也行，复杂的 run: bash scripts/xxx.sh

## 什么时候沉淀 skill

- 同一个服务第 2 次开始裸调 API → 停下来，封装成 skill
- 有一串要复用的多步调用（登录换 token 再调、翻页聚合、过滤整形）→ 写进脚本
- 需要管密钥 → 必须进 .env，不要贴得到处都是

## 创建流程

1. skill_call skill-dev.create_template 建骨架（给 name 和一句话 description）
2. 写 skill.md（语义 + 坑）和 skill.yml（工具声明），脚本放 scripts/
3. skill_check 验一遍，✘ 项修掉
4. 挑只读工具 skill_call 试跑，通了再干活
5. 服务接口变了、调用坏了，修 skill 本身（skill_check 诊断），别绕过它继续裸调

## 完整例子（mcsm 面板）

skill.yml：

```yaml
env: [MCSM_URL, MCSM_API_KEY]
tools:
  - name: list_instances
    desc: 列出所有实例（服务器）及运行状态
    params:
      - {name: keyword, type: string, required: false}
    run: bash scripts/list_instances.sh
    timeout: 30
```

scripts/list_instances.sh：

```bash
#!/usr/bin/env bash
# env: MCSM_URL MCSM_API_KEY KEYWORD
curl -sS "$MCSM_URL/api/instance/list" -H "Authorization: Bearer $MCSM_API_KEY" \
  | jq --arg k "${KEYWORD:-}" '.instances[] | select(.name | contains($k)) | {name, status, uptime}'
```

.env：

```
MCSM_URL=http://127.0.0.1:23333
MCSM_API_KEY=xxxx
```
