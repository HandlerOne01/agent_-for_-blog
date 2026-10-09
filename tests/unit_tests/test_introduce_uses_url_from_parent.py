"""父图已经指明是哪一篇时，介绍子图不许再"模糊查标题"。

背景（真实 trace 抓到的）：
  用户：「推荐几篇八股文」→ 推荐出 id=66《C++》
  → need_introduce 预填 target_query='C++'
  → 介绍子图 extract_title 早退（篇名是预填的）
  → locate_article 拿 'C++' 去 find_by_title
  → 那里是 `title LIKE '%C++%'` 模糊匹配 → 命中 3 篇：
        《C++基础语法》/《C++11 并发编程》/《C++》(八股文)
  → 反问用户"你说的是哪一篇"

问题不在"抽错了篇名"，而在**父图明明知道是 id=66，却只传了个会撞名的标题**。
库里 blog_url 是唯一的，用它传递就能精确定位。
"""
from unittest.mock import patch

from src.agent.node import introduce as I

EXACT = {
    "id": 66, "title": "C++", "category": "八股文", "word_count": 110712,
    "difficulty": 2, "blog_url": "/posts/interview/cpp/",
}


class _FakeState(dict):
    pass


def _state(**kw):
    base = {"messages": [], "target_query": None, "target_post": None}
    base.update(kw)
    return _FakeState(base)


def _run(state, by_url=None, by_title=None):
    """跑 locate_article，记录它调了哪条查找路径。"""
    calls = {"url": 0, "title": 0, "vec": 0}

    def _find_by_url(u):
        calls["url"] += 1
        return by_url if by_url is not None else []

    def _find_by_title(t):
        calls["title"] += 1
        return by_title if by_title is not None else []

    def _search_ids(q, k=None):
        calls["vec"] += 1
        return []

    with patch.object(I, "find_by_url", _find_by_url, create=True), \
            patch.object(I, "find_by_title", _find_by_title), \
            patch.object(I, "search_ids", _search_ids), \
            patch.object(I, "interrupt") as itr:
        out = I.locate_article(state)
    return out, calls, itr


# --------------------------------------------------------------------------
# 父图预填了 blog_url（精确）
# --------------------------------------------------------------------------
def test_prefilled_blog_url_resolves_without_fuzzy_title_match():
    """用 URL 精确取那一篇，且**不做**标题模糊匹配、不反问。"""
    out, calls, itr = _run(_state(target_query="/posts/interview/cpp/"),
                           by_url=[dict(EXACT)])
    assert out.get("target_post") == EXACT
    assert calls["url"] == 1, "应当走 URL 精确查找"
    assert calls["title"] == 0, "不该再去模糊查标题（那正是撞名的原因）"
    assert not itr.called, "已经唯一确定，不该反问用户"


def test_url_path_short_circuits_even_when_title_would_ambiguous():
    """URL 命中时，即便标题模糊匹配会命中多篇，也不该走到那一步。"""
    ambiguous = [
        {"id": 34, "title": "C++基础语法", "blog_url": "/posts/c/cpp-jichu/"},
        {"id": 58, "title": "C++11 并发编程", "blog_url": "/posts/cpp11/bingfa/"},
        dict(EXACT),
    ]
    out, calls, itr = _run(_state(target_query="/posts/interview/cpp/"),
                           by_url=[dict(EXACT)], by_title=ambiguous)
    assert out.get("target_post") == EXACT
    assert calls["title"] == 0
    assert not itr.called


def test_url_not_found_falls_back_to_title_search():
    """URL 查不到（比如文章下架）→ 退回原来的标题匹配，不是直接说没找到。"""
    out, calls, itr = _run(_state(target_query="/posts/interview/cpp/"),
                           by_url=[], by_title=[dict(EXACT)])
    assert calls["title"] == 1, "URL 没命中时应当退回标题匹配"
    assert out.get("target_post") == EXACT


# --------------------------------------------------------------------------
# 用户自己点名（没有 URL）时，行为一点不能变
# --------------------------------------------------------------------------
def test_user_named_title_still_uses_title_search():
    """用户说「介绍一下继承这篇文章」→ 仍然走标题匹配，行为不变。"""
    jicheng = {"id": 42, "title": "继承", "blog_url": "/posts/c/jicheng/"}
    out, calls, itr = _run(_state(target_query="继承"),
                           by_url=[], by_title=[jicheng])
    assert calls["url"] == 0, "不是 URL 就不该走 URL 查找"
    assert calls["title"] == 1
    assert out.get("target_post") == jicheng


def test_ambiguous_title_still_asks_the_user():
    """用户点名一个会撞名的词（如「C++」）→ 仍然按原逻辑反问，这是对的。

    区别在于：这种撞名是**用户自己**造成的，反问是合理的；
    父图预填 URL 那种撞名是系统自己造成的，不该反问。
    """
    rows = [
        {"id": 34, "title": "C++基础语法", "category": "C/C++", "word_count": 9018},
        {"id": 66, "title": "C++", "category": "八股文", "word_count": 110712},
    ]
    with patch.object(I, "find_by_title", lambda t: rows), \
            patch.object(I, "interrupt", return_value="2") as itr:
        out = I.locate_article(_state(target_query="C++"))
    assert itr.called, "用户自己的撞名应当反问"
    assert out.get("target_post", {}).get("id") == 66
