from langchain_core.messages import SystemMessage
from langgraph.graph import MessagesState

from src.agent.common.llm import model
from src.agent.common.persona import with_persona


def extend_node(state: MessagesState):
    # 面向用户的通用回复节点 —— 需要人设
    return {
        "messages": [model.invoke(
        [SystemMessage(content=with_persona(
            "根据历史对话回答用户的问题。"
            "如果用户只是闲聊，就自然地聊；如果问的是本站文章或技术问题，认真回答。"))]
        + state["messages"]
    )]
    }
