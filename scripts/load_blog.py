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
``view_count``        0
``blog_url``          ``https://imiss.example.com/posts/<相对目录>/<slug>/``
``status``            ``draft: true`` → 0，否则 1
``blog_content``      一篇一行：content=正文 markdown 原文（仅统一换行为 \n、
                      去掉首尾空行，其他排版原样保留）、content_fmt=markdown、
                      content_hash=该 content 的 sha256、updated_at=now；
                      toc / code_lang 留空（NULL）
====================  ==========================================================

用法::

    # 只看统计，不连库
    python scripts/load_blog.py --dry-run

    # 在影子库建库建表建视图，然后灌数据
    python scripts/load_blog.py --init-schema --database my_blog_rebuild

    # 只灌 10 篇做抽样
    python scripts/load_blog.py --database my_blog_rebuild --limit 10
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

try:
    import pymysql
except ImportError:  # pragma: no cover - 环境缺依赖时的友好提示
    pymysql = None  # type: ignore[assignment]


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


def connect(args: argparse.Namespace) -> "pymysql.connections.Connection":
    """连接目标库（库名走 --database，凭据取 CLI > 环境变量 > .env）。

    先不带 database 连接，再 ``USE``：这样 ``--init-schema`` 时影子库还不存在也能连上。
    """
    if pymysql is None:
        raise SystemExit("[FATAL] 需要 pymysql（本项目 .venv 已装，请用 .venv\\Scripts\\python.exe 运行）")
    env = load_env_file(Path(args.env_file))
    host = args.host or os.environ.get("DB_HOST") or env.get("DB_HOST")
    port = args.port or int(os.environ.get("DB_PORT") or env.get("DB_PORT") or 3306)
    user = args.user or os.environ.get("DB_USER") or env.get("DB_USER")
    password = args.password or os.environ.get("DB_PASSWORD") or env.get("DB_PASSWORD")
    if not (host and user and password):
        raise SystemExit(f"[FATAL] 连接信息不全，检查 {args.env_file} 里的 DB_HOST/DB_USER/DB_PASSWORD")
    conn = pymysql.connect(
        host=host, port=port, user=user, password=password,
        charset="utf8mb4", autocommit=False,
    )
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


def init_schema(conn: "pymysql.connections.Connection") -> None:
    """按生产库原样创建两张表 + v_blog 视图（幂等，IF NOT EXISTS）。"""
    with conn.cursor() as cur:
        cur.execute(DDL_BLOG)
        cur.execute(DDL_BLOG_CONTENT)
        cur.execute(DDL_V_BLOG)
    conn.commit()


def upsert_posts(conn: "pymysql.connections.Connection", posts: list[Post]) -> tuple[int, int]:
    """写入(幂等)：命中 ``(title, blog_url)`` 就 UPDATE，否则 INSERT。

    返回 ``(inserted, updated)``。整批一个事务，单篇失败回滚到该篇的 SAVEPOINT。
    """
    inserted = updated = 0
    with conn.cursor() as cur:
        for post in posts:
            cur.execute("SAVEPOINT sp_post")
            row = None
            is_insert = False
            try:
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
                               intro=%s, publish_date=%s, view_count=%s, status=%s
                           WHERE id=%s""",
                        (post.topic, post.category, post.blog_type, post.tags, post.word_count,
                         post.has_code, post.difficulty, AUTHOR, post.intro, post.publish_date,
                         0, post.status, blog_id),
                    )
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
                cur.execute("RELEASE SAVEPOINT sp_post")
                if is_insert:
                    inserted += 1
                else:
                    updated += 1
            except pymysql.err.MySQLError as exc:
                # 该篇整体回滚（含 blog 行），不影响其他篇
                cur.execute("ROLLBACK TO SAVEPOINT sp_post")
                cur.execute("RELEASE SAVEPOINT sp_post")
                print(f"  [FAIL] {post.source.name}: {type(exc).__name__}: {exc}")
    conn.commit()
    return inserted, updated


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

    scanned = len(sorted(posts_root.rglob("*.md")))
    if args.limit is not None:
        scanned = min(scanned, args.limit)

    posts, failures = collect_posts(posts_root, args.limit)
    print_report(posts, failures, posts_root, scanned, args.dry_run, args.database)

    if args.dry_run:
        print("\n[dry-run] 未连接数据库，未写入任何数据。")
        return 0

    conn = connect(args)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DATABASE()")
            print(f"\n已连接 : {cur.fetchone()[0]}")
        if args.init_schema:
            init_schema(conn)
            print(f"[--init-schema] {args.database}: blog / blog_content / v_blog 已就绪")
        if args.replace:
            replace_all(conn, args.database)
        inserted, updated = upsert_posts(conn, posts)

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
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
