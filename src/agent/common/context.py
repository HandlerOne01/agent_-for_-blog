"""推荐子图的运行时上下文（Runtime context）。

在 LangGraph Studio / API 调用时通过 config 传入：

    {"configurable": {"user_id": "u_001"}}

节点里通过 ``runtime.context`` 读取：

    user_id = runtime.context.get("user_id")
"""

from typing_extensions import TypedDict


class ContextSchema(TypedDict, total=False):
    """推荐子图的上下文参数。

    Attributes:
        user_id: 用户标识。用作长期记忆（store）的命名空间前缀，
            读写该用户的阅读偏好（UserPreferences）。
    """

    user_id: str


# 没传 context（Studio 里没填 Context 面板）时使用的兜底用户
DEFAULT_USER_ID = "anonymous"


def get_user_id(runtime) -> str:
    """安全地从 runtime.context 里取出 user_id。

    注意：不传 context 时 ``runtime.context`` 是 **None**（不是 ``{}``），
    而且 TypedDict 形态的 schema 不会自动实例化，所以直接
    ``runtime.context.get("user_id")`` 会抛 AttributeError。
    LangGraph 的 Runtime 和 ToolRuntime 都有 ``context`` 属性，这里统一兜底。
    """
    context = getattr(runtime, "context", None) or {}
    return context.get("user_id") or DEFAULT_USER_ID
