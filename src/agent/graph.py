from typing import Literal

from langgraph.constants import START, END
from langgraph.graph import StateGraph

from src.agent.collect import collect_graph
from src.agent.extend import extend_graph
from src.agent.recommend import recommended_graph
from src.agent.common.context import ContextSchema
from src.agent.node.main import get_store_info, identify_question, get_user_preferences, need_collect
from src.agent.state.main import State, NeedCollectOutput

builder = StateGraph(State,context_schema=ContextSchema)
builder.add_node(get_store_info)
builder.add_node(identify_question)
builder.add_node("recommended_graph", recommended_graph)
builder.add_node("collect_graph", collect_graph)
builder.add_node("extend_graph", extend_graph)
builder.add_node(get_user_preferences)
builder.add_node(need_collect)

builder.add_edge(START, "get_store_info")
builder.add_edge("get_store_info", "identify_question")

# 智能路由
def router_message(state: State) -> Literal["recommended_graph", "collect_graph", "extend_graph", "get_user_preferences"]:
    user_intent = state["user_intent"]
    if user_intent == "recommend_blog":
        return "recommended_graph"
    elif user_intent == "collect_blog":
        return "collect_graph"
    elif user_intent == "get_info":
        return "get_user_preferences"
    else:
        return "extend_graph"

builder.add_conditional_edges(
    "identify_question",
    router_message,
["recommended_graph", "collect_graph", "extend_graph", "get_user_preferences"]
)

# 路由1：推荐子图：根据用户中断信息决定后续是否继续收藏
builder.add_edge("recommended_graph","need_collect")
def should_collect(state: NeedCollectOutput):
    collect = state["collect"]
    if collect == "需要":
        return "collect_graph"
    else:
        return END

builder.add_conditional_edges(
    "need_collect",
    should_collect,
    ["collect_graph",END]
)
# 路由2：收藏子图
builder.add_edge("collect_graph", END)

# 路由3：查询我的
builder.add_edge("get_user_preferences", END)

# 路由4：其它
builder.add_edge("extend_graph", END)
graph = builder.compile()
