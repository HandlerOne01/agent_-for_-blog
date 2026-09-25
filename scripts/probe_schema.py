# -*- coding: utf-8 -*-
"""只读探针：看 my_blog 的表结构与 blog_url 真实形态。只执行 SELECT / SHOW。

绝不打印任何密钥/密码。用法：
    .venv\\Scripts\\python.exe -X utf8 scripts\\probe_schema.py
"""
import io
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

URI = (
    f"mysql+pymysql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
    f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
)


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    engine = create_engine(URI)
    with engine.connect() as conn:
        print("== SHOW TABLES ==")
        for row in conn.execute(text("SHOW TABLES")):
            print("  ", row[0])
        print("== DESCRIBE blog ==")
        cols = []
        for row in conn.execute(text("SHOW COLUMNS FROM blog")):
            cols.append(row[0])
            print("   %-14s %-14s null=%s key=%s default=%s" % (row[0], row[1], row[2], row[3], row[4]))
        print("== COUNT ==")
        print("   total rows:", conn.execute(text("SELECT COUNT(*) FROM blog")).scalar())
        print("== sample rows (title/category/word_count/difficulty/blog_url) ==")
        q = ("SELECT title, category, word_count, difficulty, blog_url "
             "FROM blog ORDER BY id LIMIT 6")
        for row in conn.execute(text(q)):
            print("   ", tuple(row))
        print("== blog_url shape: distinct prefixes ==")
        q2 = ("SELECT SUBSTRING_INDEX(blog_url,'/',3) AS pfx, COUNT(*) c "
              "FROM blog GROUP BY pfx ORDER BY c DESC LIMIT 10")
        for row in conn.execute(text(q2)):
            print("   %-40s %s" % (row[0], row[1]))
        print("== linux-ish rows (for the Linux test query) ==")
        q3 = ("SELECT title, category, word_count, difficulty, blog_url FROM blog "
              "WHERE title LIKE '%Linux%' OR category LIKE '%Linux%' OR title LIKE '%进程%' "
              "ORDER BY word_count LIMIT 8")
        for row in conn.execute(text(q3)):
            print("   ", tuple(row))
        print("== does a quantum/量子 topic exist? ==")
        n = conn.execute(text("SELECT COUNT(*) FROM blog WHERE title LIKE '%量子%' OR intro LIKE '%量子%' OR tags LIKE '%量子%'")).scalar()
        print("   rows matching 量子:", n)
    engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
