# free-router — Universal Endpoint (OpenAI + Anthropic)

**One local gateway, any agent, any SDK.** ~4–6s per request (upstream provider latency, not router overhead).

```
Base URL: http://127.0.0.1:8765/v1
Port file: C:\Users\gokoq\.free-router\port.txt  (always 8765 unless busy)
Health:    http://127.0.0.1:8765/v1/_status
Models:    http://127.0.0.1:8765/v1/models
Dashboard: http://127.0.0.1:8765/
```

No API key required by default (`FREE_ROUTER_API_KEY` not set → `Authorization: Bearer not-needed` or omit).

## Model Groups (use as `model` id)

All groups auto-failover across healthy providers (vyceai + inferx).

| Group | Best for | Routes behind it |
|-------|----------|------------------|
| `free-fast` | speed / chat | vyceai/deepseek-v4-flash, vyceai/gpt-5.6-luna, inferx/Qwen3.8-27B, inferx/Qwen3.6-35B, inferx/gemma-4 |
| `free-reasoning` | reasoning | vyceai/claude-sonnet-4-6, vyceai/deepseek-v4-flash, inferx/glm-5.3-flash |
| `free-coder` | code | inferx/Qwen3-Coder-Next, inferx/Devstral-2-123B, vyceai/claude-sonnet-4-6 |
| `free-vision` | image+text | inferx/deepseek-v4-flash-vision-exp |
| `free-default` | auto (recommended) | vyceai/gpt-5.6-luna + claude + deepseek + inferx/glm-5.3 (round-robin) |

## How routing works (fast)

Router picks the least-recently-used healthy route and proxies **directly** — no pre-flight health-check call per request. If the upstream fails (quota/rate/5xx), it auto-fails over to the next route in the group. Cooldown is 1 minute by default.

## 1) OpenAI-compatible (`/v1/chat/completions`)

Any OpenAI SDK / tool that accepts `baseURL`:

**curl**
```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"free-default","messages":[{"role":"user","content":"ping"}],"max_tokens":100}'
```

**Python (openai)**
```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="not-needed")
resp = client.chat.completions.create(model="free-default", messages=[{"role":"user","content":"hello"}])
print(resp.choices[0].message.content)
```

**Node**
```js
import OpenAI from "openai";
const client = new OpenAI({ baseURL: "http://127.0.0.1:8765/v1", apiKey: "not-needed" });
const r = await client.chat.completions.create({ model: "free-default", messages: [{role:"user", content:"hi"}] });
```

**Streaming** — add `"stream": true`, router proxies SSE without buffering.

## 2) Anthropic-compatible (`/v1/messages`)

Same baseURL, Anthropic SDKs / tools that expect `https://api.anthropic.com` shape:

**curl**
```bash
curl http://127.0.0.1:8765/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: not-needed" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"free-default","max_tokens":200,"messages":[{"role":"user","content":"ping"}]}'
```

**Python (anthropic)**
```python
import anthropic
client = anthropic.Anthropic(base_url="http://127.0.0.1:8765/v1", api_key="not-needed")
msg = client.messages.create(model="free-default", max_tokens=200, messages=[{"role":"user","content":"hello"}])
print(msg.content[0].text)
```

`system` as string or blocks, `stop_sequences`, `temperature`, `top_p`, `count_tokens` all supported.

## 3) Drop-in configs for popular agents

### OpenCode (`~/.config/opencode/opencode.json`)
```json
{
  "model": "free-router/free-default",
  "provider": {
    "free-router": {
      "npm": "@ai-sdk/openai-compatible",
      "options": { "baseURL": "http://127.0.0.1:8765/v1" },
      "models": {
        "free-fast": {"name":"Free Fast","limit":{"context":120000,"output":8192}},
        "free-reasoning": {"name":"Free Reasoning","limit":{"context":180000,"output":8192}},
        "free-coder": {"name":"Free Coder","limit":{"context":128000,"output":8192}},
        "free-vision": {"name":"Free Vision","limit":{"context":64000,"output":8192}},
        "free-default": {"name":"Free Default","limit":{"context":120000,"output":8192}}
      }
    }
  }
}
```

### OMP / OpenCode Zen (`~/.omp/agent/models.yml`)
```yaml
providers:
  free-router:
    baseUrl: http://127.0.0.1:8765/v1
    api: openai-completions
    apiKey: not-needed
    models:
      - { id: free-fast, name: Free Fast, contextWindow: 120000 }
      - { id: free-reasoning, name: Free Reasoning, contextWindow: 180000 }
      - { id: free-coder, name: Free Coder, contextWindow: 128000 }
      - { id: free-vision, name: Free Vision, contextWindow: 64000 }
      - { id: free-default, name: Free Default, contextWindow: 120000 }
```
Then set `~/.omp/agent/config.yml` → `default: free-router/free-default:auto`

For Anthropic-native agents, same `baseUrl` with `api: anthropic-messages` and `POST /v1/messages`.

### Cursor / Continue.dev / Cline / RooCode / Anything with custom baseUrl
- **OpenAI-style:** `baseUrl = http://127.0.0.1:8765/v1`, `apiKey = not-needed`, `model = free-default`
- **Anthropic-style:** `baseUrl = http://127.0.0.1:8765/v1`, `apiKey = not-needed`, `model = free-default`, provider type = `anthropic`

### Generic `.env` for any SDK
```
OPENAI_BASE_URL=http://127.0.0.1:8765/v1
OPENAI_API_KEY=not-needed
ANTHROPIC_BASE_URL=http://127.0.0.1:8765/v1
ANTHROPIC_API_KEY=not-needed
FREE_ROUTER_MODEL=free-default
```

## 4) OpenCode Zen — same key, separate provider

`sk-hfpLwcOcYA8W6KyGc8EjMRdwObQYhiFeTnIELhFYnUrdwkxxDqhkai8PTHx4cmUR` is already wired as `opencode-zen` (base `https://opencode.ai/zen/v1`).

- **OpenCode:** `opencode-zen/mimo-v2.5-free`, `opencode-zen/big-pickle`, `opencode-zen/muse-spark-1.3-contributor-free`, etc. — free tier works *only inside OpenCode* (Zen returns `MissingSessionID` if proxied via `free-router`). Use them directly via `opencode-zen`, not via `free-router`.
- **OMP:** same 6 free models under provider `opencode-zen` in `~/.omp/agent/models.yml`.
- **Paid Zen models** (`deepseek-v4-flash`, `glm-5.3-flash`, `minimax-m3` via Zen) will work via `free-router` once you add credits — add to `providers.yml` as `opencode-zen/<model>` and to a group, then `POST /v1/_test_all`.
- Auth: `~/.local/share/opencode/auth.json` has `opencode` + `opencode-zen` + `opencode-go` all same key.

## 5) Managing providers (no client edits needed)

Edit `C:\Users\gokoq\.free-router\providers.yml` — add a provider, add its routes to groups, then:

```bash
curl -X POST http://127.0.0.1:8765/v1/_test_all -H "Content-Type: application/json" -d '{"force":true}'
curl http://127.0.0.1:8765/v1/_status
```

Router auto-reloads via `reload_from_disk()` on dashboard save.

## 6) Health & Logs

- `GET /v1/_status` — per-route healthy/cooldown/manual + failures
- `GET /v1/_health` — per-group healthy_pct
- `GET /v1/_dashboard` — groups + routes + request_log (JSON)
- `POST /v1/_test/{route}` / `POST /v1/_test_all` — on-demand checks (parallel, 4 concurrent)
- Files: `log/health.log`, `log/router.log`, `log/requests.log`, `status.json`

Docs mirror: `C:\Users\gokoq\Documents\free-router\CONNECT.md` — copy this file to any new machine/tool.
