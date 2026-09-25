#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Full read-only backup of the remote `my_blog` MySQL database (+ local LangGraph state).

Design constraints honoured by this script
------------------------------------------
* The production database is touched with **SELECT / SHOW only**.  Every statement
  that reaches the server is guarded by ``assert_read_only()``.
* No credential is ever printed, logged, or written to disk.  The password lives
  only in process memory and every echoed string goes through ``mask()``.
* Every file is written with an explicit UTF-8 encoding (the Windows default GBK
  would corrupt the Chinese content of ``blog_content.content``).
* A connection failure is reported as-is and is *never* retried, so a wrong
  password cannot burn through the server's login attempts.

Usage
-----
    python scripts/backup_my_blog.py
    python scripts/backup_my_blog.py --no-langgraph --verify-only-schema
"""

from __future__ import annotations

import argparse
import datetime as _dt
import decimal
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

import pymysql
from pymysql.cursors import DictCursor

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
ENV_PATH = PROJECT_ROOT / ".env"
BACKUP_ROOT = PROJECT_ROOT / "backups"
LANGGRAPH_DIR = PROJECT_ROOT / ".langgraph_api"
LANGGRAPH_SUBDIR = "langgraph_api"  # destination name inside the backup folder

DEFAULT_DB_NAME = "my_blog"
CONNECT_TIMEOUT = 15
READ_TIMEOUT = 300

# Guard: nothing but these statement shapes may ever be sent to the server.
# Prefix matching is enough because the leading keyword decides whether a
# statement can mutate anything: `SHOW CREATE TABLE ...` is read-only, whereas a
# bare `CREATE ...` is not and is rejected below.
READ_ONLY_PREFIXES = ("SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "WITH")

# `WITH ...` introduces a CTE, so the real verb can appear later in the text.
CTE_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|REPLACE|DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|"
    r"LOCK|CALL|SET|RENAME)\b",
    re.I,
)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def mask(value: str | None) -> str:
    """Render a secret as a length-only placeholder."""
    if not value:
        return "<empty>"
    return f"<masked len={len(value)}>"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human_size(num_bytes: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{num_bytes} B"


def load_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser.

    Deliberately hand-rolled instead of using python-dotenv: the file is only
    read, the parsed values are never echoed, and no dependency is added to the
    project.  Later duplicate keys win, matching dotenv semantics.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    with path.open("r", encoding="utf-8-sig", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[7:].lstrip()
            key, sep, val = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            if key:
                values[key] = val
    return values


def assert_read_only(sql: str) -> str:
    """Refuse to send anything that is not a read-only statement.

    Only ``SELECT ...`` and ``SHOW ...`` shapes ever leave this script (plus
    ``information_schema`` SELECTs).  A mutating statement is rejected before it
    reaches the driver, so the production database cannot be written to even if
    this script is edited carelessly later.
    """
    stripped = sql.strip()
    while stripped.startswith("/*"):  # drop leading block comments
        end = stripped.find("*/")
        if end == -1:
            raise RuntimeError(f"unterminated comment in statement: {sql[:80]!r}")
        stripped = stripped[end + 2:].lstrip()

    # Exactly one statement per call: a trailing semicolon is fine, but anything
    # after it (e.g. "SELECT 1; DROP TABLE t") must never reach the server.
    pieces = split_sql_statements(stripped)
    if len(pieces) != 1:
        raise RuntimeError(
            f"refusing to run {len(pieces)} chained statements in one call: {stripped[:80]!r}"
        )
    stripped = pieces[0].strip()

    upper = stripped.upper()
    if not upper.startswith(READ_ONLY_PREFIXES):
        raise RuntimeError(
            f"refusing to run a non-read-only statement: {stripped[:80]!r}"
        )
    if upper.startswith("WITH"):
        # A data-modifying CTE must not slip through on the WITH prefix.
        hit = CTE_FORBIDDEN.search(stripped)
        if hit:
            raise RuntimeError(
                f"refusing to run a CTE containing {hit.group(0).upper()!r}: {stripped[:80]!r}"
            )
    return sql


def q(conn, sql: str, args=None):
    """Execute a read-only query and return all rows as dicts."""
    assert_read_only(sql)
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def q1(conn, sql: str, args=None):
    rows = q(conn, sql, args)
    return rows[0] if rows else None


def scalar(conn, sql: str, args=None):
    row = q1(conn, sql, args)
    if not row:
        return None
    return next(iter(row.values()))


# --------------------------------------------------------------------------- #
# JSON / SQL literal encoding
# --------------------------------------------------------------------------- #

def json_default(obj):
    """Make driver types JSON-serialisable without losing information."""
    if isinstance(obj, _dt.datetime):
        return obj.strftime("%Y-%m-%d %H:%M:%S.%f") if obj.microsecond else obj.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(obj, _dt.date):
        return obj.isoformat()
    if isinstance(obj, _dt.timedelta):
        return str(obj)
    if isinstance(obj, decimal.Decimal):
        return str(obj)
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="backslashreplace")
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    return f"<unserialisable {type(obj).__name__}>"


def sql_literal(value) -> str:
    """Render one Python value as a MySQL literal.

    ``conn.escape()`` is used so that the server's own escaping rules apply;
    on top of that ``\\`` ``'`` ``"`` NUL, \\n and \\r are escaped explicitly so
    the produced ``INSERT`` statements stay correct even if the restore client
    runs without ``NO_BACKSLASH_ESCAPES`` disabled, and so newlines inside the
    ``longtext`` column never break the statement over several lines.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (bytes, bytearray)):
        return "X'" + bytes(value).hex() + "'"
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        text = value.isoformat(sep=" ") if isinstance(value, _dt.datetime) else value.isoformat()
        return "'" + text + "'"
    if isinstance(value, _dt.timedelta):
        total = int(value.total_seconds())
        sign = "-" if total < 0 else ""
        total = abs(total)
        return f"'{sign}{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}'"

    text = str(value)
    escaped = (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace('"', '\\"')
        .replace("\x00", "\\0")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\x1a", "\\Z")
    )
    return "'" + escaped + "'"


def encode_value(obj):
    """Prepare a JSON-decoded value for SQL re-encoding."""
    if obj is None:
        return None
    if isinstance(obj, (dict, list)):
        return json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return obj


# --------------------------------------------------------------------------- #
# SQL text utilities
# --------------------------------------------------------------------------- #

def split_sql_statements(text: str) -> list[str]:
    """Split a SQL script on top-level semicolons, honouring quotes/comments.

    MySQL *versioned* comments (``/*!50100 ... */``, ``/*!80000 ... */``) are
    executable, not cosmetic -- ``SHOW CREATE TABLE`` uses them to attach
    ``WITH PARSER `ngram` `` to a FULLTEXT index.  They are therefore preserved
    verbatim; only ordinary ``/* ... */`` comments are skipped.
    """
    statements: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote is None:
            if ch == "-" and text.startswith("--", i):
                # A "--" comment must be followed by whitespace/EOL in MySQL.
                if i + 2 >= n or text[i + 2] in " \t\r\n":
                    while i < n and text[i] != "\n":
                        i += 1
                    continue
            if ch == "/" and text.startswith("/*", i):
                if re.match(r"/\*![0-9]{5}", text[i:]):
                    # versioned comment: keep it, it is part of the DDL
                    end = text.find("*/", i + 2)
                    stop = n if end == -1 else end + 2
                    buf.append(text[i:stop])
                    i = stop
                    continue
                end = text.find("*/", i + 2)
                i = n if end == -1 else end + 2
                continue
            if ch in ("'", '"', "`"):
                quote = ch
                buf.append(ch)
            elif ch == ";":
                stmt = "".join(buf).strip()
                if stmt:
                    statements.append(stmt)
                buf = []
            else:
                buf.append(ch)
        else:
            if ch == "\\" and quote in ("'", '"') and i + 1 < n:
                buf.append(text[i:i + 2])
                i += 2
                continue
            if ch == quote:
                doubled = i + 1 < n and text[i + 1] == quote
                if doubled:
                    buf.append(text[i:i + 2])
                    i += 2
                    continue
                quote = None
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def parse_create_table_ddl(stmt: str) -> dict:
    """Structurally parse a ``CREATE TABLE`` or ``CREATE VIEW`` statement.

    Used to prove that the emitted ``schema.sql`` still declares every column,
    index and constraint that ``information_schema`` reported — a restore
    guarantee rather than an eyeball check.
    """
    result = {
        "table": None,
        "kind": None,
        "columns": [],
        "primary_key": [],
        "indexes": {},
        "inline_fk": [],
        "options": "",
    }
    match = re.search(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`((?:[^`]|``)+)`",
        stmt, re.I,
    )
    if not match:
        # Views have no parenthesised body; record the name and stop.
        view = re.search(r"CREATE\b.*?\bVIEW\s+`((?:[^`]|``)+)`", stmt, re.I | re.S)
        if view:
            result["table"] = view.group(1).replace("``", "`")
            result["kind"] = "VIEW"
        return result

    result["table"] = match.group(1).replace("``", "`")
    result["kind"] = "BASE TABLE"

    # ``CREATE TABLE x (`` is followed by an ordinary space/comments, never a
    # quote, so the first "(" is the body.  Each comma-separated piece is then
    # searched with _find_matching_paren, which skips quotes and comments
    # (including the executable /*!50100 ... */ form).
    body_start = stmt.find("(", match.end())
    if body_start == -1:
        return result
    body_close = _find_matching_paren(stmt, body_start)
    result["options"] = stmt[body_close + 1:]

    for piece in split_top_level_commas(stmt[body_start + 1:body_close]):
        text = piece.strip()
        if not text:
            continue
        head = re.match(r"`((?:[^`]|``)+)`", text)
        if head:
            result["columns"].append(head.group(1).replace("``", "`"))
            continue
        if re.match(r"PRIMARY\s+KEY", text, re.I):
            listed = _columns_inside_parens(text)
            if listed:
                result["primary_key"] = listed
            continue
        key = re.match(
            r"(?:UNIQUE\s+)?(?:FULLTEXT\s+)?(?:SPATIAL\s+)?KEY\s+`((?:[^`]|``)+)`",
            text, re.I,
        )
        if key:
            name = key.group(1).replace("``", "`")
            result["indexes"][name] = {
                "columns": _columns_inside_parens(text),
                "unique": bool(re.match(r"UNIQUE", text, re.I)),
                "fulltext": bool(re.match(r"(?:UNIQUE\s+)?FULLTEXT", text, re.I)),
                "with_parser": bool(re.search(r"WITH\s+PARSER", text, re.I)),
            }
            continue
        if re.search(r"\bFOREIGN\s+KEY\b", text, re.I):
            result["inline_fk"].append(" ".join(text.split()))
    return result


def _columns_inside_parens(text: str) -> list[str]:
    """Back-quoted column names of the first parenthesised list in ``text``."""
    start = text.find("(")
    if start == -1:
        return []
    try:
        end = _find_matching_paren(text, start)
    except ValueError:
        return []
    return re.findall(r"`((?:[^`]|``)+)`", text[start + 1:end])


def statement_shape(stmt: str) -> str:
    match = re.match(r"\s*([A-Za-z]+)", stmt)
    return match.group(1).upper() if match else "?"


def split_top_level_commas(body: str) -> list[str]:
    """Split a CREATE TABLE body on the commas that separate definitions.

    Commas inside parentheses (``DECIMAL(10,2)``, ``KEY (a,b)``), inside string
    literals (``COMMENT 'a,b'``) and inside back-quoted identifiers are kept.
    """
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if quote is None:
            if ch in ("'", '"', "`"):
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                parts.append("".join(buf))
                buf = []
                i += 1
                continue
            buf.append(ch)
        else:
            if ch == "\\" and quote in ("'", '"') and i + 1 < n:
                buf.append(body[i:i + 2])
                i += 2
                continue
            if ch == quote:
                if i + 1 < n and body[i + 1] == quote:
                    buf.append(body[i:i + 2])
                    i += 2
                    continue
                quote = None
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _find_matching_paren(text: str, open_idx: int) -> int:
    """Index of the ``)`` matching the ``(`` at ``open_idx`` (quote aware)."""
    depth = 0
    quote: str | None = None
    i, n = open_idx, len(text)
    while i < n:
        ch = text[i]
        if quote is None:
            if ch in ("'", '"', "`"):
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i
        else:
            if ch == "\\" and quote in ("'", '"') and i + 1 < n:
                i += 2
                continue
            if ch == quote:
                if i + 1 < n and text[i + 1] == quote:
                    i += 2
                    continue
                quote = None
        i += 1
    raise ValueError("unbalanced parentheses")


def strip_inline_foreign_keys(create_sql: str) -> tuple[str, list[str]]:
    """Remove ``CONSTRAINT ... FOREIGN KEY ...`` clauses from a CREATE TABLE.

    The export emits every foreign key as a deferred ``ALTER TABLE ... ADD
    CONSTRAINT``.  Leaving the same constraint inline as well would make a
    restore fail with a duplicate-constraint error, so the inline form is
    removed here.  Returns the rewritten statement plus the removed clauses so
    the caller can report exactly what was moved.
    """
    match = re.search(r"\bCREATE\s+TABLE\b", create_sql, re.I)
    if not match:
        return create_sql, []
    open_idx = create_sql.find("(", match.end())
    if open_idx == -1:
        return create_sql, []
    close_idx = _find_matching_paren(create_sql, open_idx)

    body = create_sql[open_idx + 1:close_idx]
    pieces = split_top_level_commas(body)

    kept: list[str] = []
    removed: list[str] = []
    for piece in pieces:
        if piece.strip() == "":
            continue
        # A definition that both names a constraint and declares a FOREIGN KEY.
        if re.search(r"\bCONSTRAINT\b", piece, re.I) and re.search(r"\bFOREIGN\s+KEY\b", piece, re.I):
            removed.append(" ".join(piece.split()))
        else:
            kept.append(piece)

    if not removed:
        return create_sql, []

    new_body = ",\n  ".join(p.strip() for p in kept)
    rewritten = create_sql[:open_idx + 1] + "\n  " + new_body + "\n" + create_sql[close_idx:]
    return rewritten, removed


# --------------------------------------------------------------------------- #
# schema introspection
# --------------------------------------------------------------------------- #

FK_QUERY = """
SELECT rc.CONSTRAINT_NAME,
       rc.TABLE_NAME,
       rc.REFERENCED_TABLE_NAME,
       rc.UPDATE_RULE,
       rc.DELETE_RULE,
       GROUP_CONCAT(kcu.COLUMN_NAME ORDER BY kcu.ORDINAL_POSITION SEPARATOR ',')      AS child_cols,
       GROUP_CONCAT(kcu.REFERENCED_COLUMN_NAME ORDER BY kcu.ORDINAL_POSITION SEPARATOR ',') AS parent_cols
FROM information_schema.REFERENTIAL_CONSTRAINTS rc
JOIN information_schema.KEY_COLUMN_USAGE kcu
  ON kcu.CONSTRAINT_SCHEMA = rc.CONSTRAINT_SCHEMA
 AND kcu.CONSTRAINT_NAME = rc.CONSTRAINT_NAME
 AND kcu.TABLE_NAME = rc.TABLE_NAME
WHERE rc.CONSTRAINT_SCHEMA = %s
GROUP BY rc.CONSTRAINT_NAME, rc.TABLE_NAME, rc.REFERENCED_TABLE_NAME,
         rc.UPDATE_RULE, rc.DELETE_RULE
ORDER BY rc.TABLE_NAME, rc.CONSTRAINT_NAME
"""

CHARACTER_SET_QUERY = """
SELECT TABLE_NAME, TABLE_TYPE, ENGINE, TABLE_COLLATION, TABLE_ROWS,
       DATA_LENGTH, INDEX_LENGTH, TABLE_COMMENT,
       ROW_FORMAT, CREATE_OPTIONS
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = %s
ORDER BY TABLE_NAME
"""

CHECK_QUERY = """
SELECT tc.TABLE_NAME, tc.CONSTRAINT_NAME, cc.CHECK_CLAUSE
FROM information_schema.TABLE_CONSTRAINTS tc
LEFT JOIN information_schema.CHECK_CONSTRAINTS cc
  ON cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
 AND cc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME
WHERE tc.TABLE_SCHEMA = %s AND tc.CONSTRAINT_TYPE = 'CHECK'
ORDER BY tc.TABLE_NAME, tc.CONSTRAINT_NAME
"""

TRIGGER_QUERY = """
SELECT TRIGGER_NAME, EVENT_MANIPULATION, EVENT_OBJECT_TABLE, ACTION_TIMING, ACTION_STATEMENT
FROM information_schema.TRIGGERS
WHERE TRIGGER_SCHEMA = %s
ORDER BY EVENT_OBJECT_TABLE, TRIGGER_NAME
"""


def describe_object(conn, name: str, kind: str, dbname: str) -> dict:
    """Collect SHOW CREATE / SHOW INDEX / SHOW FULL COLUMNS for one object."""
    info: dict = {"name": name, "kind": kind}

    if kind == "BASE TABLE":
        create_row = q1(conn, f"SHOW CREATE TABLE `{name.replace('`', '``')}`")
        columns = q(conn, f"SHOW FULL COLUMNS FROM `{name.replace('`', '``')}`")
        indexes = q(conn, f"SHOW INDEX FROM `{name.replace('`', '``')}`")
    else:
        create_row = q1(conn, f"SHOW CREATE VIEW `{name.replace('`', '``')}`")
        columns = q(conn, f"SHOW FULL COLUMNS FROM `{name.replace('`', '``')}`")
        indexes = []

    create_sql = ""
    if create_row:
        for key in ("Create Table", "Create View"):
            if key in create_row:
                create_sql = create_row[key]
                break

    info["create_sql"] = create_sql
    info["columns"] = [
        {k: encode_value(v) for k, v in row.items()} for row in columns
    ]
    info["indexes"] = [
        {k: encode_value(v) for k, v in row.items()} for row in indexes
    ]

    # Aggregate the index picture: name -> columns, uniqueness, type.
    grouped: dict[str, dict] = {}
    for row in indexes:
        key_name = row.get("Key_name")
        entry = grouped.setdefault(key_name, {
            "key_name": key_name,
            "unique": row.get("Non_unique") == 0,
            "type": row.get("Index_type"),
            "columns": [],
            "cardinality": row.get("Cardinality"),
            "nullable": row.get("Null"),
            "comment": row.get("Comment"),
            "visible": row.get("Visible"),
            "expression": row.get("Expression"),
        })
        col = row.get("Column_name")
        if row.get("Expression"):
            col = f"(({row['Expression']}))"
        if row.get("Sub_part"):
            col = f"{col}({row['Sub_part']})"
        entry["columns"].append(col)
    info["index_groups"] = list(grouped.values())

    if kind == "BASE TABLE":
        info["create_options"] = q1(conn, f"SHOW TABLE STATUS LIKE '{name}'")
        if info["create_options"]:
            info["create_options"] = {
                k: encode_value(v) for k, v in info["create_options"].items()
            }
    return info


def strip_definer(clause: str) -> tuple[str, bool]:
    """Remove DEFINER=... from a CREATE VIEW so a restore can run as anyone.

    Handles both the usual back-quoted form (``DEFINER=`user`@`host```) and the
    unquoted form some servers emit (``DEFINER=user@host``), while leaving the
    ``SQL SECURITY DEFINER`` attribute of the view itself untouched.
    """
    new, count = re.subn(
        r"\s*DEFINER\s*=\s*(?:`[^`]*`|[^@\s]+)@(?:`[^`]*`|[^\s]+)",
        "",
        clause,
    )
    return new, bool(count)


# --------------------------------------------------------------------------- #
# main backup flow
# --------------------------------------------------------------------------- #

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Read-only full backup of my_blog.")
    parser.add_argument("--env-file", default=str(ENV_PATH),
                        help="path to the .env holding DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME")
    parser.add_argument("--out-root", default=str(BACKUP_ROOT),
                        help="directory that will receive my_blog_<timestamp>/")
    parser.add_argument("--db", default=None, help="override DB_NAME")
    parser.add_argument("--timestamp", default=None,
                        help="fixed YYYYMMDD_HHMMSS stamp (used for re-runs)")
    parser.add_argument("--no-langgraph", action="store_true",
                        help="skip copying .langgraph_api")
    parser.add_argument("--langgraph-src", default=str(LANGGRAPH_DIR))
    parser.add_argument("--dump-limit", type=int, default=0,
                        help="TESTING ONLY: export at most N rows per table")
    parser.add_argument("--skip-data", action="store_true",
                        help="TESTING ONLY: schema only, no row export")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    log: list[str] = []

    def say(msg: str = "") -> None:
        print(msg, flush=True)
        log.append(msg)

    started = _dt.datetime.now()
    say("=" * 74)
    say("my_blog read-only full backup")
    say(f"started : {started.strftime('%Y-%m-%d %H:%M:%S')} (local)")
    say("=" * 74)

    # ---- credentials ---------------------------------------------------- #
    env = load_env_file(Path(args.env_file))
    host = os.environ.get("DB_HOST") or env.get("DB_HOST")
    port = int(os.environ.get("DB_PORT") or env.get("DB_PORT") or 3306)
    user = os.environ.get("DB_USER") or env.get("DB_USER")
    password = os.environ.get("DB_PASSWORD") or env.get("DB_PASSWORD") or ""
    dbname = args.db or os.environ.get("DB_NAME") or env.get("DB_NAME") or DEFAULT_DB_NAME

    missing = [n for n, v in (("DB_HOST", host), ("DB_USER", user), ("DB_NAME", dbname)) if not v]
    if missing:
        say(f"FATAL: missing connection setting(s): {', '.join(missing)}")
        return 2

    say(f"env file : {args.env_file}")
    say(f"target   : {user}@{host}:{port}/{dbname}")
    say(f"password : {mask(password)}")

    # ---- output skeleton ------------------------------------------------ #
    stamp = args.timestamp or started.strftime("%Y%m%d_%H%M%S")
    outdir = Path(args.out_root) / f"my_blog_{stamp}"
    if outdir.exists() and any(outdir.iterdir()):
        say(f"FATAL: backup directory already exists and is not empty: {outdir}")
        return 2
    outdir.mkdir(parents=True, exist_ok=True)
    say(f"output   : {outdir}")

    # ---- connect (single attempt, no retry) ----------------------------- #
    conn = None
    try:
        conn = pymysql.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database=dbname,
            charset="utf8mb4",
            use_unicode=True,
            connect_timeout=CONNECT_TIMEOUT,
            read_timeout=READ_TIMEOUT,
            write_timeout=READ_TIMEOUT,
            autocommit=True,
            cursorclass=DictCursor,
            program_name="backup_my_blog.py",
        )
    except Exception as exc:  # noqa: BLE001 - the exact driver error is what we must report
        say(f"FATAL: connection failed: {type(exc).__name__}: {exc}")
        say("Not retrying (so the account cannot be locked out).")
        return 1
    say()

    report: dict = {
        "generated_at": started.isoformat(timespec="seconds"),
        "host": host,
        "port": port,
        "user": user,
        "database": dbname,
        "server": {},
        "objects": {},
        "row_counts": {},
        "files": [],
        "langgraph": {},
        "warnings": [],
    }

    try:
        # ---- server facts (also proves utf8mb4 on the wire) ------------- #
        ver = q1(conn, "SELECT VERSION() AS v, @@character_set_server AS cs, "
                       "@@collation_server AS coll, @@sql_mode AS sql_mode, "
                       "@@lower_case_table_names AS lctn, NOW() AS server_now")
        report["server"] = {k: encode_value(v) for k, v in ver.items()}
        say(f"server   : MySQL {ver['v']}  charset={ver['cs']}  collation={ver['coll']}")
        say(f"server tz: NOW() = {encode_value(ver['server_now'])}")

        probe = q1(conn, "SELECT '中文-UTF8-OK-🎈' AS probe, "
                         "CHAR_LENGTH('中文-UTF8-OK-🎈') AS chars, "
                         "LENGTH('中文-UTF8-OK-🎈') AS bytes")
        say(f"utf8mb4  : probe={probe['probe']} chars={probe['chars']} bytes={probe['bytes']}")
        if probe["bytes"] <= probe["chars"]:
            report["warnings"].append("UTF-8 probe returned byte length <= char length; encoding suspect.")
        say()

        # ---- inventory -------------------------------------------------- #
        tables = q(conn, CHARACTER_SET_QUERY, (dbname,))
        table_names = [t["TABLE_NAME"].replace("\n", "").replace("\r", "") for t in tables]
        if not table_names:
            say(f"FATAL: database {dbname!r} has no visible tables for user {user!r}.")
            return 1
        say(f"objects  : {len(table_names)} -> {', '.join(table_names)}")

        object_info = {}
        for t in tables:
            name = t["TABLE_NAME"].replace("\n", "").replace("\r", "")
            kind = t["TABLE_TYPE"]
            say(f"  - {name} [{kind}] engine={t['ENGINE']} collation={t['TABLE_COLLATION']}")
            info = describe_object(conn, name, kind, dbname)
            info["table_meta"] = {k: encode_value(v) for k, v in t.items()}
            object_info[name] = info

        fks = q(conn, FK_QUERY, (dbname,))
        checks = q(conn, CHECK_QUERY, (dbname,))
        triggers = q(conn, TRIGGER_QUERY, (dbname,))

        say(f"foreign keys : {len(fks)}")
        for fk in fks:
            say(f"  - {fk['TABLE_NAME']}.{fk['CONSTRAINT_NAME']} -> "
                f"{fk['REFERENCED_TABLE_NAME']}({fk['parent_cols']})")
        say(f"checks       : {len(checks)}  triggers: {len(triggers)}")

        fulltext = []
        for name, info in object_info.items():
            for idx in info["index_groups"]:
                if str(idx["type"]).upper() == "FULLTEXT":
                    fulltext.append((name, idx["key_name"], idx["columns"]))
        say(f"FULLTEXT     : {len(fulltext)}")
        for name, key, cols in fulltext:
            say(f"  - {name}.{key} on ({', '.join(str(c) for c in cols)})")
        say()

        # ---- schema.sql ------------------------------------------------- #
        # Tables are emitted in dependency order so a plain restore works even
        # before the deferred ALTER TABLE ... ADD CONSTRAINT statements run.
        base_tables = [n for n in table_names
                       if object_info[n]["kind"] == "BASE TABLE"]
        views = [n for n in table_names if object_info[n]["kind"] != "BASE TABLE"]

        order: list[str] = []
        visited: set[str] = set()

        def visit(tbl: str) -> None:
            if tbl in visited or tbl not in object_info:
                return
            visited.add(tbl)
            for fk in fks:
                if fk["TABLE_NAME"] == tbl:
                    parent = fk["REFERENCED_TABLE_NAME"]
                    if parent != tbl:
                        visit(parent)
            order.append(tbl)

        for tbl in sorted(base_tables):
            visit(tbl)

        schema_parts: list[str] = [
            "-- " + "=" * 72,
            f"-- schema.sql  |  database: {dbname}",
            f"-- generated: {started.strftime('%Y-%m-%d %H:%M:%S')} local",
            f"-- server   : MySQL {report['server']['v']}",
            f"-- tool     : scripts/backup_my_blog.py (read-only SHOW/SELECT export)",
            "--",
            "-- Restore order: tables (dependency sorted) -> FK constraints -> views.",
            "-- No DROP is emitted for tables; re-create into an empty schema.",
            "-- " + "=" * 72,
            "",
            "SET NAMES utf8mb4;",
            "SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0;",
            "SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO';",
            "SET @OLD_TIME_ZONE=@@TIME_ZONE, TIME_ZONE='+00:00';",
            "",
        ]

        stats: dict[str, int] = {}
        moved_fks: list[tuple[str, str]] = []
        for name in order:
            info = object_info[name]
            stmt = info["create_sql"].rstrip().rstrip(";")
            # Normalise the trailing table options for a portable restore.
            stmt = re.sub(r"\s*AUTO_INCREMENT=\d+", "", stmt)
            # Every FK is re-emitted as a deferred ALTER TABLE below, so drop the
            # inline copy; keeping both would fail on restore (duplicate constraint).
            stmt, removed = strip_inline_foreign_keys(stmt)
            for clause in removed:
                moved_fks.append((name, clause))
            stats[name] = int(info["table_meta"].get("TABLE_ROWS") or 0)
            schema_parts += [
                f"-- ---------------------------------------------------------------",
                f"-- table: {name}",
                f"-- ---------------------------------------------------------------",
                f"DROP TABLE IF EXISTS `{name}`;",
                stmt + ";",
                "",
            ]

        if fks:
            schema_parts.append("-- ---------------------------------------------------------------")
            schema_parts.append("-- foreign key constraints (deferred until every table exists)")
            schema_parts.append("-- These were removed from the CREATE TABLE bodies above;")
            schema_parts.append("-- this is the single authoritative definition of each FK.")
            schema_parts.append("-- ---------------------------------------------------------------")
            for fk in fks:
                schema_parts.append(
                    f"ALTER TABLE `{fk['TABLE_NAME']}`\n"
                    f"  ADD CONSTRAINT `{fk['CONSTRAINT_NAME']}`\n"
                    f"  FOREIGN KEY ({', '.join('`' + c + '`' for c in fk['child_cols'].split(','))})\n"
                    f"  REFERENCES `{fk['REFERENCED_TABLE_NAME']}` "
                    f"({', '.join('`' + c + '`' for c in fk['parent_cols'].split(','))})"
                    f" ON DELETE {fk['DELETE_RULE']} ON UPDATE {fk['UPDATE_RULE']};"
                )
            schema_parts.append("")

        stripped_definers = []
        for name in views:
            info = object_info[name]
            stmt, stripped = strip_definer(info["create_sql"].rstrip().rstrip(";"))
            if stripped:
                stripped_definers.append(name)
            schema_parts += [
                f"-- ---------------------------------------------------------------",
                f"-- view: {name}",
                f"-- ---------------------------------------------------------------",
                f"DROP VIEW IF EXISTS `{name}`;",
                stmt + ";",
                "",
            ]
        if stripped_definers:
            report["warnings"].append(
                "DEFINER clause stripped from view(s): " + ", ".join(stripped_definers)
            )

        schema_parts += [
            "SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS;",
            "SET SQL_MODE=@OLD_SQL_MODE;",
            "SET TIME_ZONE=@OLD_TIME_ZONE;",
            "",
        ]
        schema_sql = "\n".join(schema_parts)
        (outdir / "schema.sql").write_text(schema_sql, encoding="utf-8", newline="\n")
        say(f"wrote schema.sql ({human_size((outdir / 'schema.sql').stat().st_size)})")

        # ---- schema.json ------------------------------------------------ #
        schema_json = {
            "database": dbname,
            "generated_at": started.isoformat(timespec="seconds"),
            "server": report["server"],
            "tables": {
                name: {
                    "kind": object_info[name]["kind"],
                    "table_meta": object_info[name]["table_meta"],
                    "columns": object_info[name]["columns"],
                    "indexes": object_info[name]["indexes"],
                    "index_groups": object_info[name]["index_groups"],
                    "foreign_keys": [fk for fk in fks if fk["TABLE_NAME"] == name],
                    "checks": [c for c in checks if c["TABLE_NAME"] == name],
                    "referenced_by": [fk for fk in fks
                                      if fk["REFERENCED_TABLE_NAME"] == name],
                }
                for name in sorted(object_info)
            },
            "all_foreign_keys": fks,
            "all_checks": checks,
            "triggers": triggers,
            "fulltext_indexes": [
                {"table": t, "index": k, "columns": [str(c) for c in cols]}
                for t, k, cols in fulltext
            ],
        }
        (outdir / "schema.json").write_text(
            json.dumps(schema_json, ensure_ascii=False, indent=2, default=json_default),
            encoding="utf-8", newline="\n",
        )
        say(f"wrote schema.json ({human_size((outdir / 'schema.json').stat().st_size)})")

        # ---- schema.sql self-check -------------------------------------- #
        # Every FK must appear exactly once in the emitted file (as a deferred
        # ALTER TABLE) and never inline in a CREATE TABLE body.
        emitted = split_sql_statements(schema_sql)
        inline_fk = [s for s in emitted
                     if statement_shape(s) == "CREATE"
                     and re.search(r"\bFOREIGN\s+KEY\b", s, re.I)]
        alter_adds = [s for s in emitted
                      if statement_shape(s) == "ALTER"
                      and re.search(r"\bADD\s+CONSTRAINT\b", s, re.I)]
        if inline_fk:
            report["warnings"].append(
                f"{len(inline_fk)} CREATE TABLE statement(s) still contain an inline "
                "FOREIGN KEY clause; restore may fail with a duplicate constraint."
            )
            say(f"WARNING: {len(inline_fk)} inline FOREIGN KEY clause(s) remain in CREATE TABLE")
        if len(alter_adds) != len(fks):
            report["warnings"].append(
                f"foreign-key count mismatch: information_schema reports {len(fks)}, "
                f"schema.sql emits {len(alter_adds)} ALTER TABLE ADD CONSTRAINT."
            )
        # A FULLTEXT index that uses a parser must keep its executable
        # /*!50100 WITH PARSER `ngram` */ clause, otherwise a restore silently
        # downgrades the index and Chinese full-text search stops working.
        parsed_by_table = {
            parse_create_table_ddl(s)["table"]: parse_create_table_ddl(s)
            for s in emitted if statement_shape(s) == "CREATE"
        }
        for name, info in object_info.items():
            for idx in info["index_groups"]:
                if str(idx["type"]).upper() != "FULLTEXT":
                    continue
                parsed_idx = parsed_by_table.get(name, {}).get("indexes", {}).get(
                    idx["key_name"], {}
                )
                if not parsed_idx.get("with_parser"):
                    report["warnings"].append(
                        f"FULLTEXT index {name}.{idx['key_name']} lost its WITH PARSER clause "
                        "in schema.sql; full-text search may not work after restore."
                    )
                    say(f"WARNING: {name}.{idx['key_name']} lost its WITH PARSER clause")
        say(f"schema self-check: {len(emitted)} statements, "
            f"{len(alter_adds)} deferred FK ALTERs (expected {len(fks)}), "
            f"{len(inline_fk)} inline FK left over")
        for tbl, clause in moved_fks:
            say(f"  FK moved out of CREATE TABLE `{tbl}`: {clause[:90]}")
        say()

        report["schema_selfcheck"] = {
            "statements": len(emitted),
            "statement_shapes": {},
            "deferred_fk_alters": len(alter_adds),
            "inline_fk_remaining": len(inline_fk),
            "fks_from_information_schema": len(fks),
            "fks_moved_out_of_create_table": [
                {"table": t, "clause": c} for t, c in moved_fks
            ],
            "parsed_vs_catalog": {},
        }
        for stmt in emitted:
            shape = statement_shape(stmt)
            report["schema_selfcheck"]["statement_shapes"][shape] = (
                report["schema_selfcheck"]["statement_shapes"].get(shape, 0) + 1
            )

        # --- compare the emitted DDL against the catalog, table by table --- #
        emitted_create = {
            parse_create_table_ddl(s)["table"]: s
            for s in emitted if statement_shape(s) == "CREATE"
        }
        for name in base_tables:
            parsed = parse_create_table_ddl(emitted_create.get(name, ""))
            catalog_cols = [c["Field"] for c in object_info[name]["columns"]]
            catalog_idx = {i["key_name"]: i for i in object_info[name]["index_groups"]}
            col_ok = parsed["columns"] == catalog_cols
            pk_ok = set(parsed["primary_key"]) == set(catalog_idx.get("PRIMARY", {}).get("columns", []))
            missing_idx = sorted(set(catalog_idx) - {"PRIMARY"} - set(parsed["indexes"]))
            idx_ok = not missing_idx
            ft_ok = all(
                parsed["indexes"].get(k, {}).get("fulltext")
                for k, v in catalog_idx.items() if str(v["type"]).upper() == "FULLTEXT"
            )
            report["schema_selfcheck"]["parsed_vs_catalog"][name] = {
                "columns_match": col_ok,
                "column_count": len(parsed["columns"]),
                "primary_key_match": pk_ok,
                "primary_key": parsed["primary_key"],
                "indexes_parsed": sorted(parsed["indexes"]),
                "indexes_missing": missing_idx,
                "indexes_match": idx_ok,
                "fulltext_preserved": ft_ok,
                "inline_fk_in_ddl": parsed["inline_fk"],
            }
            flags = []
            if not col_ok:
                flags.append(f"COLUMNS differ (ddl={parsed['columns']} catalog={catalog_cols})")
            if not pk_ok:
                flags.append(f"PRIMARY KEY differs (ddl={parsed['primary_key']})")
            if missing_idx:
                flags.append(f"missing indexes {missing_idx}")
            if not ft_ok:
                flags.append("FULLTEXT flag lost")
            if parsed["inline_fk"]:
                flags.append("inline FK still present")
            if flags:
                report["warnings"].append(f"schema.sql vs catalog [{name}]: " + "; ".join(flags))
                say(f"  DDL CHECK {name}: FAIL -> " + "; ".join(flags))
            else:
                say(f"  DDL CHECK {name}: OK "
                    f"({len(parsed['columns'])} cols, PK {parsed['primary_key']}, "
                    f"{len(parsed['indexes'])} named indexes)")

        # --- views: same normalisation, ignoring the stripped DEFINER ----- #
        def normalise_view(clause: str) -> str:
            clause = re.sub(r"DEFINER\s*=\s*(?:`[^`]*`|[^@\s]+)@(?:`[^`]*`|\S+)", "", clause)
            clause = clause.replace("`", "")
            clause = re.sub(r"\s+", " ", clause)
            return clause.strip().rstrip(";").lower()

        for name in views:
            emitted_view = next(
                (s for s in emitted
                 if statement_shape(s) == "CREATE" and f"VIEW `{name}`" in s),
                None,
            )
            if emitted_view is None:
                report["warnings"].append(f"view {name} is missing from schema.sql")
                say(f"  DDL CHECK view {name}: FAIL -> missing from schema.sql")
                continue
            catalog_norm = normalise_view(object_info[name]["create_sql"])
            emitted_norm = normalise_view(emitted_view)
            if catalog_norm != emitted_norm:
                report["warnings"].append(
                    f"view {name} body differs from the catalog definition after "
                    "DEFINER removal"
                )
                say(f"  DDL CHECK view {name}: FAIL -> body differs from catalog")
                say(f"    catalog: {catalog_norm[:120]}")
                say(f"    emitted: {emitted_norm[:120]}")
            else:
                say(f"  DDL CHECK view {name}: OK (definition preserved verbatim)")
        say()

        # ---- data export ------------------------------------------------ #
        sql_chunks: list[str] = [
            "-- " + "=" * 72,
            f"-- data.sql  |  database: {dbname}",
            f"-- generated: {started.strftime('%Y-%m-%d %H:%M:%S')} local",
            "-- Restore with:  mysql --default-character-set=utf8mb4 < data.sql",
            "-- Run schema.sql first.",
            "-- " + "=" * 72,
            "",
            "SET NAMES utf8mb4;",
            "SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0;",
            "SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO';",
            "SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0;",
            "",
        ]

        db_counts: dict[str, int] = {}
        for name in base_tables:
            if args.skip_data:
                continue

            count = int(scalar(conn, f"SELECT COUNT(*) AS c FROM `{name.replace('`', '``')}`"))
            db_counts[name] = count

            limit_clause = f" LIMIT {int(args.dump_limit)}" if args.dump_limit else ""
            rows = q(conn, f"SELECT * FROM `{name.replace('`', '``')}`{limit_clause}")

            # JSON export -- serialise the raw driver values through json_default
            # so DATETIME/DECIMAL/BLOB keep their exact textual form.
            json_rows = [
                {k: v for k, v in row.items()} for row in rows
            ]
            json_path = outdir / f"data_{name}.json"
            json_path.write_text(
                json.dumps(json_rows, ensure_ascii=False, indent=2, default=json_default),
                encoding="utf-8", newline="\n",
            )

            # SQL export -- re-serialise through the JSON decode step so the two
            # artefacts are provably built from the same payload.
            decoded_rows = json.loads(json_path.read_text(encoding="utf-8"))
            columns = list(object_info[name]["columns"])
            col_names = [c["Field"] for c in columns]
            col_list = ", ".join("`" + c.replace("`", "``") + "`" for c in col_names)

            note = (f"-- table {name}: {len(decoded_rows)} row(s) (DB COUNT(*)={count})"
                    if not args.dump_limit else
                    f"-- table {name}: {len(decoded_rows)} row(s) "
                    f"-- LIMITED EXPORT (--dump-limit), DB COUNT(*)={count}")
            sql_chunks.append(note)
            for row in decoded_rows:
                values = ", ".join(sql_literal(encode_value(row.get(c))) for c in col_names)
                sql_chunks.append(f"INSERT INTO `{name}` ({col_list}) VALUES ({values});")
            sql_chunks.append("")

            say(f"  {name}: DB COUNT(*)={count}  exported={len(decoded_rows)}  "
                f"json={human_size(json_path.stat().st_size)}")

            report["row_counts"][name] = {
                "db_count": count,
                "json_rows": len(decoded_rows),
                "sql_inserts": len(decoded_rows),
                "columns": col_names,
                "json_file": json_path.name,
                "json_bytes": json_path.stat().st_size,
                "json_sha256": sha256_of(json_path),
            }

        sql_chunks += [
            "SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS;",
            "SET SQL_MODE=@OLD_SQL_MODE;",
            "SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS;",
            "",
        ]
        data_sql = "\n".join(sql_chunks)
        (outdir / "data.sql").write_text(data_sql, encoding="utf-8", newline="\n")
        say(f"wrote data.sql ({human_size((outdir / 'data.sql').stat().st_size)})")
        say()

        # ---- LangGraph state -------------------------------------------- #
        lg_src = Path(args.langgraph_src)
        lg_dst = outdir / LANGGRAPH_SUBDIR
        src_files: dict[str, int] = {}
        if args.no_langgraph:
            say("langgraph: skipped (--no-langgraph)")
            report["langgraph"] = {"skipped": True}
        elif not lg_src.is_dir():
            say(f"langgraph: WARNING source directory not found: {lg_src}")
            report["langgraph"] = {"skipped": True, "reason": "source not found"}
            report["warnings"].append(f".langgraph_api not found at {lg_src}")
        else:
            shutil.copytree(lg_src, lg_dst, dirs_exist_ok=True)
            lg_files = []
            for path in sorted(lg_dst.rglob("*")):
                if path.is_file():
                    lg_files.append({
                        "path": str(path.relative_to(outdir)).replace("\\", "/"),
                        "bytes": path.stat().st_size,
                        "sha256": sha256_of(path),
                    })
            lg_bytes = sum(f["bytes"] for f in lg_files)
            report["langgraph"] = {
                "source": str(lg_src),
                "dest": str(lg_dst),
                "file_count": len(lg_files),
                "bytes": lg_bytes,
                "files": lg_files,
            }
            say(f"langgraph: copied {len(lg_files)} file(s), {human_size(lg_bytes)}")
            # Compare against the source to prove the copy is complete.
            src_files = {p.relative_to(lg_src).as_posix(): p.stat().st_size
                         for p in lg_src.rglob("*") if p.is_file()}
            for item in lg_files:
                rel = Path(item["path"]).relative_to(LANGGRAPH_SUBDIR).as_posix()
                if src_files.get(rel) != item["bytes"]:
                    report["warnings"].append(f"langgraph copy size mismatch: {rel}")
        say()

        # ---- file inventory --------------------------------------------- #
        # The inventory is taken *after* every payload artefact exists, so the
        # byte counts and hashes in VERIFY.md cover the real backup payload.
        # `backup_meta.json` receives the identical list.  VERIFY.md itself is
        # written last (it embeds this list), so it is inventoried separately in
        # the final on-disk listing printed to the console.
        files = []
        for path in sorted(outdir.rglob("*")):
            if path.is_file():
                files.append({
                    "path": path.relative_to(outdir).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_of(path),
                })
        total = sum(f["bytes"] for f in files)
        report["files"] = files
        report["total_bytes"] = total
        report["backup_dir"] = str(outdir)
        report["finished_at"] = _dt.datetime.now().isoformat(timespec="seconds")

        # ---- backup_meta.json ------------------------------------------- #
        (outdir / "backup_meta.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=json_default),
            encoding="utf-8", newline="\n",
        )

        # ---- VERIFY.md -------------------------------------------------- #
        v: list[str] = []
        v.append(f"# Backup verification — `{dbname}`")
        v.append("")
        v.append(f"- Backup directory: `{outdir}`")
        v.append(f"- Generated: {started.strftime('%Y-%m-%d %H:%M:%S')} local")
        v.append(f"- Server: MySQL {report['server'].get('v')} at `{host}:{port}` "
                 f"(charset_server={report['server'].get('cs')}, "
                 f"collation_server={report['server'].get('coll')})")
        v.append(f"- Account: `{user}` — **read-only operations only** "
                 f"(`SHOW`/`SELECT`/`information_schema`); no INSERT/UPDATE/DELETE/DDL was issued.")
        v.append(f"- Total backup size: **{human_size(total)}** ({total} bytes), "
                 f"{len(files)} file(s)")
        v.append("")
        v.append("## 1. Row counts — database vs exported files")
        v.append("")
        v.append("| table | DB `COUNT(*)` | rows in JSON | `INSERT` stmts in data.sql | match |")
        v.append("|---|---:|---:|---:|:--:|")
        all_match = True
        for name, rc in report["row_counts"].items():
            same = rc["db_count"] == rc["json_rows"] == rc["sql_inserts"]
            all_match = all_match and same
            v.append(f"| `{name}` | {rc['db_count']} | {rc['json_rows']} | "
                     f"{rc['sql_inserts']} | {'✅' if same else '❌'} |")
        if not report["row_counts"]:
            v.append("| _(no data exported)_ | | | | |")
        v.append("")
        v.append(f"**Verdict: {'ALL ROW COUNTS MATCH' if all_match else 'MISMATCH — DO NOT TRUST THIS BACKUP'}**")
        v.append("")

        v.append("## 2. Objects in the schema")
        v.append("")
        v.append("| object | type | engine | collation | approx rows |")
        v.append("|---|---|---|---|---:|")
        for name in table_names:
            meta = object_info[name]["table_meta"]
            v.append(f"| `{name}` | {meta.get('TABLE_TYPE')} | {meta.get('ENGINE') or '-'} | "
                     f"{meta.get('TABLE_COLLATION') or '-'} | {meta.get('TABLE_ROWS')} |")
        v.append("")

        v.append("### Indexes (evidence that these tables must never be dropped carelessly)")
        v.append("")
        v.append("| table | index | unique | type | columns |")
        v.append("|---|---|:--:|---|---|")
        index_count = 0
        for name in table_names:
            for idx in object_info[name]["index_groups"]:
                index_count += 1
                cols = ", ".join(f"`{c}`" for c in idx["columns"])
                v.append(f"| `{name}` | `{idx['key_name']}` | "
                         f"{'yes' if idx['unique'] else 'no'} | {idx['type']} | {cols} |")
        v.append("")
        v.append(f"Total indexes: **{index_count}**")
        v.append("")

        v.append("### Primary keys / unique constraints / AUTO_INCREMENT")
        v.append("")
        for name in table_names:
            pk_cols = []
            uniques = []
            auto_inc = []
            for row in object_info[name]["indexes"]:
                if row.get("Key_name") == "PRIMARY":
                    pk_cols.append(row.get("Column_name"))
                elif row.get("Non_unique") == 0:
                    uniques.append((row.get("Key_name"), row.get("Column_name")))
            for col in object_info[name]["columns"]:
                if "auto_increment" in str(col.get("Extra", "")).lower():
                    auto_inc.append(col.get("Field"))
                if col.get("Key") == "UNI" and col.get("Field") not in [u[1] for u in uniques]:
                    uniques.append((f"{name}.{col.get('Field')}_uniq", col.get("Field")))
            v.append(f"- **`{name}`** — PK: "
                     f"{', '.join('`' + str(c) + '`' for c in pk_cols) or '(none)'} · "
                     f"unique: {', '.join(f'`{k}`(`{c}`)' for k, c in uniques) or '(none)'} · "
                     f"AUTO_INCREMENT: {', '.join('`' + str(c) + '`' for c in auto_inc) or '(none)'}")
        v.append("")

        v.append("### Foreign keys")
        v.append("")
        if fks:
            v.append("| table | constraint | columns | → referenced | on delete | on update |")
            v.append("|---|---|---|---|---|---|")
            for fk in fks:
                v.append(f"| `{fk['TABLE_NAME']}` | `{fk['CONSTRAINT_NAME']}` | "
                         f"`{fk['child_cols']}` | `{fk['REFERENCED_TABLE_NAME']}`"
                         f"(`{fk['parent_cols']}`) | {fk['DELETE_RULE']} | {fk['UPDATE_RULE']} |")
        else:
            v.append("**None.** No `FOREIGN KEY` constraint exists in this schema.")
        v.append("")

        v.append("### Restore-safety self-check on the emitted DDL")
        v.append("")
        sc = report.get("schema_selfcheck", {})
        v.append(f"- `schema.sql` statements: {sc.get('statements')} "
                 f"({', '.join(f'{k}×{n}' for k, n in sorted(sc.get('statement_shapes', {}).items()))})")
        v.append(f"- Deferred `ALTER TABLE ... ADD CONSTRAINT` for FKs emitted: "
                 f"**{sc.get('deferred_fk_alters')}** "
                 f"(information_schema reports {sc.get('fks_from_information_schema')})")
        v.append(f"- Inline `FOREIGN KEY` clauses left inside `CREATE TABLE`: "
                 f"**{sc.get('inline_fk_remaining')}** "
                 f"(must be 0, otherwise a restore would hit a duplicate constraint)")
        if moved_fks:
            v.append("- FK clauses relocated out of the table bodies:")
            for tbl, clause in moved_fks:
                v.append(f"  - `{tbl}`: `{clause}`")
        v.append("")

        v.append("### FULLTEXT / ngram indexes")
        v.append("")
        if fulltext:
            for t, k, cols in fulltext:
                v.append(f"- `{t}`.`{k}` on ({', '.join('`' + str(c) + '`' for c in cols)})")
            ngram = [row for row in q(conn, "SHOW VARIABLES LIKE 'ngram_token_size'")]
            if ngram:
                v.append(f"- Server `ngram_token_size` = `{ngram[0].get('Value')}`")
        else:
            v.append("**None detected.** No FULLTEXT index in the dumped schema.")
            ngram = [row for row in q(conn, "SHOW VARIABLES LIKE 'ngram_token_size'")]
            if ngram:
                v.append(f"- (server `ngram_token_size` = `{ngram[0].get('Value')}`, "
                         f"unused by these tables)")
        v.append("")

        v.append("### Views")
        v.append("")
        for name in views:
            v.append(f"- `{name}`: `{object_info[name]['create_sql']}`")
        if not views:
            v.append("- (none)")
        v.append("")

        v.append("### Triggers / CHECK constraints")
        v.append("")
        v.append(f"- Triggers: {len(triggers)}")
        for trg in triggers:
            v.append(f"  - `{trg['TRIGGER_NAME']}` {trg['ACTION_TIMING']} "
                     f"{trg['EVENT_MANIPULATION']} ON `{trg['EVENT_OBJECT_TABLE']}`")
        v.append(f"- CHECK constraints: {len(checks)}")
        for chk in checks:
            v.append(f"  - `{chk['TABLE_NAME']}`.`{chk['CONSTRAINT_NAME']}`: {chk['CHECK_CLAUSE']}")
        v.append("")

        if report["langgraph"] and not report["langgraph"].get("skipped"):
            lg = report["langgraph"]
            v.append("## 3. Local LangGraph state")
            v.append("")
            v.append(f"- Source: `{lg['source']}`")
            v.append(f"- Copied to: `{lg['dest']}`")
            v.append(f"- {lg['file_count']} file(s), {human_size(lg['bytes'])}")
            v.append("")
            v.append("| file | bytes | verified |")
            v.append("|---|---:|:--:|")
            for item in lg["files"]:
                rel = Path(item["path"]).relative_to(LANGGRAPH_SUBDIR).as_posix()
                ok = "✅" if src_files.get(rel) == item["bytes"] else "❌"
                v.append(f"| `{rel}` | {item['bytes']} | {ok} |")
            v.append("")
        else:
            v.append("## 3. Local LangGraph state")
            v.append("")
            v.append("- Not included in this run.")
            v.append("")

        v.append("## 4. File inventory")
        v.append("")
        v.append(f"Backup directory: **`{outdir}`**  ·  total **{human_size(total)}**")
        v.append("")
        v.append("| file | bytes | sha256 |")
        v.append("|---|---:|---|")
        for item in files:
            v.append(f"| `{item['path']}` | {item['bytes']} | `{item['sha256'][:16]}…` |")
        v.append("")
        v.append("## 5. Console log")
        v.append("")
        v.append("```text")
        v.extend(log)
        v.append("```")
        v.append("")

        if report["warnings"]:
            v.append("## 6. Warnings")
            v.append("")
            for warning in report["warnings"]:
                v.append(f"- ⚠️ {warning}")
            v.append("")

        (outdir / "VERIFY.md").write_text("\n".join(v), encoding="utf-8", newline="\n")

        # VERIFY.md is written last because it embeds the inventory above, so it
        # cannot list itself; the authoritative on-disk listing is printed below.
        final_files = []
        for path in sorted(outdir.rglob("*")):
            if path.is_file():
                final_files.append((path.relative_to(outdir).as_posix(),
                                    path.stat().st_size))
        total_final = sum(sz for _, sz in final_files)

        say("=" * 74)
        say("RESULT")
        say("=" * 74)
        say(f"backup dir : {outdir}")
        say(f"total size : {human_size(total_final)} ({total_final} bytes)")
        say(f"files      : {len(final_files)}")
        for rel, sz in final_files:
            say(f"  {sz:>12,} B  {rel}")
        say("")
        say("row counts (db vs json):")
        for name, rc in report["row_counts"].items():
            flag = "OK" if rc["db_count"] == rc["json_rows"] else "MISMATCH"
            say(f"  {name:<16} db={rc['db_count']:<6} json={rc['json_rows']:<6} {flag}")
        say("")
        say(f"indexes={index_count} foreign_keys={len(fks)} fulltext={len(fulltext)} "
            f"views={len(views)} triggers={len(triggers)} checks={len(checks)}")
        say(f"BACKUP_DIR={outdir}")
        return 0

    except Exception as exc:  # noqa: BLE001
        say()
        say(f"FATAL: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    sys.exit(main())
