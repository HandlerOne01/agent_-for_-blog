"""只读诊断：读 recommend 子图 namespace 下的 checkpoint，找出悬空 tool_calls。"""

import glob
import os
import pickle
import sys
import traceback
from collections import Counter

sys.path.insert(0, os.path.abspath("."))
import src.agent.graph  # noqa: E402,F401

THREAD = "01a0dbb5-0934-78d3-8882-8f214ea64013"


def load(fn):
    with open(fn, "rb") as f:
        return pickle.load(f)


def walk_messages(vals):
    """channel_values -> messages"""
    if isinstance(vals, dict) and vals.get("messages"):
        return vals["messages"]
    return None


def dangling(msgs):
    pending, bad = {}, []
    for i, m in enumerate(msgs):
        tc = getattr(m, "tool_calls", None)
        if tc:
            for c in tc:
                pending[c.get("id")] = (i, c.get("name"))
        elif getattr(m, "type", None) == "tool":
            pending.pop(getattr(m, "tool_call_id", None), None)
    for cid, (i, name) in pending.items():
        bad.append((i, cid, name))
    return bad


def show(msgs):
    for i, m in enumerate(msgs):
        t = getattr(m, "type", "?")
        tc = getattr(m, "tool_calls", None)
        s = f"  [{i:>2}] {t:<6} id={str(getattr(m,'id',None))[:46]:<46}"
        if tc:
            s += f" TOOLCALLS={[(c.get('name'), c.get('id')) for c in tc]}"
            s += f"\n        args={str([c.get('args') for c in tc])[:300]}"
        elif t == "tool":
            s += f" replies_to={getattr(m,'tool_call_id',None)}"
            s += f"\n        content={str(getattr(m,'content',''))[:200]!r}"
        else:
            s += f"\n        content={str(getattr(m,'content',''))[:300]!r}"
        print(s)


def main():
    files = sorted(glob.glob(".langgraph_api/.langgraph_checkpoint.*.pckl"))
    for fn in files:
        try:
            data = load(fn)
        except Exception as exc:  # noqa: BLE001
            print(f"!! {fn} 无法反序列化: {type(exc).__name__}: {exc}")
            continue
        print("=" * 90)
        print(f"FILE {fn}  top={type(data).__name__} size={len(data)}")

        # 形态 A：{thread_id: {ns: state}}
        if isinstance(data, dict) and THREAD in data:
            nsmap = data[THREAD]
            print(f"  找到 thread（形态A）namespaces={list(nsmap.keys())}")
            for ns, st in nsmap.items():
                print(f"  --- ns='{ns}' keys={list(st.keys())[:15] if isinstance(st, dict) else type(st)}")
                vals = st.get("channel_values") if isinstance(st, dict) else None
                msgs = walk_messages(vals)
                if msgs:
                    print(f"      messages={len(msgs)} {dict(Counter(getattr(m,'type','?') for m in msgs))}")
                    print(f"      dangling={dangling(msgs)}")
                    show(msgs)
                    print(f"      other channels={ {k: str(v)[:200] for k, v in vals.items() if k != 'messages'} }")

        # 形态 B：{(thread, ns, ckpt): {...}}
        if isinstance(data, dict):
            hits = [k for k in data.keys() if isinstance(k, tuple) and k and k[0] == THREAD]
            if hits:
                print(f"  找到 thread（形态B）checkpoint 数={len(hits)}")
                for k in sorted(hits, key=lambda x: str(x[2])):
                    try:
                        blob = pickle.loads(data[k]) if isinstance(data[k], (bytes, bytearray)) else data[k]
                    except Exception:  # noqa: BLE001
                        blob = data[k]
                    print(f"  --- key={k}")
                    print(f"      blob type={type(blob).__name__}")
                    if isinstance(blob, dict):
                        print(f"      blob keys={list(blob.keys())[:20]}")
                        for kk, vv in blob.items():
                            if kk in ("channel_values", "values") and isinstance(vv, dict):
                                msgs = walk_messages(vv)
                                if msgs:
                                    print(f"      {kk}.messages={len(msgs)} {dict(Counter(getattr(m,'type','?') for m in msgs))}")
                                    print(f"      dangling={dangling(msgs)}")
                                    show(msgs)
                                    break


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
