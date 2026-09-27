"""降级路径不能把消息序列弄坏。

现场故障（2026-09-26，thread 01a0dbb5，可复现）：
  collect_user_info → list_tables → call_get_schema（写了 AIMessage[tool_calls]）
  → get_schema 连不上库，走 _degraded()
  → 当时 _degraded() 只返回一句 AI 话，那条 tool_call 没人应答
  → 图接着走到 generate_query，一调模型就 400：
     An assistant message with 'tool_calls' must be followed by tool messages
     responding to each 'tool_call_id'.

用户看到的是 traceback，而不是那句人设化的「我连不上…」。
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from src.agent.node.recommend import (
    DB_UNAVAILABLE_REPLY,
    _degraded,
    _pending_tool_call_ids,
)

CALL_ID = "call_00_CLBVtlfhxrcqPvEPjWIA6155"


def _schema_call_state() -> dict:
    """还原 call_get_schema 刚写完、get_schema 准备跑的那一刻。"""
    return {
        "messages": [
            HumanMessage(content="提取用户期望推荐的博客文章信息如下："),
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
        "recommend_failed": False,
    }


def _unanswered(messages) -> dict:
    """返回没有被 ToolMessage 应答的 tool_call_id -> 所在消息下标。"""
    pending: dict = {}
    for i, msg in enumerate(messages):
        for call in getattr(msg, "tool_calls", None) or []:
            pending[call["id"]] = i
        if isinstance(msg, ToolMessage):
            pending.pop(msg.tool_call_id, None)
    return pending


def test_degraded_answers_pending_tool_call() -> None:
    """核心回归：降级必须把在飞的 tool_call 应答掉，否则下一次调模型 400。"""
    state = _schema_call_state()
    assert _unanswered(state["messages"]) == {CALL_ID: 1}, "前置条件：确实有一条悬空 tool_call"

    result = _degraded(state)
    merged = state["messages"] + list(result["messages"])

    assert _unanswered(merged) == {}, "降级后仍有 tool_call 没人应答 —— 下一次 LLM 调用会 400"

    replies = [m for m in merged if isinstance(m, ToolMessage)]
    assert len(replies) == 1
    assert replies[0].tool_call_id == CALL_ID
    assert replies[0].status == "error"

    # 人设化的降级话术还是照旧要说
    assert merged[-1].content == DB_UNAVAILABLE_REPLY
    assert result["recommend_failed"] is True


def test_degraded_leaves_no_dangling_when_nothing_pending() -> None:
    """没人在飞（比如 collect_user_info 阶段就挂了）时，不要凭空造 tool 应答。"""
    state = {"messages": [HumanMessage(content="推荐几篇")], "recommend_failed": False}

    result = _degraded(state)

    assert [m.type for m in result["messages"]] == ["ai"]
    assert result["recommend_failed"] is True


def test_degraded_answers_only_the_pending_call() -> None:
    """已经答过的那条不许再答一遍，未答的两条要各答一条。"""
    state = {
        "messages": [
            HumanMessage(content="推荐几篇"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "sql_db_list_tables", "args": {}, "id": "done-1", "type": "tool_call"},
                    {"name": "sql_db_schema", "args": {}, "id": "open-1", "type": "tool_call"},
                    {"name": "sql_db_schema", "args": {}, "id": "open-2", "type": "tool_call"},
                ],
            ),
            ToolMessage(content="blog, blog_content", tool_call_id="done-1"),
        ],
        "recommend_failed": False,
    }
    assert _pending_tool_call_ids(state) == ["open-1", "open-2"]

    result = _degraded(state)
    merged = state["messages"] + list(result["messages"])

    assert _unanswered(merged) == {}
    answered = sorted(m.tool_call_id for m in merged if isinstance(m, ToolMessage))
    assert answered == ["done-1", "open-1", "open-2"]


def test_should_continue_ends_when_degraded() -> None:
    """降级后要收场，不许再进 generate_query 调模型。

    这条守住的是「顺序」：should_continue 必须先看 recommend_failed。
    降级时最后一条 AI 消息没有 tool_calls，先看 tool_calls 的话会被
    误判成「取数结束」送去 compose_answer —— 那等于拿降级话术冒充正常收尾。
    """
    from langgraph.constants import END

    from src.agent.recommend import should_continue

    state = _schema_call_state()
    state.update(_degraded(state))

    assert should_continue(state) == END
