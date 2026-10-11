from unittest.mock import patch

from langchain_core.messages import HumanMessage


def test_empty_title_still_searches_user_request():
    from src.agent.node.introduce import locate_article

    request = "介绍一下个人博客助手这个项目"
    with patch("src.agent.node.introduce.model_deterministic") as model, patch(
        "src.agent.node.introduce.find_by_title", return_value=[]
    ), patch("src.agent.node.introduce.search_ids", return_value=[(135, .8)]) as search, patch(
        "src.agent.node.introduce.fetch_post_meta", return_value={"id": 135}
    ):
        model.with_structured_output.return_value.invoke.return_value.query = ""
        out = locate_article({"messages": [HumanMessage(content=request)]})
    assert out["target_post"]["id"] == 135
    search.assert_called_once_with(request)


def test_schema_failure_has_no_unconditional_query_edge():
    from src.agent.recommend import recommended_graph

    edges = recommended_graph.get_graph().edges
    assert not any(e.source == "get_schema" and not e.conditional for e in edges)
    assert any(e.source == "get_schema" and e.target == "__end__" for e in edges)


def test_recommend_database_checks_pooled_connections():
    from src.agent.common import db

    with patch.object(db, "_db_singleton", None), patch.object(db.SQLDatabase, "from_uri") as factory:
        db.get_db()
    options = factory.call_args.kwargs["engine_args"]
    assert options["pool_pre_ping"] is True
    assert options["pool_recycle"] == 300


def test_unavailable_vector_is_not_reported_as_no_match():
    from src.agent.node.introduce import locate_article

    with patch("src.agent.node.introduce.find_by_title", return_value=[]), patch(
        "src.agent.node.introduce.search_ids", return_value=None
    ):
        out = locate_article({"messages": [], "target_query": "博客助手"})
    assert out["introduce_failed"] is True
    assert "没定位到" not in out["messages"][-1].content


def test_database_initialization_retries_lost_connection_once():
    import pymysql
    from sqlalchemy.exc import OperationalError
    from src.agent.common import db

    error = OperationalError("SELECT 1", {}, pymysql.err.OperationalError(2013, "Lost connection"))
    ready = object()
    with patch.object(db, "db_uri", return_value="mysql+pymysql://test@localhost/test"), patch.object(db, "_db_singleton", None), patch.object(
        db.SQLDatabase, "from_uri", side_effect=[error, ready]
    ) as factory:
        assert db.get_db() is ready
        assert factory.call_count == 2


def test_schema_retries_lost_connection_once():
    import pymysql
    from sqlalchemy.exc import OperationalError
    from src.agent.node.recommend import get_schema_node

    error = OperationalError("SELECT 1", {}, pymysql.err.OperationalError(2013, "Lost connection"))
    with patch("src.agent.node.recommend._tool_node") as node:
        node.return_value.invoke.side_effect = [error, {"messages": []}]
        assert get_schema_node({"messages": []}) == {"messages": []}
        assert node.return_value.invoke.call_count == 2
