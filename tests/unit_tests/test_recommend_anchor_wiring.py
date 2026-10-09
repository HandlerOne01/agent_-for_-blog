"""generate_query 的锚点接线：候选串的优先级、k 的放大、与向量的求交。

为什么单独测接线：`anchor_ids` 本身测得再全，只要 generate_query 传错串、
传错顺序、或者忘了拿向量结果去求交，整条链路照样是坏的
—— 这正是之前踩过的坑（extract_title 写好了却没接进子图，测试全绿但功能从没生效）。
"""
from unittest.mock import patch

from src.agent.node import recommend as R

ROWS = [
    {"id": i, "title": t, "topic": tp, "category": c, "tags": g}
    for i, t, tp, c, g in [
        (64, "八股文面试总结", "八股文", "八股文", ",八股文,"),
        (65, "操作系统", "八股文", "八股文", ",八股文,"),
        (66, "C++", "八股文", "八股文", ",八股文,"),
        (67, "计算机网络", "八股文", "八股文", ",八股文,"),
        (68, "leetcode刷题", "八股文", "八股文", ",八股文,"),
        (30, "AVL树", "C/C++", "C/C++", ",C++,"),
        (34, "C++基础语法", "C/C++", "C/C++", ",C++,"),
        (42, "继承", "C/C++", "C/C++", ",C++,"),
    ]
]


def _state(**kw):
    base = {"messages": [], "topic": None, "category": None, "tags": None,
            "blog_type": None, "others": None, "blog_count": 5,
            "word_count_min": None, "word_count_max": None}
    base.update(kw)
    return base


def _sql_of(state, hits, seen=None, rows=None):
    """跑一次 generate_query，返回拼出来的 SQL。

    关于阈值：`generate_query` 现在有一条规则 —— 向量召回 < VECTOR_MIN_HITS
    就回落数据库检索（LLM 写 SQL），那时就没有 `id IN (...)` 了。
    这个文件测的是**接线**（候选串优先级 / k 放大 / 与向量求交），
    阈值本身另有 test_vector_recall_threshold.py 专测，所以这里各用例
    自己把 hits 给够（给不够的用例会在断言里直接说明）。
    """
    def _fake_search_ids(q, k=None):
        if seen is not None:
            seen["q"] = q
            seen["k"] = k
        return list(hits)

    with patch.object(R, "_anchor_rows", return_value=ROWS if rows is None else rows), \
            patch("src.agent.common.retrieval.search_ids", side_effect=_fake_search_ids):
        out = R.generate_query(state)
    calls = out["messages"][0].tool_calls
    return (calls[0]["args"]["query"] if calls else None)


def _sql_of_with(rows, state, hits):
    """同上，但用指定的行集。"""
    return _sql_of(state, hits, rows=rows)


# --------------------------------------------------------------------------
# 候选串优先级 / 取最紧的锚点
# --------------------------------------------------------------------------
# 生产里算三串，然后取**最紧**（候选最少）的那个非空锚点：
#   1) topic + tags                       （具体主题词都在这两个字段）
#   2) topic + tags + blog_type + category（模型把分类词塞错字段时的补救）
#   3) 五槽全拼                            （兜底）
def test_anchor_uses_topic_phrase_first():
    """只有 topic 时，'八股文' 锚住 5 篇候选。"""
    sql = _sql_of(_state(topic="八股文"), [(64, 0.9), (66, 0.8), (67, 0.7)])
    assert "id IN (64, 66, 67)" in sql, sql


def test_anchor_sees_tags_because_direction_lands_there():
    """实测"C++ 方向"被抽成 tags='C++'（不是 topic）→ 第一串必须包含 tags。"""
    sql = _sql_of(_state(tags="C++"), [(30, 0.9), (34, 0.8), (42, 0.7)])
    assert sql is not None and "id IN (30, 34, 42)" in sql, sql


def test_tightest_anchor_wins_across_candidates():
    """真实抽取 topic='C++'、tags='八股文'：要取**最紧**的锚点，不是第一个。

    第一串 "C++" 能锚住 4 篇（都是 C/C++ 语言笔记）；
    第二串 "C++ 八股文" 求交只有 1 篇 —— 那 1 篇才是用户要的《C++》。
    """
    sql = _sql_of(
        _state(topic="C++", tags="八股文"),
        [(66, 0.83), (30, 0.7), (34, 0.6), (42, 0.5)],
    )
    assert "id IN (66)" in sql, sql


def test_anchor_falls_back_when_subject_landed_in_blog_type():
    """模型把"八股文"塞进 blog_type（实测发生过）→ 第二串把它救回来。"""
    sql = _sql_of(
        _state(topic="C++", blog_type="八股文"),
        [(66, 0.83), (30, 0.7), (34, 0.6), (42, 0.5)],
    )
    assert "id IN (66)" in sql, sql


def test_single_term_anchor_still_used():
    """只有一个词可用时（"Linux 进程" 拆成两个词但各自命中面不同），照样收窄。"""
    sql = _sql_of(_state(topic="八股文", tags="C++"),
                  [(66, 0.9), (64, 0.8), (67, 0.7)])
    assert "id IN (66)" in sql, sql


# --------------------------------------------------------------------------
# 与向量求交
# --------------------------------------------------------------------------
def test_vector_hits_outside_the_anchor_are_dropped():
    """向量挑出来的候选外的文章必须被丢掉 —— 锚点才是范围。"""
    sql = _sql_of(
        _state(topic="八股文"),
        [(64, 0.9), (30, 0.85), (34, 0.8), (66, 0.7)],  # 30/34 不是八股文
    )
    assert "id IN (64, 66)" in sql, sql
    assert "30" not in sql.split("WHERE", 1)[-1], sql


def test_keeps_vector_order_not_anchor_order():
    """候选内按向量分数排，不是按 id 排。"""
    sql = _sql_of(_state(topic="八股文"), [(66, 0.9), (64, 0.8), (67, 0.7)])
    assert "id IN (66, 64, 67)" in sql, sql


def test_k_is_widened_to_cover_the_anchor():
    """k 要开到锚点规模，否则候选里排在 k 名外的会被"没进前 k"误伤。

    用大锚点（12 篇）复现：blog_count=5、RECALL_TOP_K=10，若不放大 k，
    排在 11/12 名的候选永远没机会进结果。
    """
    big = [{"id": 100 + i, "title": "t%d" % i, "topic": "八股文",
            "category": "八股文", "tags": ",八股文,", "blog_type": "面试总结"}
           for i in range(12)]
    seen = {}
    with patch.object(R, "_anchor_rows", return_value=big), patch(
        "src.agent.common.retrieval.search_ids",
        side_effect=lambda q, k=None: (seen.update(k=k), [(100, .9)])[1],
    ):
        R.generate_query(_state(topic="八股文", blog_count=5))
    assert seen.get("k") >= 12, (
        "锚点 12 篇时 k 必须放大到至少 12，实际 %r" % seen.get("k")
    )


def test_first_anchor_phrase_alone_is_not_enough():
    """候选串必须**每条都带 tags**；只靠第一条是不够的（不能把它顶成空串）。

    真实抽取里模型会把分类词放进 tags（"C++ 方向" → tags='C++'），
    所以除了 topic-only 的首串，后面几串都得把 tags 带上。
    """
    # tags 里有 4 篇；topic 里没有能锚住的词
    rows = [
        {"id": 30 + i, "title": "cpp%d" % i, "topic": "C/C++", "category": "C/C++",
         "tags": ",C++,", "blog_type": "语言笔记"}
        for i in range(4)
    ]
    sql = _sql_of_with(rows, _state(tags="C++"),
                       [(30, .9), (31, .8), (32, .7)])
    assert sql is not None, "tags 里的词必须能被锚到，否则会退回纯向量"
    assert "id IN (30, 31, 32)" in sql, sql


def test_falls_back_to_vector_when_anchor_has_no_overlap():
    """向量在候选内一篇都没排上 → 放弃锚点，按全库向量结果走（别交白卷）。"""
    sql = _sql_of(
        _state(topic="八股文"),
        [(30, 0.9), (34, 0.8), (42, 0.7)],  # 全在候选外（42 也在候选外）
    )
    assert "id IN (30, 34, 42)" in sql, sql


# --------------------------------------------------------------------------
# 锚点失效时必须不影响原链路
# --------------------------------------------------------------------------
def test_no_anchor_falls_back_to_plain_vector():
    """锚不住（词太泛/库里没有）→ 原样用向量结果，行为跟加锚点之前一致。"""
    sql = _sql_of(_state(topic="量子计算"),
                  [(1, 0.9), (2, 0.8), (3, 0.7)])
    assert "id IN (1, 2, 3)" in sql, sql


def test_anchor_db_failure_does_not_break_recommend():
    """锚点查库挂掉 → 不许把推荐拖垮，照常走向量。"""
    def _fake_search_ids(q, k=None):
        return [(1, 0.9), (2, 0.8), (3, 0.7)]

    with patch.object(R, "_anchor_rows", side_effect=RuntimeError("db down")), patch(
        "src.agent.common.retrieval.search_ids", side_effect=_fake_search_ids
    ):
        out = R.generate_query(_state(topic="八股文"))
    sql = out["messages"][0].tool_calls[0]["args"]["query"]
    assert "id IN (1, 2, 3)" in sql, sql


def test_anchor_does_not_touch_word_count_conditions():
    """锚点只管 id，字数过滤仍然照旧加在 WHERE 里。"""
    sql = _sql_of(_state(topic="八股文", word_count_min=1000, word_count_max=5000),
                  [(64, 0.9), (66, 0.8), (67, 0.7)])
    where = sql.split("WHERE", 1)[-1]
    assert "word_count >= 1000" in where, sql
    assert "word_count <= 5000" in where, sql
