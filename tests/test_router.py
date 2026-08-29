"""Unit tests for free-router core logic.

Run with: pytest tests/test_router.py -v
"""
import os, sys, time
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

# Ensure the project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Pre-import mocking: startup() is called at module level in router.py, so we
# must stub out apscheduler and the watchdog thread before it's imported.
# ---------------------------------------------------------------------------
_mock_scheduler = MagicMock()
sys.modules["apscheduler"] = MagicMock()
sys.modules["apscheduler.schedulers"] = MagicMock()
sys.modules["apscheduler.schedulers.background"] = MagicMock()

# Patch save_status to be a no-op before import (avoids file I/O in tests)
import router as r
r.save_status = lambda: None  # prevent disk writes during tests

# Clear any state loaded from real providers.yml during import
r.status.clear()
r.groups.clear()
r.providers.clear()
r.disabled_routes.clear()


@pytest.fixture(autouse=True)
def reset_state():
    """Reset router state before each test."""
    r.status.clear()
    r.groups.clear()
    r.providers.clear()
    r.disabled_routes.clear()
    yield
    r.status.clear()
    r.groups.clear()
    r.providers.clear()
    r.disabled_routes.clear()


# ── mark_ok / mark_fail / mark_cool ──

def test_mark_ok_sets_healthy():
    r.mark_ok("test/route", "200 OK")
    s = r.status["test/route"]
    assert s["healthy"] is True
    assert s["last_result"] == "200 OK"
    assert s["failures"] == 0
    assert s["cooldown_until"] == 0


def test_mark_fail_increments_failures():
    r.mark_fail("test/route", "HTTP 500")
    s = r.status["test/route"]
    assert s["healthy"] is False
    assert s["failures"] == 1
    assert s["cooldown_until"] > r.now()


def test_mark_fail_3_consecutive_triggers_manual_review():
    for _ in range(3):
        r.mark_fail("test/route", "fail")
    s = r.status["test/route"]
    assert s["manual"] is True
    assert s["cooldown_until"] == r.now() + 24 * 3600
    assert "manual-review" in s["last_result"]


def test_mark_ok_clears_failures():
    r.mark_fail("test/route", "fail")
    r.mark_fail("test/route", "fail")
    r.mark_ok("test/route", "recovered")
    s = r.status["test/route"]
    assert s["healthy"] is True
    assert s["failures"] == 0


def test_mark_cool_sets_cooldown():
    r.mark_cool("test/route", 10)
    s = r.status["test/route"]
    assert s["healthy"] is False
    assert s["cooldown_until"] == r.now() + 600


# ── is_cool ──

def test_is_cool_returns_false_when_not_cooling():
    r.status["test/route"] = {"cooldown_until": 0, "manual": False}
    assert r.is_cool("test/route") is False


def test_is_cool_returns_true_when_in_cooldown():
    r.status["test/route"] = {"cooldown_until": r.now() + 600, "manual": False}
    assert r.is_cool("test/route") is True


def test_is_cool_returns_true_when_manual():
    r.status["test/route"] = {"cooldown_until": 0, "manual": True}
    assert r.is_cool("test/route") is True


# ── pick_route ──

def test_pick_route_prefers_healthy():
    r.groups = {"test-group": ["a/model", "b/model"]}
    r.status["a/model"] = {"healthy": True, "last_check": 0, "cooldown_until": 0, "manual": False}
    r.status["b/model"] = {"healthy": True, "last_check": 100, "cooldown_until": 0, "manual": False}
    route = r.pick_route("test-group")
    assert route == "a/model"  # least recently checked


def test_pick_route_skips_disabled():
    r.groups = {"test-group": ["a/model", "b/model"]}
    r.disabled_routes = {"a/model"}
    r.status["a/model"] = {"healthy": True, "last_check": 0, "cooldown_until": 0, "manual": False}
    r.status["b/model"] = {"healthy": True, "last_check": 0, "cooldown_until": 0, "manual": False}
    route = r.pick_route("test-group")
    assert route == "b/model"


def test_pick_route_skips_cooling():
    r.groups = {"test-group": ["a/model", "b/model"]}
    r.status["a/model"] = {"healthy": True, "last_check": 0, "cooldown_until": r.now() + 600, "manual": False}
    r.status["b/model"] = {"healthy": True, "last_check": 0, "cooldown_until": 0, "manual": False}
    route = r.pick_route("test-group")
    assert route == "b/model"


def test_pick_route_returns_none_when_all_unavailable():
    r.groups = {"test-group": ["a/model"]}
    r.status["a/model"] = {"healthy": True, "last_check": 0, "cooldown_until": r.now() + 600, "manual": False}
    assert r.pick_route("test-group") is None


def test_pick_route_returns_none_for_unknown_group():
    r.groups = {}
    assert r.pick_route("nonexistent") is None


# ── model_list ──

def test_model_list_returns_groups():
    r.groups = {"free-fast": ["a/x"], "free-coder": ["b/y"]}
    result = r.model_list()
    ids = {m["id"] for m in result["data"]}
    assert ids == {"free-fast", "free-coder"}


# ── provider_of / route_model / cooldown_for ──

def test_provider_of():
    assert r.provider_of("bai/model") == "bai"
    assert r.provider_of("inferx/Qwen3") == "inferx"


def test_route_model():
    assert r.route_model("bai/model") == "model"
    assert r.route_model("inferx/Qwen3-Coder") == "Qwen3-Coder"
    assert r.route_model("solo") == "solo"


def test_cooldown_for():
    r.providers = {"myprov": {"cooldown_minutes": 15}}
    assert r.cooldown_for("myprov/model") == 15
    assert r.cooldown_for("unknown/model") == 10  # default


# ── auth ──

def test_check_api_auth_no_key_set():
    """When FREE_ROUTER_API_KEY is empty, no auth required."""
    with patch.object(r, "API_KEY", ""):
        req = MagicMock()
        req.url.path = "/v1/chat/completions"
        req.headers = {}
        r.check_api_auth(req)  # should not raise


def test_check_api_auth_valid_key():
    with patch.object(r, "API_KEY", "sk-secret123"):
        req = MagicMock()
        req.url.path = "/v1/chat/completions"
        req.headers = {"authorization": "Bearer sk-secret123"}
        r.check_api_auth(req)  # should not raise


def test_check_api_auth_invalid_key():
    from fastapi import HTTPException
    with patch.object(r, "API_KEY", "sk-secret123"):
        req = MagicMock()
        req.url.path = "/v1/chat/completions"
        req.headers = {"authorization": "Bearer wrong-key"}
        with pytest.raises(HTTPException) as exc_info:
            r.check_api_auth(req)
        assert exc_info.value.status_code == 401


def test_check_api_auth_missing_key():
    from fastapi import HTTPException
    with patch.object(r, "API_KEY", "sk-secret123"):
        req = MagicMock()
        req.url.path = "/v1/chat/completions"
        req.headers = {}
        with pytest.raises(HTTPException) as exc_info:
            r.check_api_auth(req)
        assert exc_info.value.status_code == 401


def test_check_api_auth_skips_non_v1():
    """Web UI routes and /api/* should not require API key auth."""
    with patch.object(r, "API_KEY", "sk-secret123"):
        req = MagicMock()
        req.url.path = "/"
        req.headers = {}
        r.check_api_auth(req)  # should not raise

        req.url.path = "/api/ping"
        r.check_api_auth(req)  # should not raise


# ── cooldown_for integration ──

def test_cooldown_for_uses_provider_config():
    r.providers = {
        "fast": {"cooldown_minutes": 5},
        "slow": {"cooldown_minutes": 30},
    }
    assert r.cooldown_for("fast/model") == 5
    assert r.cooldown_for("slow/model") == 30
