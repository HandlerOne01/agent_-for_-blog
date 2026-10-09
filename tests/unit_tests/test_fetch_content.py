"""取正文：连不上库要降级；只有元数据没有正文要如实标记，不许假装读了正文。"""

from unittest.mock import patch

from src.agent.common.db import get_db
from src.agent.node.introduce import fetch_content_node


def test_content_layer_returns_structured_rows():
    """取数层必须返回**结构化 dict**，不是字符串化的 Python 字面量。

    为什么：SQLDatabase.run() 返回 "(42, '继承')" 这种字符串，得再 literal_eval
    解一遍，而且它内部还会把字符串列截断。走原生连接后：
    - 类型直接是 int / str，不需要解析
    - 不再经过 truncate_word，长正文天然安全
    回归网：把实现改回 db.run() + ast.literal_eval 就会红。
    """
    from src.agent.common.content import find_by_title
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE blog (id INTEGER, title, category, word_count INTEGER, difficulty, blog_url, status)"))
            conn.execute(text("INSERT INTO blog VALUES (42, '继承', 'C++', 1234, 1, '/posts/inherit/', 1)"))
        with patch("src.agent.common.content.get_engine", return_value=engine):
            rows = find_by_title("继承")
    finally:
        engine.dispose()
    assert rows, "库里应该有《继承》"
    row = rows[0]
    assert isinstance(row, dict)
    assert isinstance(row["id"], int), f"id 应该是 int，实际 {type(row['id'])}"
    assert isinstance(row["word_count"], int)


def test_db_disables_string_truncation():
    """SQLDatabase.run 默认把字符串砍到 300 字符。

    不关掉的话，一篇 21242 字的正文只会取回 299 字，而且**完全静默**：
    truncate_word 的结果仍是合法 Python 字面量，literal_eval 照样成功，
    没有异常、没有日志。于是 content_len（数据库里算的）是真的、
    content（应用层拿到的）是假的，分档/预算/兜底全被绕过。

    这条测试是回归网：把 max_string_length 调回正数就会红。
    """
    # 不去真连库（那会随手抖动），直接拦住构造调用、检查它拿到的参数
    captured = {}

    def _fake_from_uri(uri, **kwargs):
        captured["uri"] = uri
        captured.update(kwargs)
        return object()

    with patch("src.agent.common.db.SQLDatabase.from_uri", side_effect=_fake_from_uri):
        import src.agent.common.db as dbmod

        dbmod._db_singleton = None
        try:
            dbmod.get_db()
        finally:
            dbmod._db_singleton = None

    assert captured.get("max_string_length", 300) <= 0, (
        "构造 SQLDatabase 时必须传 max_string_length <= 0，"
        "否则长正文会被静默砍成 300 字（实际参数：%r）" % (captured,)
    )


def test_content_layer_exposes_only_plain_functions():
    """取数层只提供纯函数，不包 @tool。

    为什么钉这条：曾经加过三个 @tool 封装 + CONTENT_TOOLS 列表，但**没有任何
    生产代码调用**（节点里是代码在选文章，直接调纯函数）。无人引用的封装就是
    死代码 —— 这条测试防止它被重新加回来。
    """
    import src.agent.common.content as content

    assert not hasattr(content, "tool"), "不该 import @tool"
    for name in ("find_by_title", "fetch_post_meta", "fetch_content"):
        assert isinstance(getattr(content, name).__call__, object)
        assert type(getattr(content, name)).__name__ == "function", (
            f"{name} 应该是普通函数，不是 StructuredTool"
        )
    assert not hasattr(content, "CONTENT_TOOLS")


def test_query_errors_become_dbunavailable():
    """查询期异常必须转成 DBUnavailable，否则用户看到的是 traceback。

    这条路径曾经漏转过：引擎创建被 try 包住了，但 `conn.execute` 没有 ——
    于是远程 MySQL 一掉线，原始 OperationalError 一路冒到界面直接爆红
    （介绍子图的 `except DBUnavailable` 兜不住）。
    """
    from sqlalchemy.exc import OperationalError

    from src.agent.common.content import find_by_title
    from src.agent.common.db import DBUnavailable

    class _Boom:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **kw):
            raise OperationalError("select 1", {}, Exception("boom"))

    class _Engine:
        def connect(self):
            return _Boom()

    with patch("src.agent.common.content.get_engine", return_value=_Engine()):
        try:
            find_by_title("继承")
        except DBUnavailable:
            pass
        else:
            raise AssertionError("查询异常没有转成 DBUnavailable")


def test_non_transient_error_is_not_retried():
    """真·连不上（认证失败/库名错）不该重试 —— 白等一遍没意义。"""
    from src.agent.common.content import find_by_title
    from src.agent.common.db import DBUnavailable

    calls = {"n": 0}

    class _Boom:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **kw):
            calls["n"] += 1
            raise ValueError("bad credentials")

    class _Engine:
        def connect(self):
            return _Boom()

    with patch("src.agent.common.content.get_engine", return_value=_Engine()):
        try:
            find_by_title("继承")
        except DBUnavailable:
            pass
    assert calls["n"] == 1, "非瞬时错误不该重试，实际执行了 %d 次" % calls["n"]


def test_transient_error_is_retried_once():
    """瞬时掉线要重试一次 —— 远程 MySQL 抖动时能自愈。"""
    from sqlalchemy.exc import OperationalError

    from src.agent.common.content import find_by_title
    from src.agent.common.db import DBUnavailable

    calls = {"n": 0}

    class _Boom:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **kw):
            calls["n"] += 1
            raise OperationalError("select 1", {}, Exception("Lost connection"))

    class _Engine:
        def connect(self):
            return _Boom()

    with patch("src.agent.common.content.get_engine", return_value=_Engine()):
        try:
            find_by_title("继承")
        except DBUnavailable:
            pass
    assert calls["n"] == 2, "瞬时掉线应重试一次（共 2 次），实际 %d 次" % calls["n"]


def _state(pid=42):
    return {
        "messages": [],
        "target_post": {"id": pid, "title": "继承", "blog_url": "https://x/posts/a/"},
    }


def test_fetch_content_ok():
    row = {
        "id": 42,
        "title": "继承",
        "blog_url": "https://x/posts/a/",
        "content": "正文…",
        "toc": None,
        "content_len": 3,
    }
    with patch("src.agent.node.introduce.fetch_content", return_value=row):
        out = fetch_content_node(_state())
    assert out["post_content"] == "正文…"
    assert out["content_len"] == 3
    assert out.get("body_missing") is not True


def test_fetch_content_missing_body_marks_flag():
    """LEFT JOIN 出 NULL：只有元数据。必须标记，供答复如实说明。"""
    row = {
        "id": 42,
        "title": "继承",
        "blog_url": "https://x/posts/a/",
        "content": "",
        "toc": None,
        "content_len": 0,
    }
    with patch("src.agent.node.introduce.fetch_content", return_value=row):
        out = fetch_content_node(_state())
    assert out["body_missing"] is True
    assert out["post_content"] == ""


def test_fetch_content_degraded_on_db_error():
    from src.agent.common.db import DBUnavailable
    from src.agent.common.reply import DB_UNAVAILABLE_REPLY

    with patch(
        "src.agent.node.introduce.fetch_content", side_effect=DBUnavailable("boom")
    ):
        out = fetch_content_node(_state())
    assert out["messages"], "降级时必须产出一条人设化提示"
    assert DB_UNAVAILABLE_REPLY in str(out["messages"][-1].content)
    assert "post_content" not in out, "降级时不该写入正文键"


def test_fetch_content_missing_row_degrades():
    """定位到了却取不到行（并发下架等）——按降级处理，别硬编。"""
    from src.agent.common.reply import DB_UNAVAILABLE_REPLY

    with patch("src.agent.node.introduce.fetch_content", return_value=None):
        out = fetch_content_node(_state())
    assert out["messages"], "降级时必须产出一条人设化提示"
    assert DB_UNAVAILABLE_REPLY in str(out["messages"][-1].content)
