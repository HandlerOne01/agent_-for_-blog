from typing import TypedDict

from langgraph.graph import MessagesState


# 主图状态
class State(MessagesState):
    user_preferences: dict   # 用户偏好数据
    user_intent: str         # 用戶意图
    # 推荐子图的取数是否已降级（数据库连不上）。
    # 必须写在这里，父图才收得到子图这个键：
    # 降级时一条文章都没推荐出来，就不该再问「要不要收藏」。
    recommend_failed: bool

# 私有状态
class NeedCollectOutput(TypedDict):
    collect: str  # 需要、不需要
    # 这个状态不会出现在最终状态
