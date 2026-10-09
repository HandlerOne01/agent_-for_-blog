"""定位：用户报篇名 → 一个确定 post_id。不读正文、不调 LLM。"""

from unittest.mock import patch

from src.agent.node.introduce import locate_article


def _state(q="继承"):
    return {"messages": [], "target_query": q}


def test_locate_single_hit_skips_vector():
    """标题精确命中 1 篇 → 不再查向量（省一次 embedding 调用）。"""
    with patch(
        "src.agent.node.introduce.find_by_title",
        return_value=[{"id": 42, "title": "继承"}],
    ) as f, patch("src.agent.node.introduce.search_ids") as s:
        out = locate_article(_state())
    assert out["target_post"]["id"] == 42
    s.assert_not_called()
    f.assert_called_once()


def test_locate_multi_hit_interrupts():
    """命中多篇 → 反问用户，绝不自己猜。用户回篇名也要能选中。"""
    rows = [{"id": 42, "title": "继承"}, {"id": 66, "title": "C++"}]
    with patch("src.agent.node.introduce.find_by_title", return_value=rows), patch(
        "src.agent.node.introduce.interrupt", return_value="继承"
    ) as it:
        out = locate_article(_state("继承"))
    it.assert_called_once()
    assert out["target_post"]["id"] == 42


def test_locate_multi_hit_accepts_index():
    """用户回序号也要能选中。"""
    rows = [{"id": 42, "title": "继承"}, {"id": 66, "title": "C++"}]
    with patch("src.agent.node.introduce.find_by_title", return_value=rows), patch(
        "src.agent.node.introduce.interrupt", return_value="2"
    ):
        out = locate_article(_state("继承"))
    assert out["target_post"]["id"] == 66


def test_locate_fallback_vector_uses_top1():
    """标题 0 命中 → 回落向量，采用 top1。"""
    with patch("src.agent.node.introduce.find_by_title", return_value=[]), patch(
        "src.agent.node.introduce.search_ids", return_value=[(42, 0.686)]
    ), patch(
        "src.agent.node.introduce.fetch_post_meta",
        return_value={"id": 42, "title": "继承"},
    ):
        out = locate_article(_state("那篇讲继承的"))
    assert out["target_post"]["id"] == 42


def test_locate_vector_ambiguous_interrupts():
    """向量前两名分数接近 → 反问。"""
    with patch("src.agent.node.introduce.find_by_title", return_value=[]), patch(
        "src.agent.node.introduce.search_ids", return_value=[(42, 0.62), (66, 0.61)]
    ), patch(
        "src.agent.node.introduce.fetch_post_meta",
        return_value={"id": 42, "title": "继承"},
    ), patch(
        "src.agent.node.introduce.interrupt", return_value="1"
    ) as it:
        locate_article(_state("继承那个"))
    it.assert_called_once()


def test_locate_not_found_sets_no_target():
    """两条路都空 → 不设 target_post，让图给出「没这篇」的答复。"""
    with patch("src.agent.node.introduce.find_by_title", return_value=[]), patch(
        "src.agent.node.introduce.search_ids", return_value=[]
    ):
        out = locate_article(_state("不存在的文章"))
    assert not out.get("target_post")


def test_extract_title_degrades_to_empty_query():
    """篇名抽取失败降级成空篇名，不冒 traceback。

    为什么是"吞掉"而不是"让框架重试"：extract_title **自己就是图节点**，
    异常抛出去重试耗尽后没人接，用户会看到 traceback。
    （代价：RetryPolicy 对本节点基本不生效 —— 见 ledger 的裁定。）
    """
    with patch("src.agent.node.introduce.model_deterministic") as m:
        m.with_structured_output.return_value.invoke.side_effect = RuntimeError("boom")
        from src.agent.node.introduce import extract_title

        out = extract_title({"messages": []})
    assert out["target_query"] == ""


def test_locate_article_without_query_sets_no_target():
    """没篇名（用户没点名或抽取失败）→ 不设 target_post，交给 compose_intro 说人话。"""
    out = locate_article({"messages": []})
    assert not out.get("target_post")


def test_locate_article_never_raises_when_db_down():
    """连库失败必须降级成人设化提示，不许抛。"""
    from src.agent.common.db import DBUnavailable

    with patch(
        "src.agent.node.introduce.find_by_title", side_effect=DBUnavailable("boom")
    ):
        out = locate_article(_state("继承"))
    assert out.get("messages"), "降级必须产出一条人设化提示"
    assert out["introduce_failed"], "子图条件边必须收到失败标记才能停止后续生成"


def test_ask_which_reprompts_once_then_accepts():
    """用户回「第二个」这种识别不了的答案时，要再问一次，而不是谎报「没这篇」。

    静默放弃会让用户被告知"没这篇"，而文章其实找到了（与成功标准 5 冲突）。
    """
    rows = [{"id": 42, "title": "继承"}, {"id": 66, "title": "C++"}]
    with patch("src.agent.node.introduce.find_by_title", return_value=rows), patch(
        "src.agent.node.introduce.interrupt", side_effect=["第二个", "继承"]
    ) as it:
        out = locate_article(_state("继承"))
    assert it.call_count == 2
    assert out["target_post"]["id"] == 42


def test_locate_escapes_like_wildcards():
    """用户输入含 % 或 _ 时必须按字面匹配，否则会全表命中。"""
    from src.agent.common.content import escape_like

    assert escape_like("100%") == "100\\%"
    assert escape_like("a_b") == "a\\_b"
