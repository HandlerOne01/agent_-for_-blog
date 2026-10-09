"""New LangGraph Agent.

This module defines a custom graph.
"""

# 用相对导入，**不要**写成 `from agent.graph import graph`。
#
# 为什么：`agent` 是一个顶层包名，它由 editable 安装决定指向哪份 src/agent。
# 当本仓库借另一个仓库（agent_-for_-blog）的虚拟环境运行时，`agent` 会被映射到
# 那个仓库，于是本文件会把**别人的 graph** 拉进来 —— 表现为本仓库改了
# graph.py 却仍读到旧代码，或者莫名 ImportError。相对导入永远指向自己。
from .graph import graph

__all__ = ["graph"]
