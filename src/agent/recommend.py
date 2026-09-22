from langgraph.constants import START, END
from langgraph.graph import StateGraph

from src.agent.common.context import ContextSchema
from src.agent.node.recommend import collect_user_info, list_tables, call_get_schema, get_schema_node, generate_query, \
    check_query, run_query_node
from src.agent.state.recommend import RecommendState

builder = StateGraph(RecommendState,context_schema=ContextSchema)
builder.add_node(collect_user_info)
builder.add_node(list_tables)
builder.add_node(call_get_schema)
builder.add_node("get_schema",get_schema_node)
builder.add_node(generate_query)
builder.add_node(check_query)
builder.add_node("run_query",run_query_node)

builder.add_edge(START, "collect_user_info")
builder.add_edge("collect_user_info", "list_tables")
builder.add_edge("list_tables", "call_get_schema")
builder.add_edge("call_get_schema", "get_schema")
builder.add_edge("get_schema", "generate_query")

def should_continue(state: RecommendState):
    last_msg = state["messages"][-1]
    if not last_msg.tool_calls:
        return END
    return "check_query"

builder.add_conditional_edges(
    "generate_query",
    should_continue,
    [END,"check_query"]
)
builder.add_edge("check_query", "run_query")
builder.add_edge("run_query", "generate_query")
# 推荐子图
recommended_graph = builder.compile()
