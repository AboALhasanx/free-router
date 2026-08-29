# Free Model API Gateway — router.py
# Self-hosted OpenAI-compatible router fronting free text providers.
# Endpoints:
#   GET  /v1/models            -> OpenAI-compatible model list (group names)
#   POST /v1/chat/completions  -> routes to a healthy free provider
#   GET  /v1/_status           -> per-route health + cooldowns (JSON)
#   POST /v1/_test/{route}     -> one-shot test of a single route
#   POST /v1/_test_all         -> run the full daily sweep on demand
# Deps: fastapi, uvicorn, httpx, pyyaml, apscheduler (all already on box)
import json, os, time, yaml, threading, socket, secrets, collections
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import JSONResponse, StreamingResponse
import base64
import httpx
from apscheduler.schedulers.background import BackgroundScheduler

BASE = Path(__file__).resolve().parent
PROVIDERS_YML = BASE / "providers.yml"
CONFIG_YML = BASE / "config.yml"
STATUS_JSON = BASE / "status.json"
LOG_DIR = BASE / "log"
LOG_DIR.mkdir(exist_ok=True)

app = FastAPI(title="free-router")

# --- Auth config ---
API_KEY = os.environ.get("FREE_ROUTER_API_KEY", "")  # if set, require Bearer token on /v1/*
UI_USER = os.environ.get("FREE_ROUTER_UI_USER", "")  # if set, require basic auth on web UI
UI_PASS = os.environ.get("FREE_ROUTER_UI_PASS", "")

# --- Rate limiting (token bucket per client IP) ---
RATE_LIMIT_RPM = int(os.environ.get("FREE_ROUTER_RPM", "30"))  # requests per minute
_rate_buckets = {}  # ip -> (tokens, last_refill)
_rate_lock = threading.Lock()

def _rate_check(client_ip: str) -> bool:
    """Return True if request is allowed, False if rate-limited."""
    with _rate_lock:
        now_ts = time.time()
        tokens, last = _rate_buckets.get(client_ip, (RATE_LIMIT_RPM, now_ts))
        # refill tokens based on elapsed time
        elapsed = now_ts - last
        tokens = min(RATE_LIMIT_RPM, tokens + elapsed * (RATE_LIMIT_RPM / 60.0))
        if tokens < 1:
            _rate_buckets[client_ip] = (tokens, now_ts)
            return False
        _rate_buckets[client_ip] = (tokens - 1, now_ts)
        return True

# --- Request logging ---
_request_log = collections.deque(maxlen=200)  # last 200 requests
_request_log_lock = threading.Lock()

def _log_request(method: str, path: str, status: int, ms: float, client_ip: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"{ts} {method} {path} {status} {ms:.0f}ms {client_ip}"
    with _request_log_lock:
        _request_log.append(entry)
    # also write to log/requests.log
    try:
        LOG_DIR.joinpath("requests.log").open("a").write(entry + "\n")
    except Exception:
        pass

def check_api_auth(request: Request):
    """Validate Bearer token on /v1/* API routes. Skip /api/* and web UI."""
    if not API_KEY:
        return  # auth disabled
    path = request.url.path
    # only protect /v1/* endpoints, skip web UI and internal /api/*
    if not path.startswith("/v1/"):
        return
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and secrets.compare_digest(auth[7:], API_KEY):
        return
    raise HTTPException(status_code=401, detail="invalid or missing API key")

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    start = time.time()
    client_ip = request.client.host if request.client else "-"
    check_api_auth(request)
    # basic auth for web UI (skip API routes and ping)
    if UI_USER and UI_PASS and not request.url.path.startswith("/v1/") and not request.url.path.startswith("/api/"):
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode()
                user, pwd = decoded.split(":", 1)
                if secrets.compare_digest(user, UI_USER) and secrets.compare_digest(pwd, UI_PASS):
                    resp = await call_next(request)
                    _log_request(request.method, request.url.path, resp.status_code, (time.time() - start) * 1000, client_ip)
                    return resp
            except Exception:
                pass
        _log_request(request.method, request.url.path, 401, (time.time() - start) * 1000, client_ip)
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="free-router"'})
    # rate limit /v1/chat/completions
    if request.url.path == "/v1/chat/completions" and request.method == "POST":
        if not _rate_check(client_ip):
            _log_request(request.method, request.url.path, 429, (time.time() - start) * 1000, client_ip)
            return Response(status_code=429, content=json.dumps({"error": "rate limit exceeded, try again later"}),
                            media_type="application/json", headers={"Retry-After": "60"})
    resp = await call_next(request)
    _log_request(request.method, request.url.path, resp.status_code, (time.time() - start) * 1000, client_ip)
    return resp

lock = threading.Lock()
reload_lock = threading.Lock()
status = {}  # route_id -> {healthy, last_check, last_result, cooldown_until, failures, manual}
cfg = {}
providers = {}
groups = {}
disabled_routes = set()  # routes marked disabled (skipped in pick_route)
actual_port = None  # resolved at startup (dynamic if preferred busy)
actual_bind = None

def now():
    return int(time.time())

def load_config():
    global cfg, providers, groups, disabled_routes
    with open(CONFIG_YML) as f:
        cfg = yaml.safe_load(f)
    with open(PROVIDERS_YML) as f:
        p = yaml.safe_load(f)
    providers = p.get("providers", {})
    groups = p.get("groups", {})
    disabled_routes = set(p.get("disabled_routes", []) or [])
    # route_overrides keyed by "provider/model"
    overrides = p.get("route_overrides", {})
    # build status for every route in every group (preserve existing health)
    with lock:
        for gname, routes in groups.items():
            for r in routes:
                if r not in status:
                    status[r] = {
                        "healthy": True,
                        "last_check": 0,
                        "last_result": "pending",
                        "cooldown_until": 0,
                        "failures": 0,
                        "manual": False,
                    }
                status[r]["group"] = gname
                status[r]["max_input_tokens"] = overrides.get(r, {}).get("max_input_tokens")
        # drop status for routes no longer referenced
        live = {r for rs in groups.values() for r in rs}
        for dead in [k for k in list(status) if k not in live]:
            del status[dead]

def reload_from_disk():
    """Hot-reload providers.yml groups/overrides without losing manual flags
    for routes that still exist. Safe during a sweep (guarded by reload_lock)."""
    global providers, groups, disabled_routes
    with reload_lock:
        with open(PROVIDERS_YML) as f:
            p = yaml.safe_load(f)
        providers = p.get("providers", {})
        groups = p.get("groups", {})
        disabled_routes = set(p.get("disabled_routes", []) or [])
        overrides = p.get("route_overrides", {})
        with lock:
            # preserve manual/cooldown for surviving routes
            preserved = {k: v for k, v in status.items()}
            status.clear()
            for gname, routes in groups.items():
                for r in routes:
                    st = preserved.get(r, {
                        "healthy": True, "last_check": 0, "last_result": "pending",
                        "cooldown_until": 0, "failures": 0, "manual": False})
                    st["group"] = gname
                    st["max_input_tokens"] = overrides.get(r, {}).get("max_input_tokens")
                    status[r] = st
            live = {r for rs in groups.values() for r in rs}
            for dead in [k for k in list(status) if k not in live]:
                del status[dead]
        save_status()
    return True

def save_status():
    with lock:
        tmp = STATUS_JSON.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, indent=2, default=str))
        tmp.replace(STATUS_JSON)

def provider_of(route):
    return route.split("/", 1)[0]

def route_model(route):
    return route.split("/", 1)[1] if "/" in route else route

def cooldown_for(route):
    prov = provider_of(route)
    return providers.get(prov, {}).get("cooldown_minutes", 10)

def is_cool(route):
    s = status.get(route, {})
    if s.get("manual"):
        return True
    return s.get("cooldown_until", 0) > now()

def mark_cool(route, minutes):
    with lock:
        s = status.setdefault(route, {})
        s["cooldown_until"] = now() + minutes * 60
        s["healthy"] = False
        s["last_check"] = now()
        save_status()

def mark_ok(route, note=""):
    with lock:
        s = status.setdefault(route, {})
        s["healthy"] = True
        s["last_result"] = note or "ok"
        s["last_check"] = now()
        s["failures"] = 0
        s["cooldown_until"] = 0 if not s.get("manual") else s["cooldown_until"]
        save_status()

def mark_fail(route, note):
    with lock:
        s = status.setdefault(route, {})
        s["healthy"] = False
        s["last_result"] = note
        s["last_check"] = now()
        s["failures"] = s.get("failures", 0) + 1
        # transient (10m); 3 consecutive -> manual review 24h
        if s["failures"] >= 3:
            s["manual"] = True
            s["cooldown_until"] = now() + 24 * 3600
            s["last_result"] = note + " [manual-review 24h]"
        else:
            s["cooldown_until"] = now() + cooldown_for(route) * 60
        save_status()

async def test_route(route, timeout: float = 20):
    """Return (ok: bool, detail: str). Does NOT mutate status.
    Async (httpx.AsyncClient) so it never blocks the uvicorn loop.
    Inspired by free-llm-gateway's parallel key validation: 15s cap, not 30s."""
    prov = provider_of(route)
    model = route_model(route)
    p = providers.get(prov, {})
    base = p.get("baseUrl", "").rstrip("/")
    key = p.get("apiKey", "")
    if not base:
        return False, "no baseUrl for provider"
    url = f"{base}/chat/completions"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "max_tokens": 8,
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as c:
            r = await c.post(url, headers=headers, json=payload)
        if r.status_code == 200:
            try:
                txt = r.json()["choices"][0]["message"]["content"]
            except Exception:
                return True, f"200 but unexpected body ({r.status_code})"
            return True, f"200 {txt.strip()[:40]}"
        return False, f"HTTP {r.status_code} {r.text[:120]}"
    except Exception as e:
        return False, f"ERR {type(e).__name__}: {str(e)[:120]}"

def model_list():
    """OpenAI-compatible /v1/models using group names as model ids."""
    out = []
    for gname in groups:
        out.append({"id": gname, "object": "model", "owned_by": "free-router",
                    "created": now(), "root": gname, "parent": None})
    return {"object": "list", "data": out}

def pick_route(group):
    routes = groups.get(group, [])
    # round-robin by least-recently-used healthy route not in cooldown, not disabled
    candidates = [r for r in routes if (not is_cool(r)) and r not in disabled_routes]
    if not candidates:
        return None
    candidates.sort(key=lambda r: status.get(r, {}).get("last_check", 0))
    return candidates[0]

@app.get("/v1/models")
def get_models():
    return model_list()

@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    body = await req.json()
    model = body.get("model")
    if model not in groups:
        raise HTTPException(status_code=404, detail=f"unknown group '{model}'. known: {list(groups)}")
    # input-token estimate (chars/4) to skip routes with a tighter cap
    prompt_chars = sum(len(m.get("content", "")) for m in body.get("messages", []) if isinstance(m, dict))
    est_input = prompt_chars // 4
    tried = []
    route = pick_route(model)
    while route:
        tried.append(route)
        cap = status.get(route, {}).get("max_input_tokens") or 10**9
        if est_input > cap:
            with lock:
                status.setdefault(route, {})["last_result"] = f"skip input ~{est_input} > cap {cap}"
            route = pick_route(model)
            # avoid re-picking same
            if route in tried:
                break
            continue
        ok, detail = await test_route(route)
        if ok:
            # proxy the real request to this route
            prov = provider_of(route); mdl = route_model(route)
            p = providers[prov]; base = p["baseUrl"].rstrip("/"); key = p["apiKey"]
            url = f"{base}/chat/completions"
            fwd = dict(body); fwd["model"] = mdl
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            is_stream = body.get("stream", False)
            try:
                c = httpx.AsyncClient(timeout=120, trust_env=False)
                upstream = await c.post(url, headers=headers, json=fwd)
                if is_stream and upstream.status_code == 200:
                    # SSE streaming: relay chunks as they arrive, close client when done
                    async def stream_gen():
                        try:
                            async for chunk in upstream.aiter_bytes():
                                yield chunk
                        finally:
                            await c.aclose()
                    mark_ok(route, f"200 stream via {route}")
                    return StreamingResponse(stream_gen(), status_code=upstream.status_code,
                                             media_type=upstream.headers.get("content-type", "text/event-stream"))
                else:
                    # non-streaming: buffer and return
                    content = upstream.content
                    await c.aclose()
                    mark_ok(route, f"200 via {route}")
                    return Response(content=content, status_code=upstream.status_code,
                                    media_type=upstream.headers.get("content-type", "application/json"))
            except Exception as e:
                mark_fail(route, f"proxy ERR {type(e).__name__}: {str(e)[:80]}")
                route = pick_route(model)
                if route in tried:
                    break
                continue
        else:
            mark_fail(route, detail)
            route = pick_route(model)
            if route in tried:
                break
            continue
    raise HTTPException(status_code=503, detail=f"all routes for '{model}' unavailable. tried: {tried}")

@app.get("/api/ping")
def ping():
    return {"ok": True, "ts": now()}

@app.get("/v1/_request_log")
def get_request_log():
    with _request_log_lock:
        entries = list(_request_log)
    return {"entries": entries[-50:], "total": len(entries)}

@app.get("/v1/_status")
def get_status():
    with lock:
        snap = {k: dict(v) for k, v in status.items()}
    return JSONResponse(snap)

@app.post("/v1/_test/{route:path}")
async def test_one(route: str):
    if route not in [r for rs in groups.values() for r in rs]:
        raise HTTPException(status_code=404, detail=f"unknown route '{route}'")
    ok, detail = await test_route(route)
    if ok:
        mark_ok(route, detail)
    else:
        mark_fail(route, detail)
    return {"route": route, "ok": ok, "detail": detail}

# free-llm-gateway style: parallel batch with semaphore, like their /api/keys/validate-all
import asyncio as _asyncio

@app.post("/v1/_test_all")
@app.post("/api/keys/validate-all")
async def test_all():
    sem = _asyncio.Semaphore(4)
    results = {}

    async def _one(r):
        async with sem:
            ok, detail = await test_route(r, timeout=20)
            if ok:
                mark_ok(r, detail)
            else:
                mark_fail(r, detail)
            return r, ok, detail

    all_routes = [r for rs in groups.values() for r in rs]
    # deduplicate (same route may appear in multiple groups)
    uniq = list(dict.fromkeys(all_routes))
    outs = await _asyncio.gather(*[_one(r) for r in uniq])
    for r, ok, detail in outs:
        results[r] = {"ok": ok, "detail": detail}
    return {"results": results}

def scheduled_sweep(tag=""):
    import asyncio
    async def _sweep():
        out = []
        for gname, routes in groups.items():
            for r in routes:
                ok, detail = await test_route(r)
                if ok:
                    mark_ok(r, detail)
                else:
                    mark_fail(r, detail)
                out.append(f"  {r}: {'OK' if ok else 'FAIL'} {detail}")
        return out
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"{ts} [sweep{(' '+tag) if tag else ''}]"]
    lines += asyncio.run(_sweep())
    LOG_DIR.joinpath("health.log").open("a").write("\n".join(lines) + "\n")
    save_status()

def _find_port(preferred: int, bind_host: str, tries: int = 20) -> int:
    for p in range(preferred, preferred + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((bind_host, p))
                return p
            except OSError:
                continue
    return preferred  # fallback: let uvicorn report the error

# ---- startup (module-level; avoids on_event kwarg removed in this fastapi) ----
def startup():
    load_config()
    save_status()
    sched = BackgroundScheduler()
    # 3 daily sweeps at 5:00 / 5:20 / 5:40
    sched.add_job(scheduled_sweep, "cron", hour=5, minute=0, args=["0500"])
    sched.add_job(scheduled_sweep, "cron", hour=5, minute=20, args=["0520"])
    sched.add_job(scheduled_sweep, "cron", hour=5, minute=40, args=["0540"])
    sched.start()
    # watchdog: self-ping; log only (Task Scheduler handles restart on boot/crash)
    def watchdog():
        consec = 0
        while True:
            time.sleep(30)
            # use actual_port if already resolved, else cfg port
            port = actual_port or int(os.environ.get("FREE_ROUTER_PORT") or cfg.get("port", 8765))
            bind = actual_bind or os.environ.get("FREE_ROUTER_BIND") or cfg.get("bind", "127.0.0.1")
            # 127.0.0.1 always works for self-ping even if bind is 0.0.0.0
            ping_host = "127.0.0.1"
            try:
                with httpx.Client(timeout=5, trust_env=False) as c:
                    c.get(f"http://{ping_host}:{port}/v1/_status")
                consec = 0
            except Exception:
                consec += 1
                LOG_DIR.joinpath("router.log").open("a").write(
                    f"{datetime.now()} watchdog: status unreachable ({consec}) "
                    f"- Task Scheduler will restart on crash\n")
    threading.Thread(target=watchdog, daemon=True).start()

startup()

# web management UI (same port, "/")
from webui import router as _webui_router
app.include_router(_webui_router)

if __name__ == "__main__":
    import uvicorn
    pref_port = int(os.environ.get("FREE_ROUTER_PORT") or (cfg.get("port", 8765) if cfg else 8765))
    pref_bind = os.environ.get("FREE_ROUTER_BIND") or (cfg.get("bind", "127.0.0.1") if cfg else "127.0.0.1")
    actual_port = _find_port(pref_port, pref_bind)
    actual_bind = pref_bind
    # persist so launchers/clients can discover the chosen port
    try:
        (BASE / "port.txt").write_text(str(actual_port))
        (BASE / "bind.txt").write_text(actual_bind)
    except Exception:
        pass
    if actual_port != pref_port:
        LOG_DIR.joinpath("router.log").open("a").write(
            f"{datetime.now()} port {pref_port} busy, using {actual_port}\n")
        print(f"[free-router] port {pref_port} busy, using {actual_port}")
    else:
        print(f"[free-router] listening on http://{actual_bind}:{actual_port}")
    uvicorn.run(app, host=pref_bind, port=actual_port, log_level="info")
