"""没提字数要求就不该筛字数。

背景：用户说"推荐几篇八股文"、被问篇幅时答「不提供」，原来的代码硬塞
word_count_min=500 / word_count_max=5000 —— 结果 12 篇 category='八股文' 的文章里
只有 1 篇（3923 字）落在区间内，用户会觉得"怎么只有一篇"。
现在改成：**没提就不筛**（两个字段保持 None，SQL 里不加字数条件）。
"""
from unittest.mock import patch

from langchain_core.messages import HumanMessage

from src.agent.node import recommend as R


def _slots(**kw):
    class _Info:
        def model_dump(self, exclude_none=True):
            return dict(kw)

    return _Info()


def test_decline_keeps_word_count_unset():
    """回答「不提供」→ 不该塞默认字数。"""
    state = {"messages": [HumanMessage(content="推荐几篇八股文")],
             "user_preferences": {}}
    with patch.object(R, "interrupt", return_value="不提供"), patch.object(
        R, "model_deterministic"
    ) as m, patch.object(R, "get_user_id", return_value="u"):
        m.with_structured_output.return_value.invoke.return_value = _slots(topic="八股文")
        out = R.collect_user_info(state, runtime=None, store=_FakeStore())

    assert out.get("word_count_min") is None, (
        "用户没提字数、也答了「不提供」，不该塞默认下限，实际 %r" % out.get("word_count_min")
    )
    assert out.get("word_count_max") is None, (
        "同上，不该塞默认上限，实际 %r" % out.get("word_count_max")
    )


def test_explicit_word_count_is_kept():
    """用户明确给了字数 → 原样保留（别把这条一起改坏）。"""
    state = {"messages": [HumanMessage(content="推荐几篇短的，1000 字以内")],
             "user_preferences": {}}
    with patch.object(R, "interrupt", return_value="不提供"), patch.object(
        R, "model_deterministic"
    ) as m, patch.object(R, "get_user_id", return_value="u"):
        m.with_structured_output.return_value.invoke.return_value = _slots(
            topic="八股文", word_count_max=1000
        )
        out = R.collect_user_info(state, runtime=None, store=_FakeStore())

    assert out.get("word_count_max") == 1000


def test_vector_path_omits_word_count_clauses_when_unset():
    """没字数时，向量拼出的 SQL 里不该出现 word_count 条件。"""
    import re

    with patch.object(R, "_anchor_rows", return_value=[]), patch(
        "src.agent.common.retrieval.search_ids", return_value=[(1, 0.9), (2, 0.8), (3, 0.7)]
    ):
        out = R.generate_query({
            "messages": [], "topic": "八股文", "category": None, "tags": None,
            "blog_type": None, "others": None, "blog_count": 5,
            "word_count_min": None, "word_count_max": None,
        })
    sql = out["messages"][0].tool_calls[0]["args"]["query"]
    # 注意 word_count 是"五列约定"里的选列名，必然出现在 SELECT 里 ——
    # 要查的是 WHERE 之后有没有用它做条件
    where = sql.split("WHERE", 1)[-1]
    assert "word_count" not in where, (
        "没字数要求时不该拿 word_count 当过滤条件：%s" % where
    )
    m = re.search(r"id IN \(([^)]*)\)", sql)
    assert m and len(m.group(1).split(",")) >= 2, "应当召回多篇：%s" % sql


class _FakeStore:
    """最小 store 桩：没有历史偏好。"""

    def search(self, namespace):
        return []

    def put(self, *a, **kw):
        return None
