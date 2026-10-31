import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "workspace.db"
        self.data = self.root / "data"
        self.data.mkdir()
        with patch.object(app, "ROOT", self.root), patch.object(app, "DATA_DIR", self.data), patch.object(app, "DB_PATH", self.db), patch.object(app, "HISTORY_PATH", self.data / "history.db"):
            with __import__("sqlite3").connect(self.db) as con:
                con.executescript("CREATE TABLE orders(id INTEGER PRIMARY KEY, amount REAL, status TEXT); INSERT INTO orders VALUES (1,10,'done'),(2,20,'pending');")
            app.init_workspace()

    def tearDown(self):
        self.tmp.cleanup()

    def run_sql(self, sql):
        with patch.object(app, "DB_PATH", self.db):
            return app.execute_readonly(sql)

    def test_select_returns_columns_and_rows(self):
        cols, rows, truncated = self.run_sql("SELECT status, SUM(amount) AS total FROM orders GROUP BY status ORDER BY total DESC")
        self.assertEqual(cols, ["status", "total"])
        self.assertEqual(rows, [["pending", 20.0], ["done", 10.0]])
        self.assertFalse(truncated)

    def test_with_select_allowed(self):
        _, rows, _ = self.run_sql("WITH totals AS (SELECT SUM(amount) AS n FROM orders) SELECT n FROM totals")
        self.assertEqual(rows, [[30.0]])

    def test_write_is_rejected(self):
        with self.assertRaises(ValueError):
            self.run_sql("DELETE FROM orders")

    def test_cte_cannot_write(self):
        with self.assertRaises(Exception):
            self.run_sql("WITH x AS (DELETE FROM orders RETURNING id) SELECT * FROM x")

    def test_multiple_statements_rejected(self):
        with self.assertRaises(ValueError):
            self.run_sql("SELECT 1; SELECT 2")

    def test_semicolon_inside_string_is_safe(self):
        _, rows, _ = self.run_sql("SELECT ';' AS punctuation")
        self.assertEqual(rows, [[";"]])

    def test_schema_discovery(self):
        with patch.object(app, "DB_PATH", self.db):
            tables, _ = app.schema_description()
        self.assertEqual([t["name"] for t in tables], ["orders"])

    def test_dotenv_respects_shell_environment(self):
        envfile = self.root / ".env"
        envfile.write_text("SQL_ASSISTANT_TEST_SETTING=from-file\n", encoding="utf-8")
        with patch.object(app, "ROOT", self.root), patch.dict(os.environ, {"SQL_ASSISTANT_TEST_SETTING": "from-shell"}):
            app.load_env_file()
            self.assertEqual(os.environ["SQL_ASSISTANT_TEST_SETTING"], "from-shell")

    def test_app_query_api_uses_model_and_executes(self):
        with patch.object(app, "DB_PATH", self.db), patch.object(app, "HISTORY_PATH", self.data / "history.db"), patch.object(app, "llm_generate", return_value=("SELECT COUNT(*) AS n FROM orders", "Two rows in the source table.")):
            body = b'{"question":"How many orders?"}'
            env = {"PATH_INFO":"/api/query", "REQUEST_METHOD":"POST", "CONTENT_LENGTH":str(len(body)), "wsgi.input":io.BytesIO(body)}
            captured = {}
            def start(status, headers): captured["status"] = status
            payload = b"".join(app.app(env, start))
            import json
            result = json.loads(payload)
            self.assertEqual(captured["status"], "200 OK")
            self.assertEqual(result["rows"], [[2]])


if __name__ == "__main__":
    unittest.main()
