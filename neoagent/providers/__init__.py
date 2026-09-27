"""providers 包：一切 provider-specific API 适配都收敛在这里（硬性原则）。

核心（agent_loop / channels / tools）只依赖 Protocol 接口；
换供应商 = 新增实现 + 改 config 的 provider 名，零改动核心。
"""
