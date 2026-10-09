"""Regression coverage for per-turn state, failure exits and generation retries."""
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from openai import APIConnectionError, BadRequestError, InternalServerError, RateLimitError

from src.agent.common.db import DBUnavailable
from src.agent.common.reply import DB_UNAVAILABLE_REPLY
from src.agent.graph import builder
from src.agent.introduce import introduce_graph
from src.agent.node import introduce as I
from src.agent.node import recommend as R


POST_A = dict(id=1, title="Article A", category="Python", word_count=100,
              difficulty=1, blog_url="/posts/a/")
POST_B = dict(POST_A, id=2, title="Article B", blog_url="/posts/b/")


@pytest.fixture
def intro_dependencies():
    with patch.object(I, "model_deterministic") as extractor, patch.object(
        I, "find_by_title", side_effect=lambda q: [POST_A if q == "Article A" else POST_B]
    ), patch.object(I, "find_by_url", return_value=[POST_B]), patch.object(
        I, "fetch_content", side_effect=lambda pid: dict(
            POST_A if pid == 1 else POST_B, content="article body", toc=None, content_len=12
        )
    ), patch.object(I, "model") as generator:
        extractor.with_structured_output.return_value.invoke.side_effect = [
            SimpleNamespace(query="Article A"), SimpleNamespace(query="Article B")
        ]
        generator.invoke.side_effect = lambda messages: AIMessage(
            content="Article A /posts/a/" if "标题（必须原样写进回答里）：Article A" in messages[0].content
            else "Article B /posts/b/"
        )
        yield


def test_new_turn_changes_article_and_preserves_history(intro_dependencies):
    with patch("src.agent.node.main.model_deterministic") as intent:
        intent.with_structured_output.return_value.invoke.return_value = SimpleNamespace(type="introduce_blog")
        graph = builder.compile(checkpointer=InMemorySaver(), store=InMemoryStore())
        config = {"configurable": {"thread_id": "two-articles"}}
        graph.invoke({"messages": [HumanMessage(content="Introduce Article A")]}, config)
        first_checkpoint = graph.get_state(config).config
        result = graph.invoke({"messages": [HumanMessage(content="Introduce Article B")]}, config)
    assert result["target_query"] == "Article B"
    assert result["messages"][-1].content == "Article B /posts/b/"
    assert [m.content for m in result["messages"][:2]] == [
        "Introduce Article A", "Article A /posts/a/"
    ]
    assert graph.get_state(first_checkpoint).values["target_query"] == "Article A"


@pytest.mark.parametrize("answer", ["2", "不用"])
def test_resume_keeps_selection_or_ends_on_decline(answer, intro_dependencies):
    graph = builder.compile(checkpointer=InMemorySaver(), store=InMemoryStore())
    config = {"configurable": {"thread_id": "selection"}}
    graph.update_state(config, {
        "messages": [HumanMessage(content="Recommend articles")],
        "target_query": "/posts/a/", "recommended_posts": [POST_A, POST_B],
        "recommend_failed": False,
    }, as_node="recommended_graph")
    paused = graph.invoke(None, config)
    assert paused["__interrupt__"]
    result = graph.invoke(Command(resume=answer), config)
    if answer == "2":
        assert result["target_query"] == "/posts/b/"
        assert result["messages"][-1].content == "Article B /posts/b/"
    else:
        assert not result.get("target_query")
        assert not any(m.type == "ai" for m in result["messages"])
    assert not graph.get_state(config).next


def test_new_recommendation_retries_database_after_previous_failure():
    with patch("src.agent.node.main.model_deterministic") as intent, patch.object(
        R, "model_deterministic"
    ) as extractor, patch.object(R, "_tool", side_effect=DBUnavailable("offline")) as database:
        intent.with_structured_output.return_value.invoke.return_value = SimpleNamespace(type="recommend_blog")
        extractor.with_structured_output.return_value.invoke.return_value = R.UserInfo(topic="Python", blog_count=1)
        graph = builder.compile(checkpointer=InMemorySaver(), store=InMemoryStore())
        config = {"configurable": {"thread_id": "recover-recommendation"}}
        graph.invoke({"messages": [HumanMessage(content="Recommend Python")]}, config)
        result = graph.invoke({"messages": [HumanMessage(content="Try again")]}, config)
    assert database.call_count == 2
    assert result["messages"][-1].type == "ai"
    assert result["messages"][-1].content == DB_UNAVAILABLE_REPLY
    assert result["recommended_posts"] == []


@pytest.mark.parametrize("failure", ["title", "url", "content", "missing_row", "vector", "vector_tie"])
def test_introduction_failure_stops_without_generating(failure):
    query = "/posts/a/" if failure == "url" else "Article A"
    with patch.object(I, "find_by_title", return_value=[POST_A]) as title, patch.object(
        I, "find_by_url", side_effect=DBUnavailable("offline")
    ), patch.object(I, "fetch_content", side_effect=DBUnavailable("offline")) as content, patch.object(
        I, "search_ids", return_value=[(1, 0.9)] if failure == "vector" else [(1, 0.9), (2, 0.89)]
    ), patch.object(I, "fetch_post_meta", side_effect=DBUnavailable("offline")), patch.object(I, "model") as model:
        if failure == "title":
            title.side_effect = DBUnavailable("offline")
        if failure.startswith("vector"):
            title.return_value = []
        if failure == "missing_row":
            content.side_effect = None
            content.return_value = None
        out = introduce_graph.invoke({"messages": [HumanMessage(content=query)], "target_query": query})
    assert [m.content for m in out["messages"][1:]] == [DB_UNAVAILABLE_REPLY]
    model.invoke.assert_not_called()


def test_new_introduction_clears_failure_and_old_body(intro_dependencies):
    out = introduce_graph.invoke({
        "messages": [HumanMessage(content="Introduce Article B")],
        "target_query": "/posts/b/", "introduce_failed": True,
        "target_post": POST_A, "post_content": "old body", "body_missing": True,
    })
    assert out["messages"][-1].content == "Article B /posts/b/"
    assert not out["introduce_failed"]


def test_missing_article_does_not_reuse_old_post():
    with patch.object(I, "find_by_title", return_value=[]), patch.object(
        I, "search_ids", return_value=[]
    ), patch.object(I, "fetch_content") as fetch, patch.object(I, "model") as model:
        out = introduce_graph.invoke({
            "messages": [HumanMessage(content="Missing")], "target_query": "Missing",
            "target_post": POST_A, "post_content": "old body",
        })
    assert "没定位到" in out["messages"][-1].content
    fetch.assert_not_called()
    model.invoke.assert_not_called()


REQUEST = httpx.Request("POST", "https://example.invalid")


@pytest.mark.parametrize("error", [
    TimeoutError("timeout"), ConnectionError("offline"),
    APIConnectionError(request=REQUEST),
    RateLimitError("busy", response=httpx.Response(429, request=REQUEST), body=None),
    InternalServerError("busy", response=httpx.Response(503, request=REQUEST), body=None),
])
def test_transient_generation_failure_recovers(error):
    with patch.object(I, "model") as model, patch("time.sleep"):
        model.invoke.side_effect = [error, AIMessage(content="Article A recovered /posts/a/")]
        out = I.compose_intro({"target_post": POST_A, "post_content": "body"})
    assert out["messages"][0].content == "Article A recovered /posts/a/"


def test_retry_exhaustion_returns_template():
    with patch.object(I, "model") as model, patch("time.sleep"):
        model.invoke.side_effect = TimeoutError("timeout")
        out = I.compose_intro({"target_post": POST_A, "post_content": "body"})
    assert model.invoke.call_count == 3
    assert "没能好好讲" in out["messages"][0].content
    assert "/posts/a/" in out["messages"][0].content


def test_nontransient_generation_failure_is_not_retried():
    with patch.object(I, "model") as model:
        model.invoke.side_effect = BadRequestError(
            "invalid", response=httpx.Response(400, request=REQUEST), body=None
        )
        out = I.compose_intro({"target_post": POST_A, "post_content": "body"})
    assert model.invoke.call_count == 1
    assert "没能好好讲" in out["messages"][0].content
