from langgraph.constants import START
from langgraph.graph import StateGraph, MessagesState

from src.agent.node.extend import extend_node

extend_graph = (
    StateGraph(MessagesState)
    .add_node(extend_node)
    .add_edge(START, "extend_node")
    .compile()
)