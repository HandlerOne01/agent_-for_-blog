"""对着正在跑的那个 langgraph dev（127.0.0.1:2024）做一次最小冒烟：
  - 服务在不在、图能不能建起来（模块能被重新加载）
  - blog_agent 跑一轮闲聊，确认没有 import / 建图错误
  - 跑完把这次测试的 thread 删掉，不在 Studio 的 thread 列表里留垃圾
不碰你已有的任何 thread。
"""

import asyncio
import sys

from langgraph_sdk import get_client

URL = "http://127.0.0.1:2024"


async def main():
    client = get_client(url=URL)

    assistants = await client.assistants.search(graph_id="blog_agent", limit=3)
    if not assistants:
        print("找不到 blog_agent assistant")
        return 1
    aid = assistants[-1]["assistant_id"]
    print(f"assistant_id = {aid}")

    thread = await client.threads.create()
    tid = thread["thread_id"]
    print(f"临时 thread_id = {tid}")

    try:
        result = await client.runs.wait(
            tid,
            aid,
            input={"messages": [{"role": "user", "content": "在吗"}]},
        )
        msgs = result.get("messages") or []
        print(f"跑完，messages={len(msgs)} 条")
        last = msgs[-1] if msgs else {}
        content = last.get("content") if isinstance(last, dict) else str(last)
        print(f"最后一条: {str(content)[:80]!r}")
        print("SMOKE OK：服务已加载当前源码，图能正常跑完")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"SMOKE FAIL: {type(exc).__name__}: {str(exc)[:500]}")
        return 1
    finally:
        try:
            await client.threads.delete(tid)
            print("临时 thread 已删除")
        except Exception as exc:  # noqa: BLE001
            print(f"（临时 thread 删除失败，可手动清理：{tid} —— {exc}）")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
