from langgraph.constants import START, END
from langgraph.graph import StateGraph
from langgraph.prebuilt import tools_condition

from src.agent.common.context import ContextSchema
from src.agent.node.collect import get_title, get_author, get_email, add_collect_message, call_collects, tool_node
from src.agent.state.collect import CollectState

builder = StateGraph(CollectState,context_schema= ContextSchema)
builder.add_sequence([get_title,get_author,get_email,add_collect_message,call_collects])
builder.add_node("tool_node", tool_node)
builder.add_edge(START, "get_title")
builder.add_conditional_edges(
    "call_collects",
    tools_condition,
    {
        "tools": "tool_node",
        "__end__": END
    }
)
builder.add_edge("tool_node","call_collects")
collect_graph = builder.compile()