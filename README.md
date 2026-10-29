# free-router

Self-hosted OpenAI-compatible API gateway fronting free LLM providers with automatic health checking, failover, and cooldown management.

## Features

- **OpenAI-compatible API** — drop-in replacement for `/v1/chat/completions` and `/v1/models`
- **Automatic failover** — routes requests to the least-recently-used healthy provider; retries on failure
- **Health sweeps** — daily automated checks (3x at 5:00 AM) + manual test-all from the dashboard
- **Cooldown system** — failing routes enter a 10-min cooldown; 3 consecutive failures trigger 24h manual review
- **Input token caps** — per-route `max_input_tokens` prevents upstream "token exceed" errors
- **SSE streaming** — transparently proxies streaming responses without buffering
- **Web dashboard** — manage providers, groups, routes, and view logs at `http://localhost:8765/`
- **Authentication** — optional API key auth (`FREE_ROUTER_API_KEY`) and basic auth for the web UI
- **Windows service** — auto-starts on boot via Task Scheduler; dynamic port selection

## Setup

```bash
pip install fastapi uvicorn httpx pyyaml apscheduler
python router.py
```

The dashboard is available at `http://127.0.0.1:8765/`.

## Configuration

### Environment Variables

| Variable | Description |
|----------|-------------|
| `FREE_ROUTER_PORT` | Port to listen on (default: 8765) |
| `FREE_ROUTER_BIND` | Bind address (default: 127.0.0.1) |
| `FREE_ROUTER_API_KEY` | If set, `/v1/*` endpoints require `Authorization: Bearer <key>` |
| `FREE_ROUTER_UI_USER` | Web UI basic auth username |
| `FREE_ROUTER_UI_PASS` | Web UI basic auth password |

### providers.yml

Define providers, model groups, and input caps:

```yaml
groups:
  free-fast:
    - bai/model-name
  free-coder:
    - inferx/code-model

providers:
  bai:
    baseUrl: https://api.example.com/v1
    apiKey: sk-...
    cooldown_minutes: 10

route_overrides:
  bai/model-name: { max_input_tokens: 32000 }
```

## Universal Endpoint (OpenAI + Anthropic)

**Base URL:** `http://127.0.0.1:8765/v1` — works for *any* agent (OpenAI or Anthropic). See `docs/UNIVERSAL_GUIDE.md` and `C:\Users\gokoq\Documents\free-router\CONNECT.md` for copy-paste configs (OpenCode, OMP, Cursor, Continue, Cline, generic SDKs).

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/v1/models` | List available model groups |
| `POST` | `/v1/chat/completions` | Chat completions (OpenAI-compatible) |
| `POST` | `/v1/messages` | Messages (Anthropic-compatible) — same models, same failover |
| `POST` | `/v1/messages/count_tokens` | Anthropic token count (estimate) |
| `GET` | `/v1/_status` | Per-route health status |
| `GET` | `/v1/_health` | Per-group health summary |
| `GET` | `/v1/_dashboard` | Full dashboard JSON |
| `POST` | `/v1/_test/{route}` | Test a single route |
| `POST` | `/v1/_test_all` | Test all routes (parallel) |
| `GET` | `/api/ping` | Liveness check |

## Tests

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```

## License

MIT
