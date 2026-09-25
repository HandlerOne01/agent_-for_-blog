from langgraph.constants import START, END
from langgraph.graph import StateGraph

from src.agent.common.context import ContextSchema
from src.agent.node.recommend import (
    collect_user_info,
    compose_answer,
    list_tables,
    call_get_schema,
    get_schema_node,
    generate_query,
    check_query,
    run_query_node,
)
from src.agent.state.recommend import RecommendState

builder = StateGraph(RecommendState,context_schema=ContextSchema)
builder.add_node(collect_user_info)
builder.add_node(list_tables)
builder.add_node(call_get_schema)
builder.add_node("get_schema",get_schema_node)
builder.add_node(generate_query)
builder.add_node(check_query)
builder.add_node("run_query",run_query_node)
# 答复节点：SQL 只取数，取完由它把人设 + 查询结果说成爱弥斯的话
builder.add_node(compose_answer)

builder.add_edge(START, "collect_user_info")


# 取数是否已经降级（数据库连不上）。
# 降级了就直奔 END —— 不要让图带着空结果继续走到"答复"，
# 更不要让 need_collect 在一条都没推荐出来的时候问"要不要收藏"。
def db_ready(state: RecommendState):
    return "failed" if state.get("recommend_failed") else "ok"


builder.add_conditional_edges(
    "collect_user_info", db_ready, {"ok": "list_tables", "failed": END}
)
builder.add_conditional_edges(
    "list_tables", db_ready, {"ok": "call_get_schema", "failed": END}
)
builder.add_edge("call_get_schema", "get_schema")
builder.add_edge("get_schema", "generate_query")

def should_continue(state: RecommendState):
    # 查询跑挂了 → 收场，别再让 LLM 接着编
    if state.get("recommend_failed"):
        return END
    last_msg = state["messages"][-1]
    if not last_msg.tool_calls:
        # 取数结束 —— 交给 compose_answer 说人话（原来这里直接 END，
        # 于是用户看到的是 SQL 代理提示词写出来的客服腔）
        return "compose_answer"
    return "check_query"

builder.add_conditional_edges(
    "generate_query",
    should_continue,
    [END,"check_query","compose_answer"]
)
builder.add_edge("check_query", "run_query")
builder.add_conditional_edges(
    "run_query", db_ready, {"ok": "generate_query", "failed": END}
)
builder.add_edge("compose_answer", END)
# 推荐子图
recommended_graph = builder.compile()
