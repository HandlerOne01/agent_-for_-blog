"""Execute recommendation queries to verify filtering and alias wiring."""
import sqlite3
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text

from src.agent.common.retrieval import anchor_ids
from src.agent.node import recommend as R


@pytest.mark.parametrize("first_word_count, first_status", [(5000, 1), (100, 0)])
def test_limit_follows_hard_filters(first_word_count, first_status):
    with patch.object(R, "_anchor_rows", return_value=[]), patch(
        "src.agent.common.retrieval.search_ids", return_value=[(1, 0.9), (3, 0.8), (2, 0.7)]
    ):
        out = R.generate_query({"messages": [], "topic": "Python", "blog_count": 1,
                                "word_count_max": 1000})
    query = out["messages"][0].tool_calls[0]["args"]["query"]
    with sqlite3.connect(":memory:") as db:
        db.create_function("FIELD", -1, lambda value, *ids: ids.index(value) + 1 if value in ids else 0)
        db.execute("CREATE TABLE blog (id INTEGER PRIMARY KEY, title, category, word_count, difficulty, blog_url, status)")
        db.executemany("INSERT INTO blog VALUES (?, ?, '', ?, 1, '', ?)", [
            (1, "excluded", first_word_count, first_status),
            (2, "lower relevance", 500, 1), (3, "best eligible", 800, 1),
        ])
        assert [row[0] for row in db.execute(query)] == ["best eligible"]


def test_alias_reads_blog_type_from_real_anchor_query(monkeypatch):
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE blog (id INTEGER PRIMARY KEY, title, topic, category, tags, blog_type)"))
            conn.execute(text("INSERT INTO blog VALUES (:id, 'Python', '', '', '', '面试总结')"),
                         [{"id": 1}, {"id": 2}, {"id": 3}])
        monkeypatch.setattr(R, "_anchor_rows_cache", None)
        with patch.object(R, "get_engine", return_value=engine):
            assert anchor_ids("面经", R._anchor_rows()) == [1, 2, 3]
    finally:
        engine.dispose()
