#!/usr/bin/env python3
"""A local-first, read-only natural-language SQL assistant. Python stdlib only."""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from wsgiref.simple_server import make_server

ROOT = Path(__file__).resolve().parent


def load_env_file() -> None:
    """Load simple KEY=value settings; process environment always wins."""
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
            value = value[1:-1]
        if key and key.replace("_", "").isalnum():
            os.environ.setdefault(key, value)


load_env_file()
DATA_DIR = Path(os.environ.get("SQL_ASSISTANT_DATA_DIR", ROOT / "data")).expanduser().resolve()
DB_PATH = Path(os.environ.get("SQLITE_PATH", DATA_DIR / "workspace.db")).expanduser().resolve()
HISTORY_PATH = DATA_DIR / "history.db"
PORT = int(os.environ.get("PORT", "8080"))
MAX_ROWS = 500
MAX_RESULT_BYTES = 8_000_000
MAX_QUERY_SECONDS = 5
MAX_BODY = 64 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_response(start_response, data: Any, status: str = "200 OK"):
    body = json.dumps(data, ensure_ascii=False, default=str).encode()
    start_response(status, [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(body))), ("Cache-Control", "no-store")])
    return [body]


def init_workspace() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # The generated local sample is isolated from any user's other databases.
    if DB_PATH == (DATA_DIR / "workspace.db").resolve() and not DB_PATH.exists():
        with sqlite3.connect(DB_PATH) as con:
            con.executescript("""
                CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT, segment TEXT, joined_on TEXT);
                CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id), order_date TEXT NOT NULL, amount REAL NOT NULL, status TEXT NOT NULL);
                CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT, unit_price REAL NOT NULL);
                INSERT INTO customers VALUES
                  (1,'Aster & Co','Mumbai','Enterprise','2024-01-12'),(2,'Bluebird Studio','Bengaluru','Small business','2024-03-08'),
                  (3,'Cedar House','Pune','Enterprise','2023-11-21'),(4,'Daybreak Labs','Delhi','Startup','2024-06-02'),
                  (5,'Ember Works','Chennai','Small business','2024-08-16'),(6,'Fern & Field','Hyderabad','Startup','2025-01-10');
                INSERT INTO orders VALUES
                  (101,1,'2025-01-04',14200,'completed'),(102,1,'2025-02-10',8400,'completed'),(103,2,'2025-01-19',2300,'completed'),
                  (104,2,'2025-03-02',3900,'pending'),(105,3,'2025-02-03',21750,'completed'),(106,3,'2025-03-16',6200,'completed'),
                  (107,4,'2025-03-22',5100,'cancelled'),(108,4,'2025-04-09',12800,'completed'),(109,5,'2025-04-14',1750,'pending'),
                  (110,6,'2025-04-28',9300,'completed'),(111,6,'2025-05-03',4600,'completed'),(112,1,'2025-05-10',3800,'completed');
                INSERT INTO products VALUES
                  (1,'Insight plan','Software',49),(2,'Growth plan','Software',129),(3,'Team workshop','Services',750),(4,'Data review','Services',420),(5,'Starter kit','Hardware',85);
            """)
    with sqlite3.connect(HISTORY_PATH) as con:
        con.execute("CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL, sql TEXT, row_count INTEGER, elapsed_ms INTEGER, status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL)")


def get_db() -> sqlite3.Connection:
    if not DB_PATH.is_file():
        raise FileNotFoundError(f"Database file not found: {DB_PATH}. Set SQLITE_PATH to a local .db file.")
    con = sqlite3.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    for limit_name, value in (("SQLITE_LIMIT_LENGTH", 1_000_000), ("SQLITE_LIMIT_SQL_LENGTH", 100_000), ("SQLITE_LIMIT_COLUMN", 100), ("SQLITE_LIMIT_COMPOUND_SELECT", 50)):
        limit = getattr(sqlite3, limit_name, None)
        if limit is not None:
            con.setlimit(limit, value)
    con.set_authorizer(sql_authorizer)
    return con


def sql_authorizer(action, arg1, arg2, db_name, trigger):
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
    if action == sqlite3.SQLITE_PRAGMA and arg1 in {"table_info", "table_xinfo", "foreign_key_list"}:
        return sqlite3.SQLITE_OK
    if action not in allowed:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or arg1 or "").lower() in {"load_extension", "readfile", "writefile"}:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def schema_description() -> tuple[list[dict[str, Any]], str]:
    with get_db() as con:
        tables = []
        for row in con.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            name = row["name"]
            columns = [dict(c) for c in con.execute(f'PRAGMA table_info("{name.replace(chr(34), chr(34)*2)}")')]
            fks = [dict(fk) for fk in con.execute(f'PRAGMA foreign_key_list("{name.replace(chr(34), chr(34)*2)}")')]
            tables.append({"name": name, "kind": row["type"], "columns": columns, "foreign_keys": fks})
        return tables, "\n".join(f"{t['name']}({', '.join(c['name'] + ' ' + c['type'] for c in t['columns'])})" for t in tables)


def llm_generate(question: str, schema: str) -> tuple[str, str]:
    provider = os.environ.get("LLM_PROVIDER", "ollama").lower()
    if provider == "ollama":
        base = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
        model = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")
        url = base + "/api/chat"
        payload = {"model": model, "stream": False, "format": "json", "messages": [
            {"role": "system", "content": f"You write SQLite SELECT queries. Use only the schema below. Return JSON with keys sql and explanation. Never return markdown. Never mutate data. Limit to 100 rows unless aggregation. Schema:\n{schema}"},
            {"role": "user", "content": question}], "options": {"temperature": 0.1}}
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                outer = json.load(resp)
            result = json.loads(outer.get("message", {}).get("content", "{}"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
            raise RuntimeError(f"Ollama is unavailable. Start Ollama and pull {model}, or choose a configured provider. Details: {exc}") from exc
        return str(result.get("sql", "")).strip(), str(result.get("explanation", "Generated from your question and the current database schema."))
    if provider in {"openai", "openai-compatible"}:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("Set OPENAI_API_KEY in your environment to use the OpenAI provider.")
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        payload = {"model": model, "response_format": {"type": "json_object"}, "messages": [
            {"role": "system", "content": f"Write one safe, read-only SQLite SELECT. Use only this schema: {schema}. Return JSON with sql and explanation."},
            {"role": "user", "content": question}]}
        req = urllib.request.Request(base + "/chat/completions", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                result = json.load(resp)["choices"][0]["message"]["content"]
            parsed = json.loads(result)
            return str(parsed.get("sql", "")).strip(), str(parsed.get("explanation", "Generated from your question and the current database schema."))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError) as exc:
            raise RuntimeError(f"The configured model provider could not generate a query: {exc}") from exc
    raise RuntimeError(f"Unknown LLM_PROVIDER {provider!r}. Choose 'ollama' or 'openai-compatible'.")


def execute_readonly(sql: str) -> tuple[list[str], list[list[Any]], bool]:
    cleaned = re.sub(r"^\s*```(?:sql)?\s*|\s*```\s*$", "", sql, flags=re.I).strip()
    if not cleaned:
        raise ValueError("The model returned an empty query.")
    # sqlite3.execute rejects multiple statements; it never executes a partial batch.
    if not re.match(r"^(SELECT|WITH)\b", cleaned, re.I):
        raise ValueError("Only SELECT queries are allowed. Edit the query to use SELECT or WITH.")
    deadline = time.monotonic() + MAX_QUERY_SECONDS
    with get_db() as con:
        con.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        try:
            cur = con.execute(cleaned)
        except sqlite3.ProgrammingError as exc:
            if "one statement at a time" in str(exc):
                raise ValueError("Only one SQL statement is allowed.") from exc
            raise
        if cur.description is None:
            raise ValueError("The statement did not return rows.")
        columns = [d[0] for d in cur.description]
        rows: list[list[Any]] = []
        size = 0
        truncated = False
        while len(rows) < MAX_ROWS + 1:
            row = cur.fetchone()
            if row is None:
                break
            values = [f"[BLOB · {len(v)} bytes]" if isinstance(v, bytes) else v for v in row]
            row_size = len(json.dumps(values, ensure_ascii=False, default=str).encode("utf-8"))
            if size + row_size > MAX_RESULT_BYTES:
                truncated = True
                break
            rows.append(values)
            size += row_size
        if len(rows) > MAX_ROWS:
            rows, truncated = rows[:MAX_ROWS], True
        return columns, rows, truncated


def save_history(question: str, sql: str | None, count: int, ms: int, status: str, error: str | None) -> None:
    with sqlite3.connect(HISTORY_PATH) as con:
        con.execute("INSERT INTO history(question,sql,row_count,elapsed_ms,status,error,created_at) VALUES(?,?,?,?,?,?,?)", (question, sql, count, ms, status, error, utc_now()))


def app(environ, start_response):
    path, method = environ.get("PATH_INFO", "/"), environ.get("REQUEST_METHOD", "GET")
    try:
        if path.startswith("/api/"):
            if path == "/api/health" and method == "GET":
                try:
                    tables, _ = schema_description()
                    provider = os.environ.get("LLM_PROVIDER", "ollama").lower()
                    host = urllib.parse.urlparse(os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")).hostname
                    local_llm = provider == "ollama" and host in {"localhost", "127.0.0.1", "::1"}
                    return json_response(start_response, {"ok": True, "database": DB_PATH.name, "tables": len(tables), "provider": provider, "local_llm": local_llm})
                except Exception as e:
                    return json_response(start_response, {"ok": False, "error": str(e)}, "503 Service Unavailable")
            if path == "/api/schema" and method == "GET":
                tables, _ = schema_description()
                return json_response(start_response, {"tables": tables, "database": DB_PATH.name})
            if path == "/api/history" and method == "GET":
                with sqlite3.connect(HISTORY_PATH) as con:
                    con.row_factory = sqlite3.Row
                    items = [dict(x) for x in con.execute("SELECT * FROM history ORDER BY id DESC LIMIT 50")]
                return json_response(start_response, {"history": items})
            if path == "/api/query" and method == "POST":
                size = int(environ.get("CONTENT_LENGTH") or 0)
                if size > MAX_BODY:
                    return json_response(start_response, {"error": "Request is too large."}, "413 Payload Too Large")
                data = json.loads(environ["wsgi.input"].read(size) or b"{}")
                question, supplied_sql = str(data.get("question", "")).strip()[:2000], str(data.get("sql", "")).strip()
                if not question and not supplied_sql:
                    return json_response(start_response, {"error": "Enter a question or SQL query."}, "400 Bad Request")
                started = time.monotonic(); sql = supplied_sql; explanation = "Your SQL was checked and run in read-only mode."
                try:
                    if not sql:
                        _, schema = schema_description()
                        sql, explanation = llm_generate(question, schema)
                    cols, rows, truncated = execute_readonly(sql)
                    ms = round((time.monotonic() - started) * 1000)
                    save_history(question or "SQL editor query", sql, len(rows), ms, "success", None)
                    return json_response(start_response, {"sql": sql, "explanation": explanation, "columns": cols, "rows": rows, "row_count": len(rows), "truncated": truncated, "elapsed_ms": ms})
                except Exception as e:
                    ms = round((time.monotonic() - started) * 1000)
                    save_history(question or "SQL editor query", sql or None, 0, ms, "error", str(e))
                    return json_response(start_response, {"error": str(e), "sql": sql, "elapsed_ms": ms}, "400 Bad Request")
            return json_response(start_response, {"error": "Not found."}, "404 Not Found")
        target = ROOT / ("index.html" if path == "/" else path.lstrip("/"))
        if not target.is_file() or ROOT not in target.resolve().parents:
            start_response("404 Not Found", [("Content-Type", "text/plain")]); return [b"Not found"]
        content = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in {"application/javascript"}:
            mime += "; charset=utf-8"
        start_response("200 OK", [("Content-Type", mime), ("Content-Length", str(len(content)))])
        return [content]
    except Exception as e:
        return json_response(start_response, {"error": str(e)}, "500 Internal Server Error")


if __name__ == "__main__":
    init_workspace()
    print(f"AI SQL Assistant ready at http://127.0.0.1:{PORT}")
    print(f"SQLite workspace: {DB_PATH}")
    make_server("127.0.0.1", PORT, app).serve_forever()
