# -*- coding: utf-8 -*-
"""按标题 / id 取文章（只读）。

为什么不放在 node/recommend.py：推荐路径的解析按**列位置**走（_parse_rows +
RECO_COLUMNS），多一列 content 会整条错位，两边的约定不能共用。

为什么走原生 SQLAlchemy 连接而不是 SQLDatabase.run()：run() 返回**字符串化的
Python 字面量**，调用方得再 literal_eval 解一遍、再按列名对齐；而且它内部把每个
字符串列截断到 300 字符（长正文被静默砍短）。原生连接直接给结构化行，还能用
:name 参数绑定，省掉手工拼 SQL 字面量。

这里只放**纯函数**，不做 @tool 封装：介绍模块的节点里是**代码**在选文章
（先标题匹配、落空才向量兜底），不是模型在选工具。包成 tool 只会多一层
StructuredTool 包装、返回值变成字符串、而且没人调用。真要 agentic 取数时再加。
"""
from typing import Optional

import re

from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError

from src.agent.common.db import DBUnavailable, get_engine, safe_err

META_COLUMNS = ("id", "title", "category", "word_count", "difficulty", "blog_url")

_META_SELECT = ", ".join(META_COLUMNS)


def escape_like(text_value: str) -> str:
    """把 LIKE 通配符转义成字面量。

    为什么必须做：用户可能问「介绍一下 100% 那篇」，不转义的话
    % 会被当成"任意字符"，标题匹配直接全表命中。
    """
    return (
        str(text_value)
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _is_transient(exc: BaseException) -> bool:
    """是不是"连接层面的瞬时故障"（远程 MySQL 会间歇性掉线）。

    这类错误重试一次基本就好了；真·连不上（认证失败、库名错）不在其列。
    """
    if isinstance(exc, (OperationalError, InterfaceError)):
        return True
    text = str(exc)
    return "Lost connection" in text or "server has gone away" in text


def _rows(sql: str, params: dict, columns) -> list:
    """执行只读查询，返回 [{列名: 值}, ...]。

    **异常必须转成 DBUnavailable**：调用方（介绍子图的 locate/fetch）靠
    `except DBUnavailable` 降级成一句人话。若把原始 OperationalError 冒出去，
    用户看到的是 traceback —— 这条路径曾经漏转过，界面上直接爆红。
    """
    engine = get_engine()
    attempts = 2
    for i in range(attempts):
        try:
            with engine.connect() as conn:
                result = conn.execute(text(sql), params)
                return [dict(zip(columns, row)) for row in result]
        except Exception as exc:  # noqa: BLE001 统一走降级路径
            # 瞬时故障重试一次（远程库抖动）；再失败或非瞬时则降级
            if i + 1 < attempts and _is_transient(exc):
                print(f"[content] 查询遇到瞬时故障，重试一次：{type(exc).__name__}")
                continue
            raise DBUnavailable(safe_err(exc)) from exc



def find_by_title(title: str) -> list:
    """标题模糊匹配（字面量语义）。返回 0/1/多 行元数据。"""
    pattern = "%" + escape_like(title.strip()) + "%"
    return _rows(
        f"SELECT {_META_SELECT} FROM blog "
        "WHERE status = 1 AND title LIKE :pat ORDER BY id",
        {"pat": pattern},
        META_COLUMNS,
    )


def _url_path(blog_url: str) -> str:
    """从任意写法里取出 `/posts/...` 那一段；取不到返回空串。

    库里存的是**绝对** URL（如 https://imiss.example.com/posts/interview/cpp/），
    而模型/前端有时会写成相对的（/posts/interview/cpp/）。两边要能对上。
    """
    m = re.search(r"/posts/[^\s?#]*", blog_url or "")
    return m.group(0) if m else ""


def find_by_url(blog_url: str) -> list:
    """按 blog_url 定位**唯一**一篇（返回 0/1 行元数据）。

    为什么要它：`find_by_title` 是 `title LIKE '%...%'` 模糊匹配，短篇名会撞名 ——
    实测 "C++" 一次命中 4 篇（《C++基础语法》《C++11 并发编程》《C++》
    《C++ 搜索引擎 课程笔记》）。
    父图（推荐完追问要不要介绍）手里有**库里的原始 blog_url**，那是唯一的，
    用它传递就能精确定位，不必让用户再从一堆同名里挑。

    两步走，先精确后后缀：
      1. 等值匹配整串（库里存的什么就传什么，最常见）
      2. 等值没中时，用 `/posts/...` 那一段做后缀匹配
         （调用方给的是相对路径、或前缀域名不一致时靠这一步）
    """
    raw = (blog_url or "").strip()
    if not raw:
        return []

    rows = _rows(
        f"SELECT {_META_SELECT} FROM blog WHERE status = 1 AND blog_url = :url",
        {"url": raw},
        META_COLUMNS,
    )
    if rows:
        return rows

    path = _url_path(raw)
    if not path:
        return []
    # 尾斜杠两种写法都试：库里可能存 "/posts/x/y/"，调用方可能给 "/posts/x/y"。
    suffixes = [path]
    if path.endswith("/"):
        suffixes.append(path.rstrip("/"))
    else:
        suffixes.append(path + "/")
    for suffix in suffixes:
        rows = _rows(
            f"SELECT {_META_SELECT} FROM blog "
            "WHERE status = 1 AND blog_url LIKE :suffix ORDER BY id",
            {"suffix": "%" + escape_like(suffix)},
            META_COLUMNS,
        )
        if rows:
            return rows
    return []


def fetch_post_meta(post_id: int) -> Optional[dict]:
    """按 id 取元数据（不含正文）。"""
    rows = _rows(
        f"SELECT {_META_SELECT} FROM blog WHERE status = 1 AND id = :pid",
        {"pid": int(post_id)},
        META_COLUMNS,
    )
    return rows[0] if rows else None


def fetch_content(post_id: int) -> Optional[dict]:
    """按 id 取元数据 + 正文 + 目录。缺正文行时 content 为空串。"""
    columns = META_COLUMNS + ("content", "toc", "content_len")
    rows = _rows(
        "SELECT b.id, b.title, b.category, b.word_count, b.difficulty, b.blog_url, "
        "       c.content, c.toc, CHAR_LENGTH(COALESCE(c.content,'')) AS content_len "
        "FROM blog b LEFT JOIN blog_content c ON c.blog_id = b.id "
        "WHERE b.status = 1 AND b.id = :pid",
        {"pid": int(post_id)},
        columns,
    )
    return rows[0] if rows else None

