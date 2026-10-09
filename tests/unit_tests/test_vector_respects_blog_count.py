"""向量路径必须尊重 blog_count —— 用户说"几篇"就不能给更多。

背景（真机截图 + 真库复现）：
    用户：「推荐几篇 Linux 的文章」→ blog_count=5
    SQL ：WHERE status = 1 AND id IN (91, 96, 79, 86, 80, 69)   ← 6 个 id
    实际返回 6 篇，第 6 条（linux服务器）多出来了。

为什么会多：k 取的是
    max(blog_count, RECALL_TOP_K[, len(anchor)])
取 max 是故意的（多给向量一些候选，别把好的挤在 k 名之外），
但 SQL 没有 LIMIT、`_collect_posts` 也不截断，
于是 hits 比 blog_count 多多少就多返回多少。
"""
import sqlite3
from unittest.mock import patch

from src.agent.node import recommend as R

ROWS = [
    {"id": i, "title": "t%d" % i, "topic": "八股文", "category": "八股文",
     "tags": ",八股文,", "blog_type": "面试总结"}
    for i in range(64, 76)          # 12 篇
]


def _state(**kw):
    base = {"messages": [], "topic": "八股文", "category": None, "tags": None,
            "blog_type": None, "others": None, "blog_count": 5,
            "word_count_min": None, "word_count_max": None}
    base.update(kw)
    return base


def _ids_of(state, hits):
    with patch.object(R, "_anchor_rows", return_value=ROWS), patch(
        "src.agent.common.retrieval.search_ids", return_value=list(hits)
    ):
        out = R.generate_query(state)
    calls = getattr(out["messages"][0], "tool_calls", None) or []
    if not calls:
        return None
    sql = calls[0]["args"]["query"]
    # Execute the SQL: IN-list order and size do not establish result order/size.
    with sqlite3.connect(":memory:") as db:
        db.create_function("FIELD", -1, lambda value, *ids: ids.index(value) + 1 if value in ids else 0)
        db.execute("CREATE TABLE blog (id INTEGER PRIMARY KEY, title, category, word_count, difficulty, blog_url, status)")
        db.executemany("INSERT INTO blog VALUES (?, ?, '', 100, 1, '', 1)",
                       [(i, str(i)) for i, _ in hits])
        return [int(row[0]) for row in db.execute(sql)]


def _hits(n):
    return [(64 + i, 0.9 - i * 0.01) for i in range(n)]


# --------------------------------------------------------------------------
# 截断
# --------------------------------------------------------------------------
def test_vector_path_caps_to_blog_count():
    """召回 6 篇、用户要 5 篇 → 只返回 5 篇（本 bug 的直接复现）。"""
    ids = _ids_of(_state(topic="八股文", blog_count=5), _hits(6))
    assert ids is not None, "应当走向量路径"
    assert len(ids) == 5, "blog_count=5 时不该返回 %d 篇：%s" % (len(ids), ids)
    assert ids == [64, 65, 66, 67, 68], ids


def test_vector_path_keeps_the_top_ranked_ones():
    """截断要保留向量排序的前 N 篇，不是随便砍。"""
    ids = _ids_of(_state(topic="八股文", blog_count=3), _hits(6))
    assert ids == [64, 65, 66], ids


def test_result_order_matches_relevance_not_primary_key():
    ids = _ids_of(_state(blog_count=3), [(68, 0.9), (64, 0.8), (66, 0.7)])
    assert ids == [68, 64, 66]


def test_vector_path_does_not_pad_when_fewer_hits():
    """召回本来就少于 blog_count → 有几篇给几篇，不许凑数。"""
    ids = _ids_of(_state(topic="八股文", blog_count=5), _hits(4))
    assert ids == [64, 65, 66, 67], ids


def test_blog_count_one_is_respected():
    """要 1 篇就给 1 篇（边界）。"""
    ids = _ids_of(_state(topic="八股文", blog_count=1), _hits(6))
    assert ids == [64], ids


def test_missing_blog_count_does_not_truncate_to_zero():
    """blog_count 缺失/为 0 时不能把结果截成空。

    这时 `k` 走的是 RECALL_TOP_K 兜底，截断也必须跳过 ——
    截成 0 篇比多给几篇糟得多。
    """
    for bc in (None, 0):
        ids = _ids_of(_state(topic="八股文", blog_count=bc), _hits(6))
        assert ids, "blog_count=%r 时不该把结果全截没" % (bc,)


def test_truncation_happens_after_anchor_narrowing():
    """锚点已把范围收到 2 篇、用户要 5 篇 → 不该被"补"到 5 篇，也不该被截成 0。"""
    rows = [
        {"id": 66, "title": "C++", "topic": "八股文", "category": "八股文",
         "tags": ",八股文,C++,", "blog_type": "面试总结"},
        {"id": 64, "title": "八股文面试总结", "topic": "八股文", "category": "八股文",
         "tags": ",八股文,C++,", "blog_type": "面试总结"},
    ]
    with patch.object(R, "_anchor_rows", return_value=rows), patch(
        "src.agent.common.retrieval.search_ids",
        return_value=[(66, 0.9), (64, 0.8), (99, 0.7)],   # 99 不在锚点内，会被丢掉
    ):
        out = R.generate_query(_state(topic="八股文 C++", blog_count=5))
    calls = out["messages"][0].tool_calls
    sql = calls[0]["args"]["query"]
    assert "id IN (66, 64)" in sql, sql
