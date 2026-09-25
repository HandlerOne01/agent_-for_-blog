# -*- coding: utf-8 -*-
"""收尾核查：数据库零写入的证据。只执行 SELECT / SHOW。

诚实说明：本库账号 `kiana_mysql` **是有写权限的**（见下），所以
"零写入" 不能靠权限证明，只能靠三样东西：
  1. 本次所有实际执行过的 SQL —— 全部是 SELECT，逐条列出（从验证存档里抽）；
  2. 行数与内容指纹，供事后比对；
  3. blog_content.updated_at 的最大值 —— 只要有行被改过，它就会动。

绝不打印任何密钥/密码的值。
"""
import io
import json
import os
import re
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

URI = (
    f"mysql+pymysql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
    f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
)

DML = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|REPLACE|CREATE)\b", re.I)


def extract_executed_sql():
    """从验证存档里把真正发给数据库的 SQL 抽出来。"""
    found = []
    for name in ("verify_v3_reco.txt", "verify_v3_chat.txt", "db_degrade_result.txt"):
        path = os.path.join(PROJ, name)
        if not os.path.exists(path):
            continue
        with io.open(path, encoding="utf-8") as f:
            for line in f:
                if '"name": "sql_db_query"' in line:
                    for m in re.finditer(r'"query":\s*"((?:[^"\\]|\\.)*)"', line):
                        # 用 json 解转义，别用 unicode_escape —— 后者会把
                        # UTF-8 中文再解一遍，变成乱码（第一版就是这么错的）。
                        sql = json.loads('"%s"' % m.group(1))
                        found.append((name, sql))
    return found


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    print("=" * 78)
    print("A) 本次实际发给数据库的查询语句（全部来自验证存档）")
    print("=" * 78)
    executed = extract_executed_sql()
    seen = []
    for src, sql in executed:
        if sql not in seen:
            seen.append(sql)
    for i, sql in enumerate(seen, 1):
        is_dml = bool(DML.search(sql))
        print("%2d. [%s] %s" % (i, "!! DML" if is_dml else "SELECT", sql))
    print("   共 %d 条不同语句；其中含 DML 的：%d 条"
          % (len(seen), sum(1 for s in seen if DML.search(s))))

    engine = create_engine(URI)
    with engine.connect() as conn:
        print()
        print("=" * 78)
        print("B) 账号与权限（诚实记录：该账号确实有写权限）")
        print("=" * 78)
        print("   CURRENT_USER =", conn.execute(text("SELECT CURRENT_USER()")).scalar())
        grants = [r[0] for r in conn.execute(text("SHOW GRANTS FOR CURRENT_USER()"))]
        ddl_dml = sorted({k for g in grants for k in
                          re.findall(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE)\b", g, re.I)})
        print("   权限里出现的写类关键字:", ddl_dml if ddl_dml else "无")
        print("   -> 结论：不能靠权限证明零写入，改用下面的内容证据。")

        print()
        print("=" * 78)
        print("C) 行数 / 内容指纹 / 最后修改时间")
        print("=" * 78)
        n_blog = conn.execute(text("SELECT COUNT(*) FROM blog")).scalar()
        n_body = conn.execute(text("SELECT COUNT(*) FROM blog_content")).scalar()
        print("   blog          rows = %s" % n_blog)
        print("   blog_content  rows = %s" % n_body)

        fp = conn.execute(text(
            "SELECT COUNT(*), "
            "COALESCE(SUM(CRC32(CONCAT_WS('|', id, title, word_count, difficulty, blog_url))),0), "
            "COALESCE(SUM(CRC32(CONCAT_WS('|', id, MD5(intro)))),0), "
            "COALESCE(MAX(id),0) FROM blog"
        )).first()
        print("   blog   指纹 (count, crc32(核心列), crc32(md5(intro)), max_id) = %s" % (tuple(fp),))

        fp2 = conn.execute(text(
            "SELECT COUNT(*), "
            "COALESCE(SUM(CRC32(CONCAT_WS('|', blog_id, content_hash))),0), "
            "COALESCE(MAX(updated_at),'<null>') FROM blog_content"
        )).first()
        print("   content 指纹 (count, crc32(blog_id+content_hash), max_updated_at) = %s" % (tuple(fp2),))
        print("   ^ 只要有任意一行被 UPDATE 过，max_updated_at 就会往前跳。")

        print()
        print("=" * 78)
        print("D) 服务端写入类计数器快照（供事后比对；是自服务器启动以来的累计值）")
        print("=" * 78)
        for name in ("Com_insert", "Com_update", "Com_delete", "Com_replace",
                     "Com_create_table", "Com_drop_table", "Com_alter_table"):
            v = conn.execute(text("SHOW GLOBAL STATUS LIKE :n"), {"n": name}).first()
            print("   %-20s = %s" % (name, v[1] if v else "?"))

        print()
        print("=" * 78)
        print("E) information_schema 里的表元数据")
        print("=" * 78)
        for r in conn.execute(text(
            "SELECT table_name, table_rows, update_time, create_time FROM "
            "information_schema.tables WHERE table_schema = :s ORDER BY table_name"
        ), {"s": os.getenv("DB_NAME")}):
            print("   %-14s table_rows=%-8s update_time=%-20s create_time=%s"
                  % (r[0], r[1], r[2], r[3]))

    engine.dispose()

    print()
    print("=" * 78)
    print("F) 本次会话真正执行过的语句（从源码里 text(...) 字面量抽，不看注释）")
    print("=" * 78)
    for s in ("scripts/probe_schema.py", "scripts/verify_db_readonly.py",
              "scripts/db_degrade_test.py"):
        p = os.path.join(PROJ, s)
        if not os.path.exists(p):
            continue
        with io.open(p, encoding="utf-8") as f:
            body = f.read()
        # 只认 text("...") / text('''...''') / text(f"...") 里的第一个词
        kinds = []
        for m in re.finditer(r"text\(\s*[fr]?[\"']{1,3}(.*?)[\"']{1,3}\s*[,)]",
                             body, re.S):
            head = m.group(1).strip().split()
            if head:
                kinds.append(head[0].upper())
        uniq = sorted(set(kinds))
        print("   %-38s 执行过: %s" % (s, uniq))
    print("   （A) 段列的是真正发到服务端的语句，那一份才是权威。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
