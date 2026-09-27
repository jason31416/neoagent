"""neoagent 核心包。

结构（刻意解耦，给产品文档里的 M/E/R 留位，本期不实现）：
- core/     消息归一化、通道抽象、LLM 客户端、agent 循环
- channels/ IM 通道适配器（微信 iLink / 飞书 lark-oapi）
- tools/    工具注册表 + read_file / write_file / exec
"""
