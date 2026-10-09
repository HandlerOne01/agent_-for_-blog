from langgraph.graph import MessagesState


# 主图状态
class State(MessagesState):
    user_preferences: dict   # 用户偏好数据
    user_intent: str         # 用戶意图
    # 推荐子图的取数是否已降级（数据库连不上）。
    # 必须写在这里，父图才收得到子图这个键：
    # 降级时一条文章都没推荐出来，就不该再问「要不要收藏」。
    recommend_failed: bool
    # 推荐子图产出的候选（title/category/word_count/difficulty/blog_url）。
    # 同样必须声明在父图：推荐跑完后父图要拿它列出"想听哪篇"，写进子图自己的
    # 状态是收不到的。
    recommended_posts: list[dict]
    # 用户在"要不要介绍一篇"里挑中的篇名。父图预填，介绍子图直接读 ——
    # 这样它不会再去抽篇名（用户答的是序号"1"，抽出来会是个无效篇名）。
    #
    # 名字必须与介绍子图 IntroduceState 的 target_query **完全一致**：
    # 父子图靠同名字段才能自动贯通。曾经这里叫 introduce_target、而子图读
    # target_query —— 症状是"用户答了序号、图也进了子图，但子图还去抽篇名，
    # 抽出个「1」，最后答『没定位到』"。
    target_query: str
