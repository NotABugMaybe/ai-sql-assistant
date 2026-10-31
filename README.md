# Query Studio · AI SQL Assistant

A local-first natural-language SQL workspace. Ask questions in plain English, inspect the generated SQL, run read-only queries, explore a schema, chart numeric results, export CSV, and revisit recent query history. Python 3.11+ and SQLite are enough to run the app. Ollama is the default model provider; no API key is needed.

## Run locally

1. Install [Ollama](https://ollama.com/download), start its local service, and download a model. On macOS the Ollama app starts the service; on Linux you can run `ollama serve` in a separate terminal. Then run:

   ```sh
   ollama pull llama3.2
   ```

   The model name is configurable. A smaller model may be used on machines with limited memory.

2. Optional: copy `.env.example` to `.env` and change the settings. The shell's environment variables take precedence. Keep `.env` private; it is excluded from version control.

3. In this directory, start the application:

   ```sh
   python3 app.py
   ```

4. Open [http://127.0.0.1:8080](http://127.0.0.1:8080).

On first launch, the application creates a private `data/workspace.db` SQLite file with a small, clearly labeled example business dataset, plus a local query-history database. You can use that workspace immediately or point the app at a SQLite database file you control.

## Configuration

Set environment variables before starting the server:

| Variable | Default | Purpose |
|---|---|---|
| `SQLITE_PATH` | `data/workspace.db` | Path to an existing SQLite database file |
| `SQL_ASSISTANT_DATA_DIR` | `data/` | App-owned database and query-history location |
| `LLM_PROVIDER` | `ollama` | `ollama` or `openai-compatible` |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama server address |
| `OLLAMA_MODEL` | `llama3.2:latest` | Installed Ollama model |
| `OPENAI_API_KEY` | unset | Optional provider key, supplied locally through your shell |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Optional OpenAI-compatible API base |
| `OPENAI_MODEL` | `gpt-4o-mini` | Optional provider model |
| `PORT` | `8080` | Local HTTP port |

For example, point to an existing local SQLite file (macOS/Linux):

```sh
SQLITE_PATH="/path/to/your/database.sqlite" python3 app.py
```

PowerShell:

```powershell
$env:SQLITE_PATH = "C:\path\to\your\database.sqlite"
python app.py
```

For OpenAI-compatible use, set `LLM_PROVIDER=openai-compatible`, `OPENAI_API_KEY`, and optionally `OPENAI_BASE_URL` and `OPENAI_MODEL` in `.env` or your shell before launching. Keep secrets out of source control and do not paste them into chat. Cloud model providers receive the question and discovered schema; use a local Ollama endpoint if that information should remain on this machine.

## Safety and limits

The application never connects to a database unless configured by its operator. SQLite opens the configured database in read-only mode; SQLite's authorizer blocks writes and other non-read operations. Only one `SELECT` or `WITH` query is accepted. Queries stop after five seconds, and results are capped at 500 rows and 8 MB. The UI's “Ask AI” path generates SQL and executes it; you can review the SQL in the result. There is intentionally no generic database URL support: remote databases require credentials and provider-specific read-only roles, neither of which is configured here.

Query history is stored locally in `data/history.db`. Treat the history file as sensitive if questions may contain private information. The server binds to `127.0.0.1` only and is intended for local use.

## Tests

The safety and API tests use Python's built-in `unittest` and make no network requests:

```sh
python3 -m unittest discover -s tests -v
```
