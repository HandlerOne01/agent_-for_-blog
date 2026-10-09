from langgraph.graph import MessagesState


class CollectState(MessagesState):
    title: str           # 收藏的博客文章
    author: str          # 文章作者
    email: str           # 接收文章的邮箱
