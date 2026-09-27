"""对照验证：同一个场景，只把 _degraded 换回「修复前」的行为。

期望：
  修复前 → generate_query 带着悬空 tool_call 去调模型 → OpenAI 400（现场那个报错）
  修复后 → 图收场，消息序列合法
"""

import os
import sys

sys.path.insert(0, os.path.abspath("."))

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.store.memory import InMemoryStore  # noqa: E402

import src.agent.node.recommend as R  # noqa: E402
from src.agent.recommend import recommended_graph  # noqa: E402


def run(old_style: bool):
    real_tool_node = R._tool_node
    real_degraded = R._degraded

    def flaky_tool_node(cache_key, tool_name):
        node = real_tool_node(cache_key, tool_name)

        class Boom:
            def invoke(self, state):
                raise R.DBUnavailable("模拟：取表结构时库不可达")

        return Boom() if cache_key == "get_schema" else node

    def old_degraded(state):
        # 修复前的实现：只回一句话，不管在飞的 tool_call
        return {"messages": [AIMessage(content=R.DB_UNAVAILABLE_REPLY)], "recommend_failed": True}

    R._tool_node = flaky_tool_node
    if old_style:
        R._degraded = old_degraded

    try:
        app = recommended_graph.copy()
        app.checkpointer = InMemorySaver()
        app.store = InMemoryStore()
        app.invoke(
            {
                "messages": [HumanMessage(content="推荐几篇讲算法的博客")],
                "topic": "算法",
                "word_count_min": 500,
                "word_count_max": 5000,
                "blog_count": 5,
                "user_preferences": {"word_count_min": 500, "word_count_max": 5000},
                "recommend_failed": False,
            },
            {"configurable": {"thread_id": f"{'old' if old_style else 'new'}-style"}},
        )
        return None
    except Exception as exc:  # noqa: BLE001
        return exc
    finally:
        R._tool_node = real_tool_node
        R._degraded = real_degraded


def main():
    print("=" * 78)
    print("[A] 修复前的行为（_degraded 不补 ToolMessage）")
    err = run(old_style=True)
    if err is None:
        print("    没报错？（和现场不符，值得再看）")
    else:
        print(f"    抛异常: {type(err).__name__}")
        print(f"    {str(err)[:200]}")

    print("=" * 78)
    print("[B] 修复后的行为（_degraded 先补 ToolMessage）")
    err = run(old_style=False)
    print(f"    结果: {'抛异常 ' + type(err).__name__ if err else '正常收场，无异常'}")

    print("=" * 78)
    if err is None:
        print("PASS：修复把「半路降级 → 消息序列坏掉 → 下一个 LLM 节点 400」这条路堵住了")
        return 0
    print("FAIL：修复后仍然抛异常")
    return 1


if __name__ == "__main__":
    sys.exit(main())
