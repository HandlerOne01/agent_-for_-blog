"""端到端验证：数据库在 get_schema 那一步挂掉时，子图必须干净收场。

用 InMemorySaver + monkeypatch 精确复现现场那一刻：
  collect_user_info 跳过（topic 已给）→ list_tables 成功
  → call_get_schema 成功（消息里留下一条 tool_call）
  → get_schema **抛异常**（模拟库里那一挂）
  → 断言：图不炸、消息序列里没有悬空 tool_call、最终答复是那句人设话。

不写用户的 .langgraph_api，不碰真实数据库，除 call_get_schema 外不调模型。
"""

import os
import sys
import traceback

sys.path.insert(0, os.path.abspath("."))

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.store.memory import InMemoryStore  # noqa: E402
from langchain_core.messages import HumanMessage, ToolMessage  # noqa: E402

import src.agent.node.recommend as R  # noqa: E402
from src.agent.recommend import recommended_graph  # noqa: E402


def unanswered(messages):
    pending = {}
    for i, m in enumerate(messages):
        for c in getattr(m, "tool_calls", None) or []:
            pending[c["id"]] = i
        if isinstance(m, ToolMessage):
            pending.pop(m.tool_call_id, None)
    return pending


def main():
    # get_schema 只在这一步炸：模拟「库在取表结构时不可达」
    real_tool_node = R._tool_node

    def flaky_tool_node(cache_key, tool_name):
        node = real_tool_node(cache_key, tool_name)

        class Boom:
            def invoke(self, state):
                raise R.DBUnavailable("模拟：取表结构时库不可达")

        return Boom() if cache_key == "get_schema" else node

    R._tool_node = flaky_tool_node

    # 已经 compile 过的图不能重新 compile，直接换掉 checkpointer / store（离线，
    # 不碰用户的 .langgraph_api，也不碰真实库里的偏好数据）
    app = recommended_graph.copy()
    app.checkpointer = InMemorySaver()
    app.store = InMemoryStore()

    initial = {
        # 子图拿到的是父图那份 messages（用户原话），不是 collect_user_info 写的那条
        "messages": [HumanMessage(content="推荐几篇讲算法的博客")],
        "topic": "随机领域",
        "word_count_min": 500,
        "word_count_max": 5000,
        "blog_count": 5,
        "user_preferences": {"word_count_min": 500, "word_count_max": 5000},
        "recommend_failed": False,
    }
    config = {"configurable": {"thread_id": "offline-degrade-check"}}

    print("=" * 78)
    print("跑图（DB 在 get_schema 挂掉）…")
    try:
        app.invoke(initial, config)
    except Exception:  # noqa: BLE001
        print("!!! 图抛异常了 —— 修复没生效")
        traceback.print_exc()
        return 1

    state = app.get_state(config).values
    msgs = state["messages"]
    print(f"消息 {len(msgs)} 条：{[m.type for m in msgs]}")
    for i, m in enumerate(msgs):
        tcs = [(c["name"], c["id"]) for c in (getattr(m, "tool_calls", None) or [])]
        extra = f" replies_to={m.tool_call_id}" if isinstance(m, ToolMessage) else ""
        print(f"  [{i}] {m.type:<6} tool_calls={tcs}{extra} content={str(m.content)[:46]!r}")

    dangling = unanswered(msgs)
    print("=" * 78)
    print(f"recommend_failed = {state.get('recommend_failed')}")
    print(f"悬空 tool_call   = {dangling or '无'}")

    problems = []
    if dangling:
        problems.append(f"仍有悬空 tool_call：{dangling}")
    if not state.get("recommend_failed"):
        problems.append("recommend_failed 没被置位，图会继续往下跑")
    if not any(str(m.content) == R.DB_UNAVAILABLE_REPLY for m in msgs):
        problems.append("整段消息里找不到那句人设化降级话术")
    if any(c.get("name") == "sql_db_query" for m in msgs for c in (getattr(m, "tool_calls", None) or [])):
        problems.append("降级之后居然还去跑了 SQL 查询")

    if problems:
        print("FAIL：")
        for p in problems:
            print("  -", p)
        return 1
    print("PASS：降级干净收场，消息序列合法，用户看到的是人设化提示")
    return 0


if __name__ == "__main__":
    sys.exit(main())
