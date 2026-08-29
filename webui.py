# Free Model API Gateway — web management UI
# Read + control: dashboard, route test/clear/disable, provider add/edit/delete,
# group edit, config hot-reload, log tail. Lives on the same port (8765) at "/".
# To avoid a circular import with router.py, the router module is imported lazily
# inside each handler (router is fully loaded by the time these run).
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
import yaml, json, time

WEB = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(WEB / "web" / "templates"))
router = APIRouter()

def R():
    import router as _r
    return _r

def write_providers(p):
    """Dump providers dict back to providers.yml, preserving order/comments-ish."""
    (R().PROVIDERS_YML).write_text(yaml.safe_dump(p, sort_keys=False, default_flow_style=False))

def read_providers():
    import router as _r
    with open(_r.PROVIDERS_YML) as f:
        return yaml.safe_load(f) or {}

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    r = R()
    with r.lock:
        st = {k: dict(v) for k, v in r.status.items()}
    routes_view = []
    for gname, routes in r.groups.items():
        for rt in routes:
            s = st.get(rt, {})
            state = "disabled" if rt in r.disabled_routes else (
                "manual" if s.get("manual") else (
                    "cool" if s.get("cooldown_until", 0) > time.time() else (
                        "ok" if s.get("healthy") else "fail")))
            routes_view.append({
                "route": rt, "group": gname, "state": state,
                "result": s.get("last_result", ""),
                "last_check": s.get("last_check", 0),
                "cap": s.get("max_input_tokens"),
            })
    counts = {"ok": 0, "cool": 0, "manual": 0, "fail": 0, "disabled": 0}
    for rv in routes_view:
        counts[rv["state"]] = counts.get(rv["state"], 0) + 1
    return TEMPLATES.TemplateResponse(request, "index.html", {
        "request": request, "groups": r.groups, "routes": routes_view,
        "counts": counts, "providers": list(r.providers.keys()),
        "disabled": sorted(r.disabled_routes), "now": int(time.time()),
    })

@router.get("/providers", response_class=HTMLResponse)
def providers_page(request: Request):
    r = R()
    p = read_providers()
    return TEMPLATES.TemplateResponse(request, "providers.html", {
        "request": request, "providers": p.get("providers", {}),
        "groups": p.get("groups", {}), "overrides": p.get("route_overrides", {}),
        "disabled": p.get("disabled_routes", []),
    })

@router.post("/providers/add")
def providers_add(name: str = Form(...), baseUrl: str = Form(...),
                  apiKey: str = Form(...), cooldown_minutes: int = Form(10)):
    p = read_providers()
    p.setdefault("providers", {})[name] = {
        "baseUrl": baseUrl.rstrip("/"), "apiKey": apiKey,
        "cooldown_minutes": int(cooldown_minutes)}
    write_providers(p)
    R().reload_from_disk()
    return RedirectResponse("/providers", status_code=303)

@router.post("/providers/edit/{name}")
def providers_edit(name: str, baseUrl: str = Form(...),
                   apiKey: str = Form(...), cooldown_minutes: int = Form(10)):
    p = read_providers()
    if name in p.get("providers", {}):
        p["providers"][name] = {
            "baseUrl": baseUrl.rstrip("/"), "apiKey": apiKey,
            "cooldown_minutes": int(cooldown_minutes)}
        write_providers(p)
        R().reload_from_disk()
    return RedirectResponse("/providers", status_code=303)

@router.post("/providers/delete/{name}")
def providers_delete(name: str):
    p = read_providers()
    used = [rt for rs in p.get("groups", {}).values() for rt in rs if rt.startswith(name + "/")]
    if used:
        return JSONResponse({"error": f"provider still used by routes: {used}"}, status_code=400)
    p.get("providers", {}).pop(name, None)
    write_providers(p)
    R().reload_from_disk()
    return RedirectResponse("/providers", status_code=303)

@router.post("/groups/edit")
def groups_edit(group: str = Form(...), routes: str = Form(...)):
    p = read_providers()
    p.setdefault("groups", {})
    p["groups"][group] = [x.strip() for x in routes.replace("\n", ",").split(",") if x.strip()]
    write_providers(p)
    R().reload_from_disk()
    return RedirectResponse("/providers", status_code=303)

@router.post("/route/{route:path}/test")
@router.post("/api/route/{route:path}/test")
async def route_test(route: str):
    r = R()
    # 20s cap (providers like b.ai can take 7-12s), prevents browser "keeps loading" forever
    ok, detail = await r.test_route(route, timeout=20)
    if ok:
        r.mark_ok(route, detail)
    else:
        r.mark_fail(route, detail)
    return {"route": route, "ok": ok, "detail": detail}

@router.get("/api/status")
def api_status():
    r = R()
    with r.lock:
        snap = {k: dict(v) for k, v in r.status.items()}
    return snap

@router.get("/api/ping")
def api_ping():
    import time as _t
    return {"ok": True, "ts": int(_t.time())}

@router.post("/route/{route:path}/clear")
def route_clear(route: str):
    r = R()
    with r.lock:
        s = r.status.setdefault(route, {})
        s["cooldown_until"] = 0
        s["manual"] = False
        s["failures"] = 0
        s["healthy"] = True
    r.save_status()
    return {"route": route, "ok": True}

@router.post("/route/{route:path}/disable")
def route_disable(route: str):
    r = R()
    p = read_providers()
    d = set(p.get("disabled_routes", []) or [])
    d.add(route)
    p["disabled_routes"] = sorted(d)
    write_providers(p)
    r.reload_from_disk()
    return {"route": route, "ok": True}

@router.post("/route/{route:path}/enable")
def route_enable(route: str):
    r = R()
    p = read_providers()
    d = set(p.get("disabled_routes", []) or [])
    d.discard(route)
    p["disabled_routes"] = sorted(d)
    write_providers(p)
    r.reload_from_disk()
    return {"route": route, "ok": True}

@router.post("/reload")
def do_reload():
    R().reload_from_disk()
    return {"ok": True}

@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    r = R()
    def tail(name, n=200):
        f = r.LOG_DIR / name
        if not f.exists():
            return ""
        lines = f.read_text(errors="replace").splitlines()
        return "\n".join(lines[-n:])
    return TEMPLATES.TemplateResponse(request, "logs.html", {
        "request": request,
        "health": tail("health.log"),
        "router": tail("router.log"),
        "stdout": tail("stdout.log"),
        "stderr": tail("stderr.log"),
    })

@router.get("/requests", response_class=HTMLResponse)
def requests_page(request: Request):
    r = R()
    with r._request_log_lock:
        entries = list(r._request_log)[-100:]
    entries.reverse()  # newest first
    # load from log file if memory is empty
    if not entries:
        f = r.LOG_DIR / "requests.log"
        if f.exists():
            lines = f.read_text(errors="replace").splitlines()[-100:]
            entries = list(reversed(lines))
    return TEMPLATES.TemplateResponse(request, "requests.html", {
        "request": request,
        "entries": entries,
        "total": len(entries),
    })
