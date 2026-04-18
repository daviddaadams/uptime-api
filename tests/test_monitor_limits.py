"""Tests for monitor plan limits enforcement."""
import pytest
import sqlite3
import sys, os
from pathlib import Path
import tempfile
import importlib.util
import uuid

UPTIME_API_ROOT = Path.home() / "uptime-api"
MAIN_FILE = UPTIME_API_ROOT / "main.py"


def load_main(temp_dir: Path):
    """Load main module with fresh in-memory DB in a temp dir."""
    module_name = f"uptime_api_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, MAIN_FILE)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load uptime-api main module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.DATABASE = str(temp_dir / "uptime.db")
    # Initialize the DB schema (normally done on startup event)
    module.init_db()
    return module


def signup(client, email, plan):
    """Create a monitor+account via /signup and return the api_key."""
    resp = client.post("/signup", json={
        "email": email,
        "plan": plan,
        "url": "https://example.com",
        "name": "Test Monitor",
    })
    assert resp.status_code == 201, f"signup failed: {resp.status_code} {resp.json()}"
    return resp.json()["api_key"]


class TestMonitorCountLimits:
    """Test monitor creation respects PLAN_MONITOR_LIMITS."""

    def test_free_plan_limit_enforced(self):
        """Free users cannot create more than 3 monitors total.

        Signup creates the first monitor, so only LIMIT-1 additional
        monitors can be created via /monitors before hitting the limit.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            mod = load_main(tmpdir)
            from fastapi.testclient import TestClient
            client = TestClient(mod.app)
            LIMIT = mod.PLAN_MONITOR_LIMITS["free"]
            api_key = signup(client, "free@test.com", "free")  # creates monitor 1
            headers = {"X-API-Key": api_key}

            # Create LIMIT-1 more monitors (signup already created 1)
            for i in range(LIMIT - 1):
                resp = client.post("/monitors", json={
                    "name": f"M{i+1}",
                    "url": f"https://ex{i+1}.com",
                    "plan": "free",
                }, headers=headers)
                assert resp.status_code == 201, f"Monitor {i+1} should succeed"

            # Next one exceeds the limit -> 402
            resp = client.post("/monitors", json={
                "name": "Over",
                "url": "https://over.com",
                "plan": "free",
            }, headers=headers)
            assert resp.status_code == 402
            body = resp.json()
            # Error is wrapped in {"error": ...} by http_exception_handler
            err = body.get("error", body)
            assert err["code"] == "MONITOR_LIMIT_REACHED"
            assert err["limit"] == LIMIT
            assert err["upgrade_url"] == "/checkout/pro"

    def test_pro_plan_higher_limit(self):
        """Pro users get more monitors than free."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            mod = load_main(tmpdir)
            from fastapi.testclient import TestClient
            client = TestClient(mod.app)
            free_limit = mod.PLAN_MONITOR_LIMITS["free"]
            pro_limit = mod.PLAN_MONITOR_LIMITS["pro"]
            api_key = signup(client, "pro@test.com", "pro")  # creates monitor 1
            headers = {"X-API-Key": api_key}

            # Pro should allow free_limit more monitors (signup took 1)
            for i in range(free_limit - 1):
                resp = client.post("/monitors", json={
                    "name": f"M{i+1}",
                    "url": f"https://ex{i+1}.com",
                    "plan": "pro",
                }, headers=headers)
                assert resp.status_code == 201

            # free_limit total -> still OK for pro
            resp = client.post("/monitors", json={
                "name": "Extra",
                "url": "https://extra.com",
                "plan": "pro",
            }, headers=headers)
            assert resp.status_code == 201

    def test_business_plan_highest_limit(self):
        """Business users get the most monitors."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            mod = load_main(tmpdir)
            from fastapi.testclient import TestClient
            client = TestClient(mod.app)
            biz_limit = mod.PLAN_MONITOR_LIMITS["business"]
            api_key = signup(client, "biz@test.com", "business")  # creates monitor 1
            headers = {"X-API-Key": api_key}

            for i in range(biz_limit - 1):
                resp = client.post("/monitors", json={
                    "name": f"B{i+1}",
                    "url": f"https://b{i+1}.com",
                    "plan": "business",
                }, headers=headers)
                assert resp.status_code == 201, f"Monitor {i+1} should succeed for business"

            resp = client.post("/monitors", json={
                "name": "OverBiz",
                "url": "https://overbiz.com",
                "plan": "business",
            }, headers=headers)
            assert resp.status_code == 402

    def test_biz_alias_uses_business_limits(self):
        """'biz' alias should resolve to 'business' and enforce business limits."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            mod = load_main(tmpdir)
            from fastapi.testclient import TestClient
            client = TestClient(mod.app)
            biz_limit = mod.PLAN_MONITOR_LIMITS["business"]
            assert mod.canonicalize_plan("biz") == "business"
            api_key = signup(client, "alias@test.com", "biz")  # creates monitor 1
            headers = {"X-API-Key": api_key}

            for i in range(biz_limit - 1):
                resp = client.post("/monitors", json={
                    "name": f"A{i+1}",
                    "url": f"https://a{i+1}.com",
                    "plan": "biz",
                }, headers=headers)
                assert resp.status_code == 201

            resp = client.post("/monitors", json={
                "name": "OverAlias",
                "url": "https://overalias.com",
                "plan": "biz",
            }, headers=headers)
            assert resp.status_code == 402

    def test_402_includes_upgrade_url_and_current_count(self):
        """402 response is detailed with limit, current, and upgrade path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            mod = load_main(tmpdir)
            from fastapi.testclient import TestClient
            client = TestClient(mod.app)
            limit = mod.PLAN_MONITOR_LIMITS["free"]
            api_key = signup(client, "details@test.com", "free")  # creates monitor 1
            headers = {"X-API-Key": api_key}

            # Create limit-1 more to reach the cap
            for i in range(limit - 1):
                client.post("/monitors", json={
                    "name": f"D{i+1}",
                    "url": f"https://d{i+1}.com",
                    "plan": "free",
                }, headers=headers)

            resp = client.post("/monitors", json={
                "name": "Last",
                "url": "https://last.com",
                "plan": "free",
            }, headers=headers)

            assert resp.status_code == 402
            body = resp.json()
            err = body.get("error", body)
            assert err["code"] == "MONITOR_LIMIT_REACHED"
            assert err["limit"] == limit
            assert err["current"] == limit
            assert err["upgrade_url"] == "/checkout/pro"

    def test_plan_monitor_limits_constants(self):
        """Verify PLAN_MONITOR_LIMITS values are sane."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            mod = load_main(tmpdir)
            limits = mod.PLAN_MONITOR_LIMITS
            assert limits["free"] == 3
            assert limits["pro"] == 50
            assert limits["business"] == 500

    def test_canonicalize_plan_normalizes_case_whitespace_and_default(self):
        """Plan normalization should be resilient to messy user input."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            mod = load_main(tmpdir)
            assert mod.canonicalize_plan(" BIZ ") == "business"
            assert mod.canonicalize_plan(" Pro ") == "pro"
            assert mod.canonicalize_plan("") == "free"

    def test_unknown_plan_uses_default_limit_fallback(self):
        """Unknown plans should still enforce the default safety cap."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            mod = load_main(tmpdir)
            from fastapi.testclient import TestClient
            client = TestClient(mod.app)
            api_key = signup(client, "fallback@test.com", "free")
            headers = {"X-API-Key": api_key}

            for i in range(2):
                resp = client.post("/monitors", json={
                    "name": f"F{i+1}",
                    "url": f"https://fallback{i+1}.com",
                    "plan": "free",
                }, headers=headers)
                assert resp.status_code == 201

            with sqlite3.connect(mod.DATABASE) as conn:
                team_id = conn.execute(
                    "SELECT team_id FROM monitors ORDER BY id LIMIT 1"
                ).fetchone()[0]
                with pytest.raises(mod.HTTPException) as exc_info:
                    mod.check_monitor_count_limit(conn, team_id, "enterprise")

            assert exc_info.value.status_code == 402
            assert exc_info.value.detail["limit"] == mod.PLAN_MONITOR_LIMITS["free"]

    def test_biz_alias_is_canonicalized_in_signup_and_monitor_responses(self):
        """Alias input should return canonical plan values to clients."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            mod = load_main(tmpdir)
            from fastapi.testclient import TestClient
            client = TestClient(mod.app)

            signup_resp = client.post("/signup", json={
                "email": "canonical@test.com",
                "plan": "biz",
                "url": "https://canonical.example.com",
                "name": "Canonical Monitor",
            })
            assert signup_resp.status_code == 201
            signup_body = signup_resp.json()
            assert signup_body["plan"] == "business"

            api_key = signup_body["api_key"]
            headers = {"X-API-Key": api_key}
            create_resp = client.post("/monitors", json={
                "name": "Canonical Child",
                "url": "https://canonical-child.example.com",
                "plan": "biz",
            }, headers=headers)
            assert create_resp.status_code == 201
            assert create_resp.json()["plan"] == "business"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
