# -*- coding: utf-8 -*-
"""`scripts/load_blog.py` 的写库行为回归网。

盯三件事，都是实测踩过或差点踩到的：

1. **分批提交**：138 篇一个事务跨公网跑到头，链路一抖整批回滚（实测
   `(2013, 'Lost connection')` 之后跟着 `InterfaceError`）。现在按
   `--batch-size` 分事务。
2. **SAVEPOINT 在 try 里面**：建 SAVEPOINT 自己也要发一条语句，写在 try
   外面时正好断在这里就会让异常冲出整个循环 —— 这是实测连挂两次的直接原因。
3. **UPDATE 不清零 `view_count`**：那是线上阅读量，重跑一次就清零等于丢数据。

用一个脚本化的假连接（FakeDB）跑：不碰任何真库，但把真实的 pymysql 异常类型
喂进去，所以 `is_disconnect()` 那条判据走的是**真代码路径**。
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pymysql
import pytest

PROJ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJ / "scripts"))

import load_blog  # noqa: E402

# --------------------------------------------------------------------------- #
# 假连接
# --------------------------------------------------------------------------- #


class FakeCursor:
    """记下每一条 SQL；按 pattern 决定 `fetchone()` 返回什么或抛什么。

    connection 指针在重连时会变（`upsert_posts` 换新 conn），所以游标每次向
    `db` 要当前连接 —— 不然"重连后还往旧连接写"这种 bug 测不出来。
    """

    def __init__(self, db: "FakeDB") -> None:
        self.db = db
        self.lastrowid = db.next_row_id

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, sql, params=None):
        # 连接已经废了：下一次 execute 就抛 2013（真 pymysql 在服务器侧断开后
        # 就是这个行为）。不用 InterfaceError(0)：那个错误号不在 is_disconnect 判据里。
        if not self.db.current.alive:
            raise disconnect_error()
        self.db.executed.append((sql, params))
        if sql.lstrip().startswith("INSERT INTO blog\n"):
            self.db.current.inserts += 1
        if self.db.on_execute is not None:
            self.db.on_execute(sql, params)
        if "FROM blog WHERE title=" in sql:
            return self.db.existing_id
        return None

    def fetchone(self):
        if not self.db.executed:
            return None
        sql = self.db.executed[-1][0]
        if "SELECT DATABASE()" in sql:
            return ("fake_db",)
        if "FROM blog WHERE title=" in sql:
            return (self.db.existing_id,) if self.db.existing_id else None
        if "COUNT(*)" in sql:
            return (0,)
        return None


class FakeConn:
    """够用的假连接：cursor / commit / close / alive。

    `close()` 之后再动它就抛**真实的 (2013, Lost connection)** —— 不用
    `InterfaceError(0, 'cursor closed')`：真 pymysql 的 InterfaceError 错误号是 0，
    而 `is_disconnect()` 只认 2006/2013/2055，用它会把这个假库变成"永远不该重连"，
    反而测不出东西（真链路上服务器先给的就是 OperationalError 2013）。
    """

    def __init__(self, tag: int, db: "FakeDB") -> None:
        self.tag = tag
        self.db = db
        self.alive = True
        self.commits = 0
        self.closed = 0
        #: 真 pymysql 连接上就有这两个属性：socket 没了 = 连接死了。
        #: `is_disconnect()` 的第二条判据（InterfaceError(0) 只有它能识别）认的是它。
        self._sock = object()
        self.open = True
        #: 这条连接上发出去过的 blog INSERT 数 / 成功提交那一刻的快照。
        self.inserts = 0
        self.inserts_at_commit = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.db)

    def commit(self) -> None:
        if not self.alive:
            raise disconnect_error()
        if self.db.commit_error is not None:
            raise self.db.commit_error
        self.commits += 1
        self.inserts_at_commit = self.inserts
        if self.db.on_commit is not None:
            self.db.on_commit()

    def close(self) -> None:
        self.closed += 1
        self.alive = False
        self._sock = None   # 真 pymysql 就是这么标记的：socket 没了
        self.open = False


class FakeDB:
    """若干假连接 + 一个可查询的执行记录。"""

    def __init__(self, *, existing_id=None, commit_error=None) -> None:
        self.executed: list[tuple] = []
        self.existing_id = existing_id
        self.commit_error = commit_error
        self.connections: list[FakeConn] = []
        self.factory_calls = 0
        self.next_row_id = 1000
        #: 每次 commit 之后调一次（用来"在第 N 批之后断链路"）。
        self.on_commit = None
        #: 每次 execute 成功之后调一次 (sql, params) —— 用来精确到"某篇写完之后"断链路。
        self.on_execute = None

    def fail_after_lookup(self, blog_url: str):
        """返回一个 `on_execute`：**某篇的 SELECT 查完之后**就把连接弄坏。

        这样紧接着的那条 SAVEPOINT 就会抛 2013 —— 正是老代码冲出循环的那个位置。
        只弄坏一次（第一条连接），重连之后不再断。
        """
        state = {"done": False}

        def hook(sql, params):
            if state["done"] or len(self.connections) != 1:
                return
            if "FROM blog WHERE title=" in sql and params and params[1] == blog_url:
                state["done"] = True
                self.current.close()

        return hook

    def fail_after_batch(self, n: int):
        """返回一个 `on_commit`：**第 n 批提交完之后**把连接弄坏（只弄坏一次）。

        之后重连上来的连接不再受影响 —— 不然每批提交都断，测的就不是
        "只重做未提交的那批"了。
        """
        state = {"done": False}

        def hook() -> None:
            if state["done"] or len(self.connections) != 1:
                return
            if self.current.commits == n:
                state["done"] = True
                self.current.close()

        return hook

    def factory(self, _args, **_kw) -> FakeConn:
        self.factory_calls += 1
        conn = FakeConn(self.factory_calls, self)
        self.connections.append(conn)
        return conn

    @property
    def current(self) -> FakeConn:
        return self.connections[-1]

    @property
    def commits(self) -> int:
        return sum(c.commits for c in self.connections)

    @property
    def closes(self) -> int:
        return sum(c.closed for c in self.connections)

    def lookups(self) -> list:
        """每次「按自然键查这一篇在不在」的 SELECT 参数 —— 一篇一次。"""
        return [params for sql, params in self.executed
                if params and "FROM blog WHERE title=" in sql]

    def inserts(self) -> int:
        return sum(1 for sql, _ in self.executed if sql.lstrip().startswith("INSERT INTO blog\n"))

    def row_count(self) -> int:
        """最终落库的 blog 行数 = 各连接**提交那一刻**它自己发过的 INSERT 之和。

        未提交的批（断链时随连接回滚）不算 —— 这正是"重做干净"的判据。
        """
        return sum(c.inserts_at_commit for c in self.connections)


# --------------------------------------------------------------------------- #
# 造数据
# --------------------------------------------------------------------------- #


def make_post(i: int) -> load_blog.Post:
    return load_blog.Post(
        source=Path(f"post-{i}.md"),
        rel_dir="root",
        slug=f"post-{i}",
        blog_url=f"https://example.test/posts/root/post-{i}/",
        title=f"Post {i}",
        category="项目实战",
        topic="项目实战",
        blog_type="项目实战",
        tags=",x,",
        word_count=100 + i,
        has_code=0,
        difficulty=2,
        intro="intro",
        publish_date=None,
        status=1,
        content=f"# Post {i}\n\nbody",
        content_hash="0" * 64,
    )


def disconnect_error() -> pymysql.err.OperationalError:
    return pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query")


def args_ns(**kw) -> SimpleNamespace:
    base = {
        "database": load_blog.DEFAULT_SHADOW_DB,
        "init_schema": False,
        "reconnect_retries": load_blog.DEFAULT_RECONNECT_RETRIES,
        "env_file": str(load_blog.DEFAULT_ENV_FILE),
        "host": None, "port": None, "user": None, "password": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# 1. 分批提交
# --------------------------------------------------------------------------- #


def test_posts_are_committed_in_batches():
    """7 篇 / 每批 3 → 3 个事务（不是 1 个，也不是 7 个）。"""
    db = FakeDB()
    conn = db.factory(None)
    posts = [make_post(i) for i in range(7)]

    inserted, updated, _ = load_blog.upsert_posts(conn, posts, batch_size=3)

    assert db.commits == 3, f"应当分 3 个事务提交，实际 {db.commits}"
    assert (inserted, updated) == (7, 0)
    assert len(db.lookups()) == 7, "每篇的 SELECT 都得跑到（整批没有漏）"
    assert db.inserts() == 7


def test_batch_size_one_still_works():
    """每批 1 篇：批数 == 篇数（边界不能死循环）。"""
    db = FakeDB()
    conn = db.factory(None)
    posts = [make_post(i) for i in range(4)]

    inserted, _updated, _ = load_blog.upsert_posts(conn, posts, batch_size=1)

    assert inserted == 4
    assert db.commits == 4


# --------------------------------------------------------------------------- #
# 2. SAVEPOINT 的位置
# --------------------------------------------------------------------------- #


def test_savepoint_is_established_before_any_write():
    """SAVEPOINT 必须**先于**本篇的读写建好（所以它自己撞断连能被兜住）。"""
    db = FakeDB()
    conn = db.factory(None)

    load_blog.upsert_posts(conn, [make_post(0)], batch_size=10)

    kinds = [sql for sql, _ in db.executed]
    sp = next(i for i, s in enumerate(kinds) if "SAVEPOINT sp_post" in s)
    sel = next(i for i, s in enumerate(kinds) if "FROM blog WHERE title=" in s)
    assert sp < sel, "SAVEPOINT 应当在 try 内、且在第一条读写语句之前"


def test_disconnect_while_creating_savepoint_reconnects_instead_of_crashing():
    """断连正好发生在建 SAVEPOINT 那一条上 —— 老代码就是在这里冲出循环的。

    这条是本次修复的核心回归：SAVEPOINT 在 try 外面时，异常直接冒出
    `upsert_posts`，138 篇一起回滚并崩；现在应当重连后把这一批重做完。

    断点精确落在"第 2 篇的 SELECT 查完、它的 SAVEPOINT 还没发"那一刻。
    """
    db = FakeDB()
    conn = db.factory(None)
    posts = [make_post(i) for i in range(3)]
    db.on_execute = db.fail_after_lookup(posts[1].blog_url)

    with patch.object(load_blog, "connect", side_effect=db.factory), patch.object(
        load_blog.time, "sleep"
    ):
        inserted, _updated, new_conn = load_blog.upsert_posts(
            conn, posts, args_ns(), batch_size=3
        )

    assert inserted == 3, "重连之后这 3 篇应当都写进去"
    assert db.factory_calls == 2, f"应当发生一次重连（实际 {db.factory_calls} 条连接）"
    assert new_conn is db.connections[-1], "返回的必须是重连后的新连接"
    assert new_conn is not conn, "重连必须是新对象"
    assert db.connections[0].closed >= 1, "旧连接应当被关掉"
    # 整批重做：post-0 的 SELECT 出现两次（第 1 次是断链前那半批）
    urls = [params[1] for params in db.lookups()]
    assert urls.count(posts[0].blog_url) == 2, "整批重做（不是从断点接着写）"
    assert urls.count(posts[2].blog_url) == 1, "第 3 篇只写了一次"
    assert db.commits == 1, "只有重做后的那一次提交"


# --------------------------------------------------------------------------- #
# 3. 断连重连
# --------------------------------------------------------------------------- #


def test_disconnect_after_first_batch_retries_only_the_uncommitted_batch():
    """第 1 批提交完之后断链 → 只有**未提交**的第 2 批被重做，第 1 批不重做。

    这就是 `--fail-batch` 在真库上演示的那条路径。
    """
    db = FakeDB()
    conn = db.factory(None)
    posts = [make_post(i) for i in range(5)]
    # 第 1 批提交之后断链 → 第 2 批整体重做（只断这一次，重连后恢复正常）
    db.on_commit = db.fail_after_batch(1)

    with patch.object(load_blog, "connect", side_effect=db.factory), patch.object(
        load_blog.time, "sleep"
    ):
        inserted, _updated, new_conn = load_blog.upsert_posts(
            conn, posts, args_ns(), batch_size=2
        )

    assert inserted == 5, f"5 篇都该写进去，实际 {inserted}"
    assert db.factory_calls == 2, f"断链后应当重连一次（实际 {db.factory_calls}）"
    assert db.commits == 3, f"5 篇 / 每批 2 → 3 批 3 个事务（重做的那批不再多算），实际 {db.commits}"
    # 断点落在第 2 批的 SAVEPOINT 上：这一批一条写都没发出去就被打断了，
    # 所以整批重做不会产生重复行（每篇恰好一条 INSERT）。
    assert db.inserts() == 5, f"每篇正好一条 INSERT，实际 {db.inserts()}"
    assert db.row_count() == 5, (
        f"最终落库必须只有 5 行（重做那批在旧连接上未提交），实际 {db.row_count()}"
    )
    executed_urls = [params[1] for params in db.lookups()]
    assert executed_urls.count(posts[2].blog_url) == 1, "第 2 批重做后只成功查了一次"
    assert executed_urls.count(posts[0].blog_url) == 1, "已提交的第 1 批不该重做"
    assert executed_urls.count(posts[4].blog_url) == 1, "第 3 批只做一次"
    assert new_conn is db.connections[-1]
    assert new_conn is not conn, "重连必须是**新对象**（旧对象已经废了）"
    assert db.connections[0].closed >= 1, "旧连接至少被关一次（断连时主动关）"
    assert new_conn.alive is True, "重连后的连接必须是活的（后续批次还要用它）"


def test_disconnect_in_the_middle_of_a_batch_leaves_no_duplicate_rows():
    """断链落在**一批的中间**（前半批已写过、还没 commit）→ 整批重做，不产生重复行。

    这是"整批重试"最需要证的那一点：已在废连接上发出去的 INSERT 未提交，
    随连接一起回滚；重连后整批重写，最终每篇仍只有一行。
    """
    db = FakeDB()
    conn = db.factory(None)
    posts = [make_post(i) for i in range(4)]
    # 第 2 批（post-2 / post-3）里 post-2 的 blog INSERT 发完之后断链
    state = {"done": False}

    def break_after_insert(sql, params):
        if state["done"] or len(db.connections) != 1:
            return
        if sql.lstrip().startswith("INSERT INTO blog\n") and params and params[7] == "Post 2":
            state["done"] = True
            db.current.close()

    db.on_execute = break_after_insert

    with patch.object(load_blog, "connect", side_effect=db.factory), patch.object(
        load_blog.time, "sleep"
    ):
        inserted, _updated, new_conn = load_blog.upsert_posts(
            conn, posts, args_ns(), batch_size=2
        )

    assert inserted == 4, f"4 篇都该写进去，实际 {inserted}"
    assert db.factory_calls == 2, "应当重连一次"
    # post-2 的 INSERT 发了两次（第 1 次在废连接上，未提交）
    assert db.inserts() == 5, f"日志里应当是 5 条 blog INSERT，实际 {db.inserts()}"
    # 但真正提交的只有 4 行 —— 重复那条随断开的连接回滚了
    assert db.row_count() == 4, f"最终落库必须只有 4 行，实际 {db.row_count()}"
    assert new_conn is db.connections[-1]


def test_valid_connection_is_reused_by_upsert_posts():
    """链路上午好好的：一篇都不重连、一篇都不重做（重连逻辑不能自己制造重复）。"""
    db = FakeDB()
    conn = db.factory(None)

    with patch.object(load_blog, "connect_with_retry", side_effect=AssertionError("不该重连")):
        inserted, updated, out = load_blog.upsert_posts(
            conn, [make_post(i) for i in range(4)], args_ns(), batch_size=2
        )

    assert (inserted, updated) == (4, 0)
    assert out is conn, "没断连时必须原样返回同一条连接"
    assert db.commits == 2
    assert db.row_count() == 4


def test_reconnect_is_retried_up_to_the_limit():
    """重连也连不上时：给出 `--reconnect-retries` 次机会，之后抛异常。

    这是**按批**的次数（写第一版时这里没有计数，"重连成功、一写这批就断"
    会让外面那层 while 永远转下去）。
    """
    db = FakeDB()
    conn = db.factory(None)
    db.on_commit = lambda: db.current.close()  # 每次刚提交完就断链路
    calls = {"n": 0}

    def always_disconnect(_args, **_kw):
        calls["n"] += 1
        raise disconnect_error()

    with patch.object(load_blog, "connect", side_effect=always_disconnect), patch.object(
        load_blog.time, "sleep"
    ):
        with pytest.raises(pymysql.err.OperationalError) as info:
            load_blog.upsert_posts(conn, [make_post(0), make_post(1)],
                                   args_ns(reconnect_retries=2), batch_size=1)

    assert info.value.args[0] == 2013
    assert calls["n"] == 2, f"重连次数应当正好等于 reconnect_retries=2，实际 {calls['n']}"


def test_gives_up_after_reconnect_retries_and_reraises():
    """重连**也**连不上时：`connect_with_retry` 试满 `--reconnect-retries` 后抛出。

    老版本的 upsert_posts 没有这个上限，"重连成功、一写这批就断"会让外面那层
    while 永远转下去（写这个函数第一版就踩了）。
    """
    db = FakeDB()
    conn = db.factory(None)
    db.on_commit = db.fail_after_batch(1)  # 第 1 批提交完就把连接弄废
    connect_calls = {"n": 0}

    def always_fail(_args, **_kw):
        connect_calls["n"] += 1
        raise disconnect_error()

    with patch.object(load_blog, "connect", side_effect=always_fail), patch.object(
        load_blog.time, "sleep"
    ) as sleeper:
        with pytest.raises(pymysql.err.OperationalError) as info:
            # 两批：第 1 批提交完连接就废了 → 第 2 批一写就断 → 重连永远失败
            load_blog.upsert_posts(conn, [make_post(0), make_post(1)],
                                   args_ns(reconnect_retries=3), batch_size=1)

    assert info.value.args[0] == 2013, "应当把最后一次断连原样抛出"
    # connect_with_retry(retries=1) = 首连 + 1 次重试 = 2 次尝试。
    # 整体次数由 upsert_posts 那一层的 attempts 控制（每批最多 reconnect_retries 次），
    # 两层不叠乘。
    assert connect_calls["n"] == 2, f"每次重连尝试 2 下，实际 {connect_calls['n']}"
    assert sleeper.call_count >= 1, "重试之间要等一下"


def test_non_disconnect_error_is_not_swallowed():
    """SQL 错（1064）照旧抛出，不许被"重连重试"吞掉。"""
    db = FakeDB(commit_error=pymysql.err.OperationalError(
        1064, "You have an error in your SQL syntax"))
    conn = db.factory(None)

    with patch.object(load_blog, "connect", side_effect=db.factory):
        with pytest.raises(pymysql.err.OperationalError) as info:
            load_blog.upsert_posts(conn, [make_post(0)], args_ns(), batch_size=1)

    assert info.value.args[0] == 1064
    assert db.factory_calls == 1, "SQL 错不该触发重连"


def test_auth_error_is_not_retried_by_connect_with_retry():
    """认证失败（1045）不是断连：一次就抛，不重试。"""
    calls = {"n": 0}

    def denied(_args, **_kw):
        calls["n"] += 1
        raise pymysql.err.OperationalError(1045, "Access denied for user")

    with patch.object(load_blog, "connect", side_effect=denied), patch.object(
        load_blog.time, "sleep"
    ) as sleeper:
        with pytest.raises(pymysql.err.OperationalError) as info:
            load_blog.connect_with_retry(args_ns(), retries=3)

    assert info.value.args[0] == 1045
    assert calls["n"] == 1, "认证失败重试没有意义，不该重试"
    sleeper.assert_not_called()


# --------------------------------------------------------------------------- #
# 4. view_count
# --------------------------------------------------------------------------- #


def test_update_does_not_touch_view_count():
    """已存在的行只更新 markdown 能决定的那几列 —— view_count 一个字节都不碰。"""
    db = FakeDB(existing_id=7)
    conn = db.factory(None)

    inserted, updated, _ = load_blog.upsert_posts(conn, [make_post(0)], batch_size=10)

    updates = [(sql, params) for sql, params in db.executed if "UPDATE blog SET" in sql]
    assert len(updates) == 1
    sql, params = updates[0]
    # 逐列核对 SET 子句（比 "0 not in params" 准：has_code 本来就可能是 0）
    set_clause = sql.split("SET", 1)[1].split("WHERE", 1)[0]
    columns = [
        part.split("=")[0].strip()
        for part in set_clause.split(",")
        if "=" in part
    ]
    assert columns == [
        "topic", "category", "blog_type", "tags", "word_count", "has_code",
        "difficulty", "author", "intro", "publish_date", "status",
    ], f"UPDATE 的列集合变了：{columns}"
    assert "view_count" not in columns
    assert len(params) == 12, f"11 列 + WHERE id = 12 个参数，实际 {len(params)}"
    assert (inserted, updated) == (0, 1)


def test_insert_sets_view_count_to_zero_and_update_leaves_it_alone():
    """INSERT 分支给 0（新文章从零开始），列名与取值都核对一遍。"""
    db = FakeDB()
    conn = db.factory(None)

    load_blog.upsert_posts(conn, [make_post(0)], batch_size=10)

    inserts = [(sql, params) for sql, params in db.executed if "INSERT INTO blog\n" in sql]
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert "view_count" in sql
    assert params[-2] == 0, f"INSERT 的 view_count 应当是 0，实际 {params[-2]}"


# --------------------------------------------------------------------------- #
# 5. 判据本身
# --------------------------------------------------------------------------- #


def test_is_disconnect_reuses_the_agent_side_implementation():
    """与 `src/agent/common/db.py` 共用判据：SQLAlchemy 包了一层的也要认出来。"""
    from sqlalchemy.exc import OperationalError as SAOperationalError

    wrapped = SAOperationalError("SELECT 1", {}, disconnect_error())
    assert load_blog.is_disconnect(wrapped) is True


@pytest.mark.parametrize("errno", [2006, 2013, 2055])
def test_is_disconnect_accepts_documented_errnos(errno):
    exc = pymysql.err.OperationalError(errno, "x")
    assert load_blog.is_disconnect(exc) is True


@pytest.mark.parametrize("errno", [1045, 1064, 1062, 1146])
def test_is_disconnect_rejects_non_disconnects(errno):
    exc = pymysql.err.OperationalError(errno, "x")
    assert load_blog.is_disconnect(exc) is False


def test_interface_error_on_a_dead_socket_counts_as_a_disconnect():
    """错误号 0 的 InterfaceError：只有"连接真死了"才算断连。

    这是真库上踩到的坑：`--fail-batch` 第一次跑，第 4 批撞的是
    `InterfaceError(0, '')`（错误号 0，2006/2013/2055 一个都不是），
    于是不重连、10 篇记 `[FAIL]`、整批崩。判据得看连接对象本身。
    """
    db = FakeDB()
    conn = db.factory(None)
    assert load_blog.is_disconnect(pymysql.err.InterfaceError(0, ""), conn) is False

    conn._sock = None
    conn.open = False
    assert load_blog.is_disconnect(pymysql.err.InterfaceError(0, ""), conn) is True


def test_sql_error_on_a_live_connection_is_not_a_disconnect():
    """连接活着时的 SQL 错（1064）不算断连 —— 不许拿"重连"去遮自己的 SQL bug。"""
    db = FakeDB()
    conn = db.factory(None)   # socket 还在
    assert conn._sock is not None
    assert load_blog.is_disconnect(
        pymysql.err.ProgrammingError(1064, "syntax"), conn
    ) is False


def test_batch_recovers_from_an_interface_error_after_the_socket_is_gone():
    """真库上那条路径的回归网：SAVEPOINT 上撞 InterfaceError(0) + socket 没了
    → 必须重连重试整批，而不是记 10 条 [FAIL] 然后崩。
    """
    db = FakeDB()
    conn = db.factory(None)
    posts = [make_post(i) for i in range(2)]
    state = {"n": 0}

    def raise_interface(sql, params):
        """第 2 批的 SAVEPOINT 上：socket 先没、再抛 InterfaceError(0, '')。

        完全照抄真库上的形态 —— 错误号是 0，光看编号判不出是断连。
        """
        if sql == "SAVEPOINT sp_post":
            state["n"] += 1
            if state["n"] == 2 and len(db.connections) == 1:
                db.current._sock = None
                db.current.open = False
                raise pymysql.err.InterfaceError(0, "")

    db.on_execute = raise_interface

    with patch.object(load_blog, "connect", side_effect=db.factory), patch.object(
        load_blog.time, "sleep"
    ):
        inserted, _updated, new_conn = load_blog.upsert_posts(
            conn, posts, args_ns(), batch_size=1
        )

    assert inserted == 2, "第 2 篇必须重连后写进去，不能被记成 [FAIL] 跳过"
    assert db.factory_calls == 2, "应当重连一次"
    assert new_conn is db.connections[-1]


# --------------------------------------------------------------------------- #
# 6. 安全闸门
# --------------------------------------------------------------------------- #


def test_fail_batch_is_refused_outside_the_shadow_database(capsys):
    """`--fail-batch` 是故意断链路的测试开关，只能配影子库用。"""
    rc = load_blog.main([
        "--fail-batch", "1",
        "--allow-production", "--database", load_blog.PRODUCTION_DB,
        "--posts", str(PROJ / "tests"),
    ])
    assert rc == 2
    assert "REFUSED" in capsys.readouterr().out


def test_production_write_is_still_refused_without_the_flag(capsys):
    rc = load_blog.main([
        "--database", load_blog.PRODUCTION_DB,
        "--posts", str(PROJ / "tests"),
    ])
    assert rc == 2
    assert "REFUSED" in capsys.readouterr().out
