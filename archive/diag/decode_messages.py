"""把 .langgraph_api 两个 pckl 里该 thread 子图的 messages 通道完整还原成消息对象打印。

msgpack 解出来是 (lc, [type, msgid, dict]) 的 langchain 序列化格式。
"""

import glob
import pickle
import sys
from collections import Counter

from langchain_core.messages import messages_from_dict

sys.path.insert(0, ".")
import src.agent.graph  # noqa: E402,F401

THREAD = "01a0dbb5-0934-78d3-8882-8f214ea64013"


def decode_payload(kind, payload):
    """('msgpack', bytes) -> python 对象"""
    if kind == "null":
        return None
    if kind == "msgpack":
        import ormsgpack

        return ormsgpack.unpackb(
            payload,
            ext_hook=lambda code, data: (code, data),
            option=ormsgpack.OPT_NON_STR_KEYS,
        )
    return f"<{kind} {len(payload)} bytes>"


def _fix(obj):
    """把 (code, data) ext 还原成 langchain 认的 ExtType。"""
    from langchain_core.load.serializable import Serializable
    from msgpack import ExtType  # 可能没装

    return obj


def to_msgs(payload):
    # payload 形态： [version, [ [type, id, kwargs], ... ] ]
    if not isinstance(payload, list) or len(payload) != 2:
        return None
    items = payload[1]
    if not isinstance(items, list):
        return None
    dicts = []
    for it in items:
        if not isinstance(it, list) or len(it) != 3:
            return None
        dicts.append({"type": it[0], "id": it[1], "kwargs": it[2]})
    try:
        return messages_from_dict(dicts)
    except Exception as exc:  # noqa: BLE001
        print(f"   !! messages_from_dict 失败: {exc}")
        return None


def show(msgs, label):
    print(f"   --- {label}: {len(msgs)} 条 {dict(Counter(m.type for m in msgs))}")
    pending = {}
    for i, m in enumerate(msgs):
        line = f"     [{i:>2}] {m.type:<6} id={str(m.id)[:44]:<44}"
        tc = getattr(m, "tool_calls", None)
        if tc:
            line += " TOOLCALLS=" + str([(c.get("name"), c.get("id")) for c in tc])
            for c in tc:
                pending[c.get("id")] = (i, c.get("name"))
            line += f"\n          args={str([c.get('args') for c in tc])[:200]}"
        elif m.type == "tool":
            pending.pop(getattr(m, "tool_call_id", None), None)
            line += f" replies_to={getattr(m, 'tool_call_id', None)}"
            line += f"\n          content={str(m.content)[:150]!r}"
        else:
            line += f"\n          content={str(m.content)[:260]!r}"
        print(line)
    print(f"     >>> 悬空 tool_calls: {pending if pending else '无'}")


def main():
    for fn in sorted(glob.glob(".langgraph_api/.langgraph_checkpoint.*.pckl")):
        with open(fn, "rb") as f:
            data = pickle.load(f)
        hits = [k for k in data if isinstance(k, tuple) and k and k[0] == THREAD]
        if not hits:
            continue
        print("#" * 100)
        print(f"# FILE {fn}")
        for k in sorted(hits, key=lambda x: str(x[2])):
            ns, ckpt = k[1], k[2]
            if ns == "":
                continue  # 父图已经看过
            blob = data[k]
            try:
                blob = pickle.loads(blob) if isinstance(blob, (bytes, bytearray)) else blob
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(blob, dict):
                continue
            print("=" * 96)
            print(f"ns={ns} ckpt={ckpt}  (blob type={type(blob).__name__})")
            for item in blob.items() if hasattr(blob, "items") else []:
                _k, v = item
                if not (isinstance(v, tuple) and len(v) >= 4):
                    print(f"  RAWITEM {str(item)[:300]}")
                    continue
                _id, chan, payload, _task = v
                kind = payload[0] if isinstance(payload, tuple) else None
                decoded = decode_payload(*payload) if kind in ("msgpack", "null") else payload
                msgs = to_msgs(decoded) if kind == "msgpack" else None
                if msgs is not None:
                    show(msgs, f"{chan} (task={_task})")
                else:
                    print(f"  {chan} = {str(decoded)[:300]}")


if __name__ == "__main__":
    main()
