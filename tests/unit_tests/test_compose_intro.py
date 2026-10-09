"""分档 + 校验：三个档位的边界、编造链接/禁用词必须退模板。"""

from unittest.mock import patch

from src.agent.node.introduce import (
    compose_intro,
    pick_tier,
    template_intro,
    validate_intro,
)

POST = {
    "id": 42,
    "title": "继承",
    "category": "C/C++",
    "word_count": 21242,
    "difficulty": 2,
    "blog_url": "https://imiss.example.com/posts/cpp/jicheng/",
}


def test_tier_boundaries():
    assert pick_tier(0) == "short"
    assert pick_tier(5000) == "short"  # 闭区间上界归短文
    assert pick_tier(5001) == "medium"
    assert pick_tier(50000) == "medium"
    assert pick_tier(50001) == "long"


def test_template_intro_uses_db_values_verbatim():
    text = template_intro(POST, body_missing=False)
    assert "继承" in text
    assert "/posts/cpp/jicheng/" in text  # 域名已剥离
    assert "imiss.example.com" not in text
    assert "您" not in text


def test_validate_rejects_foreign_link():
    bad = "看这篇 /posts/cpp/OTHER/ 吧"
    assert any("链接" in p for p in validate_intro(bad, POST))


def test_validate_rejects_you_and_markdown():
    assert validate_intro("您可以看 **这篇**", POST)


def test_validate_accepts_good_text():
    good = "《继承》这篇讲的是……\n/posts/cpp/jicheng/\n就这些。"
    assert validate_intro(good, POST) == []


def test_compose_validate_rejects_then_fallback():
    """LLM 编了链接 → 必须退模板，且模板里的链接来自数据库。"""
    with patch("src.agent.node.introduce.model") as m:
        m.invoke.return_value.content = "看这个 /posts/cpp/WRONG/ 吧"
        out = compose_intro(
            {"messages": [], "target_post": POST, "post_content": "x" * 100}
        )
    text = out["messages"][0].content
    assert "/posts/cpp/jicheng/" in text
    assert "WRONG" not in text


def test_compose_llm_failure_falls_back():
    with patch("src.agent.node.introduce.model") as m:
        m.invoke.side_effect = RuntimeError("boom")
        out = compose_intro(
            {"messages": [], "target_post": POST, "post_content": "x" * 100}
        )
    assert "/posts/cpp/jicheng/" in out["messages"][0].content


def test_compose_body_missing_says_so():
    out = compose_intro(
        {
            "messages": [],
            "target_post": POST,
            "post_content": "",
            "body_missing": True,
        }
    )
    assert "摘要" in out["messages"][0].content


def test_compose_no_target_says_not_found():
    out = compose_intro({"messages": [], "target_post": {}})
    assert "没定位到" in out["messages"][0].content


def test_template_intro_never_invents_a_reason():
    """兜底模板不许编"正文太长"这种理由。

    它只在"模型失败 / 校验没过"时启用，与文章长短无关。
    曾经一篇 4056 字的短文被说成"正文太长，一句两句说不完" —— 那是假话。
    """
    from src.agent.node.introduce import template_intro

    post = {"title": "HTTP 协议", "category": "Linux 网络", "word_count": 4056,
            "blog_url": "https://imiss.example.com/posts/linux-net/http-xieyi/"}
    text = template_intro(post, False)
    assert "太长" not in text, "模板不该声称正文太长（那是编的理由）"
    assert "HTTP 协议" in text and "/posts/linux-net/http-xieyi/" in text
    assert "imiss.example.com" not in text
