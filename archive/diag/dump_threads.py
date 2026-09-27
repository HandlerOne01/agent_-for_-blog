"""只读诊断：把 langgraph dev 内存里最近的 thread 状态倒出来。

目的是定位那个 OpenAI 400：
  "An assistant message with 'tool_calls' must be followed by tool messages
   responding to each 'tool_call_id'."
也就是找出消息序列里那条「有 tool_calls、后面却没有对应 ToolMessage」的 AIMessage。
"""

import asyncio
import json
import sys
from collections import Counter

from langgraph_sdk import get_client


def brief(msg):
    if not isinstance(msg, dict):
        return repr(msg)[:200]
    t = msg.get("type")
    out = {"type": t, "id": msg.get("id"), "name": msg.get("name")}
    if t == "ai":
        tc = msg.get("tool_calls") or []
        out["tool_calls"] = [
            {"name": c.get("name"), "id": c.get("id"), "args": str(c.get("args"))[:160]}
            for c in tc
        ]
        content = msg.get("content")
        out["content"] = (str(content)[:300] if content else "")
    elif t == "tool":
        out["tool_call_id"] = msg.get("tool_call_id")
        out["content"] = str(msg.get("content"))[:200]
    else:
        out["content"] = str(msg.get("content"))[:400]
    return out


def find_dangling(messages):
    """返回 [(下标, aimessage 摘要)] —— 后面没有 ToolMessage 应答的 tool_calls。"""
    pending = {}
    bad = []
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        if m.get("type") == "ai":
            for c in m.get("tool_calls") or []:
                pending[c.get("id")] = (i, c.get("name"))
        elif m.get("type") == "tool":
            pending.pop(m.get("tool_call_id"), None)
    for cid, (i, name) in pending.items():
        bad.append({"msg_index": i, "tool_call_id": cid, "tool_name": name})
    return bad


async def main():
    client = get_client(url="http://127.0.0.1:2024")
    threads = await client.threads.search(limit=20)
    print(f"threads: {len(threads)}")
    for t in threads:
        tid = t["thread_id"]
        print("=" * 78)
        print(f"thread_id={tid}  status={t.get('status')}  created={t.get('created_at')}")
        print(f"  metadata={json.dumps(t.get('metadata'), ensure_ascii=False)}")
        try:
            state = await client.threads.get_state(tid)
        except Exception as exc:  # noqa: BLE001
            print(f"  !! get_state 失败: {type(exc).__name__}: {exc}")
            continue
        vals = state.get("values") or {}
        messages = vals.get("messages") or []
        print(f"  next={state.get('next')}  checkpoint_id={state.get('checkpoint', {}).get('checkpoint_id')}")
        print(f"  tasks={json.dumps(state.get('tasks'), ensure_ascii=False, default=str)[:600]}")
        print(f"  message types: {dict(Counter(m.get('type') for m in messages if isinstance(m, dict)))}")
        dangling = find_dangling(messages)
        if dangling:
            print(f"  *** DANGLING tool_calls: {json.dumps(dangling, ensure_ascii=False)}")
        else:
            print("  dangling tool_calls: none")
        print("  --- 消息序列 ---")
        for i, m in enumerate(messages):
            print(f"  [{i:>2}] {json.dumps(brief(m), ensure_ascii=False, default=str)}")
        other = {k: v for k, v in vals.items() if k != "messages"}
        print("  --- 其它 state 字段 ---")
        print("  " + json.dumps(other, ensure_ascii=False, default=str)[:2000])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
