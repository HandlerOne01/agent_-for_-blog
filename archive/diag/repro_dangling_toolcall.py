"""可执行的 bug 复现：DB 降级后消息序列坏掉，下一次 LLM 调用必定 400。

背景（现场取到的证据，不是推测）：
  线程 01a0dbb5-0934-78d3-8882-8f214ea64013，用户 resume 了 「不提供」，
  子图 recommended_graph 依次写了 6 个 checkpoint：
    1. collect_user_info  → topic=随机领域, wc=500-5000, blog_count=5
    2. list_tables        → AIMessage(tool_call sql_db_list_tables) + ToolMessage + AIMessage
    3. call_get_schema    → AIMessage(tool_calls=[sql_db_schema])
    4. get_schema         → AIMessage("我连不上放文章的那个地方了…") + recommend_failed=True   ← 这里降级了
    5. generate_query     → __error__: OpenAIInvalidRequestError 400
         "An assistant message with 'tool_calls' must be followed by tool messages
          responding to each 'tool_call_id'."

也就是说：get_schema 降级时**扔掉了 call_get_schema 那个 tool_call 的应答**，
消息序列从此不合法；下一个节点 generate_query 一调 LLM 就 400。

这个脚本只做断言，不改状态库、不调外部模型。
  python archive/diag/repro_dangling_toolcall.py
"""

import os
import sys

sys.path.insert(0, os.path.abspath("."))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402

from src.agent.node.recommend import (  # noqa: E402
    DB_UNAVAILABLE_REPLY,
    _degraded,
    get_recommend_info,
)

CALL_ID = "call_00_CLBVtlfhxrcqPvEPjWIA6155"


def unresolved_tool_calls(messages):
    """返回没有被任何 ToolMessage 应答的 tool_call_id -> 消息下标。"""
    pending = {}
    for i, m in enumerate(messages):
        for c in getattr(m, "tool_calls", None) or []:
            pending[c.get("id")] = i
        if isinstance(m, ToolMessage):
            pending.pop(m.tool_call_id, None)
    return pending


def scenario_state():
    """还原第 3 步之后的子图状态（call_get_schema 已经吐了 tool_call）。"""
    return {
        "messages": [
            HumanMessage(content=get_recommend_info({"topic": "随机领域", "blog_count": 5})),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "sql_db_schema",
                        "args": {"table_names": "blog, blog_content"},
                        "id": CALL_ID,
                        "type": "tool_call",
                    }
                ],
            ),
        ],
        "topic": "随机领域",
        "word_count_min": 500,
        "word_count_max": 5000,
        "blog_count": 5,
        "recommend_failed": False,
    }


def main():
    state = scenario_state()
    print("=" * 78)
    print("[1] 降级前的消息序列")
    for i, m in enumerate(state["messages"]):
        tcs = [(c["name"], c["id"]) for c in (getattr(m, "tool_calls", None) or [])]
        print(f"    [{i}] {m.type:<6} tool_calls={tcs}")
    print(f"    未应答的 tool_call: {unresolved_tool_calls(state['messages']) or '无'}")

    # get_schema 这一步真实发生的事：连不上库 → 走 _degraded(state)
    degraded = _degraded(state)
    print("=" * 78)
    print("[2] get_schema 降级，节点返回的消息：")
    for m in degraded["messages"]:
        print(f"    {m.type:<6} tool_calls={[(c['name'], c['id']) for c in (getattr(m, 'tool_calls', None) or [])]}"
              f"  content={str(m.content)[:40]!r}")
    print(f"    recommend_failed={degraded.get('recommend_failed')}")

    after = state["messages"] + list(degraded["messages"])
    dangling = unresolved_tool_calls(after)
    print("=" * 78)
    print("[3] 降级后的消息序列（这就是下一步 generate_query 要发给模型的东西）")
    for i, m in enumerate(after):
        tcs = [(c["name"], c["id"]) for c in (getattr(m, "tool_calls", None) or [])]
        print(f"    [{i}] {m.type:<6} tool_calls={tcs} content={str(m.content)[:32]!r}")
    print(f"    未应答的 tool_call: {dangling or '无'}")

    print("=" * 78)
    assert not dangling, (
        "BUG 复现成功：降级后仍有一条 tool_call 没人应答 "
        f"({dangling})。消息序列不合法 —— generate_query 调 LLM 必然 400："
        "An assistant message with 'tool_calls' must be followed by tool messages "
        "responding to each 'tool_call_id'."
    )
    print("OK：降级没有留下悬空 tool_call，序列合法")
    print(f"（降级文案仍是：{DB_UNAVAILABLE_REPLY[:18]}…）")


if __name__ == "__main__":
    main()
