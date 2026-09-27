"""只读诊断：把出 400 的那个 thread 的 state / tasks / 子图 checkpoint 全部倒出来。

目标：找出「有 tool_calls 却没被 ToolMessage 应答」的那条 AIMessage
到底在哪个节点、在哪一次运行里产生的。
"""

import asyncio
import json
import os
import pickle
import sys
from collections import Counter

sys.path.insert(0, os.path.abspath("."))

from langgraph_sdk import get_client  # noqa: E402

# 先 import 项目模块，让 pickle 能反序列化项目里的自定义类型
import src.agent.graph  # noqa: E402,F401
import src.agent.state.recommend  # noqa: E402,F401
import src.agent.node.recommend  # noqa: E402,F401

THREAD = "01a0dbb5-0934-78d3-8882-8f214ea64013"


def brief(m):
    if not isinstance(m, dict):
        return repr(m)[:200]
    t = m.get("type")
    if t == "ai":
        return {
            "type": t,
            "id": m.get("id"),
            "tool_calls": [
                {"name": c.get("name"), "id": c.get("id"), "args": str(c.get("args"))[:200]}
                for c in (m.get("tool_calls") or [])
            ],
            "content": str(m.get("content"))[:400],
        }
    if t == "tool":
        return {"type": t, "tool_call_id": m.get("tool_call_id"), "name": m.get("name"),
                "content": str(m.get("content"))[:200]}
    return {"type": t, "id": m.get("id"), "content": str(m.get("content"))[:400]}


async def main():
    client = get_client(url="http://127.0.0.1:2024")

    print("############ 1. 父图 state（完整） ############")
    state = await client.threads.get_state(THREAD)
    print(json.dumps(state, ensure_ascii=False, default=str, indent=2)[:6000])

    print("\n############ 2. 完整历史（每个 checkpoint 一层） ############")
    hist = await client.threads.get_history(THREAD, limit=50)
    print(f"checkpoints: {len(hist)}")
    for h in hist:
        cid = (h.get("checkpoint") or {}).get("checkpoint_id")
        vals = h.get("values") or {}
        msgs = vals.get("messages") or []
        print("-" * 70)
        print(f"ckpt={cid} next={h.get('next')} types={dict(Counter(m.get('type') for m in msgs if isinstance(m, dict)))}")
        for i, m in enumerate(msgs):
            print(f"   [{i:>2}] {json.dumps(brief(m), ensure_ascii=False, default=str)}")
        print(f"   meta={json.dumps(h.get('metadata'), ensure_ascii=False, default=str)[:800]}")

    print("\n############ 3. 原始 checkpoint pckl：找子图状态 ############")
    for fn in (".langgraph_api/.langgraph_checkpoint.1.pckl",
               ".langgraph_api/.langgraph_checkpoint.2.pckl",
               ".langgraph_api/.langgraph_checkpoint.3.pckl"):
        if not os.path.exists(fn):
            continue
        print("=" * 70)
        print(f"file {fn} ({os.path.getsize(fn)} bytes)")
        with open(fn, "rb") as f:
            data = pickle.load(f)
        print(f"  top type={type(data)}")
        blobs = None
        if isinstance(data, dict):
            print(f"  keys={list(data.keys())[:20]}")
            blobs = data.get("blobs") or data
        if isinstance(blobs, dict):
            print(f"  blobs: {len(blobs)}")
            for k, v in blobs.items():
                try:
                    obj = pickle.loads(v) if isinstance(v, (bytes, bytearray)) else v
                except Exception as exc:  # noqa: BLE001
                    print(f"  key={str(k)[:70]} !! unpickle 失败 {type(exc).__name__}: {exc}")
                    continue
                desc = ""
                if isinstance(obj, dict):
                    desc = f"dict keys={list(obj.keys())[:12]}"
                    ch = obj.get("channel_values")
                    if isinstance(ch, dict) and ch.get("messages"):
                        msgs = ch["messages"]
                        desc += f" | messages={len(msgs)} {dict(Counter(getattr(m,'type','?') for m in msgs))}"
                        for m in msgs:
                            if getattr(m, "tool_calls", None):
                                desc += f"\n        TOOLCALLS {getattr(m,'id',None)}: {[ (c.get('name'), c.get('id')) for c in m.tool_calls]}"
                print(f"  key={str(k)[:70]} -> {type(obj).__name__} {desc[:1500]}")


if __name__ == "__main__":
    asyncio.run(main())
