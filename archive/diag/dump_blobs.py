"""读取 .langgraph_checkpoint.2.pckl 里该 thread 的全部 checkpoint：
父图 + 子图、每一次运行的消息序列与 tool_calls，找悬空 tool call。"""

import pickle
import sys
from collections import Counter

sys.path.insert(0, ".")
import src.agent.graph  # noqa: E402,F401

THREAD = "01a0dbb5-0934-78d3-8882-8f214ea64013"


def main():
    with open(".langgraph_api/.langgraph_checkpoint.2.pckl", "rb") as f:
        data = pickle.load(f)

    hits = [k for k in data if isinstance(k, tuple) and k and k[0] == THREAD]
    print(f"checkpoint 键: {len(hits)}")
    for k in sorted(hits, key=lambda x: str(x[2])):
        thread, ns, ckpt = k[0], k[1], k[2]
        blob = data[k]
        try:
            blob = pickle.loads(blob) if isinstance(blob, (bytes, bytearray)) else blob
        except Exception as exc:  # noqa: BLE001
            print(f"!! {k} 反序列化失败 {exc}")
            continue
        print("=" * 100)
        print(f"ns='{ns}'  ckpt={ckpt}")
        if not isinstance(blob, dict):
            print(f"  blob={type(blob).__name__} {str(blob)[:200]}")
            continue
        print(f"  blob keys={list(blob.keys())[:25]}")
        for field, val in blob.items():
            if field in ("v", "versions", "id", "ts"):
                print(f"  {field}={str(val)[:200]}")
            elif isinstance(val, (bytes, bytearray)):
                try:
                    inner = pickle.loads(val)
                except Exception:  # noqa: BLE001
                    inner = f"<bytes {len(val)}>"
                print(f"  {field} -> {brief(inner)}")
            else:
                print(f"  {field} -> {brief(val)}")


def brief(obj):
    if isinstance(obj, dict):
        if "messages" in obj and isinstance(obj["messages"], list):
            msgs = obj["messages"]
            out = [f"channel_values: messages={len(msgs)} {dict(Counter(getattr(m,'type','?') for m in msgs))}"]
            out += show_msgs(msgs)
            others = {k: str(v)[:160] for k, v in obj.items() if k != "messages"}
            out.append(f"  其它={others}")
            return "\n     ".join(out)
        return f"dict({list(obj.keys())[:15]})"
    if isinstance(obj, list) and obj and hasattr(obj[0], "type"):
        return f"messages[{len(obj)}]:\n     " + "\n     ".join(show_msgs(obj))
    return f"{type(obj).__name__}: {str(obj)[:300]}"


def show_msgs(msgs):
    lines = []
    pending = {}
    for i, m in enumerate(msgs):
        t = getattr(m, "type", "?")
        mid = str(getattr(m, "id", None))
        line = f"[{i:>2}] {t:<6} id={mid[:44]:<44}"
        tcs = getattr(m, "tool_calls", None)
        if tcs:
            line += " TOOLCALLS=" + str([(c.get("name"), c.get("id")) for c in tcs])
            for c in tcs:
                pending[c.get("id")] = (i, c.get("name"))
            line += f"\n       args={str([c.get('args') for c in tcs])[:250]}"
        elif t == "tool":
            line += f" replies_to={getattr(m,'tool_call_id',None)}"
            pending.pop(getattr(m, "tool_call_id", None), None)
            line += f"\n       content={str(getattr(m,'content',''))[:160]!r}"
        else:
            line += f"\n       content={str(getattr(m,'content',''))[:260]!r}"
        ak = getattr(m, "additional_kwargs", None)
        if ak:
            line += f"\n       additional_kwargs={str(ak)[:200]}"
        lines.append(line)
    if pending:
        lines.append(f"!!!! 悬空 tool_calls: {pending}")
    else:
        lines.append("悬空 tool_calls: 无")
    return lines


if __name__ == "__main__":
    main()
