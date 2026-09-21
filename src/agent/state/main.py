from typing import TypedDict

from langgraph.graph import MessagesState


# 主图状态
class State(MessagesState):
    user_preferences: dict   # 用户偏好数据
    user_intent: str         # 用戶意图

# 私有状态
class NeedCollectOutput(TypedDict):
    collect: str  # 需要、不需要
    # 这个状态不会出现在最终状态
