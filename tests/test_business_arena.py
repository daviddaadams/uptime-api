from __future__ import annotations

import importlib.util
import json
import tempfile
import types
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient


WORKSPACE_ROOT = Path.home() / ".openclaw" / "workspace"
UPTIME_API_ROOT = Path.home() / "uptime-api"
MAIN_FILE = UPTIME_API_ROOT / "main.py"


if str(WORKSPACE_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(WORKSPACE_ROOT))

from business_arena.ledger import ensure_app_telemetry_schema, fetch_reward_summary, open_sqlite, record_app_event
from business_arena.owlpulse_adapter import render_home_html
from business_arena.schemas import AttributionContext, ExperimentEvent
from business_arena.simulator import score_offer_program
from business_arena.runtime import load_offer_program, run_autonomous_cycle
from business_arena.agency import run_agency_cycle


def load_main_module(temp_dir: Path) -> types.ModuleType:
    module_name = f"uptime_api_main_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, MAIN_FILE)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load uptime-api main module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.DATABASE = str(temp_dir / "uptime.db")
    module.DEFAULT_ARENA_DB_PATH = temp_dir / "business_arena.db"
    module.STATUS_PAGE_RENDERER_DIR = temp_dir / "renderer"
    module.LOCAL_STATUS_PAGE_RENDERER_DIR = temp_dir / "renderer_local"
    module.STATUS_PAGE_DESIGN_DIR = temp_dir / "design"
    module.LOCAL_STATUS_PAGE_DESIGN_DIR = temp_dir / "design_local"
    module.STATUS_PAGE_TEMPLATE_DIR = temp_dir / "templates"
    module.LOCAL_STATUS_PAGE_TEMPLATE_DIR = temp_dir / "templates_local"
    return module


class BusinessArenaTests(unittest.TestCase):
    def test_simulator_returns_weighted_components(self) -> None:
        program = load_offer_program()
        result = score_offer_program(program)
        self.assertIn("components", result)
        self.assertIn("persona_conversion_proxy", result["components"])
        self.assertGreater(result["score"], 0.5)

    def test_reward_rollup_uses_highest_session_milestone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "telemetry.db"
            conn = open_sqlite(db_path)
            try:
                ensure_app_telemetry_schema(conn)
                attribution = AttributionContext(
                    source="test",
                    experiment_id="exp-1",
                    variant_id="var-a",
                    session_id="session-1",
                )
                record_app_event(
                    conn,
                    ExperimentEvent(
                        event_name="landing_page_view",
                        surface="landing_page",
                        attribution=attribution,
                    ),
                )
                record_app_event(
                    conn,
                    ExperimentEvent(
                        event_name="signup_success",
                        surface="signup",
                        attribution=attribution,
                    ),
                )
                rewards = fetch_reward_summary(conn, experiment_id="exp-1")
                self.assertEqual(len(rewards), 1)
                self.assertEqual(rewards[0]["session_count"], 1)
                self.assertAlmostEqual(rewards[0]["reward_mean"], 0.55, places=6)
                self.assertEqual(rewards[0]["signup_successes"], 1)
            finally:
                conn.close()

    def test_signup_flow_records_arena_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_main_module(Path(temp_dir))
            with TestClient(module.app) as client:
                home = client.get("/")
                self.assertEqual(home.status_code, 200)
                self.assertIn("Freshping Is Gone.", home.text)
                self.assertIn("Trusted migration signals", home.text)

                event_resp = client.post(
                    "/internal/arena/events",
                    json={
                        "event_name": "landing_page_view",
                        "surface": "landing_page",
                        "experiment_id": "exp-home",
                        "variant_id": "baseline",
                        "session_id": "session-home",
                        "source": "test-suite",
                    },
                )
                self.assertEqual(event_resp.status_code, 202)

                signup_resp = client.post(
                    "/signup",
                    json={
                        "name": "Acme API",
                        "url": "https://example.com/health",
                        "email": "ops@example.com",
                        "plan": "pro",
                        "source": "test-suite",
                        "channel": "integration",
                        "campaign": "business-arena-tests",
                        "experiment_id": "exp-signup",
                        "variant_id": "var-a",
                        "session_id": "session-signup",
                        "create_status_page": True,
                    },
                )
                self.assertEqual(signup_resp.status_code, 201, signup_resp.text)
                signup_payload = signup_resp.json()
                self.assertEqual(signup_payload["experiment_id"], "exp-signup")
                self.assertEqual(signup_payload["variant_id"], "var-a")
                self.assertTrue(signup_payload["status_page"]["slug"])

                rewards_resp = client.get("/internal/arena/rewards", params={"experiment_id": "exp-signup"})
                self.assertEqual(rewards_resp.status_code, 200)
                rewards_payload = rewards_resp.json()
                self.assertEqual(rewards_payload["count"], 1)
                self.assertGreaterEqual(rewards_payload["rewards"][0]["reward_mean"], 0.70)

                snapshot_resp = client.get("/internal/arena/snapshot")
                self.assertEqual(snapshot_resp.status_code, 200)
                self.assertIn("signup", snapshot_resp.json()["allowed_surfaces"])

                stats_resp = client.get("/internal/stats")
                self.assertEqual(stats_resp.status_code, 200)
                self.assertGreaterEqual(stats_resp.json()["signups_today"], 1)

    def test_render_home_html_uses_runtime_trust_metrics_with_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            app_db = Path(temp_dir) / "app.db"
            with open_sqlite(app_db) as conn:
                conn.execute(
                    """
                    CREATE TABLE monitors (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE checks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        monitor_id INTEGER
                    )
                    """
                )
                conn.executemany(
                    "INSERT INTO monitors (url) VALUES (?)",
                    [("https://a.example.com",), ("https://b.example.com",), ("https://c.example.com",)],
                )
                conn.executemany(
                    "INSERT INTO checks (monitor_id) VALUES (?)",
                    [(1,)] * 1250,
                )
                conn.commit()

            rendered = render_home_html(
                load_offer_program(),
                AttributionContext(),
                app_db_path=app_db,
            )

            self.assertIn("<strong>3</strong> Active Monitors", rendered)
            self.assertIn("<strong>14</strong> Status Pages", rendered)
            self.assertIn("<strong>1.2K+</strong> Checks Run", rendered)
            self.assertIn("cohort=trust-bar-enabled", rendered)

    def test_trust_bar_metadata_emitted_for_landing_signup_and_checkout_events(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            module = load_main_module(Path(temp_dir))
            with TestClient(module.app) as client:
                landing_event = client.post(
                    "/internal/arena/events",
                    json={
                        "event_name": "landing_page_view",
                        "surface": "landing_page",
                        "source": "test-suite",
                        "experiment_id": "exp-trust",
                        "variant_id": "baseline",
                        "cohort": "trust-bar-enabled",
                        "session_id": "session-landing",
                    },
                )
                self.assertEqual(landing_event.status_code, 202)

                signup_resp = client.post(
                    "/signup",
                    json={
                        "name": "Trust API",
                        "url": "https://example.com/health",
                        "email": "ops@example.com",
                        "plan": "pro",
                        "source": "test-suite",
                        "channel": "integration",
                        "campaign": "business-arena-tests",
                        "experiment_id": "exp-trust",
                        "variant_id": "var-a",
                        "cohort": "trust-bar-enabled",
                        "session_id": "session-signup",
                        "create_status_page": True,
                    },
                )
                self.assertEqual(signup_resp.status_code, 201, signup_resp.text)

                checkout_resp = client.get(
                    "/checkout/pro",
                    params={
                        "source": "test-suite",
                        "experiment_id": "exp-trust",
                        "variant_id": "var-a",
                        "cohort": "trust-bar-enabled",
                        "session_id": "session-checkout",
                    },
                    follow_redirects=False,
                )
                self.assertIn(checkout_resp.status_code, {302, 307})

            with open_sqlite(module.DATABASE) as conn:
                rows = conn.execute(
                    """
                    SELECT event_name, cohort, metadata_json
                    FROM experiment_events
                    WHERE event_name IN ('landing_page_view', 'signup_success', 'checkout_click')
                    ORDER BY id ASC
                    """
                ).fetchall()

            self.assertEqual(len(rows), 3)
            events = {row["event_name"]: row for row in rows}
            for event_name in ("landing_page_view", "signup_success", "checkout_click"):
                metadata = json.loads(events[event_name]["metadata_json"])
                self.assertTrue(metadata["trust_bar_enabled"])
                self.assertEqual(metadata["trust_bar_variant"], "metrics-badges-v1")
            self.assertEqual(events["landing_page_view"]["cohort"], "trust-bar-enabled")
            self.assertEqual(events["signup_success"]["cohort"], "trust-bar-enabled")
            self.assertEqual(events["checkout_click"]["cohort"], "trust-bar-enabled")

    def test_autonomous_cycle_creates_help_request_and_keeps_shadow_cycle_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            app_db = temp_root / "app.db"
            arena_db = temp_root / "arena.db"
            results_dir = temp_root / "results"
            runtime_dir = temp_root / "runtime"
            help_requests = temp_root / "help-requests.md"

            with open_sqlite(app_db) as conn:
                conn.execute(
                    """
                    CREATE TABLE monitors (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE status_pages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        slug TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE checks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        monitor_id INTEGER,
                        timestamp INTEGER
                    )
                    """
                )
                conn.execute("INSERT INTO monitors (url) VALUES ('https://example.com/health')")
                ensure_app_telemetry_schema(conn)
                conn.commit()

            program = load_offer_program().to_dict()
            program["home"]["hero_headline"] = ""
            offer_program = temp_root / "offer_program.json"
            offer_program.write_text(json.dumps(program, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

            result = run_autonomous_cycle(
                app_db_path=app_db,
                arena_db_path=arena_db,
                offer_program_path=offer_program,
                output_dir=results_dir,
                runtime_output_dir=runtime_dir,
                help_requests_path=help_requests,
            )

            self.assertEqual(result["mode"], "degraded")
            self.assertGreaterEqual(len(result["issues"]), 2)
            self.assertEqual(result["operator_requests"][0]["status"], "created")
            self.assertIn("BUSINESS_ARENA:SMOKE_CHECK_FAIL", help_requests.read_text(encoding="utf-8"))
            self.assertTrue((runtime_dir / "latest.json").exists())
            self.assertTrue((results_dir / "runs").exists())

            second = run_autonomous_cycle(
                app_db_path=app_db,
                arena_db_path=arena_db,
                offer_program_path=offer_program,
                output_dir=results_dir,
                runtime_output_dir=runtime_dir,
                help_requests_path=help_requests,
            )

            self.assertEqual(second["operator_requests"][0]["status"], "existing_open")
            self.assertEqual(help_requests.read_text(encoding="utf-8").count("BUSINESS_ARENA:SMOKE_CHECK_FAIL"), 1)

    def test_agency_cycle_resumes_active_run_and_synthesizes_followups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            app_db = temp_root / "app.db"
            arena_db = temp_root / "arena.db"
            results_dir = temp_root / "results"
            runtime_dir = temp_root / "runtime"
            help_requests = temp_root / "help-requests.md"
            comms_dir = temp_root / "comms"
            comms_dir.mkdir()

            for name in ("researcher", "strategist", "growth", "content", "designer", "coder", "plumber", "sentinel", "tron"):
                (comms_dir / f"{name}.md").write_text("", encoding="utf-8")

            with open_sqlite(app_db) as conn:
                conn.execute(
                    """
                    CREATE TABLE monitors (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE status_pages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        slug TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE checks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        monitor_id INTEGER,
                        timestamp INTEGER
                    )
                    """
                )
                conn.execute("INSERT INTO monitors (url) VALUES ('https://example.com/health')")
                ensure_app_telemetry_schema(conn)
                conn.commit()

            program = load_offer_program().to_dict()
            offer_program = temp_root / "offer_program.json"
            offer_program.write_text(json.dumps(program, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

            result = run_agency_cycle(
                app_db_path=app_db,
                arena_db_path=arena_db,
                offer_program_path=offer_program,
                output_dir=results_dir,
                runtime_output_dir=runtime_dir,
                help_requests_path=help_requests,
                comms_dir=comms_dir,
            )

            self.assertEqual(result["focus_key"], "traffic_activation")
            self.assertEqual(result["dispatch_count"], 8)
            self.assertTrue((Path(result["artifact_dir"]) / "plan.json").exists())
            self.assertIn("TASK-BA-TRAFFIC-ACTIVATION-GROWTH", (comms_dir / "growth.md").read_text(encoding="utf-8"))
            self.assertIn("BUSINESS_ARENA:AGENCY:TRAFFIC_ACTIVATION:GROWTH", help_requests.read_text(encoding="utf-8"))

            artifact_dir = Path(result["artifact_dir"])
            research_brief = artifact_dir / "research-brief.md"
            strategist_thesis = artifact_dir / "strategist-thesis.md"
            conversion_audit = artifact_dir / "conversion-audit.md"
            research_brief.write_text(
                "\n".join(
                    [
                        "# Research Brief",
                        "",
                        "| 1 | \"Freshping doesn't offer bulk exports. You'll need to manually document your monitors\" |",
                        '| 2 | "We just need something that works" |',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            strategist_thesis.write_text(
                "\n".join(
                    [
                        "# Thesis",
                        "",
                        "## THESIS: Freshping Refugee Capture via Compare Page",
                        "",
                        "Optimize the compare page, publish one owned migration asset, and prepare Reddit drafts for approval.",
                        "",
                        "## EXECUTION ORDER",
                        "1. Update compare page copy and attribution.",
                        "2. Publish a Freshping migration asset on owned surfaces.",
                        "3. Prepare Reddit traffic drafts for approval.",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            conversion_audit.write_text(
                "\n".join(
                    [
                        "# Conversion Audit",
                        "",
                        "| Field | Current Value | Suggested Change |",
                        "|-------|---------------|------------------|",
                        '| `home.hero_headline` | "Stop Getting Woken Up by False Alarms" | "Your Users Google You When You Go Down. Be Ready." |',
                        '| `home.cta_text` | "Start Free Monitoring" | "Create Free Page ->" |',
                        '| `compare.cta_text` | "Start free with OwlPulse" | "Start Free - No Credit Card" |',
                        '| `signup.header` | "Launch Your Status Page In Under 60 Seconds" | "Claim Your Free Status Page" |',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            (comms_dir / "researcher.md").write_text(
                (comms_dir / "researcher.md").read_text(encoding="utf-8").rstrip()
                + "\n\n"
                + "\n".join(
                    [
                        "### [2026-03-15 14:10 PT] researcher -> business-arena (TASK-BA-TRAFFIC-ACTIVATION-RESEARCHER)",
                        "**TASK_ID:** TASK-BA-TRAFFIC-ACTIVATION-RESEARCHER",
                        "**Status:** COMPLETE",
                        "**Summary:** Freshping migration pressure and exact user language captured.",
                        f"**Artifact:** `{research_brief}`",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (comms_dir / "strategist.md").write_text(
                (comms_dir / "strategist.md").read_text(encoding="utf-8").rstrip()
                + "\n\n"
                + "\n".join(
                    [
                        "### [2026-03-15 14:15 PT] strategist -> business-arena (TASK-BA-TRAFFIC-ACTIVATION-STRATEGIST)",
                        "**TASK_ID:** TASK-BA-TRAFFIC-ACTIVATION-STRATEGIST",
                        "**Status:** COMPLETE",
                        "**Summary:** Freshping compare-page thesis selected.",
                        f"**Artifact:** `{strategist_thesis}`",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (comms_dir / "designer.md").write_text(
                (comms_dir / "designer.md").read_text(encoding="utf-8").rstrip()
                + "\n\n"
                + "\n".join(
                    [
                        "### [2026-03-15 14:18 PT] designer -> business-arena (TASK-BA-TRAFFIC-ACTIVATION-DESIGNER)",
                        "**TASK_ID:** TASK-BA-TRAFFIC-ACTIVATION-DESIGNER",
                        "**Status:** COMPLETE",
                        "**Summary:** Conversion audit delivered with exact offer program field changes.",
                        f"**Artifact:** `{conversion_audit}`",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            second = run_agency_cycle(
                app_db_path=app_db,
                arena_db_path=arena_db,
                offer_program_path=offer_program,
                output_dir=results_dir,
                runtime_output_dir=runtime_dir,
                help_requests_path=help_requests,
                comms_dir=comms_dir,
            )

            self.assertEqual(second["dispatch_count"], 0)
            self.assertTrue(second["resumed_run"])
            self.assertEqual(second["run_id"], result["run_id"])
            self.assertTrue(Path(second["feedback_path"]).exists())
            self.assertTrue(Path(second["execution_plan_path"]).exists())
            self.assertTrue(Path(second["candidate_offer_program_path"]).exists())
            self.assertEqual(second["followup_dispatch_count"], 5)
            self.assertIn("BUSINESS_ARENA:APPROVAL:TRAFFIC_ACTIVATION:PUBLIC_DISTRIBUTION", help_requests.read_text(encoding="utf-8"))
            self.assertIn("TASK-BA-TRAFFIC-ACTIVATION-FOLLOWUP-GROWTH", (comms_dir / "growth.md").read_text(encoding="utf-8"))

            third = run_agency_cycle(
                app_db_path=app_db,
                arena_db_path=arena_db,
                offer_program_path=offer_program,
                output_dir=results_dir,
                runtime_output_dir=runtime_dir,
                help_requests_path=help_requests,
                comms_dir=comms_dir,
            )

            self.assertEqual(third["followup_dispatch_count"], 0)
            self.assertEqual((comms_dir / "growth.md").read_text(encoding="utf-8").count("TASK-BA-TRAFFIC-ACTIVATION-FOLLOWUP-GROWTH"), 1)


if __name__ == "__main__":
    unittest.main()
