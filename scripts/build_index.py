# -*- coding: utf-8 -*-
"""建向量索引：把 blog 表里每篇文章的「标题 + 摘要」灌进 LangChain 的
InMemoryVectorStore，然后 dump 成 data/blog_index.json。

只读数据库（只有 SELECT），不改任何东西。跑一次就够；以后加了新文章再跑一次。

注意：向量是【建库时算的】，所以一旦换了 EMBEDDING_MODEL 就必须重跑本脚本，
否则检索出来的东西没有意义。

用法（在 blog-agent 目录下）：
    .venv\\Scripts\\python.exe -X utf8 scripts\\build_index.py --dry-run
    .venv\\Scripts\\python.exe -X utf8 scripts\\build_index.py
"""
import io
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.documents import Document
from langchain_core.vectorstores import InMemoryVectorStore
from sqlalchemy import create_engine, text

# 让脚本能 import 到项目里的模块（scripts/ 的上一层就是项目根）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.common import retrieval  # noqa: E402

load_dotenv()

# 一篇一行；status=1 才是已发布的，和 v_blog 视图一个口径
SQL = """
SELECT id, title, intro, category, word_count, difficulty, blog_url, tags
FROM blog
WHERE status = 1
ORDER BY id
"""

# 一次喂多少条给 embedding 接口。批量太大有的服务会拒，16 稳妥。
BATCH = 16


def db_uri() -> str:
    """按环境变量拼连接串（和 node/recommend.py 一致）。"""
    return (
        f"mysql+pymysql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
        f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
    )


def fetch_posts(limit=None) -> list:
    engine = create_engine(db_uri())
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(SQL)).fetchall()
    finally:
        engine.dispose()

    posts = []
    for row in rows:
        posts.append(
            {
                "id": int(row[0]),
                "title": row[1] or "",
                "intro": row[2] or "",
                "category": row[3] or "",
                "word_count": int(row[4]) if row[4] is not None else None,
                "difficulty": int(row[5]) if row[5] is not None else None,
                "blog_url": row[6] or "",
                "tags": row[7] or "",
            }
        )
    return posts[:limit] if limit else posts


def build_text(post: dict) -> str:
    """喂给模型的文本：标题 + 摘要 + 分类 + 标签。

    为什么带分类和标签：库里 category="八股文" 的有 12 篇，但其中只有 1 篇的
    标题出现"八股"两个字。旧口径（标题+摘要）下，另外 11 篇对"八股文"这个
    查询完全不可见 —— 实测 top1 只有 0.497，连 0.60 的门都过不了。分类和标签
    是库里的结构化事实，拼进来才能被语义命中。

    tags 存的是 ",八股文," 这种带前缀逗号的形态（为了 SQL LIKE 精确匹配），
    这里 strip(",") 还原成裸词再喂模型，少一点标点噪声。

    必须和 retrieval.py 标定门槛时用的格式一致，改这里等于换了一套向量，
    门槛 RECALL_SCORE_MIN 必须重标。
    """
    tags = str(post.get("tags") or "").strip(",").strip()
    return "。".join(
        p for p in (post["title"], post["intro"], post["category"], tags) if p
    ).strip()


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    dry_run = "--dry-run" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    posts = fetch_posts(limit)
    print(f"从库里读到 {len(posts)} 篇（status=1）")
    if not posts:
        print("一篇都没读到，停手 —— 检查 .env 里的 DB_* 配置")
        return 1

    if dry_run:
        print(f"\n--dry-run：要喂给 {retrieval.EMBEDDING_MODEL} 的文本长这样")
        for post in posts[:5]:
            print(f"  [{post['id']}] {build_text(post)[:80]}…")
        print(f"\n共 {len(posts)} 条，模型 = {retrieval.EMBEDDING_MODEL}")
        return 0

    docs = [
        Document(
            page_content=build_text(post),
            metadata={
                "id": post["id"],
                "title": post["title"],
                "category": post["category"],
                "word_count": post["word_count"],
                "difficulty": post["difficulty"],
                "blog_url": post["blog_url"],
                "tags": post["tags"],
            },
        )
        for post in posts
    ]

    store = InMemoryVectorStore(embedding=retrieval.get_embeddings())
    for start in range(0, len(docs), BATCH):
        store.add_documents(docs[start : start + BATCH])
        print(f"  已灌入 {min(start + BATCH, len(docs))}/{len(docs)}")

    store.dump(str(retrieval.INDEX_PATH))
    size_mb = retrieval.INDEX_PATH.stat().st_size / 1024 / 1024
    print(
        f"\n索引写好了：{len(docs)} 篇\n"
        f"  {retrieval.INDEX_PATH}  ({size_mb:.1f} MB)\n"
        f"  模型 = {retrieval.EMBEDDING_MODEL}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
