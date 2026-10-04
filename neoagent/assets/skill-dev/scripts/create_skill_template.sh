#!/usr/bin/env bash
# skill-dev 的 create_template 工具：创建一份新 skill 的骨架。
# 参数（环境变量）：NAME（必填，skill 名）、DESCRIPTION（选填，一句话简介）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"        # ~/.neoagent/skills
NAME="${NAME:-}"
DESC="${DESCRIPTION:-这个 skill 是干嘛的（一句话）}"

if [ -z "$NAME" ]; then
  echo "缺必填参数 NAME（skill 名）" >&2
  exit 1
fi
if ! [[ "$NAME" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
  echo "skill 名不合法：${NAME}（只允许小写字母/数字/-/_，不含点）" >&2
  exit 1
fi
DIR="$ROOT/$NAME"
if [ -e "$DIR" ]; then
  echo "已经存在：${DIR}（换个名字，或直接在上面改）" >&2
  exit 1
fi

mkdir -p "$DIR/scripts"

cat > "$DIR/skill.md" <<EOF
---
name: $NAME
description: $DESC
---

# $NAME

（这个 skill 是干嘛的：一两句话）

## 工具
- hello — 示例工具：回声问候（改成你的第一个真工具）

## 参数与密钥
- 参数以环境变量传进脚本（参数 name → 环境变量 NAME）
- 密钥放 .env（KEY=VALUE 一行一个），并在 skill.yml 的 env: 里声明键名

## 坑与例子
- （接口的坑、限流、字段怪癖，踩到就写这里）
EOF

cat > "$DIR/skill.yml" <<'YML'
env: []            # 需要的密钥键名（值放 .env）
tools:
  - name: hello
    desc: 示例工具：回声问候（改成你的第一个真工具）
    params:
      - {name: name, type: string, required: false}
    run: bash scripts/hello.sh
    timeout: 30
YML

cat > "$DIR/scripts/hello.sh" <<'SH'
#!/usr/bin/env bash
echo "hello, ${NAME:-world}"
SH
chmod +x "$DIR/scripts/hello.sh"

touch "$DIR/.env"

echo "已创建 skill 骨架：$DIR"
find "$DIR" -type f | sed "s|^$ROOT/||"
echo "下一步：skill_check $NAME 验一遍；工具脚本写好后 skill_call $NAME.hello 试跑。"
