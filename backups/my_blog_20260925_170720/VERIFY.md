# Backup verification — `my_blog`

- Backup directory: `D:\HuaweiMoveData\Users\HUAWEI\Desktop\agent_-for_-blog\backups\my_blog_20260925_170720`
- Generated: 2026-09-25 17:07:20 local
- Server: MySQL 8.0.45-0ubuntu0.24.04.1 at `106.52.158.48:3306` (charset_server=utf8mb4, collation_server=utf8mb4_0900_ai_ci)
- Account: `kiana_mysql` — **read-only operations only** (`SHOW`/`SELECT`/`information_schema`); no INSERT/UPDATE/DELETE/DDL was issued.
- Total backup size: **1.3 MiB** (1363529 bytes), 12 file(s)

## 1. Row counts — database vs exported files

| table | DB `COUNT(*)` | rows in JSON | `INSERT` stmts in data.sql | match |
|---|---:|---:|---:|:--:|
| `blog` | 12 | 12 | 12 | ✅ |
| `blog_content` | 1 | 1 | 1 | ✅ |

**Verdict: ALL ROW COUNTS MATCH**

## 2. Objects in the schema

| object | type | engine | collation | approx rows |
|---|---|---|---|---:|
| `blog` | BASE TABLE | InnoDB | utf8mb4_0900_ai_ci | 12 |
| `blog_content` | BASE TABLE | InnoDB | utf8mb4_0900_ai_ci | 1 |
| `v_blog` | VIEW | - | - | None |

### Indexes (evidence that these tables must never be dropped carelessly)

| table | index | unique | type | columns |
|---|---|:--:|---|---|
| `blog` | `PRIMARY` | yes | BTREE | `id` |
| `blog` | `idx_topic_word` | no | BTREE | `topic`, `word_count` |
| `blog` | `idx_topic_hot` | no | BTREE | `topic`, `view_count` |
| `blog` | `idx_publish` | no | BTREE | `publish_date` |
| `blog_content` | `PRIMARY` | yes | BTREE | `blog_id` |
| `blog_content` | `ft_content` | no | FULLTEXT | `content` |

Total indexes: **6**

### Primary keys / unique constraints / AUTO_INCREMENT

- **`blog`** — PK: `id` · unique: (none) · AUTO_INCREMENT: `id`
- **`blog_content`** — PK: `blog_id` · unique: (none) · AUTO_INCREMENT: (none)
- **`v_blog`** — PK: (none) · unique: (none) · AUTO_INCREMENT: (none)

### Foreign keys

| table | constraint | columns | → referenced | on delete | on update |
|---|---|---|---|---|---|
| `blog_content` | `fk_content_blog` | `blog_id` | `blog`(`id`) | CASCADE | NO ACTION |

### Restore-safety self-check on the emitted DDL

- `schema.sql` statements: 14 (ALTER×1, CREATE×3, DROP×3, SET×7)
- Deferred `ALTER TABLE ... ADD CONSTRAINT` for FKs emitted: **1** (information_schema reports 1)
- Inline `FOREIGN KEY` clauses left inside `CREATE TABLE`: **0** (must be 0, otherwise a restore would hit a duplicate constraint)
- FK clauses relocated out of the table bodies:
  - `blog_content`: `CONSTRAINT `fk_content_blog` FOREIGN KEY (`blog_id`) REFERENCES `blog` (`id`) ON DELETE CASCADE`

### FULLTEXT / ngram indexes

- `blog_content`.`ft_content` on (`content`)
- Server `ngram_token_size` = `2`

### Views

- `v_blog`: `CREATE ALGORITHM=MERGE DEFINER=`kiana_mysql`@`%` SQL SECURITY INVOKER VIEW `v_blog` AS select `blog`.`id` AS `id`,`blog`.`title` AS `title`,`blog`.`author` AS `author`,`blog`.`topic` AS `topic`,`blog`.`category` AS `category`,`blog`.`blog_type` AS `blog_type`,`blog`.`tags` AS `tags`,`blog`.`word_count` AS `word_count`,`blog`.`difficulty` AS `difficulty`,`blog`.`has_code` AS `has_code`,`blog`.`intro` AS `intro`,`blog`.`blog_url` AS `blog_url`,`blog`.`view_count` AS `view_count`,`blog`.`publish_date` AS `publish_date` from `blog` where (`blog`.`status` = 1)`

### Triggers / CHECK constraints

- Triggers: 3
  - `trg_blog_content_words_del` AFTER DELETE ON `blog_content`
  - `trg_blog_content_words_ins` AFTER INSERT ON `blog_content`
  - `trg_blog_content_words_upd` AFTER UPDATE ON `blog_content`
- CHECK constraints: 0

## 3. Local LangGraph state

- Source: `D:\HuaweiMoveData\Users\HUAWEI\Desktop\agent_-for_-blog\.langgraph_api`
- Copied to: `D:\HuaweiMoveData\Users\HUAWEI\Desktop\agent_-for_-blog\backups\my_blog_20260925_170720\langgraph_api`
- 7 file(s), 1.3 MiB

| file | bytes | verified |
|---|---:|:--:|
| `.langgraph_checkpoint.1.pckl` | 276828 | ✅ |
| `.langgraph_checkpoint.2.pckl` | 127244 | ✅ |
| `.langgraph_checkpoint.3.pckl` | 752576 | ✅ |
| `.langgraph_ops.pckl` | 165904 | ✅ |
| `.langgraph_retry_counter.pckl` | 6 | ✅ |
| `store.pckl` | 1614 | ✅ |
| `store.vectors.pckl` | 6 | ✅ |

## 4. File inventory

Backup directory: **`D:\HuaweiMoveData\Users\HUAWEI\Desktop\agent_-for_-blog\backups\my_blog_20260925_170720`**  ·  total **1.3 MiB**

| file | bytes | sha256 |
|---|---:|---|
| `data.sql` | 7141 | `2c622d0d8cd169f6…` |
| `data_blog.json` | 6263 | `abb077b02fad0436…` |
| `data_blog_content.json` | 611 | `d38f2a7a204e2be2…` |
| `langgraph_api/.langgraph_checkpoint.1.pckl` | 276828 | `8dc8d8054e0a48f0…` |
| `langgraph_api/.langgraph_checkpoint.2.pckl` | 127244 | `b4b4d4e0f756cd64…` |
| `langgraph_api/.langgraph_checkpoint.3.pckl` | 752576 | `dbb7280cdbc2fc99…` |
| `langgraph_api/.langgraph_ops.pckl` | 165904 | `25dcbfa9ab15bfe5…` |
| `langgraph_api/.langgraph_retry_counter.pckl` | 6 | `ad2598304a8af697…` |
| `langgraph_api/store.pckl` | 1614 | `4405a67137464859…` |
| `langgraph_api/store.vectors.pckl` | 6 | `ad2598304a8af697…` |
| `schema.json` | 21124 | `c3fe17d0030d894b…` |
| `schema.sql` | 4212 | `84ff71f4294cde95…` |

## 5. Console log

```text
==========================================================================
my_blog read-only full backup
started : 2026-09-25 17:07:20 (local)
==========================================================================
env file : D:\HuaweiMoveData\Users\HUAWEI\Desktop\agent_-for_-blog\.env
target   : kiana_mysql@106.52.158.48:3306/my_blog
password : <masked len=8>
output   : D:\HuaweiMoveData\Users\HUAWEI\Desktop\agent_-for_-blog\backups\my_blog_20260925_170720

server   : MySQL 8.0.45-0ubuntu0.24.04.1  charset=utf8mb4  collation=utf8mb4_0900_ai_ci
server tz: NOW() = 2026-09-25 17:07:20
utf8mb4  : probe=中文-UTF8-OK-🎈 chars=12 bytes=19

objects  : 3 -> blog, blog_content, v_blog
  - blog [BASE TABLE] engine=InnoDB collation=utf8mb4_0900_ai_ci
  - blog_content [BASE TABLE] engine=InnoDB collation=utf8mb4_0900_ai_ci
  - v_blog [VIEW] engine=None collation=None
foreign keys : 1
  - blog_content.fk_content_blog -> blog(id)
checks       : 0  triggers: 3
FULLTEXT     : 1
  - blog_content.ft_content on (content)

wrote schema.sql (4.1 KiB)
wrote schema.json (20.6 KiB)
schema self-check: 14 statements, 1 deferred FK ALTERs (expected 1), 0 inline FK left over
  FK moved out of CREATE TABLE `blog_content`: CONSTRAINT `fk_content_blog` FOREIGN KEY (`blog_id`) REFERENCES `blog` (`id`) ON DELETE CA

  DDL CHECK blog: OK (15 cols, PK ['id'], 3 named indexes)
  DDL CHECK blog_content: OK (7 cols, PK ['blog_id'], 1 named indexes)
  DDL CHECK view v_blog: OK (definition preserved verbatim)

  blog: DB COUNT(*)=12  exported=12  json=6.1 KiB
  blog_content: DB COUNT(*)=1  exported=1  json=611 B
wrote data.sql (7.0 KiB)

langgraph: copied 7 file(s), 1.3 MiB

```

## 6. Warnings

- ⚠️ DEFINER clause stripped from view(s): v_blog
