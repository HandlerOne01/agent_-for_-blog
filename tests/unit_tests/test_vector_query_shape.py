"""向量召回的查询串必须重复一遍再喂给检索。

为什么：用户只说一个分类名时（"推荐几篇八股文"→ topic="八股文"），
单次出现的查询在向量空间里"锚点太弱" —— 实测 top1 只有 0.562，刚好过
0.50 的门，但 max(0.45, top1-0.15)=0.45 的尾巴把其余候选全砍掉，只召回 1 篇。
同一串重复一遍：top1 升到 0.668，召回 1 篇 → 5 篇；"Linux" 从 0 篇变 5+ 篇。
"""
from unittest.mock import patch

from src.agent.node.recommend import generate_query


def _state(**kw):
    base = {"messages": [], "topic": None, "category": None, "tags": None,
            "blog_type": None, "others": None, "blog_count": 5}
    base.update(kw)
    return base


def test_vector_query_is_duplicated_for_short_queries():
    """短查询（单个分类名）要重复一遍，否则只召回 1 篇。"""
    seen = {}

    def _fake_search_ids(q, k=None):
        seen["q"] = q
        return [(64, 0.668)]

    with patch("src.agent.common.retrieval.search_ids", side_effect=_fake_search_ids):
        generate_query(_state(topic="八股文"))

    assert seen.get("q") == "八股文 八股文", (
        "喂给向量的查询串应当重复一遍，实际 %r —— 单次出现会因锚点太弱只召回 1 篇"
        % (seen.get("q"),)
    )


def test_duplication_applies_to_multi_slot_queries_too():
    """多个槽位拼出来的查询同样重复一遍（保持行为一致）。

    注意查询串里可能出现连续空格（空槽位也参与 join），比较时归一化。
    """
    seen = {}

    def _fake_search_ids(q, k=None):
        seen["q"] = q
        return []

    with patch("src.agent.common.retrieval.search_ids", side_effect=_fake_search_ids):
        generate_query(_state(topic="Linux", tags="进程"))

    got = " ".join(str(seen.get("q", "")).split())
    assert got == "Linux 进程 Linux 进程", got


def test_no_query_means_no_vector_call():
    """一个槽位都没有 → 不去查向量（查询串为空时重复也无意义）。

    只断言"没调 search_ids"这一件事 —— 后半段会走 LLM 写 SQL 的分支，
    那是另一个测试的地盘，这里不去碰真模型。
    """
    with patch("src.agent.common.retrieval.search_ids") as s, patch(
        "src.agent.node.recommend.model_deterministic"
    ) as m:
        m.bind_tools.return_value.invoke.return_value = "FAKE"
        generate_query(_state())
    s.assert_not_called()
