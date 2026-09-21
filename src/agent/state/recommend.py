from langgraph.graph import MessagesState

class RecommendState(MessagesState):
    # 用户偏好数据（共享主图状态）
    # 为什么？因为如果当用户提问：给我推荐几篇讲 LangGraph 的博客。
    # 此时问题中不包含字数偏好，则可以通过历史偏好数据，进行设置！！增加智能化
    user_preferences: dict

    # 以下是用户输入的推荐的关键参数
    topic: str  # 领域/分类（用于定位博客文章库）
    word_count_min: int  # 最少字数
    word_count_max: int  # 最多字数
    category: str  # 栏目/专栏
    blog_type: str  # 文章类型（教程、随笔、源码分析...）
    tags: str  # 标签
    blog_count: int  # 推荐数量
    others: str  # 其它要求

# 获取推荐信息方法
def get_recommend_info(state: dict) -> str:
    info_prompt = """
提取用户期望推荐的博客文章信息如下：
- 领域/分类: {topic}
- 栏目: {category}
- 字数: {word_count_min} - {word_count_max} 字
- 文章类型: {blog_type}
- 标签: {tags}
- 特殊要求: {others}
- 推荐数量: {blog_count}
如果某些信息未指定，请使用合适的默认值或放宽条件。"""
    return info_prompt.format(
        topic=state.get('topic', '未指定'),
        category=state.get('category', '未指定'),
        word_count_min=state.get('word_count_min', '未指定'),
        word_count_max=state.get('word_count_max', '未指定'),
        blog_type=state.get('blog_type', '未指定'),
        tags=state.get('tags', '未指定'),
        others=state.get('others', '无'),
        blog_count=state.get('blog_count', 5)
    )