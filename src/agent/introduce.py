# -*- coding: utf-8 -*-
"""介绍文章子图。

START → locate_article → fetch_content → compose_intro → END

只保留三个节点，对应三件事：**定位 → 取正文 → 说话**。
"抽篇名"不是独立节点 —— 它是"定位"的第一步，由 locate_article 内部调用
（见 node/introduce.py）。拆成节点只会在图里多一个框，还让"抽完忘了用"
这类装配错误有机可乘。

定位不到时 locate_article 清空 target_post，compose_intro 会给出"没这篇"的答复；
降级时 locate/fetch 直接产出人设化提示，并通过条件边结束，避免无正文生成。

为什么 fetch_content 节点注册成字符串 "fetch_content" 而不是直接用函数名：
common.content.fetch_content 是同名的**查询函数**，两者写在一起容易分不清
哪个是图节点、哪个是取数实现。
"""
from langgraph.constants import START, END
from langgraph.graph import StateGraph

from src.agent.common.context import ContextSchema
from src.agent.node.introduce import (
    compose_intro,
    fetch_content_node,
    locate_article,
)
from src.agent.state.introduce import IntroduceState

builder = StateGraph(IntroduceState, context_schema=ContextSchema)
builder.add_node(locate_article)
builder.add_node("fetch_content", fetch_content_node)
# 生成重试在节点内完成，耗尽后仍能返回模板。
builder.add_node(compose_intro)


def intro_ready(state: IntroduceState):
    """Stop the introduction after a database failure."""
    return "failed" if state.get("introduce_failed") else "ok"


builder.add_edge(START, "locate_article")
builder.add_conditional_edges("locate_article", intro_ready, {"failed": END, "ok": "fetch_content"})
builder.add_conditional_edges("fetch_content", intro_ready, {"failed": END, "ok": "compose_intro"})
builder.add_edge("compose_intro", END)

introduce_graph = builder.compile()
