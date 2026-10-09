import uuid
from typing import Annotated, Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode, ToolRuntime, InjectedStore
from langgraph.types import interrupt

from src.agent.common.context import get_user_id
from src.agent.common.llm import model
from src.agent.common.persona import with_persona
from src.agent.common.store import CollectedInfo, UserPreferences
from src.agent.state.collect import CollectState

# 节点：获取收藏文章名称
def get_title(state: CollectState):
    prompt = "请输入要收藏的博客文章标题"

    while True:
        title = interrupt(prompt)
        if title:
            return {"title": title}
        prompt = f"'{title}'不是一个有效的博客文章标题，请更正"

# 节点：获取文章作者
def get_author(state: CollectState):
    prompt = "请输入要收藏的文章作者名"

    while True:
        author = interrupt(prompt)
        if author:  # 验证操作
            return {"author": author}

        # 验证失败：再次输入
        prompt = f"‘{author}’ 不是一个有效的作者名，请更正"


# 节点：获取接收文章全文的邮箱
def get_email(state: CollectState):
    prompt = "请输入接收文章全文的邮箱地址"

    while True:
        email = interrupt(prompt)
        if email:  # 验证操作
            return {"email": email}

        # 验证失败：再次输入
        prompt = f"‘{email}’ 不是一个有效的邮箱地址，请更正"

# 节点：更新HumanMessage
def add_collect_message(state: CollectState):
    collect_prompt = """
    根据提供的信息，帮我收藏博客文章。
    - 收藏的文章标题：{title}
    - 文章作者：{author}
    - 接收文章全文的邮箱：{email}
        """
    return {"messages": [HumanMessage(content=collect_prompt.format(
        title = state["title"],
        author = state["author"],
        email = state["email"]
    ))]}

# 1. 生成收藏单
# 2. 持久化存储收藏信息
#    运行时数据：ToolRuntime
#    store: Annotated[Any, InjectedStore()]  在工具中注入store
@tool
def generate_collects(author: str,email: str,blog_title: str,
                      runtime: ToolRuntime,
                      store: Annotated[Any,InjectedStore()])->str:
    """
    根据文章作者、邮箱、收藏的博客文章，生成收藏单号。

Args:
    author: 文章作者
    email：接收文章全文的邮箱
    blog_title：用户要收藏的博客文章标题
    runtime：工具的运行时信息
    store：注入工具的持久存储
    """

    # 1. 模拟生成收藏单号(扩展：持久化收藏表)
    collect_id = str(uuid.uuid4())

    # 2. 构建收藏信息
    collected_info = CollectedInfo(
        collect_id = collect_id,
        title=blog_title,
        author=author,
    )

    # 3. 持久化存储用户偏好（收藏信息）
    user_id = get_user_id(runtime)
    namespace = (user_id,"preferences")
    # 查询
    prefs_result = store.search(namespace)
    if len(prefs_result) == 0:
        # 没有持久化信息，新增
        prefs = UserPreferences(
            collected_info = [collected_info]
        )
        store.put(
            namespace,
            str(uuid.uuid4()),
            prefs.model_dump(exclude_none=True)
        )
    else:
        # 有偏好数据，更新
        prefs = prefs_result[0].value or {}
        prefs.setdefault("collected_info", []).append(collected_info)
        store.put(namespace, prefs_result[0].key, prefs)
    return f"已成功收藏博客文章：{blog_title}, 收藏单号为：{collect_id}"

# 将来就可以将这个工具，使用ToolNode进行定义
tool_node = ToolNode([generate_collects])

# 节点：执行模型：1. 决定进行工具（收藏文章）调用  2. 返回最终结果
def call_collects(state: CollectState):
    # 面向用户产出最终答复 —— 需要人设
    return {
        "messages": [
            model.bind_tools([generate_collects]).invoke(
                [
                    SystemMessage(
                        content=with_persona(
                            "你负责生成收藏单：调用工具完成博客文章收藏，"
                            "然后根据工具返回的结果，用自己的话告诉用户收藏结果（含收藏单号）。"
                        )
                    )
                ]
                + state["messages"]
            )
        ]
    }




