"""子图装配：节点齐全、顺序正确，且主图能路由到它。

节点保持三个 —— 定位 / 取正文 / 说话。"抽篇名"是定位的第一步（内部调用），
不单独成节点：多一个框只是噪声，还让"抽完忘了用"这类装配错误有机可乘。
"""


def test_subgraph_has_three_nodes():
    """子图只暴露三个节点，别把内部步骤也注册成节点。"""
    from src.agent.introduce import introduce_graph

    names = {n for n in introduce_graph.get_graph().nodes if not n.startswith("__")}
    assert names == {"locate_article", "fetch_content", "compose_intro"}, names


def test_subgraph_runs_end_to_end_without_injected_state():
    """跑真图：从一句人话走到"定位到 42"。

    **这条测试是这个模块最重要的回归网**。它不手工注入 target_query，
    而是让图自己抽篇名 —— 历史上"抽篇名"曾经是个独立节点却没接线，
    导致功能整体静默失效，而所有手工构造 state 的单测都发现不了。
    """
    from unittest.mock import patch

    from langchain_core.messages import HumanMessage

    from src.agent.introduce import introduce_graph

    class _T:
        query = "继承"

    post = {
        "id": 42,
        "title": "继承",
        "category": "C/C++",
        "word_count": 21242,
        "difficulty": 2,
        "blog_url": "https://x/posts/cpp/jicheng/",
    }
    with patch("src.agent.node.introduce.model_deterministic") as m, patch(
        "src.agent.node.introduce.find_by_title", return_value=[post]
    ), patch(
        "src.agent.node.introduce.fetch_content",
        return_value=dict(post, content="正文" * 50, toc=None, content_len=100),
    ), patch("src.agent.node.introduce.model") as gen:
        m.with_structured_output.return_value.invoke.return_value = _T()
        gen.invoke.side_effect = RuntimeError("LLM 不可用 → 走模板兜底")
        out = introduce_graph.invoke(
            {"messages": [HumanMessage(content="介绍一下继承这篇文章")]}
        )

    assert out.get("target_query") == "继承", "篇名没有被抽出来"
    assert (out.get("target_post") or {}).get("id") == 42, "没有定位到 42"
    text = str(out["messages"][-1].content)
    assert "继承" in text
    assert "/posts/cpp/jicheng/" in text


def test_main_graph_routes_introduce_intent():
    from src.agent.graph import router_message

    assert router_message({"user_intent": "introduce_blog"}) == "introduce_graph"


def test_intent_literal_includes_introduce():
    from src.agent.node.main import UserMessage

    field = UserMessage.model_fields["type"]
    assert "introduce_blog" in str(field.annotation)


def test_main_graph_registers_introduce_node():
    """主图必须真的把子图挂上，否则路由指向一个不存在的节点。"""
    from src.agent.graph import graph

    assert "introduce_graph" in set(graph.get_graph().nodes)


def test_graph_returns_template_after_bounded_generation_retries():
    """整图最多调用生成模型三次，耗尽后仍正常结束并返回模板。"""
    from unittest.mock import patch

    from src.agent.introduce import introduce_graph

    post = {"id": 42, "title": "继承", "blog_url": "/posts/cpp/jicheng/"}
    with patch("src.agent.node.introduce.find_by_title", return_value=[post]), patch(
        "src.agent.node.introduce.fetch_content", return_value=dict(post, content="正文")
    ), patch("src.agent.node.introduce.model") as model, patch("time.sleep"):
        model.invoke.side_effect = TimeoutError("temporary")
        out = introduce_graph.invoke({"messages": [], "target_query": "继承"})
    assert model.invoke.call_count == 3
    assert "没能好好讲" in out["messages"][-1].content
    assert "/posts/cpp/jicheng/" in out["messages"][-1].content
