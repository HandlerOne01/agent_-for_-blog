"""向量召回不足 3 篇时，必须回落到「数据库检索」（LLM 写 SQL）那条路。

设计意图（用户明确）：
    推荐节点先向量召回；**召回少于 3 篇**（或失败）才进行数据库检索。
    向量召回 >= 3 篇 → 直接按向量结果走。

为什么需要这条：原来只判 `if hits:`，哪怕只召回 1 篇也算命中、直接早返回，
于是用户要 5 篇却只拿到 1 篇，而且那条"数据库检索"（多列 LIKE 那种）永远走不到。

同时钉住：向量路径产出的 SQL 必须带 `status = 1` —— 草稿/下架文章不该被推荐。
"""
from unittest.mock import patch

from src.agent.node import recommend as R

ROWS = [
    {"id": i, "title": "t%d" % i, "topic": "八股文", "category": "八股文",
     "tags": ",八股文,", "blog_type": "面试总结"}
    for i in (64, 65, 66, 67, 68)
]


def _state(**kw):
    base = {"messages": [], "topic": "八股文", "category": None, "tags": None,
            "blog_type": None, "others": None, "blog_count": 5,
            "word_count_min": None, "word_count_max": None}
    base.update(kw)
    return base


def _run(hits, state=None):
    """跑 generate_query，返回 (走向量了吗, SQL)。hits=None/异常 表示召回失败。"""
    def _fake_search_ids(q, k=None):
        if isinstance(hits, Exception):
            raise hits
        return hits

    with patch.object(R, "_anchor_rows", return_value=ROWS), patch(
        "src.agent.common.retrieval.search_ids", side_effect=_fake_search_ids
    ), patch.object(R, "model_deterministic") as m, \
            patch.object(R, "_tool", return_value=object()), \
            patch.object(R, "get_db") as db:
        db.return_value.dialect = "mysql"
        # 落到 LLM 那条路时，节点会 bind_tools 再 invoke；
        # 返回一个带标记的字符串，用来判断"走了哪条路"。
        m.bind_tools.return_value.invoke.return_value = "LLM_SQL_SENTINEL"
        out = R.generate_query(state or _state())

    msg = out["messages"][0]
    if isinstance(msg, str):
        return False, msg          # 落到 LLM 那条路
    calls = getattr(msg, "tool_calls", None) or []
    return True, (calls[0]["args"]["query"] if calls else None)


# --------------------------------------------------------------------------
# 阈值：< 3 篇 → 数据库检索
# --------------------------------------------------------------------------
def test_zero_hits_falls_back_to_database_retrieval():
    """0 篇 → 数据库检索。"""
    went_vector, sql = _run([])
    assert not went_vector, "0 篇必须回落到 LLM 写 SQL"


def test_one_hit_falls_back_to_database_retrieval():
    """1 篇 → 也算"少了"，回落数据库检索（原来这里会早返回）。"""
    went_vector, sql = _run([(64, 0.66)])
    assert not went_vector, "只召回 1 篇不该直接早返回"


def test_two_hits_fall_back_to_database_retrieval():
    """2 篇 → 仍然"少了"，回落数据库检索。"""
    went_vector, sql = _run([(64, 0.66), (66, 0.60)])
    assert not went_vector, "只召回 2 篇不该直接早返回"


def test_three_hits_use_the_vector_path():
    """3 篇 → 够了，按向量走（边界值）。"""
    went_vector, sql = _run([(64, 0.66), (66, 0.60), (67, 0.55)])
    assert went_vector, "3 篇就该按向量走"
    assert "id IN (64, 66, 67)" in sql, sql


def test_more_hits_use_the_vector_path():
    went_vector, sql = _run([(64, .9), (65, .8), (66, .7), (67, .6), (68, .5)])
    assert went_vector
    assert "id IN (64, 65, 66, 67, 68)" in sql, sql


def test_index_failure_falls_back_to_database_retrieval():
    """召回接口挂掉（None / 抛异常）→ 数据库检索。"""
    went_vector, _ = _run(None)
    assert not went_vector
    went_vector, _ = _run(RuntimeError("embedding down"))
    assert not went_vector, "召回抛异常也必须回落到 LLM 写 SQL"


# --------------------------------------------------------------------------
# 向量路径的 SQL 也必须带 status = 1
# --------------------------------------------------------------------------
def test_vector_sql_excludes_unpublished():
    """草稿/下架文章不该被推荐 —— status = 1 是硬条件。"""
    went_vector, sql = _run([(64, .9), (66, .8), (67, .7)])
    assert went_vector
    assert "status = 1" in sql, "向量路径的 SQL 缺少 status 过滤：%s" % sql


def test_fallback_path_keeps_word_count_conditions():
    """字数条件照旧只在本轮显式提过时才加。"""
    _, sql = _run([(64, .9), (66, .8), (67, .7)],
                  _state(word_count_min=1000, word_count_max=5000))
    assert "word_count >= 1000" in sql and "word_count <= 5000" in sql, sql

    _, sql2 = _run([(64, .9), (66, .8), (67, .7)])
    where = sql2.split("WHERE", 1)[-1]
    assert "word_count" not in where, where
