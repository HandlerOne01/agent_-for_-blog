from typing import Literal

from langgraph.constants import START, END
from langgraph.graph import StateGraph

from src.agent.extend import extend_graph
from src.agent.introduce import introduce_graph
from src.agent.recommend import recommended_graph
from src.agent.common.context import ContextSchema
from src.agent.node.main import (
    get_store_info,
    identify_question,
    get_user_preferences,
    need_introduce,
    NeedIntroduceOutput,
)
from src.agent.state.main import State

builder = StateGraph(State,context_schema=ContextSchema)
builder.add_node(get_store_info)
builder.add_node(identify_question)
builder.add_node("recommended_graph", recommended_graph)
builder.add_node("introduce_graph", introduce_graph)
builder.add_node("extend_graph", extend_graph)
builder.add_node(get_user_preferences)
builder.add_node(need_introduce)

builder.add_edge(START, "get_store_info")
builder.add_edge("get_store_info", "identify_question")

# 智能路由
def router_message(state: State) -> Literal[
    "recommended_graph", "extend_graph", "introduce_graph", "get_user_preferences"
]:
    user_intent = state["user_intent"]
    if user_intent == "recommend_blog":
        return "recommended_graph"
    elif user_intent == "get_info":
        return "get_user_preferences"
    elif user_intent == "introduce_blog":
        return "introduce_graph"
    else:
        return "extend_graph"

builder.add_conditional_edges(
    "identify_question",
    router_message,
["recommended_graph", "extend_graph", "introduce_graph", "get_user_preferences"]
)

# 路由1：推荐跑完 → 问一句要不要挑一篇讲讲；降级或没候选就直接收场
def after_recommend(state: State) -> Literal["need_introduce", "__end__"]:
    """推荐子图跑完后的去向。

    取数降级（数据库连不上）时子图一条文章都没给出，这时候问
    「要不要我讲讲其中一篇」就是自说自话；一条候选都没有同理。
    """
    if state.get("recommend_failed"):
        return END
    if not (state.get("recommended_posts") or []):
        return END
    return "need_introduce"


builder.add_conditional_edges(
    "recommended_graph",
    after_recommend,
    ["need_introduce", END],
)


def should_introduce(state: State):
    """挑中序号 → 进介绍子图；"不用"或认不出来 → 收场。

    序号与篇名的对应在 need_introduce 节点里就解析好了（条件边不能改状态），
    这里只看 target_query 有没有被填上（它与介绍子图的同名字段贯通）。

    参数类型必须写父图 State，**不能写 NeedIntroduceOutput** ——
    LangGraph 按函数签名决定给条件边传哪些键，而 NeedIntroduceOutput 是私有状态、
    只声明了 introduce_choice；用它就看不到预填的篇名，于是永远返回 END。
    症状：用户答了序号、状态里篇名也写对了，但介绍子图从不执行。
    """
    if state.get("target_query"):
        return "introduce_graph"
    return END


builder.add_conditional_edges(
    "need_introduce",
    should_introduce,
    ["introduce_graph", END]
)

# 路由3：查询我的
builder.add_edge("get_user_preferences", END)

# 路由4：其它
builder.add_edge("extend_graph", END)

# 路由5：介绍文章（问答型，直接收场）
builder.add_edge("introduce_graph", END)
graph = builder.compile()
