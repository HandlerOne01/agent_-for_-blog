# -*- coding: utf-8 -*-
"""降级与答复出口的共享实现。

推荐路径与介绍路径共用；刻意不放在 node/ 下面，避免两个业务模块互相 import。

与 node/recommend.py 里原来的 _degraded() 是同一套行为，只是把"失败标记用哪个键"
提成参数：推荐路径用 recommend_failed，介绍路径用 introduce_failed。
两者语义不同，共用一个键会让父图的 after_recommend 误判。
"""
from langchain_core.messages import AIMessage, ToolMessage

# 数据库不可用时，代替结果说的话（爱弥斯口吻，不用「您」）。
DB_UNAVAILABLE_REPLY = (
    "我连不上放文章的那个地方了。这会儿找不了文章，聊天还行——"
    "你要不要先随便问我点别的？"
)


def pending_tool_call_ids(state) -> list:
    """找出上一节点发出、还没有任何 ToolMessage 应答的 tool_call_id。

    为什么必须查这个：OpenAI 兼容接口有硬约束 —— 任何带 tool_calls 的
    assistant 消息，后面必须跟齐每一条 tool_call_id 对应的 tool 消息。
    卡点在于降级是「半路」发生的：call_get_schema 已经把 tool_call 写进消息序列，
    紧接着 get_schema 连不上库 —— 若直接返回一句话就收场，那条 tool_call 就成了
    孤儿，轮到下一个 LLM 节点时整轮 400。
    """
    pending: dict = {}
    for msg in state.get("messages") or []:
        for call in getattr(msg, "tool_calls", None) or []:
            call_id = call.get("id")
            if call_id:
                pending[call_id] = call.get("name") or "tool"
        if isinstance(msg, ToolMessage):
            pending.pop(msg.tool_call_id, None)
    return [cid for cid in pending]


def degrade_reply(state, mark=None) -> dict:
    """统一的降级返回：应答悬空 tool_call + 一句人设化提示（+ 可选失败标记）。

    mark 只在**真有消费者**时才传：
    - 推荐路径传 "recommend_failed" —— 它被 after_recommend / db_ready /
      compose_answer 三处真实读取；
    - 介绍路径传 "introduce_failed" —— 子图条件边收到后直接结束，
      不再继续取正文或调用生成模型。
    """
    outgoing: list = []
    for call_id in pending_tool_call_ids(state):
        # 这一条不是「查询结果」，只是给协议一个交代；正文交代了取数没成。
        outgoing.append(
            ToolMessage(
                content="取数失败：数据库不可用，这条工具调用没有执行。",
                tool_call_id=call_id,
                status="error",
            )
        )
    outgoing.append(AIMessage(content=DB_UNAVAILABLE_REPLY))
    if mark:
        return {"messages": outgoing, mark: True}
    return {"messages": outgoing}
