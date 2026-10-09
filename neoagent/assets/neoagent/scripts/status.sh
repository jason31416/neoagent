#!/usr/bin/env bash
# 本机 neoagent 部署状态（只读巡检）：home 布局 / 配置 / 数据 / skills。
# 不写任何东西；home 取 NEOAGENT_HOME，缺省 ~/.neoagent（与服务同一口径）。
set -u

H="${NEOAGENT_HOME:-$HOME/.neoagent}"
row() { printf '%s：%s\n' "$1" "$2"; }

echo "== neoagent 本机状态 =="
row "home" "$H"
if [ ! -d "$H" ]; then
  echo "home 还没建（服务第一次启动会自动生成配置骨架）"
  exit 0
fi

cfg="$H/config.yml"
if [ -f "$cfg" ]; then
  row "config" "config.yml 有（$(wc -c < "$cfg" | tr -d ' ') 字节）"
else
  row "config" "config.yml 没有（服务会生成骨架）"
fi

if [ -f "$H/personality.md" ]; then
  row "personality" "有（$(wc -c < "$H/personality.md" | tr -d ' ') 字节）"
else
  row "personality" "没有（用内置人格）"
fi

chs=""
if [ -d "$H/channels" ]; then
  for f in "$H/channels"/*.yml; do
    [ -f "$f" ] || continue
    n=$(basename "$f" .yml)
    en=$(grep -E '^[[:space:]]*enabled:' "$f" | head -1 | awk '{print $2}')
    chs="$chs $n(enabled=${en:-?})"
  done
fi
if [ -n "$chs" ]; then
  row "channels" "${chs# }"
else
  row "channels" "还没有频道配置"
fi

if [ -d "$H/data/memory" ]; then
  n=$(find "$H/data/memory" -type f | wc -l | tr -d ' ')
  sz=$(du -sh "$H/data/memory" 2>/dev/null | awk '{print $1}')
  row "记忆" "$n 个文件，$sz"
else
  row "记忆" "还没有记忆库（data/memory/）"
fi

[ -d "$H/data" ] && row "data" "$(du -sh "$H/data" 2>/dev/null | awk '{print $1}')"
[ -d "$H/data/workspace" ] && row "workspace" "$(du -sh "$H/data/workspace" 2>/dev/null | awk '{print $1}')"

if [ -d "$H/skills" ]; then
  row "skills" "$(ls "$H/skills" | tr '\n' ' ')"
else
  row "skills" "还没有"
fi

if [ -d "$H/groups" ]; then
  row "groups" "$(find "$H/groups" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ') 个群目录"
fi

if [ -f "$H/logs/latest.log" ]; then
  row "日志" "logs/latest.log（$(wc -l < "$H/logs/latest.log" | tr -d ' ') 行）"
else
  row "日志" "还没有 logs/latest.log"
fi
