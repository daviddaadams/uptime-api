#!/usr/bin/env python3
"""
Uptime Monitoring API - MVP+
Simple FastAPI service for monitoring website uptime with webhook alerts.
"""

import asyncio
import html
import json
import os
import re
import secrets
import socket
import sqlite3
import ssl
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import urlparse

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field, HttpUrl
from starlette.middleware.sessions import SessionMiddleware

try:
    from onelogin.saml2.auth import OneLogin_Saml2_Auth

    SAML_LIB_AVAILABLE = True
except Exception:
    OneLogin_Saml2_Auth = None
    SAML_LIB_AVAILABLE = False


# ============================================================================
# Configuration
# ============================================================================

DATABASE = os.getenv("DATABASE", "uptime.db")
CHECK_INTERVAL_FREE = 300  # 5 minutes
CHECK_INTERVAL_PRO = 60  # 1 minute
REQUEST_TIMEOUT = 10  # seconds
FALSE_POSITIVE_RETRY_DELAY_SECONDS = 2
MAX_PARALLEL_CHECKS = 10
SSL_EXPIRY_WARNING_DAYS = 14
DEFAULT_PORT_TIMEOUT = 5
STATUS_PAGE_UPTIME_WINDOW_DAYS = 90
STATUS_PAGE_TEMPLATE_FILE = "template.html"
STATUS_PAGE_TEMPLATE_DIR = Path("/Users/davidadams/owlpulse-design/templates")
STATUS_PAGE_DESIGN_DIR = Path("/Users/davidadams/owlpulse-design/status-page")
STATUS_PAGE_RENDERER_DIR = Path("/Users/davidadams/owlpulse-status-renderer")
LOCAL_STATUS_PAGE_TEMPLATE_DIR = Path(__file__).resolve().parent / "owlpulse-design/templates"
LOCAL_STATUS_PAGE_DESIGN_DIR = Path(__file__).resolve().parent / "owlpulse-design/status-page"
LOCAL_STATUS_PAGE_RENDERER_DIR = Path(__file__).resolve().parent / "owlpulse-status-renderer"
DEFAULT_STATUS_PAGE_TEMPLATE_NAME = "default"
STATUS_PAGE_TEMPLATE_NAMES = {"default", "minimal", "cyber", "corporate", "terminal"}

DEFAULT_STATUS_PAGE_COLORS = {
    "accent_color": "#3B82F6",
    "background_color": "#0A0E1A",
    "text_color": "#E2E8F0",
}

STATUS_PAGE_ROOT_DOMAIN = "owlpulse.org"
RESERVED_STATUS_SUBDOMAINS = {
    "www",
    "api",
    "app",
    "status",
    "admin",
    "docs",
    "checkout",
    "compare",
    "health",
}
DEGRADED_UPTIME_THRESHOLD = 99.5

PLAN_ALIASES = {"biz": "business"}
PAID_PLANS = {"pro", "business"}

# Monitor count limits per plan (None = unlimited)
PLAN_MONITOR_LIMITS = {"free": 3, "pro": 50, "business": 500}
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "").strip()
SESSION_SECRET = os.getenv("SESSION_SECRET", "owlpulse-dev-session-secret-change-me")
DASHBOARD_REQUIRE_SAML = os.getenv("DASHBOARD_REQUIRE_SAML", "false").lower() in {"1", "true", "yes", "on"}
DEFAULT_SLA_TARGET = float(os.getenv("DEFAULT_SLA_TARGET", "99.9"))
ROLE_PERMISSIONS = {
    "admin": {"read", "write", "invite", "delete"},
    "member": {"read", "write"},
    "viewer": {"read"},
}
SAML_ACS_PATH = "/auth/saml/acs"
SAML_LOGIN_PATH = "/auth/saml/login"
WORKSPACE_ROOT = Path.home() / ".openclaw" / "workspace"
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

try:
    from business_arena.ledger import (
        DEFAULT_ARENA_DB_PATH,
        ensure_app_telemetry_schema,
        ensure_business_arena_schema,
        fetch_experiment_detail,
        fetch_reward_summary,
        record_app_event,
    )
    from business_arena.owlpulse_adapter import (
        OwlPulseAdapter,
        build_trust_bar_context,
        render_compare_html,
        render_signup_html,
        render_welcome_email_html,
        render_home_html,
    )
    from business_arena.runtime import DEFAULT_OFFER_PROGRAM_PATH, write_runtime_report
    from business_arena.schemas import AttributionContext, ExperimentEvent, OfferProgram

    BUSINESS_ARENA_AVAILABLE = True
    BUSINESS_ARENA_IMPORT_ERROR = ""
except Exception as exc:
    BUSINESS_ARENA_AVAILABLE = False
    BUSINESS_ARENA_IMPORT_ERROR = str(exc)


def canonicalize_plan(plan: str) -> str:
    normalized = (plan or "free").strip().lower()
    return PLAN_ALIASES.get(normalized, normalized)


def check_monitor_count_limit(conn: sqlite3.Connection, team_id: int, plan: str) -> None:
    """Raise HTTPException if team already has max monitors for their plan."""
    limit = PLAN_MONITOR_LIMITS.get(plan, 3)
    if limit is None:
        return  # unlimited
    current = conn.execute(
        "SELECT COUNT(*) FROM monitors WHERE team_id = ?", (team_id,)
    ).fetchone()[0]
    if current >= limit:
        raise HTTPException(
            status_code=402,
            detail={
                "message": f"Monitor limit reached for {plan} plan",
                "code": "MONITOR_LIMIT_REACHED",
                "limit": limit,
                "current": current,
                "upgrade_url": "/checkout/pro",
            },
        )


ATTRIBUTION_FIELDS = (
    "source",
    "channel",
    "campaign",
    "experiment_id",
    "variant_id",
    "cohort",
    "session_id",
)
TRUST_BAR_EVENT_SURFACES = {"landing_page", "signup", "checkout"}


def business_arena_enabled() -> bool:
    return BUSINESS_ARENA_AVAILABLE


def load_business_offer_program() -> dict[str, Any]:
    if not business_arena_enabled():
        return {}
    try:
        return json.loads(Path(DEFAULT_OFFER_PROGRAM_PATH).read_text(encoding="utf-8"))
    except OSError:
        return {}


def enrich_trust_bar_metadata(
    *,
    surface: str,
    metadata: dict[str, Any] | None = None,
    offer_program: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged = dict(metadata or {})
    if surface not in TRUST_BAR_EVENT_SURFACES or not business_arena_enabled():
        return merged
    program = offer_program or load_business_offer_program()
    if not program:
        return merged
    trust_bar = build_trust_bar_context(OfferProgram.from_dict(program))
    merged.setdefault("trust_bar_enabled", bool(trust_bar["trust_bar_enabled"]))
    merged.setdefault("trust_bar_variant", str(trust_bar["trust_bar_variant"]))
    return merged


def build_attribution_context(
    *,
    request: Request | None = None,
    payload: dict[str, Any] | None = None,
    default_source: str = "",
    default_channel: str = "",
    default_campaign: str = "",
) -> AttributionContext:
    values: dict[str, Any] = {}
    if request is not None:
        for field in ATTRIBUTION_FIELDS:
            values[field] = request.query_params.get(field, "")
    if payload:
        for field in ATTRIBUTION_FIELDS:
            if payload.get(field):
                values[field] = payload[field]
    context = AttributionContext.from_mapping(values)
    return context.with_defaults(
        source=default_source,
        channel=default_channel,
        campaign=default_campaign,
    )


def record_business_event(
    conn: sqlite3.Connection,
    *,
    event_name: str,
    attribution: AttributionContext,
    surface: str,
    value: float = 0.0,
    request: Request | None = None,
    path: str = "",
    monitor_id: int | None = None,
    plan: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    if not business_arena_enabled():
        return
    record_app_event(
        conn,
        ExperimentEvent(
            event_name=event_name,
            surface=surface,
            value=value,
            monitor_id=monitor_id,
            plan=plan,
            path=path or (str(request.url.path) if request is not None else ""),
            metadata=metadata or {},
            attribution=attribution,
        ),
    )


def maybe_write_business_report() -> None:
    if not business_arena_enabled():
        return
    try:
        ensure_business_arena_schema(DEFAULT_ARENA_DB_PATH)
        write_runtime_report(app_db_path=DATABASE)
    except Exception as exc:
        print(f"Business arena report update failed: {exc}")


# ============================================================================
# Database Setup
# ============================================================================

@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
    finally:
        conn.close()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str):
    """Add column if missing (simple sqlite migration helper)."""
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {c[1] for c in cols}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    """Initialize the database schema."""
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS monitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                name TEXT NOT NULL,
                plan TEXT NOT NULL DEFAULT 'free',
                api_key TEXT UNIQUE NOT NULL,
                webhook_url TEXT,
                slack_webhook_url TEXT,
                discord_webhook_url TEXT,
                sms_number TEXT,
                email TEXT,
                source TEXT,
                monitor_type TEXT DEFAULT 'http',
                port INTEGER,
                keyword TEXT,
                keyword_should_exist INTEGER DEFAULT 1,
                custom_headers TEXT,
                maintenance_starts_at INTEGER,
                maintenance_ends_at INTEGER,
                dns_hostname TEXT,
                dns_record_type TEXT DEFAULT 'A',
                status TEXT DEFAULT 'unknown',
                last_checked INTEGER,
                created_at INTEGER NOT NULL,
                sla_target REAL DEFAULT 99.9,
                team_id INTEGER,
                total_checks INTEGER DEFAULT 0,
                successful_checks INTEGER DEFAULT 0,
                false_positives_filtered INTEGER DEFAULT 0
            )
        """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                status_code INTEGER,
                response_time REAL,
                success INTEGER NOT NULL,
                FOREIGN KEY (monitor_id) REFERENCES monitors (id) ON DELETE CASCADE
            )
        """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_checks_monitor
            ON checks(monitor_id, timestamp DESC)
        """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                previous_status TEXT NOT NULL,
                current_status TEXT NOT NULL,
                status_code INTEGER,
                response_time REAL,
                false_positive_filtered INTEGER DEFAULT 0,
                FOREIGN KEY (monitor_id) REFERENCES monitors (id) ON DELETE CASCADE
            )
        """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_alert_events_monitor
            ON alert_events(monitor_id, timestamp DESC)
        """
        )


        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS status_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_api_key TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                template_name TEXT NOT NULL DEFAULT 'default',
                theme_color TEXT NOT NULL DEFAULT '#3B82F6',
                logo_url TEXT,
                accent_color TEXT NOT NULL DEFAULT '#3B82F6',
                background_color TEXT NOT NULL DEFAULT '#0A0E1A',
                text_color TEXT NOT NULL DEFAULT '#E2E8F0',
                custom_domain TEXT,
                team_id INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_status_pages_owner
            ON status_pages(owner_api_key, created_at DESC)
        """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS status_page_monitors (
                status_page_id INTEGER NOT NULL,
                monitor_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (status_page_id, monitor_id),
                FOREIGN KEY (status_page_id) REFERENCES status_pages (id) ON DELETE CASCADE,
                FOREIGN KEY (monitor_id) REFERENCES monitors (id) ON DELETE CASCADE
            )
        """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_status_page_monitors_page
            ON status_page_monitors(status_page_id)
        """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_status_page_monitors_monitor
            ON status_page_monitors(monitor_id)
        """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS status_page_subscribers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status_page_id INTEGER NOT NULL,
                email TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE(status_page_id, email),
                FOREIGN KEY (status_page_id) REFERENCES status_pages (id) ON DELETE CASCADE
            )
        """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_status_page_subscribers_page
            ON status_page_subscribers(status_page_id, created_at DESC)
        """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                name TEXT,
                role TEXT NOT NULL DEFAULT 'member',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_login_at INTEGER
            )
        """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_api_key TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS team_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer',
                status TEXT NOT NULL DEFAULT 'invited',
                invite_token TEXT UNIQUE,
                invited_by_api_key TEXT,
                created_at INTEGER NOT NULL,
                accepted_at INTEGER,
                UNIQUE(team_id, user_id),
                FOREIGN KEY (team_id) REFERENCES teams (id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_team_members_team
            ON team_members(team_id, status, role)
        """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saml_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id INTEGER UNIQUE NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                sp_entity_id TEXT NOT NULL,
                sp_acs_url TEXT NOT NULL,
                sp_sls_url TEXT,
                idp_entity_id TEXT NOT NULL,
                idp_sso_url TEXT NOT NULL,
                idp_slo_url TEXT,
                idp_x509_cert TEXT NOT NULL,
                idp_metadata_xml TEXT,
                attribute_email TEXT NOT NULL DEFAULT 'email',
                attribute_name TEXT NOT NULL DEFAULT 'name',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (team_id) REFERENCES teams (id) ON DELETE CASCADE
            )
        """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_saml_config_team
            ON saml_config(team_id, enabled)
        """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sla_windows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                monitor_id INTEGER NOT NULL,
                period_type TEXT NOT NULL,
                period_start INTEGER NOT NULL,
                total_checks INTEGER NOT NULL DEFAULT 0,
                successful_checks INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                UNIQUE(monitor_id, period_type, period_start),
                FOREIGN KEY (monitor_id) REFERENCES monitors (id) ON DELETE CASCADE
            )
        """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sla_windows_lookup
            ON sla_windows(monitor_id, period_type, period_start DESC)
        """
        )

        # Migration-safe adds for older databases
        ensure_column(conn, "monitors", "email", "TEXT")
        ensure_column(conn, "monitors", "source", "TEXT")
        ensure_column(conn, "monitors", "slack_webhook_url", "TEXT")
        ensure_column(conn, "monitors", "discord_webhook_url", "TEXT")
        ensure_column(conn, "monitors", "sms_number", "TEXT")
        ensure_column(conn, "monitors", "sla_target", f"REAL DEFAULT {DEFAULT_SLA_TARGET}")
        ensure_column(conn, "monitors", "team_id", "INTEGER")
        ensure_column(conn, "monitors", "false_positives_filtered", "INTEGER DEFAULT 0")
        ensure_column(conn, "monitors", "channel", "TEXT")
        ensure_column(conn, "monitors", "campaign", "TEXT")
        ensure_column(conn, "monitors", "experiment_id", "TEXT")
        ensure_column(conn, "monitors", "variant_id", "TEXT")
        ensure_column(conn, "monitors", "cohort", "TEXT")
        ensure_column(conn, "monitors", "session_id", "TEXT")

        # Phase 2 parity fields (all available on free tier)
        ensure_column(conn, "monitors", "monitor_type", "TEXT DEFAULT 'http'")
        ensure_column(conn, "monitors", "port", "INTEGER")
        ensure_column(conn, "monitors", "keyword", "TEXT")
        ensure_column(conn, "monitors", "keyword_should_exist", "INTEGER DEFAULT 1")
        ensure_column(conn, "monitors", "custom_headers", "TEXT")
        ensure_column(conn, "monitors", "maintenance_starts_at", "INTEGER")
        ensure_column(conn, "monitors", "maintenance_ends_at", "INTEGER")
        ensure_column(conn, "monitors", "dns_hostname", "TEXT")
        ensure_column(conn, "monitors", "dns_record_type", "TEXT DEFAULT 'A'")
        ensure_column(conn, "status_pages", "logo_url", "TEXT")
        ensure_column(conn, "status_pages", "accent_color", "TEXT DEFAULT '#3B82F6'")
        ensure_column(conn, "status_pages", "background_color", "TEXT DEFAULT '#0A0E1A'")
        ensure_column(conn, "status_pages", "text_color", "TEXT DEFAULT '#E2E8F0'")
        ensure_column(conn, "status_pages", "custom_domain", "TEXT")
        ensure_column(conn, "status_pages", "template_name", f"TEXT DEFAULT '{DEFAULT_STATUS_PAGE_TEMPLATE_NAME}'")
        ensure_column(conn, "status_pages", "team_id", "INTEGER")

        conn.execute(
            """
            UPDATE status_pages
            SET accent_color = COALESCE(accent_color, theme_color, '#3B82F6'),
                background_color = COALESCE(background_color, '#0A0E1A'),
                text_color = COALESCE(text_color, '#E2E8F0'),
                template_name = COALESCE(template_name, 'default')
        """
        )

        conn.execute(
            """
            UPDATE monitors
            SET sla_target = COALESCE(sla_target, ?)
        """,
            (DEFAULT_SLA_TARGET,),
        )

        # Bootstrap team IDs for existing API keys to preserve backward compatibility.
        api_keys = conn.execute("SELECT DISTINCT api_key FROM monitors WHERE api_key IS NOT NULL").fetchall()
        for row in api_keys:
            ensure_team_for_api_key(conn, row["api_key"])

        if business_arena_enabled():
            ensure_app_telemetry_schema(conn)

        conn.commit()


# ============================================================================
# Pydantic Models
# ============================================================================

class MonitorCreate(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)
    name: str = Field(..., min_length=1, max_length=100)
    plan: str = Field(default="free", pattern="^(free|pro|business|biz)$")
    monitor_type: str = Field(default="http", pattern="^(http|ping|port|ssl|keyword|dns)$")
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    keyword: Optional[str] = Field(default=None, max_length=1024)
    keyword_should_exist: bool = True
    custom_headers: Optional[dict[str, str]] = None
    maintenance_starts_at: Optional[int] = None
    maintenance_ends_at: Optional[int] = None
    dns_hostname: Optional[str] = Field(default=None, max_length=255)
    dns_record_type: str = Field(default="A", pattern="^(A|AAAA)$")
    webhook_url: Optional[HttpUrl] = None
    slack_webhook_url: Optional[HttpUrl] = None
    discord_webhook_url: Optional[HttpUrl] = None
    sms_number: Optional[str] = Field(default=None, max_length=32)
    sla_target: float = Field(default=DEFAULT_SLA_TARGET, ge=90.0, le=100.0)


class SignupCreate(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)
    name: str = Field(..., min_length=1, max_length=100)
    email: Optional[str] = Field(default=None, max_length=255)
    plan: str = Field(default="free", pattern="^(free|pro|business|biz)$")
    monitor_type: str = Field(default="http", pattern="^(http|ping|port|ssl|keyword|dns)$")
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    keyword: Optional[str] = Field(default=None, max_length=1024)
    keyword_should_exist: bool = True
    custom_headers: Optional[dict[str, str]] = None
    maintenance_starts_at: Optional[int] = None
    maintenance_ends_at: Optional[int] = None
    dns_hostname: Optional[str] = Field(default=None, max_length=255)
    dns_record_type: str = Field(default="A", pattern="^(A|AAAA)$")
    webhook_url: Optional[HttpUrl] = None
    slack_webhook_url: Optional[HttpUrl] = None
    discord_webhook_url: Optional[HttpUrl] = None
    sms_number: Optional[str] = Field(default=None, max_length=32)
    sla_target: float = Field(default=DEFAULT_SLA_TARGET, ge=90.0, le=100.0)
    source: Optional[str] = Field(default=None, max_length=255)
    channel: Optional[str] = Field(default=None, max_length=255)
    campaign: Optional[str] = Field(default=None, max_length=255)
    experiment_id: Optional[str] = Field(default=None, max_length=255)
    variant_id: Optional[str] = Field(default=None, max_length=255)
    cohort: Optional[str] = Field(default=None, max_length=255)
    session_id: Optional[str] = Field(default=None, max_length=255)
    create_status_page: bool = True
    status_page_title: Optional[str] = Field(default=None, min_length=1, max_length=120)
    status_page_slug: Optional[str] = Field(default=None, pattern="^[a-z0-9-]{3,64}$")
    status_page_description: Optional[str] = Field(default=None, max_length=1000)


class ArenaEventCreate(BaseModel):
    event_name: str = Field(..., min_length=1, max_length=80)
    surface: Optional[str] = Field(default=None, max_length=80)
    source: Optional[str] = Field(default=None, max_length=255)
    channel: Optional[str] = Field(default=None, max_length=255)
    campaign: Optional[str] = Field(default=None, max_length=255)
    experiment_id: Optional[str] = Field(default=None, max_length=255)
    variant_id: Optional[str] = Field(default=None, max_length=255)
    cohort: Optional[str] = Field(default=None, max_length=255)
    session_id: Optional[str] = Field(default=None, max_length=255)
    path: Optional[str] = Field(default=None, max_length=255)
    monitor_id: Optional[int] = None
    plan: Optional[str] = Field(default=None, max_length=32)
    value: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class AlertWorkflowUpdate(BaseModel):
    webhook_url: Optional[HttpUrl] = None
    slack_webhook_url: Optional[HttpUrl] = None
    discord_webhook_url: Optional[HttpUrl] = None
    sms_number: Optional[str] = Field(default=None, max_length=32)


class MonitorResponse(BaseModel):
    id: int
    url: str
    name: str
    plan: str
    monitor_type: str
    port: Optional[int]
    keyword: Optional[str]
    keyword_should_exist: bool
    custom_headers: Optional[dict[str, str]]
    maintenance_starts_at: Optional[int]
    maintenance_ends_at: Optional[int]
    dns_hostname: Optional[str]
    dns_record_type: Optional[str]
    api_key: str
    webhook_url: Optional[str]
    slack_webhook_url: Optional[str]
    discord_webhook_url: Optional[str]
    sms_number: Optional[str]
    sla_target: float
    status: str
    last_checked: Optional[int]
    created_at: int
    uptime_percentage: float
    total_checks: int
    false_positives_filtered: int


class CheckResponse(BaseModel):
    id: int
    monitor_id: int
    timestamp: int
    status_code: Optional[int]
    response_time: Optional[float]
    success: bool


class AlertEventResponse(BaseModel):
    id: int
    monitor_id: int
    timestamp: int
    previous_status: str
    current_status: str
    status_code: Optional[int]
    response_time: Optional[float]
    false_positive_filtered: bool


class HealthScoreResponse(BaseModel):
    monitor_id: int
    monitor_name: str
    current_status: str
    health_score: float
    uptime_percentage: float
    recent_uptime_24h: float
    checks_24h: int
    total_checks: int


class StatusSummaryResponse(BaseModel):
    total_monitors: int
    up_monitors: int
    down_monitors: int
    unknown_monitors: int
    overall_uptime_percentage: float
    checks_last_24h: int
    incidents_last_24h: int


class StatusPageCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    slug: Optional[str] = Field(default=None, pattern="^[a-z0-9-]{3,64}$")
    description: Optional[str] = Field(default=None, max_length=1000)
    template_name: str = Field(default=DEFAULT_STATUS_PAGE_TEMPLATE_NAME, pattern="^(default|minimal|cyber|corporate|terminal)$")
    theme_color: str = Field(default="#3B82F6", pattern="^#[0-9A-Fa-f]{6}$")
    accent_color: Optional[str] = Field(default=None, pattern="^#[0-9A-Fa-f]{6}$")
    background_color: Optional[str] = Field(default=None, pattern="^#[0-9A-Fa-f]{6}$")
    text_color: Optional[str] = Field(default=None, pattern="^#[0-9A-Fa-f]{6}$")
    logo_url: Optional[HttpUrl] = None
    custom_domain: Optional[str] = Field(default=None, max_length=255)
    monitor_ids: list[int] = Field(default_factory=list)


class StatusPageUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=120)
    slug: Optional[str] = Field(default=None, pattern="^[a-z0-9-]{3,64}$")
    description: Optional[str] = Field(default=None, max_length=1000)
    template_name: Optional[str] = Field(default=None, pattern="^(default|minimal|cyber|corporate|terminal)$")
    theme_color: Optional[str] = Field(default=None, pattern="^#[0-9A-Fa-f]{6}$")
    accent_color: Optional[str] = Field(default=None, pattern="^#[0-9A-Fa-f]{6}$")
    background_color: Optional[str] = Field(default=None, pattern="^#[0-9A-Fa-f]{6}$")
    text_color: Optional[str] = Field(default=None, pattern="^#[0-9A-Fa-f]{6}$")
    logo_url: Optional[HttpUrl] = None
    custom_domain: Optional[str] = Field(default=None, max_length=255)
    monitor_ids: Optional[list[int]] = None


class StatusPageResponse(BaseModel):
    id: int
    slug: str
    title: str
    description: Optional[str]
    template_name: str
    theme_color: str
    accent_color: str
    background_color: str
    text_color: str
    logo_url: Optional[str]
    custom_domain: Optional[str]
    monitor_ids: list[int]
    created_at: int
    updated_at: int


class StatusPageMonitorPublic(BaseModel):
    id: int
    name: str
    url: str
    status: str
    component_status: str
    component_status_label: str
    uptime_percentage: float
    checks_90d: int
    last_checked: Optional[int]


class StatusPageIncidentPublic(BaseModel):
    id: int
    monitor_id: int
    monitor_name: str
    timestamp: int
    previous_status: str
    current_status: str
    status_code: Optional[int]
    response_time: Optional[float]


class StatusPagePublicResponse(BaseModel):
    slug: str
    title: str
    description: Optional[str]
    template_name: str
    theme_color: str
    accent_color: str
    background_color: str
    text_color: str
    logo_url: Optional[str]
    overall_status: str
    overall_uptime_percentage: float
    uptime_window_days: int
    monitor_count: int
    operational_monitors: int
    degraded_monitors: int
    up_monitors: int
    down_monitors: int
    unknown_monitors: int
    monitors: list[StatusPageMonitorPublic]
    recent_incidents: list[StatusPageIncidentPublic]
    show_powered_by_badge: bool
    generated_at: int


class StatusPageSubscribeRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)


class StatusPageSubscribeResponse(BaseModel):
    message: str
    email: str
    subscribed: bool


class TeamInviteCreate(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    role: str = Field(default="viewer", pattern="^(admin|member|viewer)$")
    name: Optional[str] = Field(default=None, max_length=120)


class TeamInvitationAccept(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    name: Optional[str] = Field(default=None, max_length=120)


class SAMLConfigUpsert(BaseModel):
    enabled: bool = True
    sp_entity_id: str = Field(..., min_length=1, max_length=255)
    sp_acs_url: HttpUrl
    sp_sls_url: Optional[HttpUrl] = None
    idp_entity_id: str = Field(..., min_length=1, max_length=255)
    idp_sso_url: HttpUrl
    idp_slo_url: Optional[HttpUrl] = None
    idp_x509_cert: str = Field(..., min_length=1)
    idp_metadata_xml: Optional[str] = None
    attribute_email: str = Field(default="email", max_length=120)
    attribute_name: str = Field(default="name", max_length=120)


# ============================================================================
# FastAPI App
# ============================================================================

app = FastAPI(
    title="Uptime Monitor API",
    description="Simple uptime monitoring with webhook alerts",
    version="1.3.0",
)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")

scheduler = AsyncIOScheduler()
check_cycle_lock = asyncio.Lock()


def raise_api_error(
    status_code: int,
    code: str,
    message: str,
    hint: Optional[str] = None,
    details: Optional[Any] = None,
):
    """Raise an HTTPException with a structured, client-friendly error payload."""
    payload: dict[str, Any] = {"code": code, "message": message}
    if hint:
        payload["hint"] = hint
    if details is not None:
        payload["details"] = details
    raise HTTPException(status_code=status_code, detail=payload)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Return consistent JSON errors with machine-readable codes."""
    if isinstance(exc.detail, dict):
        detail = exc.detail
        code = detail.get("code", f"http_{exc.status_code}")
        message = detail.get("message", "Request failed")
        hint = detail.get("hint")
        extra_details = detail.get("details")
        # Preserve all other fields from the detail dict (e.g. limit, current, upgrade_url)
        extra_fields = {k: v for k, v in detail.items()
                        if k not in ("code", "message", "hint", "details")}
    else:
        code = f"http_{exc.status_code}"
        message = str(exc.detail) if exc.detail else "Request failed"
        hint = None
        extra_details = None
        extra_fields = {}

    error_payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "status": exc.status_code,
        "path": str(request.url.path),
    }
    if hint:
        error_payload["hint"] = hint
    if extra_details is not None:
        error_payload["details"] = extra_details
    error_payload.update(extra_fields)

    return JSONResponse(status_code=exc.status_code, content={"error": error_payload})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Expose validation failures in a concise and debuggable format."""
    fields: list[dict[str, str]] = []
    for err in exc.errors():
        loc = err.get("loc", ())
        field_path = ".".join(str(part) for part in loc if part != "body")
        fields.append(
            {
                "field": field_path or "request",
                "message": err.get("msg", "Invalid value"),
                "type": err.get("type", "validation_error"),
            }
        )

    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request validation failed",
                "status": 422,
                "path": str(request.url.path),
                "hint": "Review the fields list and fix the invalid input values.",
                "details": {"fields": fields},
            }
        },
    )


# ============================================================================
# Authentication
# ============================================================================

def verify_api_key(x_api_key: Optional[str] = Header(None)) -> str:
    """Verify API key from header."""
    if not x_api_key:
        raise_api_error(
            status_code=401,
            code="missing_api_key",
            message="Missing X-API-Key header",
            hint="Pass your API key using the X-API-Key header.",
        )

    with get_db() as conn:
        cursor = conn.execute("SELECT id FROM monitors WHERE api_key = ?", (x_api_key,))
        monitor = cursor.fetchone()

        if not monitor:
            raise_api_error(
                status_code=401,
                code="invalid_api_key",
                message="API key is invalid or does not match any monitor",
                hint="Double-check the API key from your OwlPulse signup response.",
            )

        return x_api_key


def validate_monitor_payload(
    monitor_type: str,
    url: str,
    port: Optional[int],
    keyword: Optional[str],
    dns_hostname: Optional[str],
    maintenance_starts_at: Optional[int],
    maintenance_ends_at: Optional[int],
    sms_number: Optional[str],
    sla_target: float,
):
    monitor_type = monitor_type.lower()

    if maintenance_starts_at is not None and maintenance_ends_at is not None:
        if maintenance_starts_at >= maintenance_ends_at:
            raise_api_error(
                status_code=400,
                code="invalid_maintenance_window",
                message="maintenance_starts_at must be before maintenance_ends_at",
                hint="Provide unix timestamps where start < end.",
            )

    if monitor_type in {"http", "keyword", "ssl"}:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise_api_error(
                status_code=400,
                code="invalid_url",
                message="HTTP/keyword/SSL monitors require a valid http(s) URL",
                hint="Example: https://example.com/health",
            )

    if monitor_type == "port" and not port:
        raise_api_error(
            status_code=400,
            code="missing_port",
            message="Port monitors require the port field",
            hint="Set port to a value between 1 and 65535.",
        )

    if monitor_type == "keyword" and not keyword:
        raise_api_error(
            status_code=400,
            code="missing_keyword",
            message="Keyword monitors require keyword",
            hint="Set keyword and optional keyword_should_exist flag.",
        )

    if monitor_type == "dns" and not (dns_hostname or url):
        raise_api_error(
            status_code=400,
            code="missing_dns_hostname",
            message="DNS monitors require dns_hostname or url",
            hint="Provide a hostname such as example.com.",
        )

    if sms_number:
        validate_sms_number(sms_number)

    if sla_target < 90.0 or sla_target > 100.0:
        raise_api_error(
            status_code=400,
            code="invalid_sla_target",
            message="sla_target must be between 90.0 and 100.0",
            hint="Use a realistic SLA target like 99.9.",
        )


# ============================================================================
# Core Monitoring Logic
# ============================================================================

def parse_custom_headers(raw_headers: Optional[str]) -> dict[str, str]:
    if not raw_headers:
        return {}
    try:
        parsed = json.loads(raw_headers)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    except Exception:
        pass
    return {}


def normalize_target_host(monitor: sqlite3.Row) -> str:
    monitor_type = (monitor["monitor_type"] or "http").lower()
    if monitor_type in {"http", "keyword", "ssl"}:
        parsed = urlparse(monitor["url"])
        return parsed.hostname or monitor["url"]
    if monitor_type == "dns":
        return (monitor["dns_hostname"] or monitor["url"]).strip()

    raw = monitor["url"].strip()
    if "://" in raw:
        parsed = urlparse(raw)
        return parsed.hostname or raw
    return raw


def is_in_maintenance_window(monitor: sqlite3.Row, now_ts: int) -> bool:
    start = monitor["maintenance_starts_at"]
    end = monitor["maintenance_ends_at"]
    if start is None or end is None:
        return False
    return start <= now_ts <= end


async def run_http_check(url: str, headers: dict[str, str]) -> tuple[bool, Optional[int], Optional[float], str]:
    start = time.time()
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers=headers or None) as client:
        try:
            response = await client.get(url, follow_redirects=True)
            elapsed = (time.time() - start) * 1000
            success = 200 <= response.status_code < 400
            return success, response.status_code, elapsed, response.text
        except Exception:
            elapsed = (time.time() - start) * 1000
            return False, None, elapsed, ""


async def run_ping_check(host: str) -> tuple[bool, Optional[int], Optional[float], dict[str, Any]]:
    start = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping",
            "-c",
            "1",
            "-W",
            str(REQUEST_TIMEOUT * 1000),
            host,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=REQUEST_TIMEOUT + 2)
        elapsed = (time.time() - start) * 1000
        output = stdout.decode("utf-8", errors="ignore")
        return proc.returncode == 0, proc.returncode, elapsed, {"ping_output": output[-300:]}
    except Exception:
        elapsed = (time.time() - start) * 1000
        return False, None, elapsed, {}


async def run_port_check(host: str, port: int) -> tuple[bool, Optional[int], Optional[float], dict[str, Any]]:
    start = time.time()
    try:
        conn = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(conn, timeout=DEFAULT_PORT_TIMEOUT)
        writer.close()
        await writer.wait_closed()
        elapsed = (time.time() - start) * 1000
        return True, 1, elapsed, {}
    except Exception:
        elapsed = (time.time() - start) * 1000
        return False, None, elapsed, {}


async def run_dns_check(host: str, record_type: str) -> tuple[bool, Optional[int], Optional[float], dict[str, Any]]:
    start = time.time()
    family = socket.AF_UNSPEC
    if record_type == "A":
        family = socket.AF_INET
    elif record_type == "AAAA":
        family = socket.AF_INET6

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, family=family, type=socket.SOCK_STREAM)
        elapsed = (time.time() - start) * 1000
        resolved_ips = sorted({info[4][0] for info in infos if info[4]})
        return len(resolved_ips) > 0, len(resolved_ips), elapsed, {"resolved_ips": resolved_ips[:5]}
    except Exception:
        elapsed = (time.time() - start) * 1000
        return False, None, elapsed, {}


async def run_ssl_check(url: str) -> tuple[bool, Optional[int], Optional[float], dict[str, Any]]:
    start = time.time()
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return False, None, 0.0, {"ssl_error": "missing_hostname"}

    port = parsed.port or 443
    ctx = ssl.create_default_context()
    try:
        def _fetch_expiry_days() -> int:
            with socket.create_connection((host, port), timeout=REQUEST_TIMEOUT) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as tls_sock:
                    cert = tls_sock.getpeercert()
            not_after = cert.get("notAfter")
            if not not_after:
                raise ValueError("ssl_cert_missing_notAfter")
            expires_at = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
            delta = expires_at - datetime.now(timezone.utc)
            return int(delta.total_seconds() // 86400)

        loop = asyncio.get_running_loop()
        days_left = await loop.run_in_executor(None, _fetch_expiry_days)
        elapsed = (time.time() - start) * 1000
        success = days_left >= 0
        return success, days_left, elapsed, {"ssl_days_left": days_left, "expiry_warning_days": SSL_EXPIRY_WARNING_DAYS}
    except Exception as e:
        elapsed = (time.time() - start) * 1000
        return False, None, elapsed, {"ssl_error": str(e)}


async def run_monitor_check(monitor: sqlite3.Row) -> tuple[bool, Optional[int], Optional[float], dict[str, Any]]:
    monitor_type = (monitor["monitor_type"] or "http").lower()
    headers = parse_custom_headers(monitor["custom_headers"])

    if monitor_type == "ping":
        host = normalize_target_host(monitor)
        return await run_ping_check(host)

    if monitor_type == "port":
        host = normalize_target_host(monitor)
        port = monitor["port"] or 80
        return await run_port_check(host, port)

    if monitor_type == "ssl":
        return await run_ssl_check(monitor["url"])

    if monitor_type == "keyword":
        success, status_code, response_time, body = await run_http_check(monitor["url"], headers)
        keyword = monitor["keyword"] or ""
        should_exist = bool(monitor["keyword_should_exist"])
        keyword_found = keyword in body if keyword else True
        content_ok = keyword_found if should_exist else not keyword_found
        final_success = success and content_ok
        details = {
            "keyword": keyword,
            "keyword_found": keyword_found,
            "keyword_should_exist": should_exist,
        }
        return final_success, status_code, response_time, details

    if monitor_type == "dns":
        host = normalize_target_host(monitor)
        return await run_dns_check(host, (monitor["dns_record_type"] or "A").upper())

    # default HTTP monitor
    success, status_code, response_time, _ = await run_http_check(monitor["url"], headers)
    return success, status_code, response_time, {}


async def check_monitor_with_false_positive_filter(
    monitor: sqlite3.Row,
) -> tuple[bool, Optional[int], Optional[float], bool, dict[str, Any]]:
    """Two-step check to reduce false alarms across monitor types."""
    success, status_code, response_time, details = await run_monitor_check(monitor)
    if success:
        return success, status_code, response_time, False, details

    await asyncio.sleep(FALSE_POSITIVE_RETRY_DELAY_SECONDS)
    second_success, second_status, second_time, second_details = await run_monitor_check(monitor)

    if second_success:
        return True, second_status, second_time, True, second_details

    return False, second_status, second_time, False, second_details


async def send_webhook(webhook_url: str, data: dict[str, Any]):
    """Send generic webhook notification."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(webhook_url, json=data)
    except Exception as e:
        print(f"Webhook failed ({webhook_url}): {e}")


async def send_slack_webhook(webhook_url: str, data: dict[str, Any]):
    """Send Slack formatted notification."""
    status = data.get("status", "unknown")
    emoji = "✅" if status == "up" else "🚨"
    text = (
        f"{emoji} *OwlPulse Alert*\n"
        f"*{data.get('name')}* (`{data.get('url')}`) is now *{status.upper()}*\n"
        f"Previous: {data.get('previous_status', 'unknown')}\n"
        f"Status Code: {data.get('status_code')}\n"
        f"Response Time: {round(data.get('response_time_ms') or 0, 2)}ms"
    )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(webhook_url, json={"text": text})
    except Exception as e:
        print(f"Slack webhook failed ({webhook_url}): {e}")


async def send_discord_webhook(webhook_url: str, data: dict[str, Any]):
    """Send Discord formatted notification."""
    status = data.get("status", "unknown")
    emoji = "✅" if status == "up" else "🚨"
    content = (
        f"{emoji} **OwlPulse Alert**\n"
        f"**{data.get('name')}** ({data.get('url')}) is now **{status.upper()}**\n"
        f"Previous: {data.get('previous_status', 'unknown')} | "
        f"Status Code: {data.get('status_code')} | "
        f"Response Time: {round(data.get('response_time_ms') or 0, 2)}ms"
    )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(webhook_url, json={"content": content})
    except Exception as e:
        print(f"Discord webhook failed ({webhook_url}): {e}")


async def send_twilio_sms(sms_number: str, data: dict[str, Any]):
    """Send SMS via Twilio REST API when configured."""
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER):
        print("Twilio SMS skipped: missing TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/TWILIO_FROM_NUMBER")
        return

    status = data.get("status", "unknown").upper()
    previous = str(data.get("previous_status", "unknown")).upper()
    text = (
        f"OwlPulse Alert: {data.get('name')} is {status}. "
        f"Prev: {previous}. "
        f"Code: {data.get('status_code')}."
    )
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                url,
                data={
                    "To": sms_number,
                    "From": TWILIO_FROM_NUMBER,
                    "Body": text[:1590],
                },
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            )
    except Exception as e:
        print(f"Twilio SMS failed ({sms_number}): {e}")


async def send_status_alerts(monitor: sqlite3.Row, payload: dict[str, Any]):
    """Fan out status-change alerts to all configured workflows."""
    tasks = []

    if monitor["webhook_url"]:
        tasks.append(send_webhook(monitor["webhook_url"], payload))

    if monitor["slack_webhook_url"]:
        tasks.append(send_slack_webhook(monitor["slack_webhook_url"], payload))

    if monitor["discord_webhook_url"]:
        tasks.append(send_discord_webhook(monitor["discord_webhook_url"], payload))

    if "sms_number" in monitor.keys() and monitor["sms_number"]:
        tasks.append(send_twilio_sms(monitor["sms_number"], payload))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def build_monitor_response(m: sqlite3.Row) -> MonitorResponse:
    """Convert DB row to API response model."""
    uptime = 0.0
    if m["total_checks"] > 0:
        uptime = (m["successful_checks"] / m["total_checks"]) * 100

    return MonitorResponse(
        id=m["id"],
        url=m["url"],
        name=m["name"],
        plan=m["plan"],
        monitor_type=m["monitor_type"] if "monitor_type" in m.keys() else "http",
        port=m["port"] if "port" in m.keys() else None,
        keyword=m["keyword"] if "keyword" in m.keys() else None,
        keyword_should_exist=bool(m["keyword_should_exist"]) if "keyword_should_exist" in m.keys() else True,
        custom_headers=parse_custom_headers(m["custom_headers"] if "custom_headers" in m.keys() else None),
        maintenance_starts_at=m["maintenance_starts_at"] if "maintenance_starts_at" in m.keys() else None,
        maintenance_ends_at=m["maintenance_ends_at"] if "maintenance_ends_at" in m.keys() else None,
        dns_hostname=m["dns_hostname"] if "dns_hostname" in m.keys() else None,
        dns_record_type=m["dns_record_type"] if "dns_record_type" in m.keys() else None,
        api_key=m["api_key"],
        webhook_url=m["webhook_url"],
        slack_webhook_url=m["slack_webhook_url"],
        discord_webhook_url=m["discord_webhook_url"],
        sms_number=m["sms_number"] if "sms_number" in m.keys() else None,
        sla_target=float(m["sla_target"] if "sla_target" in m.keys() and m["sla_target"] is not None else DEFAULT_SLA_TARGET),
        status=m["status"],
        last_checked=m["last_checked"],
        created_at=m["created_at"],
        uptime_percentage=round(uptime, 2),
        total_checks=m["total_checks"],
        false_positives_filtered=m["false_positives_filtered"] or 0,
    )


def slugify_status_page_title(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug[:64] or "status"


HEX_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$"
)
SMS_E164_PATTERN = re.compile(r"^\+[1-9][0-9]{7,14}$")

STATUS_PAGE_TEMPLATE_DEFAULT = """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <meta name=\"color-scheme\" content=\"light dark\" />
  <title>{{PAGE_TITLE}}</title>
  <style>
    :root {
      --accent: {{ACCENT_COLOR}};
      --bg: {{BACKGROUND_COLOR}};
      --text: {{TEXT_COLOR}};
      --text-muted: color-mix(in srgb, var(--text) 62%, #8b95aa);
      --panel: color-mix(in srgb, var(--bg) 78%, #111827);
      --panel-strong: color-mix(in srgb, var(--bg) 60%, #020617);
      --border: color-mix(in srgb, var(--text) 14%, transparent);
      --up: #22c55e;
      --down: #ef4444;
      --degraded: #f59e0b;
      --unknown: #94a3b8;
      --radius: 20px;
      --shadow: 0 16px 40px rgba(2, 6, 23, 0.35);
    }

    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; }
    body {
      min-height: 100vh;
      font-family: \"Avenir Next\", \"Manrope\", \"Segoe UI\", sans-serif;
      color: var(--text);
      background:
        radial-gradient(1200px 600px at -10% -10%, color-mix(in srgb, var(--accent) 24%, transparent), transparent 70%),
        radial-gradient(900px 520px at 110% 0%, color-mix(in srgb, #22d3ee 16%, transparent), transparent 65%),
        linear-gradient(180deg, color-mix(in srgb, var(--bg) 72%, #020617), var(--bg));
      line-height: 1.5;
      padding: 32px 16px 48px;
    }

    .status-shell {
      max-width: 1040px;
      margin: 0 auto;
      display: grid;
      gap: 20px;
    }

    .hero {
      border: 1px solid var(--border);
      border-radius: calc(var(--radius) + 6px);
      padding: clamp(20px, 4vw, 36px);
      background:
        linear-gradient(140deg, color-mix(in srgb, var(--accent) 16%, var(--panel-strong)), var(--panel));
      box-shadow: var(--shadow);
      position: relative;
      overflow: hidden;
    }

    .hero::after {
      content: \"\";
      position: absolute;
      width: 240px;
      aspect-ratio: 1;
      border-radius: 999px;
      right: -100px;
      top: -80px;
      background: color-mix(in srgb, var(--accent) 30%, transparent);
      filter: blur(10px);
      pointer-events: none;
    }

    .hero-head {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
      flex-wrap: wrap;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 0;
    }

    .brand img {
      width: 42px;
      height: 42px;
      border-radius: 10px;
      object-fit: cover;
      border: 1px solid var(--border);
      background: rgba(255, 255, 255, 0.08);
    }

    .brand-fallback {
      width: 42px;
      height: 42px;
      border-radius: 10px;
      background: color-mix(in srgb, var(--accent) 24%, #0f172a);
      border: 1px solid var(--border);
      display: grid;
      place-items: center;
      font-weight: 700;
      letter-spacing: 0.04em;
      font-size: 0.85rem;
    }

    .title {
      margin: 0;
      font-family: \"Iowan Old Style\", \"Palatino\", \"Times New Roman\", serif;
      font-size: clamp(1.6rem, 2.6vw, 2.3rem);
      line-height: 1.15;
      overflow-wrap: anywhere;
    }

    .description {
      margin: 8px 0 0;
      color: var(--text-muted);
      max-width: 70ch;
    }

    .status-pill {
      align-self: center;
      display: inline-flex;
      align-items: center;
      gap: 10px;
      border-radius: 999px;
      padding: 10px 16px;
      font-weight: 600;
      letter-spacing: 0.015em;
      border: 1px solid var(--border);
      background: color-mix(in srgb, var(--panel) 65%, #020617);
      white-space: nowrap;
    }

    .status-pill .dot {
      width: 11px;
      height: 11px;
      border-radius: 50%;
      box-shadow: 0 0 0 4px rgba(255, 255, 255, 0.05);
    }

    .status-pill.operational .dot { background: var(--up); }
    .status-pill.degraded .dot { background: var(--degraded); }
    .status-pill.down .dot { background: var(--down); }
    .status-pill.unknown .dot { background: var(--unknown); }

    .hero-metrics {
      margin-top: 24px;
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    }

    .metric {
      padding: 14px 14px 12px;
      border: 1px solid var(--border);
      border-radius: 14px;
      background: color-mix(in srgb, var(--panel) 82%, #020617);
    }

    .metric-label {
      display: block;
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--text-muted);
    }

    .metric-value {
      display: block;
      margin-top: 8px;
      font-size: 1.2rem;
      font-weight: 700;
      letter-spacing: 0.01em;
    }

    .panel {
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: color-mix(in srgb, var(--panel) 90%, #020617);
      box-shadow: var(--shadow);
      overflow: hidden;
    }

    .panel h2 {
      margin: 0;
      padding: 18px 20px;
      font-size: 1rem;
      font-weight: 700;
      letter-spacing: 0.03em;
      text-transform: uppercase;
      color: var(--text-muted);
      border-bottom: 1px solid var(--border);
    }

    .monitor-list { list-style: none; margin: 0; padding: 4px 0; }

    .monitor-row {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px 16px;
      align-items: center;
      padding: 14px 20px;
      border-top: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
    }

    .monitor-row:first-child { border-top: 0; }

    .monitor-main {
      min-width: 0;
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .monitor-name {
      font-weight: 600;
      overflow-wrap: anywhere;
    }

    .monitor-url {
      margin-top: 2px;
      color: var(--text-muted);
      font-size: 0.86rem;
      overflow-wrap: anywhere;
    }

    .monitor-url a {
      color: inherit;
      text-decoration: none;
      border-bottom: 1px solid transparent;
    }

    .monitor-url a:hover { border-color: color-mix(in srgb, var(--text-muted) 75%, transparent); }

    .status-dot {
      width: 11px;
      height: 11px;
      border-radius: 50%;
      flex: 0 0 11px;
    }
    .status-dot.up { background: var(--up); }
    .status-dot.down { background: var(--down); }
    .status-dot.unknown { background: var(--unknown); }

    .monitor-meta {
      text-align: right;
      color: var(--text-muted);
      font-size: 0.85rem;
      white-space: nowrap;
    }

    .timeline { list-style: none; margin: 0; padding: 0 20px 16px; }

    .incident {
      margin-top: 14px;
      padding-left: 16px;
      border-left: 2px solid color-mix(in srgb, var(--accent) 40%, var(--border));
    }

    .incident-head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      font-weight: 600;
    }

    .incident-time {
      color: var(--text-muted);
      font-size: 0.86rem;
    }

    .incident-status {
      border-radius: 999px;
      border: 1px solid var(--border);
      padding: 2px 8px;
      font-size: 0.75rem;
      letter-spacing: 0.02em;
      text-transform: uppercase;
    }
    .incident-status.down { color: #fecaca; background: rgba(127, 29, 29, 0.3); }
    .incident-status.up { color: #bbf7d0; background: rgba(20, 83, 45, 0.3); }
    .incident-status.unknown { color: #cbd5e1; background: rgba(30, 41, 59, 0.4); }

    .empty-state {
      padding: 18px 20px 24px;
      color: var(--text-muted);
    }

    .subscribe {
      padding: 18px 20px 22px;
      display: grid;
      gap: 12px;
    }

    .subscribe-copy {
      margin: 0;
      color: var(--text-muted);
      font-size: 0.95rem;
    }

    .subscribe-form {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      align-items: stretch;
    }

    .subscribe-form input[type=\"email\"] {
      min-height: 44px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: color-mix(in srgb, var(--panel) 75%, #020617);
      color: var(--text);
      padding: 0 12px;
      font-size: 0.95rem;
    }

    .subscribe-form button {
      min-height: 44px;
      border: 0;
      border-radius: 12px;
      padding: 0 16px;
      background: linear-gradient(135deg, var(--accent), color-mix(in srgb, var(--accent) 70%, #111827));
      color: #f8fafc;
      font-weight: 700;
      letter-spacing: 0.02em;
      cursor: pointer;
    }

    .subscribe-feedback {
      margin: 0;
      min-height: 1.2em;
      font-size: 0.9rem;
      color: var(--text-muted);
    }

    .footer-note {
      color: var(--text-muted);
      font-size: 0.83rem;
      text-align: right;
      margin-top: -2px;
    }

    @media (max-width: 740px) {
      body { padding: 20px 12px 28px; }
      .status-shell { gap: 14px; }
      .hero { border-radius: 18px; padding: 18px; }
      .panel { border-radius: 16px; }
      .monitor-row { grid-template-columns: 1fr; }
      .monitor-meta { text-align: left; }
      .subscribe-form { grid-template-columns: 1fr; }
      .footer-note { text-align: left; }
    }
  </style>
</head>
<body>
  <main class=\"status-shell\" aria-live=\"polite\">
    <section class=\"hero\">
      <div class=\"hero-head\">
        <div>
          <div class=\"brand\">
            {{LOGO_HTML}}
            <div>
              <h1 class=\"title\">{{TITLE}}</h1>
              <p class=\"description\">{{DESCRIPTION}}</p>
            </div>
          </div>
        </div>
        <div class=\"status-pill {{OVERALL_STATUS_CLASS}}\">
          <span class=\"dot\"></span>
          <span>{{OVERALL_STATUS_LABEL}}</span>
        </div>
      </div>

      <div class=\"hero-metrics\">
        <div class=\"metric\">
          <span class=\"metric-label\">Uptime (90 days)</span>
          <span class=\"metric-value\">{{UPTIME_90D}}%</span>
        </div>
        <div class=\"metric\">
          <span class=\"metric-label\">Monitors</span>
          <span class=\"metric-value\">{{MONITOR_COUNT}}</span>
        </div>
        <div class=\"metric\">
          <span class=\"metric-label\">Operational</span>
          <span class=\"metric-value\">{{UP_MONITORS}}</span>
        </div>
        <div class=\"metric\">
          <span class=\"metric-label\">Issues</span>
          <span class=\"metric-value\">{{DOWN_MONITORS}}</span>
        </div>
      </div>
    </section>

    <section class=\"panel\">
      <h2>Monitor Status</h2>
      <ul class=\"monitor-list\">
        {{MONITOR_ROWS}}
      </ul>
    </section>

    <section class=\"panel\">
      <h2>Recent Incident Timeline</h2>
      <ul class=\"timeline\">
        {{INCIDENT_ROWS}}
      </ul>
    </section>

    <section class=\"panel\">
      <h2>Subscribe For Updates</h2>
      <div class=\"subscribe\">
        <p class=\"subscribe-copy\">Get outage and recovery alerts for this status page.</p>
        <form id=\"status-subscribe-form\" class=\"subscribe-form\" method=\"post\" action=\"{{SUBSCRIBE_ENDPOINT}}\">
          <input type=\"email\" name=\"email\" placeholder=\"you@company.com\" required maxlength=\"254\" />
          <button type=\"submit\">Notify Me</button>
        </form>
        <p id=\"status-subscribe-feedback\" class=\"subscribe-feedback\" aria-live=\"polite\"></p>
      </div>
    </section>

    <div class=\"footer-note\">Last generated: {{GENERATED_AT}}</div>
  </main>

  <script>
    (function () {
      const form = document.getElementById(\"status-subscribe-form\");
      const feedback = document.getElementById(\"status-subscribe-feedback\");
      if (!form || !feedback || !window.fetch) return;

      form.addEventListener(\"submit\", async (event) => {
        event.preventDefault();
        const formData = new FormData(form);
        const email = String(formData.get(\"email\") || \"\").trim();
        if (!email) {
          feedback.textContent = \"Email is required.\";
          return;
        }

        feedback.textContent = \"Saving subscription...\";
        try {
          const response = await fetch(form.action, {
            method: \"POST\",
            headers: { \"Content-Type\": \"application/json\" },
            body: JSON.stringify({ email }),
          });

          const payload = await response.json().catch(() => ({}));
          if (!response.ok) {
            const message = payload?.error?.message || payload?.detail?.message || \"Unable to subscribe right now.\";
            feedback.textContent = message;
            return;
          }
          feedback.textContent = payload.message || \"Subscribed. You will receive updates for incidents.\";
          form.reset();
        } catch (error) {
          feedback.textContent = \"Network error while subscribing. Please retry.\";
        }
      });
    })();
  </script>
</body>
</html>
"""


def sanitize_hex_color(value: Optional[str], fallback: str) -> str:
    if value and HEX_COLOR_PATTERN.match(value):
        return value.upper()
    return fallback.upper()


def sanitize_logo_url(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def normalize_status_page_theme(page: sqlite3.Row) -> dict[str, Optional[str]]:
    theme_color = sanitize_hex_color(
        page["theme_color"] if "theme_color" in page.keys() else None,
        DEFAULT_STATUS_PAGE_COLORS["accent_color"],
    )
    accent_color = sanitize_hex_color(
        page["accent_color"] if "accent_color" in page.keys() and page["accent_color"] else theme_color,
        theme_color,
    )
    background_color = sanitize_hex_color(
        page["background_color"] if "background_color" in page.keys() else None,
        DEFAULT_STATUS_PAGE_COLORS["background_color"],
    )
    text_color = sanitize_hex_color(
        page["text_color"] if "text_color" in page.keys() else None,
        DEFAULT_STATUS_PAGE_COLORS["text_color"],
    )

    logo_url = sanitize_logo_url(page["logo_url"] if "logo_url" in page.keys() else None)
    custom_domain = page["custom_domain"] if "custom_domain" in page.keys() else None

    return {
        "theme_color": theme_color,
        "accent_color": accent_color,
        "background_color": background_color,
        "text_color": text_color,
        "logo_url": logo_url,
        "custom_domain": custom_domain,
    }


def format_unix_timestamp(ts: Optional[int]) -> str:
    if not ts:
        return "n/a"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def validate_subscriber_email(email: str) -> str:
    normalized = email.strip().lower()
    if len(normalized) > 254 or not EMAIL_PATTERN.match(normalized):
        raise_api_error(
            status_code=400,
            code="invalid_email",
            message="Please provide a valid email address",
            hint="Use a standard address format like ops@example.com.",
        )
    return normalized


def validate_sms_number(number: str) -> str:
    normalized = number.strip()
    if not SMS_E164_PATTERN.match(normalized):
        raise_api_error(
            status_code=400,
            code="invalid_sms_number",
            message="sms_number must be in E.164 format",
            hint="Use a number like +14155552671.",
        )
    return normalized


def sanitize_template_name(value: Optional[str]) -> str:
    candidate = (value or DEFAULT_STATUS_PAGE_TEMPLATE_NAME).strip().lower()
    if candidate not in STATUS_PAGE_TEMPLATE_NAMES:
        return DEFAULT_STATUS_PAGE_TEMPLATE_NAME
    return candidate


def start_of_month_utc(ts: int) -> int:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    start = datetime(dt.year, dt.month, 1, tzinfo=timezone.utc)
    return int(start.timestamp())


def start_of_quarter_utc(ts: int) -> int:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    quarter_start_month = ((dt.month - 1) // 3) * 3 + 1
    start = datetime(dt.year, quarter_start_month, 1, tzinfo=timezone.utc)
    return int(start.timestamp())


def end_of_month_utc(ts: int) -> int:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    year = dt.year + 1 if dt.month == 12 else dt.year
    month = 1 if dt.month == 12 else dt.month + 1
    return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())


def end_of_quarter_utc(ts: int) -> int:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    quarter_start_month = ((dt.month - 1) // 3) * 3 + 1
    if quarter_start_month == 10:
        return int(datetime(dt.year + 1, 1, 1, tzinfo=timezone.utc).timestamp())
    return int(datetime(dt.year, quarter_start_month + 3, 1, tzinfo=timezone.utc).timestamp())


def upsert_sla_windows(conn: sqlite3.Connection, monitor_id: int, timestamp: int, success: bool):
    for period_type, period_start in (
        ("month", start_of_month_utc(timestamp)),
        ("quarter", start_of_quarter_utc(timestamp)),
    ):
        conn.execute(
            """
            INSERT INTO sla_windows (monitor_id, period_type, period_start, total_checks, successful_checks, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(monitor_id, period_type, period_start) DO UPDATE SET
                total_checks = total_checks + 1,
                successful_checks = successful_checks + excluded.successful_checks,
                updated_at = excluded.updated_at
        """,
            (monitor_id, period_type, period_start, 1 if success else 0, timestamp),
        )


def ensure_team_for_api_key(conn: sqlite3.Connection, api_key: str) -> int:
    row = conn.execute("SELECT id FROM teams WHERE owner_api_key = ?", (api_key,)).fetchone()
    if row:
        return row["id"]

    now = int(time.time())
    seed = conn.execute("SELECT name FROM monitors WHERE api_key = ? LIMIT 1", (api_key,)).fetchone()
    team_name = f"{seed['name']} Team" if seed and seed["name"] else f"Team {api_key[:8]}"
    cursor = conn.execute(
        "INSERT INTO teams (owner_api_key, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (api_key, team_name[:120], now, now),
    )
    team_id = cursor.lastrowid

    conn.execute("UPDATE monitors SET team_id = ? WHERE api_key = ? AND (team_id IS NULL OR team_id = 0)", (team_id, api_key))
    conn.execute(
        "UPDATE status_pages SET team_id = ? WHERE owner_api_key = ? AND (team_id IS NULL OR team_id = 0)",
        (team_id, api_key),
    )
    return team_id


def upsert_user(conn: sqlite3.Connection, email: str, name: Optional[str] = None, default_role: str = "member") -> sqlite3.Row:
    now = int(time.time())
    normalized_email = validate_subscriber_email(email)
    existing = conn.execute("SELECT * FROM users WHERE email = ?", (normalized_email,)).fetchone()
    if existing:
        if name and name.strip() and (not existing["name"] or existing["name"] != name.strip()):
            conn.execute(
                "UPDATE users SET name = ?, updated_at = ? WHERE id = ?",
                (name.strip(), now, existing["id"]),
            )
            updated = conn.execute("SELECT * FROM users WHERE id = ?", (existing["id"],)).fetchone()
            return updated
        return existing

    cursor = conn.execute(
        "INSERT INTO users (email, name, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (normalized_email, name.strip() if name else None, default_role, now, now),
    )
    return conn.execute("SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)).fetchone()


def get_active_team_role(conn: sqlite3.Connection, team_id: int, user_email: str) -> Optional[str]:
    normalized_email = validate_subscriber_email(user_email)
    row = conn.execute(
        """
        SELECT tm.role
        FROM team_members tm
        JOIN users u ON u.id = tm.user_id
        WHERE tm.team_id = ?
          AND tm.status = 'active'
          AND u.email = ?
        LIMIT 1
    """,
        (team_id, normalized_email),
    ).fetchone()
    return row["role"] if row else None


def require_team_permission(
    conn: sqlite3.Connection,
    api_key: str,
    user_email: Optional[str],
    team_id: Optional[int],
    action: str,
) -> tuple[int, str]:
    owner_team_id = ensure_team_for_api_key(conn, api_key)
    effective_team_id = team_id or owner_team_id

    if not user_email:
        if effective_team_id != owner_team_id:
            raise_api_error(
                status_code=403,
                code="forbidden_team",
                message="API key cannot access another team",
            )
        role = "admin"
    else:
        role = get_active_team_role(conn, effective_team_id, user_email)
        if not role:
            raise_api_error(
                status_code=403,
                code="forbidden_membership",
                message="User is not an active member of this team",
                hint="Accept the team invitation first or use an active member account.",
            )

    if action not in ROLE_PERMISSIONS.get(role, set()):
        raise_api_error(
            status_code=403,
            code="forbidden_role",
            message=f"Role '{role}' cannot perform '{action}' operations",
        )

    return effective_team_id, role


def get_accessible_team_ids(conn: sqlite3.Connection, api_key: str, user_email: Optional[str]) -> list[int]:
    owner_team_id = ensure_team_for_api_key(conn, api_key)
    if not user_email:
        return [owner_team_id]

    normalized_email = validate_subscriber_email(user_email)
    rows = conn.execute(
        """
        SELECT DISTINCT tm.team_id
        FROM team_members tm
        JOIN users u ON u.id = tm.user_id
        WHERE tm.status = 'active'
          AND u.email = ?
    """,
        (normalized_email,),
    ).fetchall()
    team_ids = sorted({row["team_id"] for row in rows} | {owner_team_id})
    return team_ids


def build_saml_settings(config_row: sqlite3.Row) -> dict[str, Any]:
    return {
        "strict": False,
        "debug": False,
        "sp": {
            "entityId": config_row["sp_entity_id"],
            "assertionConsumerService": {
                "url": config_row["sp_acs_url"],
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "singleLogoutService": {
                "url": config_row["sp_sls_url"] or config_row["sp_acs_url"],
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "x509cert": "",
            "privateKey": "",
        },
        "idp": {
            "entityId": config_row["idp_entity_id"],
            "singleSignOnService": {
                "url": config_row["idp_sso_url"],
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "singleLogoutService": {
                "url": config_row["idp_slo_url"] or config_row["idp_sso_url"],
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "x509cert": config_row["idp_x509_cert"],
        },
        "security": {
            "nameIdEncrypted": False,
            "authnRequestsSigned": False,
            "logoutRequestSigned": False,
            "logoutResponseSigned": False,
            "signMetadata": False,
            "wantMessagesSigned": False,
            "wantAssertionsSigned": False,
            "wantAssertionsEncrypted": False,
            "wantNameId": True,
            "requestedAuthnContext": False,
            "wantXMLValidation": False,
        },
    }


def build_saml_req_dict(request: Request, post_data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.hostname or ""
    forwarded_proto = request.headers.get("x-forwarded-proto")
    scheme = forwarded_proto or request.url.scheme
    port = request.url.port or (443 if scheme == "https" else 80)
    return {
        "https": "on" if scheme == "https" else "off",
        "http_host": host,
        "server_port": str(port),
        "script_name": request.url.path,
        "get_data": dict(request.query_params),
        "post_data": post_data or {},
    }


def normalize_request_host(raw_host: Optional[str]) -> str:
    if not raw_host:
        return ""
    host = raw_host.split(",")[0].strip().lower()
    if ":" in host:
        host = host.split(":", 1)[0]
    return host


def extract_status_page_slug_from_request(request: Request) -> Optional[str]:
    raw_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    host = normalize_request_host(raw_host)
    if not host or not host.endswith(f".{STATUS_PAGE_ROOT_DOMAIN}"):
        return None

    maybe_slug = host[: -(len(STATUS_PAGE_ROOT_DOMAIN) + 1)].strip(".")
    if "." in maybe_slug or maybe_slug in RESERVED_STATUS_SUBDOMAINS:
        return None
    if not re.match(r"^[a-z0-9-]{3,64}$", maybe_slug):
        return None
    return maybe_slug


def classify_component_status(raw_monitor_status: str, uptime_percentage: float, total_checks: int) -> tuple[str, str]:
    normalized = (raw_monitor_status or "unknown").lower()
    if normalized == "down":
        return "down", "Down"

    if normalized == "up":
        if total_checks == 0:
            return "degraded", "Degraded"
        if uptime_percentage < DEGRADED_UPTIME_THRESHOLD:
            return "degraded", "Degraded"
        return "operational", "Operational"

    return "degraded", "Degraded"


def account_is_paid(conn: sqlite3.Connection, owner_api_key: str) -> bool:
    paid_plan_placeholders = ",".join("?" for _ in PAID_PLANS)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt
        FROM monitors
        WHERE api_key = ?
          AND plan IN ({paid_plan_placeholders})
    """,
        (owner_api_key, *sorted(PAID_PLANS)),
    ).fetchone()
    return bool(row and row["cnt"] > 0)


def build_status_page_public_urls(slug: str) -> dict[str, str]:
    slug = slug.strip().lower()
    return {
        "path_url": f"/status/{slug}",
        "canonical_url": f"https://{STATUS_PAGE_ROOT_DOMAIN}/status/{slug}",
        "subdomain_url": f"https://{slug}.{STATUS_PAGE_ROOT_DOMAIN}",
    }


def load_status_page_template(template_name: Optional[str] = None) -> str:
    name = sanitize_template_name(template_name)
    template_paths = [
        STATUS_PAGE_TEMPLATE_DIR / f"{name}.html",
        LOCAL_STATUS_PAGE_TEMPLATE_DIR / f"{name}.html",
    ]
    if name == DEFAULT_STATUS_PAGE_TEMPLATE_NAME:
        template_paths.extend(
            [
                STATUS_PAGE_DESIGN_DIR / STATUS_PAGE_TEMPLATE_FILE,
                LOCAL_STATUS_PAGE_DESIGN_DIR / STATUS_PAGE_TEMPLATE_FILE,
            ]
        )

    for template_path in template_paths:
        try:
            if template_path.exists():
                content = template_path.read_text(encoding="utf-8").strip()
                if content:
                    return content
        except OSError:
            continue
    return STATUS_PAGE_TEMPLATE_DEFAULT


def render_status_template(template: str, context: dict[str, str]) -> str:
    rendered = template
    for key, value in context.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return rendered


def generate_unique_status_page_slug(
    conn: sqlite3.Connection,
    title: str,
    requested_slug: Optional[str] = None,
    exclude_page_id: Optional[int] = None,
) -> str:
    base = requested_slug or slugify_status_page_title(title)
    slug = base
    suffix = 2

    while True:
        if exclude_page_id is None:
            row = conn.execute("SELECT id FROM status_pages WHERE slug = ?", (slug,)).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM status_pages WHERE slug = ? AND id != ?",
                (slug, exclude_page_id),
            ).fetchone()
        if not row:
            return slug
        slug = f"{base[:58]}-{suffix}"
        suffix += 1


def get_status_page_monitor_ids(conn: sqlite3.Connection, page_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT monitor_id FROM status_page_monitors WHERE status_page_id = ? ORDER BY monitor_id",
        (page_id,),
    ).fetchall()
    return [r["monitor_id"] for r in rows]


def assert_monitors_owned_by_api_key(
    conn: sqlite3.Connection,
    monitor_ids: list[int],
    api_key: str,
    team_id: Optional[int] = None,
):
    if not monitor_ids:
        return

    unique_ids = sorted(set(monitor_ids))
    placeholders = ",".join("?" for _ in unique_ids)
    if team_id is None:
        rows = conn.execute(
            f"SELECT id FROM monitors WHERE api_key = ? AND id IN ({placeholders})",
            [api_key, *unique_ids],
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT id
            FROM monitors
            WHERE id IN ({placeholders})
              AND (api_key = ? OR team_id = ?)
        """,
            [*unique_ids, api_key, team_id],
        ).fetchall()
    found_ids = {row["id"] for row in rows}
    missing_ids = [mid for mid in unique_ids if mid not in found_ids]
    if missing_ids:
        raise_api_error(
            status_code=400,
            code="invalid_monitor_ids",
            message="One or more monitor IDs do not belong to this API key",
            hint="Only include monitor IDs owned by the authenticated API key.",
            details={"monitor_ids": missing_ids},
        )


def build_status_page_response(conn: sqlite3.Connection, page: sqlite3.Row) -> StatusPageResponse:
    monitor_ids = get_status_page_monitor_ids(conn, page["id"])
    theme = normalize_status_page_theme(page)
    return StatusPageResponse(
        id=page["id"],
        slug=page["slug"],
        title=page["title"],
        description=page["description"],
        template_name=sanitize_template_name(page["template_name"] if "template_name" in page.keys() else None),
        theme_color=theme["theme_color"] or DEFAULT_STATUS_PAGE_COLORS["accent_color"],
        accent_color=theme["accent_color"] or DEFAULT_STATUS_PAGE_COLORS["accent_color"],
        background_color=theme["background_color"] or DEFAULT_STATUS_PAGE_COLORS["background_color"],
        text_color=theme["text_color"] or DEFAULT_STATUS_PAGE_COLORS["text_color"],
        logo_url=theme["logo_url"],
        custom_domain=theme["custom_domain"],
        monitor_ids=monitor_ids,
        created_at=page["created_at"],
        updated_at=page["updated_at"],
    )


def build_status_page_public_response(conn: sqlite3.Connection, page: sqlite3.Row) -> StatusPagePublicResponse:
    since_90d = int(time.time()) - (STATUS_PAGE_UPTIME_WINDOW_DAYS * 86400)
    monitor_rows = conn.execute(
        """
        SELECT
            m.id,
            m.name,
            m.url,
            m.status,
            m.last_checked,
            COALESCE(SUM(CASE WHEN c.success = 1 THEN 1 ELSE 0 END), 0) AS successful_checks_90d,
            COUNT(c.id) AS total_checks_90d
        FROM status_page_monitors spm
        JOIN monitors m ON m.id = spm.monitor_id
        LEFT JOIN checks c
            ON c.monitor_id = m.id
           AND c.timestamp >= ?
        WHERE spm.status_page_id = ?
        GROUP BY m.id, m.name, m.url, m.status, m.last_checked
        ORDER BY m.name ASC
    """,
        (since_90d, page["id"]),
    ).fetchall()

    monitors: list[StatusPageMonitorPublic] = []
    operational_monitors = 0
    degraded_monitors = 0
    down_monitors = 0
    up_monitors = 0
    unknown_monitors = 0
    successful_checks_total = 0
    total_checks_total = 0

    for row in monitor_rows:
        raw_status = (row["status"] or "unknown").lower()
        total_checks = row["total_checks_90d"] or 0
        successful_checks = row["successful_checks_90d"] or 0
        uptime = (successful_checks / total_checks * 100) if total_checks else 0.0
        component_status, component_status_label = classify_component_status(raw_status, uptime, total_checks)
        monitors.append(
            StatusPageMonitorPublic(
                id=row["id"],
                name=row["name"],
                url=row["url"],
                status=raw_status,
                component_status=component_status,
                component_status_label=component_status_label,
                uptime_percentage=round(uptime, 2),
                checks_90d=total_checks,
                last_checked=row["last_checked"],
            )
        )

        if component_status == "operational":
            operational_monitors += 1
        elif component_status == "down":
            down_monitors += 1
        else:
            degraded_monitors += 1

        if raw_status == "up":
            up_monitors += 1
        elif raw_status not in {"down", "up"}:
            unknown_monitors += 1

        successful_checks_total += successful_checks
        total_checks_total += total_checks

    if down_monitors > 0:
        overall_status = "down"
    elif degraded_monitors > 0:
        overall_status = "degraded"
    elif len(monitors) > 0:
        overall_status = "operational"
    else:
        overall_status = "unknown"

    overall_uptime = (successful_checks_total / total_checks_total * 100) if total_checks_total else 0.0

    incident_rows = conn.execute(
        """
        SELECT ae.id, ae.monitor_id, m.name AS monitor_name, ae.timestamp,
               ae.previous_status, ae.current_status, ae.status_code, ae.response_time
        FROM alert_events ae
        JOIN monitors m ON m.id = ae.monitor_id
        JOIN status_page_monitors spm ON spm.monitor_id = m.id
        WHERE spm.status_page_id = ?
        ORDER BY ae.timestamp DESC
        LIMIT 25
    """,
        (page["id"],),
    ).fetchall()

    incidents = [
        StatusPageIncidentPublic(
            id=row["id"],
            monitor_id=row["monitor_id"],
            monitor_name=row["monitor_name"],
            timestamp=row["timestamp"],
            previous_status=row["previous_status"],
            current_status=row["current_status"],
            status_code=row["status_code"],
            response_time=row["response_time"],
        )
        for row in incident_rows
    ]

    theme = normalize_status_page_theme(page)
    show_powered_by_badge = not account_is_paid(conn, page["owner_api_key"])

    return StatusPagePublicResponse(
        slug=page["slug"],
        title=page["title"],
        description=page["description"],
        template_name=sanitize_template_name(page["template_name"] if "template_name" in page.keys() else None),
        theme_color=theme["theme_color"] or DEFAULT_STATUS_PAGE_COLORS["accent_color"],
        accent_color=theme["accent_color"] or DEFAULT_STATUS_PAGE_COLORS["accent_color"],
        background_color=theme["background_color"] or DEFAULT_STATUS_PAGE_COLORS["background_color"],
        text_color=theme["text_color"] or DEFAULT_STATUS_PAGE_COLORS["text_color"],
        logo_url=theme["logo_url"],
        overall_status=overall_status,
        overall_uptime_percentage=round(overall_uptime, 2),
        uptime_window_days=STATUS_PAGE_UPTIME_WINDOW_DAYS,
        monitor_count=len(monitors),
        operational_monitors=operational_monitors,
        degraded_monitors=degraded_monitors,
        up_monitors=operational_monitors,
        down_monitors=down_monitors,
        unknown_monitors=unknown_monitors,
        monitors=monitors,
        recent_incidents=incidents,
        show_powered_by_badge=show_powered_by_badge,
        generated_at=int(time.time()),
    )


def render_status_page_html(public_page: StatusPagePublicResponse) -> str:
    status_label_map = {
        "operational": "All Systems Operational",
        "degraded": "Degraded Performance",
        "down": "Major Service Disruption",
        "unknown": "Status Unknown",
    }
    monitor_rows = []
    for monitor in public_page.monitors:
        component_status = monitor.component_status if monitor.component_status in {"operational", "degraded", "down"} else "degraded"
        if component_status == "operational":
            status_class = "up"
            status_dot_inline = ""
            status_badge_inline = "color:#22c55e;border:1px solid rgba(34,197,94,0.45);background:rgba(34,197,94,0.16);"
        elif component_status == "down":
            status_class = "down"
            status_dot_inline = ""
            status_badge_inline = "color:#ef4444;border:1px solid rgba(239,68,68,0.45);background:rgba(239,68,68,0.14);"
        else:
            status_class = "unknown"
            status_dot_inline = ' style="background:#f59e0b;"'
            status_badge_inline = "color:#f59e0b;border:1px solid rgba(245,158,11,0.45);background:rgba(245,158,11,0.14);"

        safe_name = html.escape(monitor.name)
        safe_url = html.escape(monitor.url)
        safe_href = html.escape(monitor.url, quote=True)
        safe_component_label = html.escape(monitor.component_status_label)
        monitor_rows.append(
            f"""
            <li class=\"monitor-row\">
              <div>
                <div class=\"monitor-main\">
                  <span class=\"status-dot {status_class}\"{status_dot_inline}></span>
                  <span class=\"monitor-name\">{safe_name}</span>
                </div>
                <div class=\"monitor-url\"><a href=\"{safe_href}\" target=\"_blank\" rel=\"noopener noreferrer\">{safe_url}</a></div>
              </div>
              <div class=\"monitor-meta\">
                <span style=\"display:inline-block;border-radius:999px;padding:2px 8px;font-size:0.75rem;font-weight:700;letter-spacing:0.02em;{status_badge_inline}\">{safe_component_label}</span>
                · {monitor.uptime_percentage:.2f}% uptime (90d) · {monitor.checks_90d} checks
              </div>
            </li>
            """.strip()
        )

    if not monitor_rows:
        monitor_rows.append('<li class="empty-state">No monitors are connected to this status page yet.</li>')

    incident_rows = []
    for incident in public_page.recent_incidents:
        incident_class = incident.current_status if incident.current_status in {"up", "down"} else "unknown"
        incident_rows.append(
            f"""
            <li class=\"incident\">
              <div class=\"incident-head\">
                <span>{html.escape(incident.monitor_name)}</span>
                <span class=\"incident-status {incident_class}\">{html.escape(incident.current_status.upper())}</span>
              </div>
              <div class=\"incident-time\">
                {html.escape(format_unix_timestamp(incident.timestamp))}
                · {html.escape(incident.previous_status)} → {html.escape(incident.current_status)}
              </div>
            </li>
            """.strip()
        )

    if not incident_rows:
        incident_rows.append('<li class="empty-state">No incidents in the recent timeline.</li>')

    logo_html = '<div class="brand-fallback">OP</div>'
    if public_page.logo_url:
        safe_logo = html.escape(public_page.logo_url, quote=True)
        logo_html = f'<img src="{safe_logo}" alt="Status page logo" loading="lazy" />'

    description = public_page.description.strip() if public_page.description else "Live service health and incident timeline."
    subscribe_endpoint = f"/status/{public_page.slug}/subscribe"
    generated_at_markup = html.escape(format_unix_timestamp(public_page.generated_at))
    if public_page.show_powered_by_badge:
        generated_at_markup += (
            ' · <a href="https://owlpulse.org" target="_blank" rel="noopener noreferrer" '
            'style="display:inline-block;padding:2px 10px;border-radius:999px;border:1px solid rgba(59,130,246,0.45);'
            'text-decoration:none;color:#93c5fd;background:rgba(59,130,246,0.16);font-weight:700;">Powered by OwlPulse</a>'
        )

    issues_total = public_page.down_monitors + public_page.degraded_monitors

    template = load_status_page_template(public_page.template_name)
    context = {
        "PAGE_TITLE": html.escape(public_page.title if public_page.title.lower().endswith(" status") else f"{public_page.title} Status"),
        "ACCENT_COLOR": html.escape(public_page.accent_color),
        "BACKGROUND_COLOR": html.escape(public_page.background_color),
        "TEXT_COLOR": html.escape(public_page.text_color),
        "LOGO_HTML": logo_html,
        "TITLE": html.escape(public_page.title),
        "DESCRIPTION": html.escape(description),
        "OVERALL_STATUS_CLASS": html.escape(public_page.overall_status),
        "OVERALL_STATUS_LABEL": html.escape(status_label_map.get(public_page.overall_status, "Status Unknown")),
        "UPTIME_90D": f"{public_page.overall_uptime_percentage:.2f}",
        "MONITOR_COUNT": str(public_page.monitor_count),
        "UP_MONITORS": str(public_page.operational_monitors),
        "DOWN_MONITORS": str(issues_total),
        "MONITOR_ROWS": "\n".join(monitor_rows),
        "INCIDENT_ROWS": "\n".join(incident_rows),
        "SUBSCRIBE_ENDPOINT": html.escape(subscribe_endpoint, quote=True),
        "GENERATED_AT": generated_at_markup,
    }
    return render_status_template(template, context)

async def perform_check(monitor_id: int):
    """Perform a single uptime check for a monitor."""
    with get_db() as conn:
        monitor = conn.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,)).fetchone()

    if not monitor:
        return

    success, status_code, response_time, false_positive_filtered, details = await check_monitor_with_false_positive_filter(
        monitor
    )
    emitted_arena_event = False

    timestamp = int(time.time())
    previous_status = monitor["status"]
    new_status = "up" if success else "down"
    in_maintenance = is_in_maintenance_window(monitor, timestamp)

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO checks (monitor_id, timestamp, status_code, response_time, success)
            VALUES (?, ?, ?, ?, ?)
        """,
            (monitor_id, timestamp, status_code, response_time, 1 if success else 0),
        )

        conn.execute(
            """
            UPDATE monitors
            SET status = ?,
                last_checked = ?,
                total_checks = total_checks + 1,
                successful_checks = successful_checks + ?,
                false_positives_filtered = false_positives_filtered + ?
            WHERE id = ?
        """,
            (
                new_status,
                timestamp,
                1 if success else 0,
                1 if false_positive_filtered else 0,
                monitor_id,
            ),
        )
        upsert_sla_windows(conn, monitor_id, timestamp, success)
        if business_arena_enabled() and success and int(monitor["successful_checks"] or 0) == 0:
            attribution = AttributionContext.from_mapping(
                {
                    "source": monitor["source"],
                    "channel": monitor["channel"],
                    "campaign": monitor["campaign"],
                    "experiment_id": monitor["experiment_id"],
                    "variant_id": monitor["variant_id"],
                    "cohort": monitor["cohort"],
                    "session_id": monitor["session_id"],
                }
            )
            record_business_event(
                conn,
                event_name="first_successful_check",
                attribution=attribution,
                surface="monitoring",
                value=0.85,
                path=f"/monitors/{monitor_id}/check",
                monitor_id=monitor_id,
                plan=monitor["plan"],
                metadata={"status_code": status_code, "response_time_ms": response_time},
            )
            emitted_arena_event = True
        conn.commit()

    # Send alerts only if status changed and monitor already had known status.
    # During maintenance windows we still record checks, but suppress alert fan-out.
    if previous_status != "unknown" and previous_status != new_status and not in_maintenance:
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO alert_events (
                    monitor_id, timestamp, previous_status, current_status,
                    status_code, response_time, false_positive_filtered
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    monitor_id,
                    timestamp,
                    previous_status,
                    new_status,
                    status_code,
                    response_time,
                    1 if false_positive_filtered else 0,
                ),
            )
            conn.commit()

        webhook_data = {
            "monitor_id": monitor_id,
            "name": monitor["name"],
            "url": monitor["url"],
            "monitor_type": monitor["monitor_type"] or "http",
            "status": new_status,
            "previous_status": previous_status,
            "timestamp": timestamp,
            "status_code": status_code,
            "response_time_ms": response_time,
            "false_positive_filtered": false_positive_filtered,
            "details": details,
        }
        await send_status_alerts(monitor, webhook_data)
    if business_arena_enabled() and emitted_arena_event:
        maybe_write_business_report()


async def check_all_monitors():
    """Background task to check all monitors with lock + bounded concurrency."""
    if check_cycle_lock.locked():
        return

    async with check_cycle_lock:
        with get_db() as conn:
            monitors = conn.execute("SELECT id, plan, last_checked FROM monitors").fetchall()

        now = int(time.time())
        due_monitor_ids = []

        for monitor in monitors:
            interval = CHECK_INTERVAL_PRO if monitor["plan"] in {"pro", "business"} else CHECK_INTERVAL_FREE
            if monitor["last_checked"] is None or (now - monitor["last_checked"]) >= interval:
                due_monitor_ids.append(monitor["id"])

        if not due_monitor_ids:
            return

        sem = asyncio.Semaphore(MAX_PARALLEL_CHECKS)

        async def run_bounded(mid: int):
            async with sem:
                await perform_check(mid)

        results = await asyncio.gather(*(run_bounded(mid) for mid in due_monitor_ids), return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                print(f"Monitor check error: {r}")


# ============================================================================
# API Endpoints
# ============================================================================

def ensure_status_page_artifacts():
    """Create status-page template assets and renderer artifacts when missing."""
    template_dirs = [LOCAL_STATUS_PAGE_TEMPLATE_DIR, STATUS_PAGE_TEMPLATE_DIR]
    for template_dir in template_dirs:
        try:
            template_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"Status-page template directory sync failed for {template_dir}: {exc}")

    for template_name in sorted(STATUS_PAGE_TEMPLATE_NAMES):
        local_template_path = LOCAL_STATUS_PAGE_TEMPLATE_DIR / f"{template_name}.html"
        external_template_path = STATUS_PAGE_TEMPLATE_DIR / f"{template_name}.html"
        template_content = ""

        try:
            if local_template_path.exists():
                template_content = local_template_path.read_text(encoding="utf-8").strip()
        except OSError:
            template_content = ""

        if not template_content and template_name == DEFAULT_STATUS_PAGE_TEMPLATE_NAME:
            template_content = STATUS_PAGE_TEMPLATE_DEFAULT

        if not template_content:
            template_content = STATUS_PAGE_TEMPLATE_DEFAULT

        try:
            if not local_template_path.exists() or not local_template_path.read_text(encoding="utf-8").strip():
                local_template_path.write_text(template_content, encoding="utf-8")
        except OSError as exc:
            print(f"Status-page local template sync failed for {local_template_path}: {exc}")

        try:
            if not external_template_path.exists() or not external_template_path.read_text(encoding="utf-8").strip():
                external_template_path.write_text(template_content, encoding="utf-8")
        except OSError as exc:
            print(f"Status-page external template sync failed for {external_template_path}: {exc}")

    # Legacy path compatibility for old renderer workflows.
    design_dirs = [STATUS_PAGE_DESIGN_DIR, LOCAL_STATUS_PAGE_DESIGN_DIR]
    for design_dir in design_dirs:
        design_template_path = design_dir / STATUS_PAGE_TEMPLATE_FILE
        try:
            design_dir.mkdir(parents=True, exist_ok=True)
            existing_design = (
                design_template_path.read_text(encoding="utf-8").strip() if design_template_path.exists() else ""
            )
            if not existing_design:
                design_template_path.write_text(load_status_page_template(DEFAULT_STATUS_PAGE_TEMPLATE_NAME), encoding="utf-8")
        except OSError as exc:
            print(f"Status-page design template sync failed for {design_dir}: {exc}")

    template_for_renderer = load_status_page_template(DEFAULT_STATUS_PAGE_TEMPLATE_NAME)
    sample_page = render_status_template(
        template_for_renderer,
        {
            "PAGE_TITLE": "Sample OwlPulse Status",
            "ACCENT_COLOR": DEFAULT_STATUS_PAGE_COLORS["accent_color"],
            "BACKGROUND_COLOR": DEFAULT_STATUS_PAGE_COLORS["background_color"],
            "TEXT_COLOR": DEFAULT_STATUS_PAGE_COLORS["text_color"],
            "LOGO_HTML": '<div class="brand-fallback">OP</div>',
            "TITLE": "Sample OwlPulse Status",
            "DESCRIPTION": "Demonstration template render output.",
            "OVERALL_STATUS_CLASS": "operational",
            "OVERALL_STATUS_LABEL": "All Systems Operational",
            "UPTIME_90D": "99.98",
            "MONITOR_COUNT": "3",
            "UP_MONITORS": "3",
            "DOWN_MONITORS": "0",
            "MONITOR_ROWS": (
                "<li class=\"monitor-row\"><div><div class=\"monitor-main\"><span class=\"status-dot up\"></span>"
                "<span class=\"monitor-name\">API</span></div><div class=\"monitor-url\">"
                "<a href=\"https://example.com/api\" target=\"_blank\" rel=\"noopener noreferrer\">"
                "https://example.com/api</a></div></div><div class=\"monitor-meta\">100.00% uptime (90d)"
                " · 120 checks</div></li>"
            ),
            "INCIDENT_ROWS": "<li class=\"empty-state\">No incidents in the recent timeline.</li>",
            "SUBSCRIBE_ENDPOINT": "/status/sample/subscribe",
            "GENERATED_AT": format_unix_timestamp(int(time.time())),
        },
    )

    renderer_readme_content = """# OwlPulse Status Renderer

This directory contains status-page rendering artifacts used by the FastAPI backend.

## Files

- `template.html`: Base HTML template consumed by the `/status/{slug}` endpoint.
- `sample-rendered.html`: Example rendered output using sample values.

## Runtime Variables

The renderer injects values into `template.html` placeholders:

- `{{TITLE}}`, `{{DESCRIPTION}}`
- `{{OVERALL_STATUS_LABEL}}`, `{{UPTIME_90D}}`
- `{{MONITOR_ROWS}}`, `{{INCIDENT_ROWS}}`
- `{{ACCENT_COLOR}}`, `{{BACKGROUND_COLOR}}`, `{{TEXT_COLOR}}`
- `{{SUBSCRIBE_ENDPOINT}}`

## Integration Notes

1. Keep placeholders intact in `template.html` so the API can substitute values.
2. Supported themes: default, minimal, cyber, corporate, terminal.
3. Update design assets in `/Users/davidadams/owlpulse-design/templates/` first.
4. `GET /status/{slug}` renders public HTML.
5. `POST /status/{slug}/subscribe` stores subscriber emails per page.
"""

    renderer_dirs = [STATUS_PAGE_RENDERER_DIR, LOCAL_STATUS_PAGE_RENDERER_DIR]
    for renderer_dir in renderer_dirs:
        renderer_template_path = renderer_dir / STATUS_PAGE_TEMPLATE_FILE
        renderer_readme_path = renderer_dir / "README.md"
        renderer_sample_path = renderer_dir / "sample-rendered.html"
        try:
            renderer_dir.mkdir(parents=True, exist_ok=True)
            renderer_template_path.write_text(template_for_renderer, encoding="utf-8")
            renderer_readme_path.write_text(renderer_readme_content, encoding="utf-8")
            renderer_sample_path.write_text(sample_page, encoding="utf-8")
        except OSError as exc:
            print(f"Status-page renderer artifact sync failed for {renderer_dir}: {exc}")


@app.on_event("startup")
async def startup_event():
    """Initialize database and start scheduler."""
    init_db()
    if business_arena_enabled():
        ensure_business_arena_schema(DEFAULT_ARENA_DB_PATH)
        maybe_write_business_report()
    ensure_status_page_artifacts()
    scheduler.add_job(
        check_all_monitors,
        "interval",
        seconds=30,
        id="monitor_checks",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=20,
    )
    scheduler.start()


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown."""
    if scheduler.running:
        scheduler.shutdown()


@app.get("/health")
async def health():
    """API health check."""
    return {
        "service": "Uptime Monitor API",
        "status": "running",
        "version": "1.3.0",
        "scheduler_running": scheduler.running,
    }


def load_static_html(filename: str) -> Optional[str]:
    static_path = os.path.join(os.path.dirname(__file__), "static", filename)
    if not os.path.exists(static_path):
        return None
    with open(static_path, "r", encoding="utf-8") as f:
        return f.read()


def render_public_status_page_by_slug(slug: str) -> HTMLResponse:
    with get_db() as conn:
        page = conn.execute("SELECT * FROM status_pages WHERE slug = ?", (slug,)).fetchone()
        if not page:
            raise_api_error(
                status_code=404,
                code="status_page_not_found",
                message=f"Status page '{slug}' was not found",
                hint="Double-check the status page URL slug.",
            )
        public_page = build_status_page_public_response(conn, page)

    html_content = render_status_page_html(public_page)
    return HTMLResponse(content=html_content)


@app.get("/")
async def root(request: Request):
    """Serve landing page or subdomain-routed status page."""
    subdomain_slug = extract_status_page_slug_from_request(request)
    if subdomain_slug:
        return render_public_status_page_by_slug(subdomain_slug)

    if business_arena_enabled():
        try:
            program = load_business_offer_program()
            if program:
                attribution = build_attribution_context(
                    request=request,
                    default_source=str(program.get("signup", {}).get("default_source", "")),
                    default_channel=str(program.get("signup", {}).get("default_channel", "")),
                    default_campaign=str(program.get("signup", {}).get("default_campaign", "")),
                )
                return HTMLResponse(
                    content=render_home_html(
                        OfferProgram.from_dict(program),
                        attribution,
                        app_db_path=DATABASE,
                    )
                )
        except Exception as exc:
            print(f"Business arena home render failed: {exc}")

    index_html = load_static_html("index.html")
    if index_html:
        return HTMLResponse(content=index_html)
    return {
        "service": "Uptime Monitor API",
        "status": "running",
        "version": "1.2.0",
    }


@app.get("/signup")
async def signup_page(request: Request):
    """Serve the self-serve onboarding flow."""
    signup_html = load_static_html("signup.html")
    if signup_html and business_arena_enabled():
        try:
            program = load_business_offer_program()
            if program:
                attribution = build_attribution_context(
                    request=request,
                    default_source=str(program.get("signup", {}).get("default_source", "")),
                    default_channel=str(program.get("signup", {}).get("default_channel", "")),
                    default_campaign=str(program.get("signup", {}).get("default_campaign", "")),
                )
                return HTMLResponse(
                    content=render_signup_html(
                        signup_html,
                        OfferProgram.from_dict(program),
                        attribution,
                    )
                )
        except Exception as exc:
            print(f"Business arena signup render failed: {exc}")
    if signup_html:
        return HTMLResponse(content=signup_html)
    return JSONResponse(
        status_code=404,
        content={"error": {"code": "signup_page_missing", "message": "Signup page not found"}},
    )


@app.get("/compare")
async def compare_page(request: Request):
    """Serve UptimeRobot comparison page."""
    if business_arena_enabled():
        try:
            program = load_business_offer_program()
            if program:
                attribution = build_attribution_context(
                    request=request,
                    default_source=str(program.get("signup", {}).get("default_source", "")),
                    default_channel=str(program.get("signup", {}).get("default_channel", "")),
                    default_campaign=str(program.get("signup", {}).get("default_campaign", "")),
                )
                return HTMLResponse(content=render_compare_html(OfferProgram.from_dict(program), attribution))
        except Exception as exc:
            print(f"Business arena compare render failed: {exc}")
    static_path = os.path.join(os.path.dirname(__file__), "static", "compare.html")
    if os.path.exists(static_path):
        with open(static_path, "r") as f:
            return HTMLResponse(content=f.read())
    return {"error": "Page not found"}


@app.get("/static/widget.js")
async def get_widget_script():
    """Serve embeddable status widget JS."""
    widget_path = Path(__file__).resolve().parent / "static" / "widget.js"
    if not widget_path.exists():
        raise_api_error(
            status_code=404,
            code="widget_not_found",
            message="Widget script is not available",
            hint="Ensure static/widget.js exists on this deployment.",
        )
    return PlainTextResponse(widget_path.read_text(encoding="utf-8"), media_type="application/javascript")


# ============================================================================
# Checkout Endpoints (Stripe Payment Links)
# ============================================================================

STRIPE_LINKS = {
    "pro": "https://buy.stripe.com/6oUdRb8S5aUqfjD5SY2Fa02",
    "business": "https://buy.stripe.com/6oUdRb8S5aUqfjD5SY2Fa02",  # TODO: separate Business link
}


@app.get("/checkout/{plan}")
async def checkout(plan: str, request: Request):
    """Redirect to Stripe Checkout for a paid plan."""
    from fastapi.responses import RedirectResponse

    plan = canonicalize_plan(plan)

    if plan not in STRIPE_LINKS:
        raise_api_error(
            status_code=404,
            code="invalid_plan",
            message=f"Plan '{plan}' not found",
            hint="Use one of: pro, business.",
        )
    if business_arena_enabled():
        offer_program = load_business_offer_program()
        attribution = build_attribution_context(
            request=request,
            default_source=str(offer_program.get("signup", {}).get("default_source", "")),
            default_channel=str(offer_program.get("signup", {}).get("default_channel", "")),
            default_campaign=str(offer_program.get("signup", {}).get("default_campaign", "")),
        )
        with get_db() as conn:
            record_business_event(
                conn,
                event_name="checkout_click",
                attribution=attribution,
                surface="checkout",
                value=0.95,
                request=request,
                plan=plan,
                metadata=enrich_trust_bar_metadata(
                    surface="checkout",
                    offer_program=offer_program,
                    metadata={"plan": plan},
                ),
            )
        maybe_write_business_report()
    return RedirectResponse(url=STRIPE_LINKS[plan])


# ============================================================================
# Stats Endpoint (for revenue dashboard / Tron)
# ============================================================================

@app.get("/stats")
async def get_stats():
    """Public stats: total monitors, checks, uptime. Used by Tron for reporting."""
    with get_db() as conn:
        monitors = conn.execute("SELECT COUNT(*) as cnt FROM monitors").fetchone()["cnt"]
        checks = conn.execute("SELECT COUNT(*) as cnt FROM checks").fetchone()["cnt"]
        false_positives = conn.execute(
            "SELECT COALESCE(SUM(false_positives_filtered), 0) as cnt FROM monitors"
        ).fetchone()["cnt"]
        plans = conn.execute("SELECT plan, COUNT(*) as cnt FROM monitors GROUP BY plan").fetchall()
        plan_counts = {row["plan"]: row["cnt"] for row in plans}
    return {
        "total_monitors": monitors,
        "total_checks": checks,
        "false_positives_filtered": false_positives,
        "plans": plan_counts,
        "status": "healthy",
    }


@app.get("/status/summary", response_model=StatusSummaryResponse)
async def get_status_summary():
    """Public status summary for dashboards and status pages."""
    now = int(time.time())
    since_24h = now - 86400

    with get_db() as conn:
        total_monitors = conn.execute("SELECT COUNT(*) AS cnt FROM monitors").fetchone()["cnt"]
        up_monitors = conn.execute("SELECT COUNT(*) AS cnt FROM monitors WHERE status = 'up'").fetchone()["cnt"]
        down_monitors = conn.execute("SELECT COUNT(*) AS cnt FROM monitors WHERE status = 'down'").fetchone()["cnt"]
        unknown_monitors = conn.execute("SELECT COUNT(*) AS cnt FROM monitors WHERE status = 'unknown'").fetchone()["cnt"]

        totals = conn.execute(
            """
            SELECT COALESCE(SUM(successful_checks), 0) AS successful,
                   COALESCE(SUM(total_checks), 0) AS total
            FROM monitors
        """
        ).fetchone()

        checks_last_24h = conn.execute(
            "SELECT COUNT(*) AS cnt FROM checks WHERE timestamp >= ?",
            (since_24h,),
        ).fetchone()["cnt"]

        incidents_last_24h = conn.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM alert_events
            WHERE timestamp >= ?
              AND current_status = 'down'
        """,
            (since_24h,),
        ).fetchone()["cnt"]

    overall_uptime = 0.0
    if totals["total"] > 0:
        overall_uptime = (totals["successful"] / totals["total"]) * 100

    return StatusSummaryResponse(
        total_monitors=total_monitors,
        up_monitors=up_monitors,
        down_monitors=down_monitors,
        unknown_monitors=unknown_monitors,
        overall_uptime_percentage=round(overall_uptime, 2),
        checks_last_24h=checks_last_24h,
        incidents_last_24h=incidents_last_24h,
    )


@app.get("/reports/sla")
async def get_sla_report(
    api_key: str = Depends(verify_api_key),
    x_user_email: Optional[str] = Header(None),
    monitor_id: Optional[int] = None,
    output: str = "json",
):
    """SLA report endpoint with monthly + quarterly compliance and error budgets."""
    now = int(time.time())
    output = output.strip().lower()
    periods = {
        "monthly": ("month", start_of_month_utc(now), end_of_month_utc(now)),
        "quarterly": ("quarter", start_of_quarter_utc(now), end_of_quarter_utc(now)),
    }

    with get_db() as conn:
        team_ids = get_accessible_team_ids(conn, api_key, x_user_email)
        placeholders = ",".join("?" for _ in team_ids)
        query = (
            "SELECT * FROM monitors WHERE "
            f"(team_id IN ({placeholders}) OR (team_id IS NULL AND api_key = ?))"
        )
        params: list[Any] = [*team_ids, api_key]
        if monitor_id is not None:
            query += " AND id = ?"
            params.append(monitor_id)
        query += " ORDER BY name ASC"
        monitors = conn.execute(query, params).fetchall()

        if monitor_id is not None and not monitors:
            raise_api_error(
                status_code=404,
                code="monitor_not_found",
                message=f"Monitor {monitor_id} was not found for this API key",
                hint="Check monitor access or team permissions.",
            )

        report_rows: list[dict[str, Any]] = []
        for monitor in monitors:
            target = float(monitor["sla_target"] if monitor["sla_target"] is not None else DEFAULT_SLA_TARGET)
            monitor_report: dict[str, Any] = {
                "monitor_id": monitor["id"],
                "name": monitor["name"],
                "url": monitor["url"],
                "target_uptime_percentage": round(target, 3),
                "windows": {},
            }

            for label, (period_type, period_start, period_end) in periods.items():
                row = conn.execute(
                    """
                    SELECT total_checks, successful_checks
                    FROM sla_windows
                    WHERE monitor_id = ?
                      AND period_type = ?
                      AND period_start = ?
                """,
                    (monitor["id"], period_type, period_start),
                ).fetchone()

                total_checks = int(row["total_checks"]) if row else 0
                successful_checks = int(row["successful_checks"]) if row else 0
                uptime = 100.0 if total_checks == 0 else (successful_checks / total_checks) * 100
                elapsed_seconds = max(1, min(now, period_end) - period_start)
                elapsed_minutes = elapsed_seconds / 60.0
                allowed_downtime_minutes = elapsed_minutes * max(0.0, (100.0 - target) / 100.0)
                observed_downtime_minutes = 0.0 if total_checks == 0 else elapsed_minutes * max(0.0, (100.0 - uptime) / 100.0)
                remaining_budget_minutes = allowed_downtime_minutes - observed_downtime_minutes

                monitor_report["windows"][label] = {
                    "period_type": period_type,
                    "period_start": period_start,
                    "period_end": period_end,
                    "total_checks": total_checks,
                    "successful_checks": successful_checks,
                    "uptime_percentage": round(uptime, 4),
                    "target_uptime_percentage": round(target, 3),
                    "is_compliant": uptime >= target,
                    "error_budget_minutes": round(allowed_downtime_minutes, 4),
                    "error_budget_remaining_minutes": round(remaining_budget_minutes, 4),
                    "observed_downtime_minutes": round(observed_downtime_minutes, 4),
                }

            report_rows.append(monitor_report)

    payload = {
        "generated_at": now,
        "monitor_count": len(report_rows),
        "monitors": report_rows,
    }

    if output == "html":
        row_html = []
        for item in report_rows:
            monthly = item["windows"]["monthly"]
            quarterly = item["windows"]["quarterly"]
            row_html.append(
                "<tr>"
                f"<td>{html.escape(str(item['monitor_id']))}</td>"
                f"<td>{html.escape(item['name'])}</td>"
                f"<td>{html.escape(item['target_uptime_percentage'].__str__())}%</td>"
                f"<td>{monthly['uptime_percentage']:.3f}%</td>"
                f"<td>{monthly['error_budget_remaining_minutes']:.2f}m</td>"
                f"<td>{'yes' if monthly['is_compliant'] else 'no'}</td>"
                f"<td>{quarterly['uptime_percentage']:.3f}%</td>"
                f"<td>{quarterly['error_budget_remaining_minutes']:.2f}m</td>"
                f"<td>{'yes' if quarterly['is_compliant'] else 'no'}</td>"
                "</tr>"
            )

        html_content = (
            "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>OwlPulse SLA Report</title>"
            "<style>body{font-family:Arial,sans-serif;margin:24px;background:#f8fafc;color:#0f172a}"
            "table{border-collapse:collapse;width:100%;background:#fff}th,td{padding:10px;border:1px solid #e2e8f0;font-size:14px;text-align:left}"
            "th{background:#0f172a;color:#f8fafc}h1{margin-top:0}.meta{color:#475569;margin-bottom:14px}</style></head><body>"
            "<h1>OwlPulse SLA Report</h1>"
            f"<div class='meta'>Generated: {html.escape(format_unix_timestamp(now))} | Monitors: {len(report_rows)}</div>"
            "<table><thead><tr><th>ID</th><th>Monitor</th><th>Target</th><th>Monthly Uptime</th><th>Monthly Budget Left</th>"
            "<th>Monthly Compliant</th><th>Quarterly Uptime</th><th>Quarterly Budget Left</th><th>Quarterly Compliant</th></tr></thead>"
            f"<tbody>{''.join(row_html) if row_html else '<tr><td colspan=\"9\">No monitors available</td></tr>'}</tbody></table>"
            "</body></html>"
        )
        return HTMLResponse(content=html_content)

    return payload




@app.post("/status-pages", response_model=StatusPageResponse, status_code=201)
async def create_status_page(
    payload: StatusPageCreate,
    api_key: str = Depends(verify_api_key),
    x_user_email: Optional[str] = Header(None),
):
    """Create a private status page definition for monitors owned by this API key."""
    now = int(time.time())
    theme_color = sanitize_hex_color(payload.theme_color, DEFAULT_STATUS_PAGE_COLORS["accent_color"])
    accent_color = sanitize_hex_color(payload.accent_color or theme_color, theme_color)
    background_color = sanitize_hex_color(payload.background_color, DEFAULT_STATUS_PAGE_COLORS["background_color"])
    text_color = sanitize_hex_color(payload.text_color, DEFAULT_STATUS_PAGE_COLORS["text_color"])
    template_name = sanitize_template_name(payload.template_name)
    logo_url = sanitize_logo_url(str(payload.logo_url) if payload.logo_url else None)
    custom_domain = payload.custom_domain.strip().lower() if payload.custom_domain else None

    with get_db() as conn:
        team_id, _ = require_team_permission(conn, api_key, x_user_email, None, "write")
        assert_monitors_owned_by_api_key(conn, payload.monitor_ids, api_key)
        slug = generate_unique_status_page_slug(conn, payload.title, payload.slug)

        cursor = conn.execute(
            """
            INSERT INTO status_pages (
                owner_api_key, slug, title, description, template_name, theme_color, logo_url, accent_color,
                background_color, text_color, custom_domain, team_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                api_key,
                slug,
                payload.title,
                payload.description,
                template_name,
                theme_color,
                logo_url,
                accent_color,
                background_color,
                text_color,
                custom_domain,
                team_id,
                now,
                now,
            ),
        )
        page_id = cursor.lastrowid

        for monitor_id in sorted(set(payload.monitor_ids)):
            conn.execute(
                """
                INSERT INTO status_page_monitors (status_page_id, monitor_id, created_at)
                VALUES (?, ?, ?)
            """,
                (page_id, monitor_id, now),
            )

        conn.commit()

        page = conn.execute("SELECT * FROM status_pages WHERE id = ?", (page_id,)).fetchone()
        return build_status_page_response(conn, page)


@app.get("/status-pages", response_model=list[StatusPageResponse])
async def list_status_pages(api_key: str = Depends(verify_api_key), x_user_email: Optional[str] = Header(None)):
    """List status pages owned by this API key."""
    with get_db() as conn:
        team_ids = get_accessible_team_ids(conn, api_key, x_user_email)
        placeholders = ",".join("?" for _ in team_ids)
        pages = conn.execute(
            """
            SELECT *
            FROM status_pages
            WHERE team_id IN ({team_ids})
               OR (team_id IS NULL AND owner_api_key = ?)
            ORDER BY created_at DESC
        """.format(team_ids=placeholders),
            [*team_ids, api_key],
        ).fetchall()
        return [build_status_page_response(conn, page) for page in pages]


@app.get("/status-pages/{status_page_id}", response_model=StatusPageResponse)
async def get_status_page(
    status_page_id: int,
    api_key: str = Depends(verify_api_key),
    x_user_email: Optional[str] = Header(None),
):
    """Get one status page by ID."""
    with get_db() as conn:
        page = conn.execute("SELECT * FROM status_pages WHERE id = ?", (status_page_id,)).fetchone()
        if not page:
            raise_api_error(
                status_code=404,
                code="status_page_not_found",
                message=f"Status page {status_page_id} was not found for this API key",
                hint="Check the status page ID and ensure it belongs to this account.",
            )
        page_team_id = page["team_id"] if page["team_id"] else ensure_team_for_api_key(conn, page["owner_api_key"])
        require_team_permission(conn, api_key, x_user_email, page_team_id, "read")
        return build_status_page_response(conn, page)


@app.put("/status-pages/{status_page_id}", response_model=StatusPageResponse)
async def update_status_page(
    status_page_id: int,
    payload: StatusPageUpdate,
    api_key: str = Depends(verify_api_key),
    x_user_email: Optional[str] = Header(None),
):
    """Update status page metadata and/or monitor mapping."""
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise_api_error(
            status_code=400,
            code="empty_update",
            message="No fields were provided to update",
            hint=(
                "Provide at least one field: title, slug, description, theme_color, accent_color, "
                "background_color, text_color, logo_url, custom_domain, template_name, or monitor_ids."
            ),
        )

    with get_db() as conn:
        existing = conn.execute("SELECT * FROM status_pages WHERE id = ?", (status_page_id,)).fetchone()
        if not existing:
            raise_api_error(
                status_code=404,
                code="status_page_not_found",
                message=f"Status page {status_page_id} was not found for this API key",
                hint="Check the status page ID and ensure it belongs to this account.",
            )
        existing_team_id = existing["team_id"] if existing["team_id"] else ensure_team_for_api_key(conn, existing["owner_api_key"])
        require_team_permission(conn, api_key, x_user_email, existing_team_id, "write")

        if "monitor_ids" in updates:
            assert_monitors_owned_by_api_key(conn, updates["monitor_ids"] or [], api_key)
            conn.execute("DELETE FROM status_page_monitors WHERE status_page_id = ?", (status_page_id,))
            for monitor_id in sorted(set(updates["monitor_ids"] or [])):
                conn.execute(
                    """
                    INSERT INTO status_page_monitors (status_page_id, monitor_id, created_at)
                    VALUES (?, ?, ?)
                """,
                    (status_page_id, monitor_id, int(time.time())),
                )

        update_parts: list[str] = []
        values: list[Any] = []

        if "title" in updates:
            update_parts.append("title = ?")
            values.append(updates["title"])

        if "description" in updates:
            update_parts.append("description = ?")
            values.append(updates["description"])

        if "template_name" in updates:
            update_parts.append("template_name = ?")
            values.append(sanitize_template_name(updates["template_name"]))

        if "theme_color" in updates:
            update_parts.append("theme_color = ?")
            normalized_theme = sanitize_hex_color(updates["theme_color"], DEFAULT_STATUS_PAGE_COLORS["accent_color"])
            values.append(normalized_theme)
            if "accent_color" not in updates:
                update_parts.append("accent_color = ?")
                values.append(normalized_theme)

        if "accent_color" in updates:
            update_parts.append("accent_color = ?")
            accent_base = updates.get("theme_color") or existing["theme_color"] or DEFAULT_STATUS_PAGE_COLORS["accent_color"]
            values.append(sanitize_hex_color(updates["accent_color"] or accent_base, accent_base))

        if "background_color" in updates:
            update_parts.append("background_color = ?")
            values.append(
                sanitize_hex_color(
                    updates["background_color"],
                    existing["background_color"] or DEFAULT_STATUS_PAGE_COLORS["background_color"],
                )
            )

        if "text_color" in updates:
            update_parts.append("text_color = ?")
            values.append(
                sanitize_hex_color(
                    updates["text_color"],
                    existing["text_color"] or DEFAULT_STATUS_PAGE_COLORS["text_color"],
                )
            )

        if "logo_url" in updates:
            update_parts.append("logo_url = ?")
            values.append(sanitize_logo_url(str(updates["logo_url"])) if updates["logo_url"] else None)

        if "custom_domain" in updates:
            update_parts.append("custom_domain = ?")
            values.append(updates["custom_domain"].strip().lower() if updates["custom_domain"] else None)

        if "slug" in updates:
            title_for_slug = updates.get("title", existing["title"])
            next_slug = generate_unique_status_page_slug(
                conn,
                title_for_slug,
                updates["slug"],
                exclude_page_id=status_page_id,
            )
            update_parts.append("slug = ?")
            values.append(next_slug)

        if update_parts:
            update_parts.append("updated_at = ?")
            values.append(int(time.time()))
            values.append(status_page_id)
            conn.execute(
                f"UPDATE status_pages SET {', '.join(update_parts)} WHERE id = ?",
                values,
            )

        conn.commit()
        page = conn.execute("SELECT * FROM status_pages WHERE id = ?", (status_page_id,)).fetchone()
        return build_status_page_response(conn, page)


@app.delete("/status-pages/{status_page_id}", status_code=204)
async def delete_status_page(
    status_page_id: int,
    api_key: str = Depends(verify_api_key),
    x_user_email: Optional[str] = Header(None),
):
    """Delete a status page owned by this API key."""
    with get_db() as conn:
        page = conn.execute("SELECT id, team_id, owner_api_key FROM status_pages WHERE id = ?", (status_page_id,)).fetchone()
        if not page:
            raise_api_error(
                status_code=404,
                code="status_page_not_found",
                message=f"Status page {status_page_id} was not found for this API key",
                hint="Check the status page ID and ensure it belongs to this account.",
            )
        page_team_id = page["team_id"] if page["team_id"] else ensure_team_for_api_key(conn, page["owner_api_key"])
        require_team_permission(conn, api_key, x_user_email, page_team_id, "delete")

        conn.execute("DELETE FROM status_page_monitors WHERE status_page_id = ?", (status_page_id,))
        cursor = conn.execute("DELETE FROM status_pages WHERE id = ?", (status_page_id,))
        conn.commit()
        if cursor.rowcount == 0:
            raise_api_error(
                status_code=404,
                code="status_page_not_found",
                message=f"Status page {status_page_id} was not found for this API key",
                hint="Check the status page ID and ensure it belongs to this account.",
            )
    return None


@app.get("/status-pages/{slug}/public", response_model=StatusPagePublicResponse)
async def get_public_status_page(slug: str):
    """Public status page endpoint. Does not require API key auth."""
    with get_db() as conn:
        page = conn.execute("SELECT * FROM status_pages WHERE slug = ?", (slug,)).fetchone()
        if not page:
            raise_api_error(
                status_code=404,
                code="status_page_not_found",
                message=f"Status page '{slug}' was not found",
                hint="Double-check the status page URL slug.",
            )
        return build_status_page_public_response(conn, page)


@app.get("/status/{slug}", response_class=HTMLResponse)
async def get_public_status_page_html(slug: str):
    """Render embeddable HTML for a public status page."""
    return render_public_status_page_by_slug(slug)


async def extract_subscribe_email_from_request(request: Request) -> str:
    raw_email: Optional[str] = None

    try:
        payload = await request.json()
        if isinstance(payload, dict):
            raw_email = payload.get("email")
    except (RuntimeError, TypeError, ValueError):
        pass

    if raw_email is None:
        try:
            form_data = await request.form()
            maybe_email = form_data.get("email")
            if maybe_email is not None:
                raw_email = str(maybe_email)
        except (RuntimeError, TypeError, ValueError):
            pass

    if raw_email is None:
        raise_api_error(
            status_code=400,
            code="missing_email",
            message="Email is required",
            hint="Send {\"email\": \"you@example.com\"} as JSON or form data.",
        )

    return validate_subscriber_email(str(raw_email))


@app.post("/status/{slug}/subscribe", response_model=StatusPageSubscribeResponse, status_code=201)
async def subscribe_status_page(slug: str, request: Request):
    """Subscribe an email for status-page updates."""
    email = await extract_subscribe_email_from_request(request)
    now = int(time.time())

    with get_db() as conn:
        page = conn.execute("SELECT id FROM status_pages WHERE slug = ?", (slug,)).fetchone()
        if not page:
            raise_api_error(
                status_code=404,
                code="status_page_not_found",
                message=f"Status page '{slug}' was not found",
                hint="Double-check the status page URL slug.",
            )

        try:
            conn.execute(
                """
                INSERT INTO status_page_subscribers (status_page_id, email, created_at)
                VALUES (?, ?, ?)
            """,
                (page["id"], email, now),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return JSONResponse(
                status_code=200,
                content=StatusPageSubscribeResponse(
                    message="This email is already subscribed to updates.",
                    email=email,
                    subscribed=True,
                ).model_dump(),
            )

    return StatusPageSubscribeResponse(
        message="Subscription successful. You'll receive future status updates.",
        email=email,
        subscribed=True,
    )

@app.post("/monitors", response_model=MonitorResponse, status_code=201)
async def create_monitor(
    monitor: MonitorCreate,
    api_key: str = Depends(verify_api_key),
):
    """Create a new monitor."""
    sms_number = validate_sms_number(monitor.sms_number) if monitor.sms_number else None
    validate_monitor_payload(
        monitor_type=monitor.monitor_type,
        url=monitor.url,
        port=monitor.port,
        keyword=monitor.keyword,
        dns_hostname=monitor.dns_hostname,
        maintenance_starts_at=monitor.maintenance_starts_at,
        maintenance_ends_at=monitor.maintenance_ends_at,
        sms_number=sms_number,
        sla_target=monitor.sla_target,
    )

    created_at = int(time.time())

    with get_db() as conn:
        team_id = ensure_team_for_api_key(conn, api_key)  # auth: user's existing key
        plan = canonicalize_plan(monitor.plan)
        check_monitor_count_limit(conn, team_id, plan)
        new_monitor_api_key = secrets.token_urlsafe(32)  # fresh key per monitor
        cursor = conn.execute(
            """
            INSERT INTO monitors (
                url, name, plan, monitor_type, port, keyword, keyword_should_exist, custom_headers,
                maintenance_starts_at, maintenance_ends_at, dns_hostname, dns_record_type,
                api_key, webhook_url, slack_webhook_url, discord_webhook_url, sms_number,
                created_at, sla_target, team_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                monitor.url,
                monitor.name,
                plan,
                monitor.monitor_type,
                monitor.port,
                monitor.keyword,
                1 if monitor.keyword_should_exist else 0,
                json.dumps(monitor.custom_headers or {}),
                monitor.maintenance_starts_at,
                monitor.maintenance_ends_at,
                monitor.dns_hostname,
                monitor.dns_record_type,
                new_monitor_api_key,
                str(monitor.webhook_url) if monitor.webhook_url else None,
                str(monitor.slack_webhook_url) if monitor.slack_webhook_url else None,
                str(monitor.discord_webhook_url) if monitor.discord_webhook_url else None,
                sms_number,
                created_at,
                float(monitor.sla_target),
                team_id,
            ),
        )
        conn.commit()
        monitor_id = cursor.lastrowid

        created = conn.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,)).fetchone()

    return build_monitor_response(created)


@app.put("/monitors/{monitor_id}/alerts")
async def update_alert_workflows(
    monitor_id: int,
    alerts: AlertWorkflowUpdate,
    api_key: str = Depends(verify_api_key),
    x_user_email: Optional[str] = Header(None),
):
    """Configure custom alert workflows for a monitor (webhook, Slack, Discord, SMS)."""
    sms_number = validate_sms_number(alerts.sms_number) if alerts.sms_number else None
    update_fields: list[tuple[str, Optional[str]]] = [
        ("webhook_url", str(alerts.webhook_url) if alerts.webhook_url else None),
        ("slack_webhook_url", str(alerts.slack_webhook_url) if alerts.slack_webhook_url else None),
        ("discord_webhook_url", str(alerts.discord_webhook_url) if alerts.discord_webhook_url else None),
        ("sms_number", sms_number),
    ]

    # Require at least one field present in the request body.
    if all(
        getattr(alerts, field) is None
        for field in ["webhook_url", "slack_webhook_url", "discord_webhook_url", "sms_number"]
    ):
        raise_api_error(
            status_code=400,
            code="missing_alert_channels",
            message="Provide at least one alert channel field",
            hint="Send webhook_url, slack_webhook_url, discord_webhook_url, or sms_number.",
        )

    set_clause = ", ".join([f"{field} = ?" for field, _ in update_fields])
    values = [value for _, value in update_fields] + [monitor_id, api_key]

    with get_db() as conn:
        existing = conn.execute("SELECT team_id FROM monitors WHERE id = ? AND api_key = ?", (monitor_id, api_key)).fetchone()
        if not existing:
            raise_api_error(
                status_code=404,
                code="monitor_not_found",
                message=f"Monitor {monitor_id} was not found for this API key",
                hint="Check the monitor ID and ensure it belongs to the provided API key.",
            )
        existing_team_id = existing["team_id"] if existing["team_id"] else ensure_team_for_api_key(conn, api_key)
        require_team_permission(conn, api_key, x_user_email, existing_team_id, "write")

        cursor = conn.execute(
            f"UPDATE monitors SET {set_clause} WHERE id = ? AND api_key = ?",
            values,
        )
        conn.commit()

        if cursor.rowcount == 0:
            raise_api_error(
                status_code=404,
                code="monitor_not_found",
                message=f"Monitor {monitor_id} was not found for this API key",
                hint="Check the monitor ID and ensure it belongs to the provided API key.",
            )

        monitor = conn.execute(
            "SELECT webhook_url, slack_webhook_url, discord_webhook_url, sms_number FROM monitors WHERE id = ?",
            (monitor_id,),
        ).fetchone()

    return {
        "message": "Alert workflows updated",
        "monitor_id": monitor_id,
        "alerts": {
            "webhook_url": monitor["webhook_url"],
            "slack_webhook_url": monitor["slack_webhook_url"],
            "discord_webhook_url": monitor["discord_webhook_url"],
            "sms_number": monitor["sms_number"],
        },
    }


@app.get("/monitors", response_model=List[MonitorResponse])
async def list_monitors(api_key: str = Depends(verify_api_key), x_user_email: Optional[str] = Header(None)):
    """List all monitors for the authenticated user."""
    with get_db() as conn:
        team_ids = get_accessible_team_ids(conn, api_key, x_user_email)
        placeholders = ",".join("?" for _ in team_ids)
        monitors = conn.execute(
            """
            SELECT *
            FROM monitors
            WHERE team_id IN ({team_ids})
               OR (team_id IS NULL AND api_key = ?)
            ORDER BY created_at DESC
        """.format(team_ids=placeholders),
            [*team_ids, api_key],
        ).fetchall()

    return [build_monitor_response(m) for m in monitors]


@app.get("/monitors/{monitor_id}", response_model=MonitorResponse)
async def get_monitor(
    monitor_id: int,
    api_key: str = Depends(verify_api_key),
    x_user_email: Optional[str] = Header(None),
):
    """Get a specific monitor."""
    with get_db() as conn:
        monitor = conn.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,)).fetchone()

    if not monitor:
        raise_api_error(
            status_code=404,
            code="monitor_not_found",
            message=f"Monitor {monitor_id} was not found for this API key",
            hint="Check the monitor ID and ensure it belongs to the provided API key.",
        )

    with get_db() as conn:
        resource_team_id = monitor["team_id"] if "team_id" in monitor.keys() and monitor["team_id"] else ensure_team_for_api_key(
            conn, monitor["api_key"]
        )
        require_team_permission(conn, api_key, x_user_email, resource_team_id, "read")

    return build_monitor_response(monitor)


@app.delete("/monitors/{monitor_id}", status_code=204)
async def delete_monitor(
    monitor_id: int,
    api_key: str = Depends(verify_api_key),
    x_user_email: Optional[str] = Header(None),
):
    """Delete a monitor."""
    with get_db() as conn:
        existing = conn.execute("SELECT id, api_key, team_id FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
        if not existing:
            raise_api_error(
                status_code=404,
                code="monitor_not_found",
                message=f"Monitor {monitor_id} was not found for this API key",
                hint="Check the monitor ID and ensure it belongs to the provided API key.",
            )

        resource_team_id = existing["team_id"] if existing["team_id"] else ensure_team_for_api_key(conn, existing["api_key"])
        require_team_permission(conn, api_key, x_user_email, resource_team_id, "delete")

        cursor = conn.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
        conn.commit()

        if cursor.rowcount == 0:
            raise_api_error(
                status_code=404,
                code="monitor_not_found",
                message=f"Monitor {monitor_id} was not found for this API key",
                hint="Check the monitor ID and ensure it belongs to the provided API key.",
            )

    return None


@app.get("/monitors/{monitor_id}/checks", response_model=List[CheckResponse])
async def get_monitor_checks(
    monitor_id: int,
    api_key: str = Depends(verify_api_key),
    x_user_email: Optional[str] = Header(None),
    limit: int = 100,
):
    """Get recent checks for a monitor."""
    with get_db() as conn:
        monitor = conn.execute("SELECT id, api_key, team_id FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
        if not monitor:
            raise_api_error(
                status_code=404,
                code="monitor_not_found",
                message=f"Monitor {monitor_id} was not found for this API key",
                hint="Check the monitor ID and ensure it belongs to the provided API key.",
            )
        resource_team_id = monitor["team_id"] if monitor["team_id"] else ensure_team_for_api_key(conn, monitor["api_key"])
        require_team_permission(conn, api_key, x_user_email, resource_team_id, "read")

        checks = conn.execute(
            """
            SELECT * FROM checks
            WHERE monitor_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """,
            (monitor_id, limit),
        ).fetchall()

    return [
        CheckResponse(
            id=c["id"],
            monitor_id=c["monitor_id"],
            timestamp=c["timestamp"],
            status_code=c["status_code"],
            response_time=c["response_time"],
            success=bool(c["success"]),
        )
        for c in checks
    ]


@app.get("/monitors/{monitor_id}/health", response_model=HealthScoreResponse)
async def get_monitor_health_score(
    monitor_id: int,
    api_key: str = Depends(verify_api_key),
    x_user_email: Optional[str] = Header(None),
):
    """Get a simple monitor health score based on uptime percentage."""
    now = int(time.time())
    since_24h = now - 86400

    with get_db() as conn:
        monitor = conn.execute(
            """
            SELECT id, name, status, total_checks, successful_checks, api_key, team_id
            FROM monitors
            WHERE id = ?
        """,
            (monitor_id,),
        ).fetchone()

        if not monitor:
            raise_api_error(
                status_code=404,
                code="monitor_not_found",
                message=f"Monitor {monitor_id} was not found for this API key",
                hint="Check the monitor ID and ensure it belongs to the provided API key.",
            )

        resource_team_id = monitor["team_id"] if monitor["team_id"] else ensure_team_for_api_key(conn, monitor["api_key"])
        require_team_permission(conn, api_key, x_user_email, resource_team_id, "read")

        recent = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(success), 0) AS successful
            FROM checks
            WHERE monitor_id = ?
              AND timestamp >= ?
        """,
            (monitor_id, since_24h),
        ).fetchone()

    uptime_percentage = 0.0
    if monitor["total_checks"] > 0:
        uptime_percentage = (monitor["successful_checks"] / monitor["total_checks"]) * 100

    recent_uptime = uptime_percentage
    if recent["total"] > 0:
        recent_uptime = (recent["successful"] / recent["total"]) * 100

    return HealthScoreResponse(
        monitor_id=monitor["id"],
        monitor_name=monitor["name"],
        current_status=monitor["status"],
        health_score=round(recent_uptime, 2),
        uptime_percentage=round(uptime_percentage, 2),
        recent_uptime_24h=round(recent_uptime, 2),
        checks_24h=recent["total"],
        total_checks=monitor["total_checks"],
    )


@app.get("/monitors/{monitor_id}/alerts/timeline", response_model=List[AlertEventResponse])
async def get_alert_timeline(
    monitor_id: int,
    api_key: str = Depends(verify_api_key),
    x_user_email: Optional[str] = Header(None),
    limit: int = 50,
):
    """Get recent alert events (status transitions) for a monitor."""
    safe_limit = max(1, min(limit, 200))

    with get_db() as conn:
        monitor = conn.execute(
            "SELECT id, api_key, team_id FROM monitors WHERE id = ?",
            (monitor_id,),
        ).fetchone()

        if not monitor:
            raise_api_error(
                status_code=404,
                code="monitor_not_found",
                message=f"Monitor {monitor_id} was not found for this API key",
                hint="Check the monitor ID and ensure it belongs to the provided API key.",
            )

        resource_team_id = monitor["team_id"] if monitor["team_id"] else ensure_team_for_api_key(conn, monitor["api_key"])
        require_team_permission(conn, api_key, x_user_email, resource_team_id, "read")

        events = conn.execute(
            """
            SELECT id, monitor_id, timestamp, previous_status, current_status,
                   status_code, response_time, false_positive_filtered
            FROM alert_events
            WHERE monitor_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """,
            (monitor_id, safe_limit),
        ).fetchall()

    return [
        AlertEventResponse(
            id=e["id"],
            monitor_id=e["monitor_id"],
            timestamp=e["timestamp"],
            previous_status=e["previous_status"],
            current_status=e["current_status"],
            status_code=e["status_code"],
            response_time=e["response_time"],
            false_positive_filtered=bool(e["false_positive_filtered"]),
        )
        for e in events
    ]


@app.post("/monitors/{monitor_id}/check", status_code=202)
async def trigger_check(
    monitor_id: int,
    background_tasks: BackgroundTasks,
    api_key: str = Depends(verify_api_key),
    x_user_email: Optional[str] = Header(None),
):
    """Manually trigger a check for a monitor."""
    with get_db() as conn:
        monitor = conn.execute("SELECT id, api_key, team_id FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
        if not monitor:
            raise_api_error(
                status_code=404,
                code="monitor_not_found",
                message=f"Monitor {monitor_id} was not found for this API key",
                hint="Check the monitor ID and ensure it belongs to the provided API key.",
            )
        resource_team_id = monitor["team_id"] if monitor["team_id"] else ensure_team_for_api_key(conn, monitor["api_key"])
        require_team_permission(conn, api_key, x_user_email, resource_team_id, "write")

    background_tasks.add_task(perform_check, monitor_id)

    return {"message": "Check triggered", "monitor_id": monitor_id}


# ============================================================================
# Team + SSO Endpoints
# ============================================================================

@app.get("/dashboard")
async def dashboard(request: Request, api_key: str = Depends(verify_api_key)):
    """Simple dashboard route gated by optional SAML session policy."""
    with get_db() as conn:
        team_id = ensure_team_for_api_key(conn, api_key)
        saml_cfg = conn.execute(
            "SELECT enabled FROM saml_config WHERE team_id = ? LIMIT 1",
            (team_id,),
        ).fetchone()

    saml_enabled = bool(saml_cfg and saml_cfg["enabled"])
    if (DASHBOARD_REQUIRE_SAML or saml_enabled) and not request.session.get("saml_user"):
        return RedirectResponse(url=SAML_LOGIN_PATH, status_code=302)

    dashboard_html = load_static_html("dashboard.html") or load_static_html("dashboard-preview.html")
    if dashboard_html:
        return HTMLResponse(content=dashboard_html)
    return {"message": "Dashboard route is ready", "team_id": team_id, "saml_enabled": saml_enabled}


@app.post("/teams/invite")
async def invite_team_member(
    payload: TeamInviteCreate,
    api_key: str = Depends(verify_api_key),
    x_user_email: Optional[str] = Header(None),
):
    """Invite a team member by email with role-based access."""
    invite_email = validate_subscriber_email(payload.email)
    now = int(time.time())
    with get_db() as conn:
        team_id, _ = require_team_permission(conn, api_key, x_user_email, None, "invite")
        user = upsert_user(conn, invite_email, payload.name, default_role=payload.role)
        token = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO team_members (team_id, user_id, role, status, invite_token, invited_by_api_key, created_at)
            VALUES (?, ?, ?, 'invited', ?, ?, ?)
            ON CONFLICT(team_id, user_id) DO UPDATE SET
                role = excluded.role,
                status = 'invited',
                invite_token = excluded.invite_token,
                invited_by_api_key = excluded.invited_by_api_key,
                accepted_at = NULL
        """,
            (team_id, user["id"], payload.role, token, api_key, now),
        )
        conn.commit()

    return {
        "message": "Invitation created",
        "team_id": team_id,
        "email": invite_email,
        "role": payload.role,
        "invite_token": token,
        "accept_url": f"/teams/invitations/{token}/accept",
    }


@app.post("/teams/invitations/{invite_token}/accept")
async def accept_team_invite(invite_token: str, payload: TeamInvitationAccept):
    """Accept a team invite by token and activate membership."""
    email = validate_subscriber_email(payload.email)
    now = int(time.time())
    with get_db() as conn:
        membership = conn.execute(
            """
            SELECT tm.id, tm.team_id, tm.user_id, tm.role, tm.status, u.email AS user_email
            FROM team_members tm
            JOIN users u ON u.id = tm.user_id
            WHERE tm.invite_token = ?
            LIMIT 1
        """,
            (invite_token,),
        ).fetchone()
        if not membership:
            raise_api_error(
                status_code=404,
                code="invite_not_found",
                message="Invite token is invalid or expired",
            )

        if membership["status"] != "invited":
            return {
                "message": "Invite already accepted",
                "team_id": membership["team_id"],
                "role": membership["role"],
                "email": membership["user_email"],
            }

        user = upsert_user(conn, email, payload.name, default_role=membership["role"])
        conn.execute(
            """
            UPDATE team_members
            SET user_id = ?, status = 'active', accepted_at = ?, invite_token = NULL
            WHERE id = ?
        """,
            (user["id"], now, membership["id"]),
        )
        conn.commit()

    return {
        "message": "Invite accepted",
        "team_id": membership["team_id"],
        "role": membership["role"],
        "email": email,
    }


@app.get("/teams/members")
async def list_team_members(api_key: str = Depends(verify_api_key), x_user_email: Optional[str] = Header(None)):
    with get_db() as conn:
        team_id, _ = require_team_permission(conn, api_key, x_user_email, None, "read")
        rows = conn.execute(
            """
            SELECT u.email, u.name, tm.role, tm.status, tm.created_at, tm.accepted_at
            FROM team_members tm
            JOIN users u ON u.id = tm.user_id
            WHERE tm.team_id = ?
            ORDER BY tm.created_at DESC
        """,
            (team_id,),
        ).fetchall()
    members = [
        {
            "email": row["email"],
            "name": row["name"],
            "role": row["role"],
            "status": row["status"],
            "invited_at": row["created_at"],
            "accepted_at": row["accepted_at"],
        }
        for row in rows
    ]
    return {"team_id": team_id, "members": members, "count": len(members)}


@app.get("/teams/dashboard")
async def get_team_dashboard(api_key: str = Depends(verify_api_key), x_user_email: Optional[str] = Header(None)):
    """Team dashboard with monitor summary, uptime, and alert stats."""
    import datetime
    today = datetime.datetime.now().date().isoformat()
    with get_db() as conn:
        team_id, _ = require_team_permission(conn, api_key, x_user_email, None, "read")

        # Monitors count
        monitors = conn.execute(
            "SELECT id, name, monitor_type, is_up, enabled FROM monitors WHERE team_id = ?",
            (team_id,),
        ).fetchall()
        monitor_count = len(monitors)
        enabled_count = sum(1 for m in monitors if m["enabled"])
        up_count = sum(1 for m in monitors if m["is_up"])
        down_count = monitor_count - up_count

        # Checks today
        monitor_ids = [m["id"] for m in monitors]
        checks_today = 0
        failed_today = 0
        if monitor_ids:
            placeholders = ",".join("?" * len(monitor_ids))
            checks_today = conn.execute(
                f"SELECT COUNT(*) as cnt FROM checks WHERE monitor_id IN ({placeholders}) AND date(created_at) = ?",
                (*monitor_ids, today),
            ).fetchone()["cnt"]
            failed_today = conn.execute(
                f"SELECT COUNT(*) as cnt FROM checks WHERE monitor_id IN ({placeholders}) AND date(created_at) = ? AND is_up = 0",
                (*monitor_ids, today),
            ).fetchone()["cnt"]

        # Alerts this week
        week_ago = (datetime.datetime.now() - datetime.timedelta(days=7)).date().isoformat()
        alerts_this_week = 0
        if monitor_ids:
            placeholders = ",".join("?" * len(monitor_ids))
            alerts_this_week = conn.execute(
                f"SELECT COUNT(*) as cnt FROM alert_events WHERE monitor_id IN ({placeholders}) AND date(created_at) >= ?",
                (*monitor_ids, week_ago),
            ).fetchone()["cnt"]

    uptime_pct = round((checks_today - failed_today) / checks_today * 100, 2) if checks_today > 0 else 100.0

    return {
        "team_id": team_id,
        "date": today,
        "monitors": {
            "total": monitor_count,
            "enabled": enabled_count,
            "up": up_count,
            "down": down_count,
        },
        "checks_today": checks_today,
        "failed_checks_today": failed_today,
        "uptime_pct": uptime_pct,
        "alerts_this_week": alerts_this_week,
    }


@app.put("/auth/saml/config")
async def upsert_saml_config(
    payload: SAMLConfigUpsert,
    api_key: str = Depends(verify_api_key),
    x_user_email: Optional[str] = Header(None),
):
    with get_db() as conn:
        team_id, _ = require_team_permission(conn, api_key, x_user_email, None, "invite")
        now = int(time.time())
        conn.execute(
            """
            INSERT INTO saml_config (
                team_id, enabled, sp_entity_id, sp_acs_url, sp_sls_url, idp_entity_id, idp_sso_url,
                idp_slo_url, idp_x509_cert, idp_metadata_xml, attribute_email, attribute_name,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(team_id) DO UPDATE SET
                enabled = excluded.enabled,
                sp_entity_id = excluded.sp_entity_id,
                sp_acs_url = excluded.sp_acs_url,
                sp_sls_url = excluded.sp_sls_url,
                idp_entity_id = excluded.idp_entity_id,
                idp_sso_url = excluded.idp_sso_url,
                idp_slo_url = excluded.idp_slo_url,
                idp_x509_cert = excluded.idp_x509_cert,
                idp_metadata_xml = excluded.idp_metadata_xml,
                attribute_email = excluded.attribute_email,
                attribute_name = excluded.attribute_name,
                updated_at = excluded.updated_at
        """,
            (
                team_id,
                1 if payload.enabled else 0,
                payload.sp_entity_id.strip(),
                str(payload.sp_acs_url),
                str(payload.sp_sls_url) if payload.sp_sls_url else None,
                payload.idp_entity_id.strip(),
                str(payload.idp_sso_url),
                str(payload.idp_slo_url) if payload.idp_slo_url else None,
                payload.idp_x509_cert.strip(),
                payload.idp_metadata_xml,
                payload.attribute_email.strip(),
                payload.attribute_name.strip(),
                now,
                now,
            ),
        )
        conn.commit()

    return {"message": "SAML config saved", "team_id": team_id, "enabled": payload.enabled}


@app.get("/auth/saml/config")
async def get_saml_config(api_key: str = Depends(verify_api_key), x_user_email: Optional[str] = Header(None)):
    with get_db() as conn:
        team_id, _ = require_team_permission(conn, api_key, x_user_email, None, "read")
        cfg = conn.execute("SELECT * FROM saml_config WHERE team_id = ?", (team_id,)).fetchone()
        if not cfg:
            return {"team_id": team_id, "configured": False}
        return {
            "team_id": team_id,
            "configured": True,
            "enabled": bool(cfg["enabled"]),
            "sp_entity_id": cfg["sp_entity_id"],
            "sp_acs_url": cfg["sp_acs_url"],
            "sp_sls_url": cfg["sp_sls_url"],
            "idp_entity_id": cfg["idp_entity_id"],
            "idp_sso_url": cfg["idp_sso_url"],
            "idp_slo_url": cfg["idp_slo_url"],
            "attribute_email": cfg["attribute_email"],
            "attribute_name": cfg["attribute_name"],
        }


@app.get("/auth/saml/login")
async def saml_login(request: Request, api_key: str = Depends(verify_api_key)):
    if not SAML_LIB_AVAILABLE:
        raise_api_error(
            status_code=503,
            code="saml_library_missing",
            message="python3-saml is not installed",
            hint="Install python3-saml in the runtime environment.",
        )

    with get_db() as conn:
        team_id = ensure_team_for_api_key(conn, api_key)
        cfg = conn.execute("SELECT * FROM saml_config WHERE team_id = ? AND enabled = 1", (team_id,)).fetchone()
        if not cfg:
            raise_api_error(
                status_code=404,
                code="saml_not_configured",
                message="No enabled SAML configuration for this team",
            )

    request.session["saml_team_id"] = team_id
    auth = OneLogin_Saml2_Auth(build_saml_req_dict(request), build_saml_settings(cfg))
    redirect_url = auth.login(return_to="/dashboard")
    return RedirectResponse(url=redirect_url, status_code=302)


@app.post("/auth/saml/acs")
async def saml_acs(request: Request):
    if not SAML_LIB_AVAILABLE:
        raise_api_error(status_code=503, code="saml_library_missing", message="python3-saml is not installed")

    team_id = request.session.get("saml_team_id")
    if not team_id:
        raise_api_error(
            status_code=400,
            code="saml_session_missing",
            message="SAML session context is missing",
            hint="Start at /auth/saml/login first.",
        )

    form_data = dict(await request.form())
    with get_db() as conn:
        cfg = conn.execute("SELECT * FROM saml_config WHERE team_id = ? AND enabled = 1", (team_id,)).fetchone()
        if not cfg:
            raise_api_error(status_code=404, code="saml_not_configured", message="SAML config was not found")

        auth = OneLogin_Saml2_Auth(build_saml_req_dict(request, form_data), build_saml_settings(cfg))
        auth.process_response()
        errors = auth.get_errors()
        if errors:
            raise_api_error(
                status_code=401,
                code="saml_assertion_invalid",
                message="SAML response validation failed",
                details={"errors": errors},
            )

        attrs = auth.get_attributes() or {}
        email_attr = cfg["attribute_email"] or "email"
        name_attr = cfg["attribute_name"] or "name"
        email_values = attrs.get(email_attr) or []
        name_values = attrs.get(name_attr) or []
        email = (email_values[0] if email_values else auth.get_nameid() or "").strip().lower()
        if not email:
            raise_api_error(
                status_code=400,
                code="saml_email_missing",
                message="Unable to map SAML response to a user email",
                hint=f"Check attribute mapping for '{email_attr}'.",
            )

        user_name = (name_values[0] if name_values else "").strip() or None
        user = upsert_user(conn, email, user_name, default_role="viewer")
        now = int(time.time())

        member = conn.execute(
            "SELECT id, role, status FROM team_members WHERE team_id = ? AND user_id = ?",
            (team_id, user["id"]),
        ).fetchone()
        if not member:
            conn.execute(
                """
                INSERT INTO team_members (team_id, user_id, role, status, invited_by_api_key, created_at, accepted_at)
                VALUES (?, ?, 'viewer', 'active', 'saml', ?, ?)
            """,
                (team_id, user["id"], now, now),
            )
            role = "viewer"
        else:
            role = member["role"]
            if member["status"] != "active":
                conn.execute(
                    "UPDATE team_members SET status = 'active', accepted_at = ?, invite_token = NULL WHERE id = ?",
                    (now, member["id"]),
                )

        conn.execute("UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?", (now, now, user["id"]))
        conn.commit()

    request.session["saml_user"] = {
        "email": email,
        "name": user_name,
        "team_id": team_id,
        "role": role,
        "logged_in_at": int(time.time()),
    }
    return RedirectResponse(url="/dashboard", status_code=302)


@app.post("/auth/saml/logout")
async def saml_logout(request: Request):
    request.session.pop("saml_user", None)
    request.session.pop("saml_team_id", None)
    return {"message": "Logged out"}


@app.get("/auth/saml/me")
async def saml_me(request: Request):
    user = request.session.get("saml_user")
    if not user:
        raise_api_error(status_code=401, code="saml_not_authenticated", message="No active SAML session")
    return {"authenticated": True, "user": user}


# ============================================================================
# Revenue Attribution Endpoints
# ============================================================================

@app.post("/signup", status_code=201)
async def create_signup(signup: SignupCreate, request: Request):
    """Create a monitor and optionally bootstrap a status page in one request."""
    sms_number = validate_sms_number(signup.sms_number) if signup.sms_number else None
    validate_monitor_payload(
        monitor_type=signup.monitor_type,
        url=signup.url,
        port=signup.port,
        keyword=signup.keyword,
        dns_hostname=signup.dns_hostname,
        maintenance_starts_at=signup.maintenance_starts_at,
        maintenance_ends_at=signup.maintenance_ends_at,
        sms_number=sms_number,
        sla_target=signup.sla_target,
    )

    api_key = secrets.token_urlsafe(32)
    created_at = int(time.time())
    normalized_email = validate_subscriber_email(signup.email) if signup.email else None
    offer_program = load_business_offer_program()
    attribution = build_attribution_context(
        request=request,
        payload=signup.model_dump(),
        default_source=str(offer_program.get("signup", {}).get("default_source", "")),
        default_channel=str(offer_program.get("signup", {}).get("default_channel", "")),
        default_campaign=str(offer_program.get("signup", {}).get("default_campaign", "")),
    )

    monitor_id = -1
    status_page_payload: Optional[dict[str, Any]] = None
    with get_db() as conn:
        team_id = ensure_team_for_api_key(conn, api_key)
        cursor = conn.execute(
            """
            INSERT INTO monitors (
                url, name, plan, monitor_type, port, keyword, keyword_should_exist, custom_headers,
                maintenance_starts_at, maintenance_ends_at, dns_hostname, dns_record_type,
                api_key, webhook_url, slack_webhook_url, discord_webhook_url, sms_number,
                email, source, channel, campaign, experiment_id, variant_id, cohort, session_id,
                created_at, sla_target, team_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                signup.url,
                signup.name,
                canonicalize_plan(signup.plan),
                signup.monitor_type,
                signup.port,
                signup.keyword,
                1 if signup.keyword_should_exist else 0,
                json.dumps(signup.custom_headers or {}),
                signup.maintenance_starts_at,
                signup.maintenance_ends_at,
                signup.dns_hostname,
                signup.dns_record_type,
                api_key,
                str(signup.webhook_url) if signup.webhook_url else None,
                str(signup.slack_webhook_url) if signup.slack_webhook_url else None,
                str(signup.discord_webhook_url) if signup.discord_webhook_url else None,
                sms_number,
                normalized_email,
                attribution.source,
                attribution.channel,
                attribution.campaign,
                attribution.experiment_id,
                attribution.variant_id,
                attribution.cohort,
                attribution.session_id,
                created_at,
                float(signup.sla_target),
                team_id,
            ),
        )
        monitor_id = cursor.lastrowid

        if signup.create_status_page:
            page_title = (signup.status_page_title or f"{signup.name} Status").strip()
            if not page_title:
                page_title = f"{signup.name} Status"
            page_description = (
                signup.status_page_description.strip()
                if signup.status_page_description
                else f"Live status updates for {signup.name}."
            )
            requested_slug = signup.status_page_slug.strip().lower() if signup.status_page_slug else None
            status_page_slug = generate_unique_status_page_slug(conn, page_title, requested_slug)

            page_cursor = conn.execute(
                """
                INSERT INTO status_pages (
                    owner_api_key, slug, title, description, template_name, theme_color, logo_url,
                    accent_color, background_color, text_color, custom_domain, team_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    api_key,
                    status_page_slug,
                    page_title,
                    page_description,
                    DEFAULT_STATUS_PAGE_TEMPLATE_NAME,
                    DEFAULT_STATUS_PAGE_COLORS["accent_color"],
                    None,
                    DEFAULT_STATUS_PAGE_COLORS["accent_color"],
                    DEFAULT_STATUS_PAGE_COLORS["background_color"],
                    DEFAULT_STATUS_PAGE_COLORS["text_color"],
                    None,
                    team_id,
                    created_at,
                    created_at,
                ),
            )
            status_page_id = page_cursor.lastrowid
            conn.execute(
                """
                INSERT INTO status_page_monitors (status_page_id, monitor_id, created_at)
                VALUES (?, ?, ?)
            """,
                (status_page_id, monitor_id, created_at),
            )

            links = build_status_page_public_urls(status_page_slug)
            status_page_payload = {
                "id": status_page_id,
                "slug": status_page_slug,
                "title": page_title,
                "public_path": links["path_url"],
                "public_url": links["canonical_url"],
                "subdomain_url": links["subdomain_url"],
                "show_powered_by_badge": canonicalize_plan(signup.plan) == "free",
            }

        if business_arena_enabled():
            record_business_event(
                conn,
                event_name="signup_success",
                attribution=attribution,
                surface="signup",
                value=0.55,
                request=request,
                monitor_id=monitor_id,
                plan=canonicalize_plan(signup.plan),
                metadata=enrich_trust_bar_metadata(
                    surface="signup",
                    offer_program=offer_program,
                    metadata={
                        "create_status_page": signup.create_status_page,
                        "monitor_type": signup.monitor_type,
                    },
                ),
            )
            if status_page_payload:
                record_business_event(
                    conn,
                    event_name="status_page_created",
                    attribution=attribution,
                    surface="signup",
                    value=0.70,
                    request=request,
                    monitor_id=monitor_id,
                    plan=canonicalize_plan(signup.plan),
                    metadata=enrich_trust_bar_metadata(
                        surface="signup",
                        offer_program=offer_program,
                        metadata={"status_page_slug": status_page_payload["slug"]},
                    ),
                )

        conn.commit()

    response_payload = {
        "id": monitor_id,
        "api_key": api_key,
        "plan": canonicalize_plan(signup.plan),
        "monitor_type": signup.monitor_type,
        "message": "Monitor created successfully",
        "email": normalized_email,
        "source": attribution.source,
        "channel": attribution.channel,
        "campaign": attribution.campaign,
        "experiment_id": attribution.experiment_id,
        "variant_id": attribution.variant_id,
        "cohort": attribution.cohort,
        "session_id": attribution.session_id,
        "create_status_page": signup.create_status_page,
    }
    if status_page_payload:
        response_payload["message"] = "Monitor and status page created successfully"
        response_payload["status_page"] = status_page_payload
        response_payload["status_page_url"] = status_page_payload["public_url"]
        response_payload["status_page_subdomain_url"] = status_page_payload["subdomain_url"]
    maybe_write_business_report()
    return response_payload


@app.get("/internal/signups")
async def get_signups(since: int = 0):
    """Internal API for revenue_arena to check new signups since timestamp."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                id, name, url, plan, monitor_type, email, source, channel, campaign,
                experiment_id, variant_id, cohort, session_id, created_at
            FROM monitors
            WHERE created_at > ?
            ORDER BY created_at DESC
        """,
            (since,),
        ).fetchall()

    signups = []
    for row in rows:
        signups.append(
            {
                "id": row["id"],
                "name": row["name"],
                "url": row["url"],
                "plan": row["plan"],
                "monitor_type": row["monitor_type"] or "http",
                "email": row["email"],
                "source": row["source"],
                "channel": row["channel"],
                "campaign": row["campaign"],
                "experiment_id": row["experiment_id"],
                "variant_id": row["variant_id"],
                "cohort": row["cohort"],
                "session_id": row["session_id"],
                "created_at": row["created_at"],
            }
        )

    return {"signups": signups, "count": len(signups), "since": since}


@app.get("/internal/stats")
async def get_daily_stats():
    """Internal API for daily monitoring stats (checks, uptime, alerts)."""
    import datetime

    today_start = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    since = int(today_start.timestamp())
    with get_db() as conn:
        total_monitors = conn.execute("SELECT COUNT(*) as cnt FROM monitors").fetchone()["cnt"]
        checks_today = conn.execute(
            "SELECT COUNT(*) as cnt FROM checks WHERE timestamp >= ?",
            (since,),
        ).fetchone()["cnt"]
        failed_today = conn.execute(
            "SELECT COUNT(*) as cnt FROM checks WHERE timestamp >= ? AND success = 0",
            (since,),
        ).fetchone()["cnt"]
        alerts_today = conn.execute(
            "SELECT COUNT(*) as cnt FROM alert_events WHERE timestamp >= ?",
            (since,),
        ).fetchone()["cnt"]
        signups_today = conn.execute(
            "SELECT COUNT(*) as cnt FROM monitors WHERE created_at >= ?",
            (since,),
        ).fetchone()["cnt"]
        status_pages_today = conn.execute(
            "SELECT COUNT(*) as cnt FROM status_pages WHERE created_at >= ?",
            (since,),
        ).fetchone()["cnt"]
        arena_rewards = fetch_reward_summary(conn, since=since) if business_arena_enabled() else []

    return {
        "date": today_start.date().isoformat(),
        "total_monitors": total_monitors,
        "checks_today": checks_today,
        "failed_checks_today": failed_today,
        "alerts_today": alerts_today,
        "signups_today": signups_today,
        "status_pages_today": status_pages_today,
        "arena_rewards": arena_rewards,
        "uptime_pct": round((checks_today - failed_today) / checks_today * 100, 2) if checks_today > 0 else 100.0,
    }


@app.post("/internal/arena/events", status_code=202)
async def create_arena_event(event: ArenaEventCreate, request: Request):
    """Append an arena telemetry event from OwlPulse-owned pages or flows."""
    if not business_arena_enabled():
        raise_api_error(
            status_code=503,
            code="business_arena_unavailable",
            message="Business arena integration is unavailable",
            details={"import_error": BUSINESS_ARENA_IMPORT_ERROR},
        )

    offer_program = load_business_offer_program()
    attribution = build_attribution_context(
        request=request,
        payload=event.model_dump(),
        default_source=str(offer_program.get("signup", {}).get("default_source", "")),
        default_channel=str(offer_program.get("signup", {}).get("default_channel", "")),
        default_campaign=str(offer_program.get("signup", {}).get("default_campaign", "")),
    )
    with get_db() as conn:
        record_business_event(
            conn,
            event_name=event.event_name,
            attribution=attribution,
            surface=event.surface or "",
            value=float(event.value),
            request=request,
            path=event.path or "",
            monitor_id=event.monitor_id,
            plan=event.plan or "",
            metadata=enrich_trust_bar_metadata(
                surface=event.surface or "",
                offer_program=offer_program,
                metadata=event.metadata,
            ),
        )
    maybe_write_business_report()
    return {
        "accepted": True,
        "event_name": event.event_name,
        "session_id": attribution.session_id,
        "experiment_id": attribution.experiment_id,
        "variant_id": attribution.variant_id,
    }


@app.get("/internal/arena/rewards")
async def get_arena_rewards(since: int = 0, experiment_id: str = "", variant_id: str = ""):
    if not business_arena_enabled():
        raise_api_error(status_code=503, code="business_arena_unavailable", message="Business arena integration is unavailable")
    with get_db() as conn:
        rewards = fetch_reward_summary(
            conn,
            since=since,
            experiment_id=experiment_id or None,
            variant_id=variant_id or None,
        )
    return {"since": since, "count": len(rewards), "rewards": rewards}


@app.get("/internal/arena/snapshot")
async def get_arena_snapshot():
    if not business_arena_enabled():
        raise_api_error(status_code=503, code="business_arena_unavailable", message="Business arena integration is unavailable")
    adapter = OwlPulseAdapter(OfferProgram.from_dict(load_business_offer_program()), DATABASE)
    return adapter.snapshot()


@app.get("/internal/arena/experiments/{experiment_id}")
async def get_arena_experiment(experiment_id: str):
    if not business_arena_enabled():
        raise_api_error(status_code=503, code="business_arena_unavailable", message="Business arena integration is unavailable")
    with get_db() as conn:
        detail = fetch_experiment_detail(DEFAULT_ARENA_DB_PATH, conn, experiment_id)
    if not detail:
        raise_api_error(
            status_code=404,
            code="arena_experiment_not_found",
            message=f"Experiment '{experiment_id}' was not found",
        )
    return detail


@app.get("/internal/arena/welcome-email-preview", response_class=HTMLResponse)
async def get_welcome_email_preview():
    if not business_arena_enabled():
        raise_api_error(status_code=503, code="business_arena_unavailable", message="Business arena integration is unavailable")
    template_html = (Path(__file__).resolve().parent / "static" / "emails" / "welcome.html").read_text(encoding="utf-8")
    program = OfferProgram.from_dict(load_business_offer_program())
    return HTMLResponse(content=render_welcome_email_html(template_html, program))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
