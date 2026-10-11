#!/usr/bin/env python
# ruff: noqa: T201, D415, D301, UP037
# 这是一个命令行脚本：T201(print) 是它的输出方式；D415 是因为文档串用中文句号收尾；
# D301/UP037 分别为含反斜杠的说明性 docstring 与带引号的 pymysql 类型注解。
"""把 Astro 博客的 Markdown 文章灌进 MySQL 的 ``blog`` / ``blog_content`` 两张表。

设计要点
--------
* 只依赖 **标准库 + pymysql**，不联网、不需要任何 API key。
* 本文件是 **新增文件**：不修改 agent 项目里任何已有文件。
* 默认目标是 **影子库** ``my_blog_rebuild``；生产库 ``my_blog`` 必须显式加
  ``--allow-production`` 才会写入（默认直接拒绝退出，见 ``--help``）。
* 幂等：默认 upsert（自然键 ``title + blog_url``），重复运行不会产生重复行；
  ``--replace`` 则先清空目标库的 ``blog`` 表（外键 CASCADE 带走 ``blog_content``）再全量插入。
* **分批提交 + 断连重连**（默认每 ``--batch-size 10`` 篇提交一次，见 ``upsert_posts()``）：
  138 篇一个事务在跨公网链路上太脆 —— 链路一抖（``Lost connection`` / ``InterfaceError``）
  整批回滚，白跑且当场崩。分批之后最多丢最后不足一批的部分，且**已提交的批不重做**。
  ``SAVEPOINT`` 一律在 ``try`` **内部**建立：写在 try 外面时，建 SAVEPOINT 这一步自己
  撞上断连就会把异常抛出循环外（这是实测连挂两次的直接原因）。

字段口径
--------
====================  ==========================================================
列                     来源 / 规则
====================  ==========================================================
``title``             frontmatter.title
``category``          frontmatter.category
``topic``             = category（10 个分类）
``intro``             frontmatter.description
``publish_date``      frontmatter.published 取日期部分（YYYY-MM-DD）
``tags``              frontmatter.tags 数组 → ``",a,b,"``（前后都带逗号）
``author``            固定 ``"IMiss"``
``word_count``        正文（去 frontmatter）字符数，见 ``normalize_body()``
``has_code``          正文含 ``` 围栏 → 1，否则 0
``difficulty``        启发式 1~5，见 ``score_difficulty()`` 注释
``blog_type``         category 映射，见 ``CATEGORY_BLOG_TYPE``
``view_count``        INSERT 时写 0；**UPDATE 分支一个字节都不碰**（见下）
``blog_url``          ``https://imiss.example.com/posts/<相对目录>/<slug>/``
``status``            ``draft: true`` → 0，否则 1
``blog_content``      一篇一行：content=正文 markdown 原文（仅统一换行为 \n、
                      去掉首尾空行，其他排版原样保留）、content_fmt=markdown、
                      content_hash=该 content 的 sha256、updated_at=now；
                      toc / code_lang 留空（NULL）
====================  ==========================================================

``view_count`` 为什么不能进 UPDATE：它是**线上阅读量**，不是本站从 md 里派生出来的
字段。早先的 upsert 固定写 12 列（含 ``view_count``），于是每重跑一次就把全表浏览量
清零 —— 当时库里的值恰好都是 0，所以无损；但以后重跑就是真丢数据。现在语义明确：
INSERT 给 0（新文章从零开始），UPDATE 只更新 markdown 真能决定的那几列。

用法::

    # 只看统计，不连库
    python scripts/load_blog.py --dry-run

    # 在影子库建库建表建视图，然后灌数据
    python scripts/load_blog.py --init-schema --database my_blog_rebuild

    # 只灌 10 篇做抽样
    python scripts/load_blog.py --database my_blog_rebuild --limit 10

    # 每 5 篇提交一次；并演示断连重连（第 2 批人为断一次链路，应当自动重连重试）
    python scripts/load_blog.py --database my_blog_rebuild --batch-size 5 --fail-batch 2
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

try:
    import pymysql
except ImportError:  # pragma: no cover - 环境缺依赖时的友好提示
    pymysql = None  # type: ignore[assignment]

#: 断连判据与 agent 侧共用一个实现 —— `src/agent/common/db.py: is_disconnect()`。
#: 它认的是 `SQLAlchemy DBAPIError.connection_invalidated` 或 pymysql 的
#: 2006/2013/2055。本脚本拿到的是**裸 pymysql 异常**（没有 SQLAlchemy 包一层），
#: 所以下面 `_is_disconnect()` 用同一套编号，只是把「读 .orig」换成「读异常自己」。
try:
    from src.agent.common.db import is_disconnect as _agent_is_disconnect
except Exception:  # noqa: BLE001 - 见 _is_disconnect()：拿不到就退回编号判据
    _agent_is_disconnect = None  # type: ignore[assignment]

#: MySQL 客户端错误码：服务器断开 / 连接丢失 / 连接已失效。
DISCONNECT_ERRNOS = frozenset({2006, 2013, 2055})


def _exc_errno(exc: BaseException) -> int | None:
    """取 MySQL 错误号（没有就是 None）。

    pymysql 的异常自己带 ``args[0]``；万一被 SQLAlchemy 包了一层（``DBAPIError``），
    错误号在 ``exc.orig`` 上 —— 两条路都认，本函数才好复用。
    """
    for candidate in (exc, getattr(exc, "orig", None)):
        args = getattr(candidate, "args", None)
        if args and isinstance(args[0], int):
            return args[0]
    return None


def _conn_is_dead(conn: Any) -> bool:
    """连接对象自己还有没有活着的 socket（没有 = 必须重连）。"""
    if conn is None:
        return False
    try:
        if getattr(conn, "_sock", "missing") is None:
            return True
        return getattr(conn, "open", True) is False
    except Exception:  # noqa: BLE001 - 假连接/其它驱动：判断不了就当没事
        return False


def is_disconnect(exc: BaseException, conn: Any = None) -> bool:
    """这次异常是不是**明确的连接丢失**（该重连重试），而不是认证/SQL/代码错。

    两条判据，任一命中即算：

    1. `src/agent/common/db.py: is_disconnect()` —— 2006/2013/2055 或
       `connection_invalidated`。判据只维护一份，agent 侧改了这里跟着改。
    2. **socket 真的没了**（``conn._sock is None`` / ``conn.open is False``）。
       为什么必须有第 2 条：真链路上"掉线"有两个阶段 —— 第一次是
       ``OperationalError(2013, 'Lost connection')``（第 1 条能认），之后这条
       连接上的任何操作 pymysql 只会抛 ``InterfaceError(0, '')``（错误号是 0，
       第 1 条认不出来），错误号与"自己代码里用错游标"完全一样、**无法区分**。
       实测就栽在这里：`--fail-batch` 在真库上第一次跑，第 4 批撞的就是
       ``InterfaceError(0, '')``，于是不重连、10 篇被记成 `[FAIL]` 后整批崩掉。
       所以判据不能只看错误号，要看连接对象本身还活着没有 —— 这样既覆盖真掉线，
       又不会把 "cursor closed" 这类**自己写错**的 InterfaceError 误判成重连。

    认证失败（1045）、SQL 语法/约束错误（1064/1062/1452…）一律不算：
    重试它们只会把同一个错误再撞一遍，还掩盖真正的原因。
    """
    if _agent_is_disconnect is not None and _agent_is_disconnect(exc):
        return True
    if _exc_errno(exc) in DISCONNECT_ERRNOS:
        return True
    return _conn_is_dead(conn)


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 生产库库名。除非显式 --allow-production，否则拒绝对它做任何写操作。
PRODUCTION_DB = "my_blog"
#: 默认（影子）库库名。
DEFAULT_SHADOW_DB = "my_blog_rebuild"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
#: 文章源目录；可用 --posts 或环境变量 BLOG_POSTS_DIR 覆盖。
DEFAULT_POSTS_DIR = Path(
    r"D:\HuaweiMoveData\Users\HUAWEI\Desktop\imiss_-blog\src\content\posts"
)

AUTHOR = "IMiss"
BLOG_URL_PREFIX = "https://imiss.example.com/posts"
CONTENT_FMT = "markdown"

#: 每批提交多少篇。跨公网链路（本机 → 106.52.158.48）一个事务跑到头太脆，
#: 见模块 docstring「分批提交 + 断连重连」。
DEFAULT_BATCH_SIZE = 10
#: 断连后最多重连几次（每次重试的都是**同一个当前批**）。
DEFAULT_RECONNECT_RETRIES = 3
#: 两次重连尝试之间等多久。远端库偶发抖动，立刻重试往往还是同一个坏时刻。
RECONNECT_DELAY_SECONDS = 2.0

#: 列宽上限（与生产库 DDL 一致），超长时安全截断并计入 warning。
MAX_LEN = {"title": 255, "intro": 500, "tags": 255, "topic": 64, "category": 64,
           "blog_type": 32, "author": 64, "blog_url": 512}

#: category → blog_type 映射表（发起人拍板的四项 + 其余按同口径补齐）。
CATEGORY_BLOG_TYPE: dict[str, str] = {
    "八股文": "面试总结",
    "算法": "算法笔记",
    "Linux 系统": "系统笔记",
    "Linux 网络": "网络笔记",
    "C/C++": "语言笔记",
    "C++11": "语言笔记",
    "STL": "语言笔记",
    "MySQL": "数据库笔记",
    "项目实战": "项目实战",
    # v1.85（博客侧分类重排，发起人要求）：原「项目实战」拆成两半 ——
    #   · 「项目梳理」= 原来的项目笔记那 6 篇（只是改名，语义没变）；
    #   · 「项目实战」= 项目展示页那 3 篇的**文章版**（同一批内容的文章形态）；
    #   · 「本站维护」= 新增的那篇《本站使用与维护手册》。
    # ⚠️ 不登记这几项的话 `blog_type` 会静默兜底成「技术笔记」（dry-run 会打 warning）。
    "项目梳理": "项目实战",
    "本站维护": "站点维护",
    "AI 应用": "AI 笔记",
}
#: 未在映射表里的 category 落到这里。
DEFAULT_BLOG_TYPE = "技术笔记"

#: 从生产库 ``SHOW CREATE TABLE`` / ``SHOW CREATE VIEW`` 原样抄下来的 DDL
#: （只去掉了 ``AUTO_INCREMENT=<当前值>`` 让影子库从 1 开始；视图去掉了显式
#: DEFINER，默认即当前连接用户，定义体完全一致）。
DDL_BLOG = """
CREATE TABLE IF NOT EXISTS `blog` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT COMMENT '文章ID',
  `topic` varchar(64) NOT NULL COMMENT '领域/分类',
  `category` varchar(64) DEFAULT NULL COMMENT '栏目/专栏',
  `blog_type` varchar(32) DEFAULT NULL COMMENT '文章类型',
  `tags` varchar(255) DEFAULT NULL COMMENT '标签，逗号分隔，形如 ,a,b,',
  `word_count` int unsigned DEFAULT NULL COMMENT '散文字数（由正文触发）',
  `has_code` tinyint(1) NOT NULL DEFAULT '0' COMMENT '是否带代码示例',
  `difficulty` tinyint NOT NULL DEFAULT '2' COMMENT '难度 1入门 2进阶 3深入',
  `title` varchar(255) NOT NULL COMMENT '标题',
  `author` varchar(64) NOT NULL COMMENT '作者',
  `intro` varchar(500) DEFAULT NULL COMMENT '摘要',
  `blog_url` varchar(512) DEFAULT NULL COMMENT '文章链接',
  `publish_date` date DEFAULT NULL COMMENT '发布日期',
  `view_count` int unsigned NOT NULL DEFAULT '0' COMMENT '阅读量，用于排序',
  `status` tinyint NOT NULL DEFAULT '1' COMMENT '1已发布 0草稿 -1下架',
  PRIMARY KEY (`id`),
  KEY `idx_topic_word` (`topic`,`word_count`),
  KEY `idx_topic_hot` (`topic`,`view_count`),
  KEY `idx_publish` (`publish_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='博客文章表'
"""

DDL_BLOG_CONTENT = """
CREATE TABLE IF NOT EXISTS `blog_content` (
  `blog_id` bigint unsigned NOT NULL,
  `content` longtext COMMENT '正文 Markdown',
  `content_fmt` varchar(16) NOT NULL DEFAULT 'markdown',
  `toc` json DEFAULT NULL,
  `code_lang` varchar(64) DEFAULT NULL,
  `content_hash` char(64) DEFAULT NULL,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`blog_id`),
  FULLTEXT KEY `ft_content` (`content`) /*!50100 WITH PARSER `ngram` */ ,
  CONSTRAINT `fk_content_blog` FOREIGN KEY (`blog_id`) REFERENCES `blog` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='博客正文表'
"""

DDL_V_BLOG = """
CREATE OR REPLACE ALGORITHM=MERGE SQL SECURITY INVOKER VIEW `v_blog` AS
select `blog`.`id` AS `id`,`blog`.`title` AS `title`,`blog`.`author` AS `author`,
       `blog`.`topic` AS `topic`,`blog`.`category` AS `category`,
       `blog`.`blog_type` AS `blog_type`,`blog`.`tags` AS `tags`,
       `blog`.`word_count` AS `word_count`,`blog`.`difficulty` AS `difficulty`,
       `blog`.`has_code` AS `has_code`,`blog`.`intro` AS `intro`,
       `blog`.`blog_url` AS `blog_url`,`blog`.`view_count` AS `view_count`,
       `blog`.`publish_date` AS `publish_date`
from `blog` where (`blog`.`status` = 1)
"""

#: 代码围栏：``` 或 ~~~，允许最多 3 个前导空格。
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
#: frontmatter 的 ``key: value``。
FM_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")
#: 允许的库名（同时防注入，因为库名不能用占位符绑定）。
DB_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


class PostError(Exception):
    """单篇解析失败。不中断整批，只记录并跳过。"""


@dataclass
class Post:
    """一篇待写入的文章。"""

    source: Path
    rel_dir: str
    slug: str
    blog_url: str
    title: str
    category: str
    topic: str
    blog_type: str
    tags: str
    word_count: int
    has_code: int
    difficulty: int
    intro: str | None
    publish_date: date | None
    status: int
    content: str
    content_hash: str
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# .env / 连接参数
# --------------------------------------------------------------------------- #


def load_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析（不引入 python-dotenv）。

    只认 ``KEY=VALUE``；跳过空行与 ``#`` 注释；去掉值两端的成对引号；
    **不**处理行尾注释（项目的 .env 明确要求注释独占一行）。
    """
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key] = value
    return env


# --------------------------------------------------------------------------- #
# frontmatter 解析（YAML 子集）
# --------------------------------------------------------------------------- #


def _split_top_level(text: str) -> list[str]:
    """按逗号切分数组字面量，忽略引号内的逗号。"""
    parts, buf, quote = [], [], None
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


_YAML_ESCAPES = {
    "0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n",
    "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"',
    "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0", "L": "\u2028", "P": "\u2029",
}


def _unescape_yaml_double(text: str) -> str:
    """处理 YAML 双引号字符串的转义（LaTeX 文章里有 ``\\\\mid`` / ``\\"``）。

    未知转义（如裸 ``\\p``）原样保留 ``\\`` + 字符，比 YAML 严格模式宽容，不会崩。
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch != "\\" or i + 1 >= len(text):
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt in _YAML_ESCAPES:
            out.append(_YAML_ESCAPES[nxt])
            i += 2
        elif nxt in "xuU":  # \xNN \uNNNN \UNNNNNNNN
            width = {"x": 2, "u": 4, "U": 8}[nxt]
            hex_digits = text[i + 2: i + 2 + width]
            if len(hex_digits) == width and all(c in "0123456789abcdefABCDEF" for c in hex_digits):
                out.append(chr(int(hex_digits, 16)))
                i += 2 + width
            else:
                out.append(ch)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _scalar(raw: str) -> Any:
    """把 frontmatter 的值字面量转成 Python 值。"""
    value = raw.strip()
    if value.startswith("[") and value.endswith("]"):
        return [_scalar(item) for item in _split_top_level(value[1:-1])]
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return _unescape_yaml_double(value[1:-1])
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~", ""):
        return None
    return value


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """切出 frontmatter 字典与正文。

    只支持 ``key: value`` 单行标量与 ``[...]`` 数组（本仓库 133 篇全部如此）。
    找不到合法 frontmatter 时抛 :class:`PostError`。
    """
    if text.startswith("\ufeff"):  # 容忍 BOM
        text = text[1:]
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise PostError("缺少 frontmatter 起始的 '---'")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise PostError("frontmatter 没有闭合的 '---'")

    meta: dict[str, Any] = {}
    for raw_line in lines[1:end]:
        line = raw_line.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = FM_LINE_RE.match(line)
        if not match:
            raise PostError(f"frontmatter 出现无法解析的行: {line[:60]!r}")
        meta[match.group(1)] = _scalar(match.group(2))
    # 去掉闭合 '---' 后紧跟的那个换行，正文即 markdown 原文。
    return meta, "\n".join(lines[end + 1:])


# --------------------------------------------------------------------------- #
# 正文派生指标
# --------------------------------------------------------------------------- #


def normalize_body(body: str) -> tuple[str, int, int]:
    """规范化正文并统计字符数 / 代码块数量。

    ``word_count`` 口径（推荐功能的核心筛选列，绝不为 NULL）：

    * **围栏外**：每行去掉两端空白 → 丢弃空行 → 行内连续空格/制表符压成单个空格；
      （这样缩进、行尾空格、连续空行都不会虚增字数）
    * **围栏内**（``` / ~~~ 之间，含围栏行本身）：**原样保留**缩进与空行；
    * 两者按原顺序用 ``\\n`` 连接后取 ``len()``，即 Unicode 字符数（中文按字计）。
    * 未闭合的围栏（本仓库确有 2 篇）按"后面全是代码"处理，不报错。
    """
    segments: list[str] = []
    code_blocks = 0
    in_fence = False
    fence_char = ""
    fence_len = 0
    for raw_line in body.split("\n"):
        line = raw_line.rstrip("\r")
        match = FENCE_RE.match(line)
        if not in_fence and match:
            in_fence, fence_char, fence_len = True, match.group(1)[0], len(match.group(1))
            code_blocks += 1
            segments.append(line)
            continue
        if (
            in_fence
            and match
            and match.group(1)[0] == fence_char
            and len(match.group(1)) >= fence_len
            and not match.group(2).strip()
        ):
            in_fence = False
            segments.append(line)
            continue
        if in_fence:
            segments.append(line)
        else:
            stripped = re.sub(r"[ \t]+", " ", line.strip())
            if stripped:
                segments.append(stripped)
    normalized = "\n".join(segments)
    return normalized, len(normalized), code_blocks


def score_difficulty(word_count: int, code_blocks: int) -> int:
    """启发式难度 1~5。

    阈值（先看 133 篇的实际分布定的，保证 1~5 都有样本）::

        difficulty = 1
        + 1  若 正文 >= 1500 字符
        + 1  若 正文 >= 5000 字符
        + 1  若 代码块 >= 3
        + 1  若 代码块 >= 12 或 正文 >= 20000 字符
        最后 clamp 到 [1, 5]

    即：1=短文无代码入门，5=长文且代码密集/超大篇幅。
    在本仓库 133 篇上的分布约为 {1:6, 2:29, 3:30, 4:22, 5:46}。
    """
    score = 1
    if word_count >= 1500:
        score += 1
    if word_count >= 5000:
        score += 1
    if code_blocks >= 3:
        score += 1
    if code_blocks >= 12 or word_count >= 20000:
        score += 1
    return max(1, min(5, score))


def _truncate(value: str | None, column: str, post_warnings: list[str]) -> str | None:
    """按列宽截断，并记一条 warning（本仓库数据实际不会触发）。"""
    if value is None:
        return None
    limit = MAX_LEN[column]
    if len(value) <= limit:
        return value
    post_warnings.append(f"{column} 超长 {len(value)}>{limit}，已截断")
    return value[:limit]


def build_tags(tags: Any, post_warnings: list[str]) -> str | None:
    """数组 → ``",a,b,"``；顺手去掉空项与重复项，并保证不超列宽。"""
    if tags is None:
        return None
    if isinstance(tags, str):
        items = [tags]
    else:
        items = [str(t).strip() for t in tags if str(t).strip()]
    seen, ordered = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    joined = "," + ",".join(ordered) + "," if ordered else ","
    if len(joined) > MAX_LEN["tags"]:
        post_warnings.append(f"tags 超长 {len(joined)}>{MAX_LEN['tags']}，已截断")
        joined = joined[: MAX_LEN["tags"] - 1] + ","
    return joined


def parse_publish_date(raw: Any, post_warnings: list[str]) -> date | None:
    """``YYYY-MM-DD HH:MM:SS`` / ``YYYY-MM-DD`` / ISO8601 → ``date``。"""
    if raw is None or raw == "":
        post_warnings.append("published 缺失，publish_date 置 NULL")
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    if match:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date()
    post_warnings.append(f"published 无法解析: {text[:40]!r}，置 NULL")
    return None


def build_post(path: Path, posts_root: Path) -> Post:
    """读一个 .md 文件 → :class:`Post`。任何问题都抛 :class:`PostError`。"""
    # 显式 utf-8 读取（解码失败由上层记录为失败并跳过），顺带统一换行符，
    # 保证 content_hash 在 Windows/Linux checkout 下一致。
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    meta, body = parse_frontmatter(text)

    warnings: list[str] = []
    title = str(meta.get("title") or "").strip()
    if not title:
        raise PostError("frontmatter 缺 title")
    category = str(meta.get("category") or "").strip() or "未分类"
    if not meta.get("category"):
        warnings.append("category 缺失，已兜底为 '未分类'")

    rel = path.relative_to(posts_root)
    rel_dir = rel.parent.as_posix() or "root"
    slug = path.stem
    blog_url = f"{BLOG_URL_PREFIX}/{rel_dir}/{slug}/"

    # content 存 markdown 原文（仅统一换行符）；字数用规范化后的文本单独统计。
    raw_content = body.strip("\n")
    _, word_count, code_blocks = normalize_body(raw_content)
    blog_type = CATEGORY_BLOG_TYPE.get(category, DEFAULT_BLOG_TYPE)
    if blog_type == DEFAULT_BLOG_TYPE and category not in CATEGORY_BLOG_TYPE:
        warnings.append(f"category {category!r} 不在映射表，blog_type 兜底为 {DEFAULT_BLOG_TYPE!r}")

    return Post(
        source=path,
        rel_dir=rel_dir,
        slug=slug,
        blog_url=blog_url,
        title=_truncate(title, "title", warnings) or title,
        category=_truncate(category, "category", warnings) or category,
        topic=_truncate(category, "topic", warnings) or category,  # topic = category
        blog_type=_truncate(blog_type, "blog_type", warnings) or blog_type,
        tags=build_tags(meta.get("tags"), warnings) or ",",
        word_count=word_count,
        has_code=1 if code_blocks > 0 else 0,
        difficulty=score_difficulty(word_count, code_blocks),
        intro=_truncate(
            (str(meta["description"]).strip() or None) if meta.get("description") is not None else None,
            "intro",
            warnings,
        ),
        publish_date=parse_publish_date(meta.get("published"), warnings),
        status=0 if meta.get("draft") is True else 1,
        content=raw_content,
        content_hash=hashlib.sha256(raw_content.encode("utf-8")).hexdigest(),
        warnings=warnings,
    )


def collect_posts(posts_root: Path, limit: int | None) -> tuple[list[Post], list[tuple[Path, str]]]:
    """扫描目录 → (成功列表, [(文件, 失败原因)])。单篇失败不影响整批。"""
    if not posts_root.is_dir():
        raise SystemExit(f"[FATAL] 文章目录不存在: {posts_root}")
    files = sorted(posts_root.rglob("*.md"))
    if limit is not None:
        files = files[:limit]
    posts: list[Post] = []
    failures: list[tuple[Path, str]] = []
    for path in files:
        try:
            posts.append(build_post(path, posts_root))
        except PostError as exc:
            failures.append((path, f"解析失败: {exc}"))
        except UnicodeDecodeError as exc:
            failures.append((path, f"编码失败(非 utf-8): {exc}"))
        except OSError as exc:
            failures.append((path, f"读取失败: {exc}"))
        except Exception as exc:  # noqa: BLE001 - 兜底：绝不因为一篇崩掉整批
            failures.append((path, f"未知错误 {type(exc).__name__}: {exc}"))
    return posts, failures


# --------------------------------------------------------------------------- #
# 数据库
# --------------------------------------------------------------------------- #


def connect_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    """把 CLI / 环境变量 / .env 合成 ``pymysql.connect(**kwargs)`` 的参数。

    单独抽出来是因为断连重连也要用同一套参数 —— 重连时不能再走一遍
    ``--init-schema`` 之类的分支，但**必须**连到同一个库。
    """
    env = load_env_file(Path(args.env_file))
    host = args.host or os.environ.get("DB_HOST") or env.get("DB_HOST")
    port = args.port or int(os.environ.get("DB_PORT") or env.get("DB_PORT") or 3306)
    user = args.user or os.environ.get("DB_USER") or env.get("DB_USER")
    password = args.password or os.environ.get("DB_PASSWORD") or env.get("DB_PASSWORD")
    if not (host and user and password):
        raise SystemExit(f"[FATAL] 连接信息不全，检查 {args.env_file} 里的 DB_HOST/DB_USER/DB_PASSWORD")
    return {
        "host": host, "port": port, "user": user, "password": password,
        "charset": "utf8mb4", "autocommit": False,
    }


def connect(args: argparse.Namespace) -> "pymysql.connections.Connection":
    """连接目标库（库名走 --database，凭据取 CLI > 环境变量 > .env）。

    先不带 database 连接，再 ``USE``：这样 ``--init-schema`` 时影子库还不存在也能连上。
    """
    if pymysql is None:
        raise SystemExit("[FATAL] 需要 pymysql（本项目 .venv 已装，请用 .venv\\Scripts\\python.exe 运行）")
    conn = pymysql.connect(**connect_kwargs(args))
    try:
        with conn.cursor() as cur:
            if args.init_schema:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{args.database}` "
                    "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                )
            cur.execute(f"USE `{args.database}`")
        conn.commit()
    except pymysql.err.MySQLError as exc:
        conn.close()
        raise SystemExit(f"[FATAL] 无法使用库 {args.database}: {exc}") from exc
    return conn


def connect_with_retry(
    args: argparse.Namespace, *, retries: int | None = None
) -> "pymysql.connections.Connection":
    """连库；**只有明确断连**才重试，其它异常（认证/SQL）照旧抛出。

    重试只包住「建连接」这一步：它是断连重连时唯一需要重做的动作，
    ``--init-schema`` 那种一次性动作不该被重放。
    """
    retries = DEFAULT_RECONNECT_RETRIES if retries is None else max(0, int(retries))
    last: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            return connect(args)
        except SystemExit:
            raise  # 认证/库名/参数问题：不是断连，别重试
        except Exception as exc:  # noqa: BLE001 - 判据在 is_disconnect()
            if not is_disconnect(exc) or attempt == retries:
                raise
            last = exc
            print(f"\n  [RECONNECT] 连接失败（{type(exc).__name__}: {exc}），"
                  f"{RECONNECT_DELAY_SECONDS:.0f}s 后重试（{attempt + 1}/{retries}）")
            time.sleep(RECONNECT_DELAY_SECONDS)
    raise AssertionError(f"不可达：{last!r}")  # pragma: no cover - 循环里必 return 或 raise


def init_schema(conn: "pymysql.connections.Connection") -> None:
    """按生产库原样创建两张表 + v_blog 视图（幂等，IF NOT EXISTS）。"""
    with conn.cursor() as cur:
        cur.execute(DDL_BLOG)
        cur.execute(DDL_BLOG_CONTENT)
        cur.execute(DDL_V_BLOG)
    conn.commit()


def _upsert_one(cur: Any, post: Post) -> bool:
    """写一篇（blog 行 + blog_content 行），返回 ``True``=INSERT / ``False``=UPDATE。

    **不做事务边界管理**：SAVEPOINT 由调用方在本函数外面、``try`` 里面建（见
    ``upsert_batch`` 里那段注释 —— 建 SAVEPOINT 自己也可能撞上断连）。

    UPDATE 分支刻意**不写 view_count**：那是线上阅读量，不是从 markdown 派生的字段，
    重跑一次就把它清零等于丢数据。INSERT 时给 0（新文章从零开始）。
    """
    cur.execute(
        "SELECT id FROM blog WHERE title=%s AND blog_url=%s LIMIT 1",
        (post.title, post.blog_url),
    )
    row = cur.fetchone()
    if row:
        blog_id = row[0]
        cur.execute(
            """UPDATE blog SET topic=%s, category=%s, blog_type=%s, tags=%s,
                   word_count=%s, has_code=%s, difficulty=%s, author=%s,
                   intro=%s, publish_date=%s, status=%s
               WHERE id=%s""",
            (post.topic, post.category, post.blog_type, post.tags, post.word_count,
             post.has_code, post.difficulty, AUTHOR, post.intro, post.publish_date,
             post.status, blog_id),
        )
        is_insert = False
    else:
        is_insert = True
        cur.execute(
            """INSERT INTO blog
                   (topic, category, blog_type, tags, word_count, has_code,
                    difficulty, title, author, intro, blog_url, publish_date,
                    view_count, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (post.topic, post.category, post.blog_type, post.tags, post.word_count,
             post.has_code, post.difficulty, post.title, AUTHOR, post.intro,
             post.blog_url, post.publish_date, 0, post.status),
        )
        blog_id = cur.lastrowid
    if post.content.strip():
        cur.execute(
            """INSERT INTO blog_content
                   (blog_id, content, content_fmt, toc, code_lang, content_hash, updated_at)
               VALUES (%s,%s,%s,NULL,NULL,%s,NOW())
               ON DUPLICATE KEY UPDATE
                   content=VALUES(content), content_fmt=VALUES(content_fmt),
                   content_hash=VALUES(content_hash), updated_at=NOW()""",
            (blog_id, post.content, CONTENT_FMT, post.content_hash),
        )
    return is_insert


def upsert_batch(
    conn: "pymysql.connections.Connection", posts: list[Post]
) -> tuple[int, int]:
    """写**一批**（调用方负责定批大小），一批 = 一个事务。

    单篇失败回滚到该篇自己的 SAVEPOINT（含它插入的 blog 行），不影响本批其它篇；
    **断连**不在这里吞掉 —— 往上抛给 ``upsert_posts`` 去重连重试整批
    （本批尚未 commit，重试时这批什么痕迹都不在，重做是干净的）。
    """
    inserted = updated = 0
    with conn.cursor() as cur:
        for post in posts:
            try:
                # ⚠️ SAVEPOINT 必须在 try **里面**：建 SAVEPOINT 本身就要往服务器发
                # 一条语句，链路正好在这里断掉时，写在 try 外面会让异常直接冲出
                # 整个循环（实测两次都是这么挂的：(2013, 'Lost connection') 之后
                # 跟着 InterfaceError，整批 138 篇一起回滚）。
                cur.execute("SAVEPOINT sp_post")
                is_insert = _upsert_one(cur, post)
                cur.execute("RELEASE SAVEPOINT sp_post")
            except pymysql.err.MySQLError as exc:
                if is_disconnect(exc, conn):
                    raise  # 交给上层重连重试整批；本批未 commit，无需回滚
                # 单篇的 SQL 错误（约束/语法/超长…）：回滚这一篇，继续下一批里的下一篇
                try:
                    cur.execute("ROLLBACK TO SAVEPOINT sp_post")
                    cur.execute("RELEASE SAVEPOINT sp_post")
                except pymysql.err.MySQLError:
                    pass  # SAVEPOINT 没建成（就是上面那条断连路径），无需回滚
                print(f"  [FAIL] {post.source.name}: {type(exc).__name__}: {exc}")
                continue
            if is_insert:
                inserted += 1
            else:
                updated += 1
    conn.commit()
    return inserted, updated


def upsert_posts(
    conn: "pymysql.connections.Connection",
    posts: list[Post],
    args: argparse.Namespace | None = None,
    *,
    batch_size: int | None = None,
    fail_batch: int | None = None,
) -> tuple[int, int, "pymysql.connections.Connection"]:
    """分批 upsert，遇明确断连**重连并重试当前这一批**。

    返回 ``(inserted, updated, conn)`` —— conn 可能是重连后的**新对象**，
    调用方必须用返回值继续（旧对象已经废了）。

    为什么整批重试是安全的：批内未 commit，断连时服务器侧已随连接断开回滚；
    已 commit 的批不受影响，也不重做（``next_batch`` 只往前走）。

    ⚠️ 重试次数是**按批**算的（``reconnect_retries`` 次），不是全局的：链路一直
    不通时，一个"每次重连都成功、写这批就断"的死循环会让脚本永远转下去
    （写这个函数第一版就踩了，被
    ``test_gives_up_after_reconnect_retries_and_reraises`` 抓住）。到上限就抛出。
    """
    if batch_size is None:
        batch_size = DEFAULT_BATCH_SIZE
    batch_size = max(1, int(batch_size))
    retries = DEFAULT_RECONNECT_RETRIES if args is None else int(
        getattr(args, "reconnect_retries", DEFAULT_RECONNECT_RETRIES)
    )

    inserted = updated = 0
    next_batch = 1
    i = 0
    while i < len(posts):
        batch = posts[i: i + batch_size]
        attempts = 0
        while True:
            try:
                add_inserted, add_updated = upsert_batch(conn, batch)
            except pymysql.err.MySQLError as exc:
                if not is_disconnect(exc, conn):
                    raise  # 认证 / SQL 错：照旧抛出，不吞
                if args is None:
                    raise  # 没给 args 就没法重连（判据/参数都在里面），别装作能重试
                # ⚠️ close() 自己也会抛：pymysql 对已经关掉的连接抛
                # `pymysql.err.Error("Already closed")`（错误号是 None），
                # 而它**不是** MySQLError 的子类 —— 不吞掉就会盖住真正的断连异常，
                # 直接崩在"善后"这一步上（实测：`--fail-batch` 第一次真跑就是这么挂的）。
                # 关连接只是善后，失败无所谓：新连接马上会建，老对象反正要丢。
                try:
                    conn.close()
                except Exception as close_exc:  # noqa: BLE001 - 善后失败不该改变结论
                    print(f"  [warn] 关闭旧连接时又报错（忽略）："
                          f"{type(close_exc).__name__}: {close_exc}")
                print(f"\n  [DISCONNECT] 第 {next_batch} 批（{len(batch)} 篇）"
                      f"写库时链路断开：{type(exc).__name__}: {exc}")
                print(f"               已提交 {inserted + updated} 篇；本批未 commit"
                      f"（服务器侧随连接一起回滚），重连后重做是干净的")
                if attempts >= retries:
                    raise  # 试满了：把最后一次断连原样抛出，别无声无息地转下去
                attempts += 1
                # 每一次重连都只试 1 下：整体次数由上面这个 attempts 控制，
                # 免得两层各自重试、次数变成乘法。
                conn = connect_with_retry(args, retries=1)
                print(f"  [RECONNECT] 已重连，重试第 {next_batch} 批"
                      f"（第 {attempts}/{retries} 次）")
                continue  # i 原地不动：重做当前这批
            inserted += add_inserted
            updated += add_updated
            print(f"  [batch {next_batch}] {len(batch)} 篇已提交"
                  f"（新增 {add_inserted} / 更新 {add_updated}，累计 {inserted + updated}/{len(posts)}）")
            # --fail-batch：只在本批 commit **之后**人为断链路，用来在真库上演示
            # 「断连 → 重连 → 只有未提交的那批被重做」。仅用于验证，正常跑不要传。
            if fail_batch and next_batch == int(fail_batch):
                print(f"  [--fail-batch] 第 {next_batch} 批提交后人为断连（仅用于验证重连路径）")
                conn.close()
            break
        i += len(batch)
        next_batch += 1
    return inserted, updated, conn


def replace_all(conn: "pymysql.connections.Connection", database: str) -> None:
    """--replace：清空目标库 blog 表（外键 ON DELETE CASCADE 连带 blog_content）。"""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM blog")
        before = cur.fetchone()[0]
        cur.execute("DELETE FROM blog")
        cur.execute("ALTER TABLE blog AUTO_INCREMENT = 1")
    conn.commit()
    print(f"  [--replace] 已清空 {database}.blog（删除 {before} 行，blog_content 随 CASCADE 一并删除）")


# --------------------------------------------------------------------------- #
# 报表
# --------------------------------------------------------------------------- #


def _fmt_len_stats(values: list[int]) -> str:
    if not values:
        return "(无)"
    ordered = sorted(values)
    if len(ordered) >= 4:
        quartiles = statistics.quantiles(ordered, n=4)
        p25, p75 = quartiles[0], quartiles[2]
    else:
        p25 = p75 = statistics.median(ordered)
    return (
        f"min={min(ordered)}  p25={p25:g}  median={statistics.median(ordered):g}  "
        f"p75={p75:g}  max={max(ordered)}  mean={statistics.mean(ordered):.1f}"
    )


def print_report(
    posts: list[Post],
    failures: list[tuple[Path, str]],
    posts_root: Path,
    scanned: int,
    dry_run: bool,
    target: str,
) -> None:
    """打印解析统计（dry-run 与正式跑共用）。"""
    print("\n=== 解析统计 ===")
    print(f"posts 目录 : {posts_root}")
    print(f"扫描文件   : {scanned}")
    print(f"解析成功   : {len(posts)}")
    print(f"解析失败   : {len(failures)}")

    print(f"\n=== 目标 ===\n数据库     : {target}{'  (dry-run，不连接、不写入)' if dry_run else ''}")

    topics = Counter(p.topic for p in posts)
    print(f"\n=== topic 分布（{len(topics)} 个分类）===")
    for topic, count in topics.most_common():
        print(f"  {count:4d}  {topic}")

    print("\n=== blog_type 分布 ===")
    for name, count in Counter(p.blog_type for p in posts).most_common():
        print(f"  {count:4d}  {name}")

    print(f"\n=== word_count ===\n  {_fmt_len_stats([p.word_count for p in posts])}")

    print("\n=== has_code / status / difficulty ===")
    code = Counter(p.has_code for p in posts)
    print(f"  has_code : 有代码={code.get(1, 0)}  无代码={code.get(0, 0)}")
    status = Counter(p.status for p in posts)
    print(f"  status   : 1已发布={status.get(1, 0)}  0草稿={status.get(0, 0)}")
    diff = Counter(p.difficulty for p in posts)
    print("  difficulty: " + "  ".join(f"{d}={diff.get(d, 0)}" for d in range(1, 6)))

    dupes = defaultdict(list)
    for post in posts:
        dupes[post.title].append(post.blog_url)
    dupes = {t: urls for t, urls in dupes.items() if len(urls) > 1}
    print(f"\n=== 重名 title（精确匹配 {len(dupes)} 组）===")
    if not dupes:
        print("  (无)")
    for title, urls in dupes.items():
        print(f"  {title!r}: {len(urls)} 篇，blog_url 不同 → 视为两条独立记录")
        for url in urls:
            print(f"      {url}")

    # MySQL 的 utf8mb4_0900_ai_ci 大小写不敏感，List/list 之类在库内也算"同名"，
    # 这里一并提示，避免以后按 title 做唯一键时踩坑。
    ci_dupes = defaultdict(list)
    for post in posts:
        ci_dupes[post.title.casefold()].append(post.title)
    ci_dupes = {k: v for k, v in ci_dupes.items() if len(v) > 1}
    if ci_dupes:
        print(f"\n=== 大小写不敏感同名（DB 排序规则 utf8mb4_0900_ai_ci 下 {len(ci_dupes)} 组）===")
        for key, titles in ci_dupes.items():
            print(f"  {key!r}: {titles} → 靠 blog_url 区分，不影响幂等键")

    warned = [p for p in posts if p.warnings]
    print(f"\n=== 字段 warning（{len(warned)} 篇）===")
    for post in warned[:20]:
        print(f"  {post.source.name}: {'; '.join(post.warnings)}")
    if not warned:
        print("  (无)")

    if failures:
        print(f"\n=== 失败明细（{len(failures)}）===")
        for path, reason in failures:
            print(f"  {path}: {reason}")

    print("\n=== 样例行（前 5 篇，按路径排序）===")
    for post in posts[:5]:
        intro = (post.intro or "")[:24]
        print(
            f"  {post.rel_dir}/{post.slug}.md | title={post.title!r} topic={post.topic!r} "
            f"type={post.blog_type} wc={post.word_count} code={post.has_code} "
            f"diff={post.difficulty} status={post.status} date={post.publish_date} "
            f"tags={post.tags} intro={intro!r}"
        )
        print(f"      url={post.blog_url}  sha256={post.content_hash[:16]}…")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """命令行参数。"""
    parser = argparse.ArgumentParser(
        description="把 Astro 博客 Markdown 灌进 MySQL（默认只写影子库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--posts", default=os.environ.get("BLOG_POSTS_DIR") or str(DEFAULT_POSTS_DIR),
                        help="文章目录（默认站点 posts 目录）")
    parser.add_argument("--database", default=DEFAULT_SHADOW_DB,
                        help=f"目标库名（默认影子库 {DEFAULT_SHADOW_DB}；生产库 {PRODUCTION_DB} 需 --allow-production）")
    parser.add_argument("--dry-run", action="store_true", help="只解析并打印统计，不连库、不写库")
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 篇（按路径排序）")
    parser.add_argument("--replace", action="store_true",
                        help="先清空目标库 blog 表再全量插入（默认是按 title+blog_url upsert）")
    parser.add_argument("--init-schema", action="store_true",
                        help="先按生产库 DDL 建库/建表/建 v_blog 视图（IF NOT EXISTS，幂等）")
    parser.add_argument("--allow-production", action="store_true",
                        help=f"显式允许写生产库 {PRODUCTION_DB}（默认拒绝）")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"每批提交多少篇（默认 {DEFAULT_BATCH_SIZE}）。"
                             "跨公网链路一个事务跑到头太脆，分批 + 断连重连见 upsert_posts()")
    parser.add_argument("--reconnect-retries", type=int, default=DEFAULT_RECONNECT_RETRIES,
                        help=f"断连后最多重连几次（默认 {DEFAULT_RECONNECT_RETRIES}），每次重试同一个当前批")
    parser.add_argument("--fail-batch", type=int, default=None,
                        help="仅用于验证：在第 N 批**提交之后**人为断开连接，"
                             "用来演示「断连 → 重连 → 只重做未提交的那批」。正常跑不要传")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help=".env 路径")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--user", default=None)
    parser.add_argument("--password", default=None, help="一般不用传，读 .env 即可（不会打印）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """入口。"""
    args = parse_args(argv)

    # --- 安全闸门：绝不允许默认/误伤生产库 ------------------------------- #
    if not DB_NAME_RE.match(args.database or ""):
        print(f"[FATAL] 库名非法（只允许字母数字下划线）: {args.database!r}")
        return 2
    if args.database == PRODUCTION_DB and not args.allow_production:
        print(f"[REFUSED] 目标库是生产库 {PRODUCTION_DB}；本脚本默认只写影子库 {DEFAULT_SHADOW_DB}。")
        print(f"          确需写生产库请显式加 --allow-production --database {PRODUCTION_DB}（本次未执行任何写操作）。")
        return 2

    posts_root = Path(args.posts)
    if not posts_root.is_dir():
        print(f"[FATAL] 文章目录不存在: {posts_root}")
        return 2
    if args.batch_size < 1:
        print(f"[FATAL] --batch-size 必须 >= 1（收到 {args.batch_size}）")
        return 2
    if args.reconnect_retries < 0:
        print(f"[FATAL] --reconnect-retries 不能为负（收到 {args.reconnect_retries}）")
        return 2
    if args.fail_batch is not None and args.database != DEFAULT_SHADOW_DB:
        # --fail-batch 是**故意**断链路。库名必须恰好是影子库：既挡住生产库，
        # 也挡住 --database 指向的其它库 —— 别把"测试开关"用在真实数据上。
        print(f"[REFUSED] --fail-batch 只能配合影子库 {DEFAULT_SHADOW_DB} 使用"
              f"（当前 --database {args.database}）。本次未执行任何写操作。")
        return 2

    scanned = len(sorted(posts_root.rglob("*.md")))
    if args.limit is not None:
        scanned = min(scanned, args.limit)

    posts, failures = collect_posts(posts_root, args.limit)
    print_report(posts, failures, posts_root, scanned, args.dry_run, args.database)

    if args.dry_run:
        print("\n[dry-run] 未连接数据库，未写入任何数据。")
        return 0

    # 首次连接也走重连逻辑：这台远端库的握手本身就会抖（本轮实测真撞到过一次
    # `OperationalError(2013)` 直接崩在 pymysql.connect 里，还没开始写就结束）。
    conn = connect_with_retry(args)
    # 重连可能把 conn 换成新对象（见 upsert_posts 的返回值）。把它记在 main 自己的
    # 名字上，finally 才关得到**最后那条**；不记录的话会在进程退出时才回收。
    conns: list[Any] = [conn]
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DATABASE()")
            print(f"\n已连接 : {cur.fetchone()[0]}")
        if args.init_schema:
            init_schema(conn)
            print(f"[--init-schema] {args.database}: blog / blog_content / v_blog 已就绪")
        if args.replace:
            replace_all(conn, args.database)
        print(f"\n=== 分批写入（每 {args.batch_size} 篇一个事务；"
              f"断连自动重连，最多 {args.reconnect_retries} 次）===")
        inserted, updated, conn = upsert_posts(
            conn, posts, args,
            batch_size=args.batch_size, fail_batch=args.fail_batch,
        )
        conns.append(conn)

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM blog")
            total = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM v_blog")
            published = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM blog_content")
            contents = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM blog WHERE word_count IS NULL OR topic IS NULL OR author IS NULL"
            )
            nulls = cur.fetchone()[0]
            # 库内自校验：存的 hash 必须等于 content 的 sha256（防编码/截断损坏）
            cur.execute("SELECT COUNT(*) FROM blog_content WHERE content_hash <> SHA2(content, 256)")
            hash_drift = cur.fetchone()[0]
        print("\n=== 写入结果 ===")
        print(f"新增 {inserted} 行 / 更新 {updated} 行")
        print(f"{args.database}.blog 总计        : {total} （本次解析 {len(posts)} 篇）")
        print(f"{args.database}.v_blog 总计      : {published}")
        print(f"{args.database}.blog_content 总计: {contents}")
        print(f"word_count/topic/author 为 NULL: {nulls}")
        print(f"content_hash 与 content 不符    : {hash_drift}")
        if total != len(posts) and not args.limit:
            print("[WARN] blog 行数与本次解析篇数不一致，请检查是否有历史残留（可用 --replace 全量重建）")
        return 0
    finally:
        # 每一条用过的连接都关一遍（重连会换新对象）。pymysql 对已关掉的连接抛
        # Error("Already closed")（不是 MySQLError 子类）—— 这里只是善后，
        # 关不上就说一声，别用它的异常盖住真正的失败原因。
        for old in conns:
            try:
                old.close()
            except Exception as close_exc:  # noqa: BLE001 - 善后失败不该改变结论
                print(f"[warn] 关闭连接时报错（忽略）：{type(close_exc).__name__}: {close_exc}")


if __name__ == "__main__":
    sys.exit(main())
