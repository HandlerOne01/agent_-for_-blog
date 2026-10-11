# -*- coding: utf-8 -*-
"""数据库连接：懒加载 + 失败降级。

从 node/recommend.py 抽出来的 —— 介绍模块要连同一个库，
但它不该反向依赖推荐模块（循环依赖），也不该照抄一份（重复）。

为什么懒加载：SQLDatabase.__init__ 会 inspect(self._engine)，也就是**当场握手**。
若在导入期执行，数据库抖一下 langgraph 连图都建不起来，连「在吗」也一起挂。
"""
import os
from typing import Optional

from dotenv import load_dotenv
from langchain_community.utilities import SQLDatabase
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

load_dotenv()


class DBUnavailable(RuntimeError):
    """数据库连不上。用于让节点降级，而不是让整张图炸掉。"""


def db_uri() -> str:
    """按环境变量拼连接串。库名可被 DB_NAME 覆盖（便于指向影子库做验证）。"""
    return (
        f"mysql+pymysql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
        f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
    )


def safe_err(exc: BaseException) -> str:
    """异常文本里可能带着含密码的连接串。落日志之前先把密码抹掉。"""
    msg = f"{type(exc).__name__}: {exc}"
    secret = os.getenv("DB_PASSWORD")
    if secret:
        msg = msg.replace(secret, "***")
    return msg


_db_singleton: Optional[SQLDatabase] = None


def is_disconnect(exc: BaseException) -> bool:
    """只重试明确的连接丢失，不重试认证或 SQL 错误。"""
    return isinstance(exc, DBAPIError) and (
        exc.connection_invalidated
        or (getattr(exc.orig, "args", ()) or (None,))[0] in (2006, 2013, 2055)
    )


def get_db() -> SQLDatabase:
    """首次调用时才真正连库；连不上抛 DBUnavailable。

    max_string_length=-1 是**必须**的，不是调优：
    SQLDatabase.run() 默认把每个字符串列砍到 300 字符
    （langchain_community/utilities/sql_database.py: truncate_word(value, length=...，
     length <= 0 时原样返回）。介绍模块要取 blog_content.content（最长 17 万字），
    不关掉的话只会拿回 299 字 + "..."，而且**完全静默** —— 截断结果仍是合法
    Python 字面量，literal_eval 照样成功，没有异常也没有日志。
    于是「数据库里算出来的字数」是真的、「拿到的正文」是假的，
    分档、预算、模板兜底全部被绕过，用户拿到"只讲了第一段"的答复还不知道。
    回归网：tests/unit_tests/test_fetch_content.py::test_db_disables_string_truncation
    """
    global _db_singleton
    if _db_singleton is None:
        for attempt in range(2):
            try:
                _db_singleton = SQLDatabase.from_uri(
                    db_uri(), max_string_length=-1,
                    engine_args={"pool_pre_ping": True, "pool_recycle": 300},
                )
                break
            except Exception as exc:  # noqa: BLE001
                _db_singleton = None
                if attempt == 0 and is_disconnect(exc):
                    continue
                raise DBUnavailable(safe_err(exc)) from exc
    return _db_singleton


_engine_singleton: Optional[Engine] = None


def get_engine() -> Engine:
    """原生 SQLAlchemy Engine —— 给"要结构化结果"的查询用。

    为什么不走 SQLDatabase.run()：
    1. run() 返回的是**字符串化的 Python 字面量**（如 "[(42, '继承')]"），
       调用方得再 ast.literal_eval 解一遍，还要自己按列名对齐；
    2. run() 内部会对每个字符串列调 truncate_word(limit=300)，长正文被静默砍短。
    走原生连接：结果直接是行对象（类型正确、长度完整），而且能用参数绑定
    （:name），不必手工拼 SQL 字面量防注入。

    SQLDatabase 那条路保留：推荐路径的工具集（SQLDatabaseToolkit）依赖它。
    """
    global _engine_singleton
    if _engine_singleton is None:
        try:
            # 针对"远程库会间歇性掉连接"做了两项：
            #   pool_pre_ping  —— 取用连接前先探一下，死连接自动换新的
            #   pool_recycle   —— 超过 5 分钟的连接主动淘汰，别等 MySQL 那边超时
            _engine_singleton = create_engine(
                db_uri(), pool_pre_ping=True, pool_recycle=300
            )
        except Exception as exc:  # noqa: BLE001 与 get_db 同一条降级路径
            _engine_singleton = None
            raise DBUnavailable(safe_err(exc)) from exc
    return _engine_singleton
