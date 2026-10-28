"""Internal database (Law 2 — the safety engine).

Local-first implementation: a single SQLite database standing in for the
production PostgreSQL (India region) + pgvector stack. The repository
enforces **client scoping** the way Postgres row-level security would:
every read/write goes through a connection bound to one ``client_id``, and
cross-client access is structurally impossible (and tested).

The production Postgres schema (with real RLS policies) ships in
``sql/schema.sql`` — same tables, same partitioning, real RLS.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA = """
-- Clients (partitioned by client_id in production)
CREATE TABLE IF NOT EXISTS clients (
    client_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    tier        TEXT NOT NULL DEFAULT 'B',   -- A | B | C
    vertical    TEXT,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS client_accounts (
    client_id   TEXT NOT NULL,
    platform    TEXT NOT NULL,               -- gsc | ga4 | google_ads | meta | ...
    vault_ref   TEXT NOT NULL,               -- vault ref only — never the secret
    created_at  REAL NOT NULL,
    PRIMARY KEY (client_id, platform)
);
CREATE TABLE IF NOT EXISTS client_brand_kit (
    client_id   TEXT NOT NULL,
    key         TEXT NOT NULL,               -- voice | palette | logo | tone ...
    value       TEXT NOT NULL,
    version     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (client_id, key, version)
);

-- Work: every agent action is a task row
CREATE TABLE IF NOT EXISTS projects (
    project_id  TEXT PRIMARY KEY,
    client_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    project_id  TEXT,
    client_id   TEXT NOT NULL,
    agent       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed
    payload     TEXT,
    result      TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL
);
CREATE TABLE IF NOT EXISTS workflows (
    workflow_id TEXT PRIMARY KEY,
    client_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'running',  -- running|paused|done|failed
    steps       TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL
);

-- Content
CREATE TABLE IF NOT EXISTS content_assets (
    asset_id    TEXT PRIMARY KEY,
    client_id   TEXT NOT NULL,
    type        TEXT NOT NULL,               -- blog | ad | email | social | video ...
    status      TEXT NOT NULL DEFAULT 'draft',-- draft|review|live
    title       TEXT,
    body        TEXT,
    sources     TEXT,                        -- JSON list of source refs (grounding)
    created_at  REAL NOT NULL,
    updated_at  REAL
);
CREATE TABLE IF NOT EXISTS content_briefs (
    brief_id    TEXT PRIMARY KEY,
    client_id   TEXT NOT NULL,
    type        TEXT NOT NULL,
    keyword     TEXT,
    cta         TEXT,
    status      TEXT NOT NULL DEFAULT 'open', -- open|approved|done
    payload     TEXT,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS content_calendar (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   TEXT NOT NULL,
    asset_type  TEXT NOT NULL,
    slot        TEXT NOT NULL,               -- ISO date
    brief_id    TEXT,
    status      TEXT NOT NULL DEFAULT 'planned'
);

-- Media
CREATE TABLE IF NOT EXISTS media_assets (
    asset_id    TEXT PRIMARY KEY,
    client_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,               -- image | video | audio
    storage_ref TEXT NOT NULL,               -- object storage ref
    license     TEXT NOT NULL DEFAULT 'agency-owned',
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS creative_briefs (
    brief_id    TEXT PRIMARY KEY,
    client_id   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    payload     TEXT,
    created_at  REAL NOT NULL
);

-- Analytics: the one place numbers live
CREATE TABLE IF NOT EXISTS metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   TEXT NOT NULL,
    source      TEXT NOT NULL,               -- ga4 | google_ads | meta | internal
    metric      TEXT NOT NULL,
    value       REAL NOT NULL,
    period      TEXT NOT NULL,               -- ISO date
    ts          REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dashboards (
    dashboard_id TEXT PRIMARY KEY,
    client_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    spec        TEXT,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    client_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',
    payload     TEXT,
    created_at  REAL NOT NULL
);

-- CRM (agency-level)
CREATE TABLE IF NOT EXISTS leads (
    lead_id     TEXT PRIMARY KEY,
    name        TEXT,
    company     TEXT,
    vertical    TEXT,
    score       REAL NOT NULL DEFAULT 0,
    stage       TEXT NOT NULL DEFAULT 'new',  -- new|qualified|proposal|won|lost
    payload     TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL
);
CREATE TABLE IF NOT EXISTS opportunities (
    opp_id      TEXT PRIMARY KEY,
    lead_id     TEXT,
    name        TEXT NOT NULL,
    value       REAL NOT NULL DEFAULT 0,
    stage       TEXT NOT NULL DEFAULT 'open',
    payload     TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL
);
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    lead_id     TEXT,
    client_id   TEXT,
    status      TEXT NOT NULL DEFAULT 'draft', -- draft|sent|approved|signed
    body        TEXT,
    price       REAL,
    created_at  REAL NOT NULL,
    updated_at  REAL
);

-- Knowledge (agency IP — never leaves)
CREATE TABLE IF NOT EXISTS playbooks (
    agent       TEXT PRIMARY KEY,
    version     TEXT NOT NULL,
    body        TEXT NOT NULL,               -- YAML text
    status      TEXT NOT NULL DEFAULT 'active',
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sops (
    sop_id      TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    body        TEXT NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS taste_library (
    entry_id    TEXT PRIMARY KEY,
    client_id   TEXT,                        -- NULL = cross-client (anonymized)
    kind        TEXT NOT NULL,               -- approved | rejected | correction
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS benchmarks (
    benchmark_id TEXT PRIMARY KEY,
    vertical    TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL NOT NULL,
    as_of       TEXT NOT NULL,               -- ISO date (freshness)
    source      TEXT
);

-- Memory (per agent+client: decisions, corrections, context)
CREATE TABLE IF NOT EXISTS agent_memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   TEXT NOT NULL,
    agent       TEXT NOT NULL,
    kind        TEXT NOT NULL,               -- decision | correction | context
    payload     TEXT NOT NULL,
    ts          REAL NOT NULL
);

-- Audit: long retention; the trust anchor
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    client_id   TEXT,
    detail      TEXT,
    signature   TEXT                         -- signed entry (demo: hmac)
);
CREATE TABLE IF NOT EXISTS egress_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    agent       TEXT,
    tool        TEXT,
    target      TEXT,
    data_class  TEXT NOT NULL,
    decision    TEXT NOT NULL,               -- allow | deny | approve
    redacted    INTEGER NOT NULL DEFAULT 0,
    detail      TEXT
);
CREATE TABLE IF NOT EXISTS playbook_runs (
    run_id      TEXT PRIMARY KEY,
    agent       TEXT NOT NULL,
    client_id   TEXT,
    status      TEXT NOT NULL,               -- ok | failed | escalated
    steps       TEXT,                        -- JSON
    egress_count INTEGER NOT NULL DEFAULT 0,
    tokens      INTEGER NOT NULL DEFAULT 0,
    started_at  REAL NOT NULL,
    finished_at REAL
);

-- Learning (L4)
CREATE TABLE IF NOT EXISTS trajectories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    client_id   TEXT,
    agent       TEXT NOT NULL,
    outcome     TEXT NOT NULL,               -- approved | rejected | published | ...
    first_pass  INTEGER NOT NULL DEFAULT 0,
    payload     TEXT,
    ts          REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS insights (
    insight_id  TEXT PRIMARY KEY,
    scope       TEXT NOT NULL,               -- global | vertical:<v> | client:<id>
    kind        TEXT NOT NULL,               -- insight | skill | lora
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'candidate',  -- candidate|quarantine|verified|promoted|rolled_back
    provenance  TEXT,                        -- JSON: source trajectories
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL,
    updated_at  REAL
);

-- Indexes
CREATE INDEX IF NOT EXISTS ix_tasks_client ON tasks(client_id);
CREATE INDEX IF NOT EXISTS ix_content_client ON content_assets(client_id);
CREATE INDEX IF NOT EXISTS ix_metrics_client ON metrics(client_id, period);
CREATE INDEX IF NOT EXISTS ix_memory_client ON agent_memory(client_id, agent);
CREATE INDEX IF NOT EXISTS ix_runs_agent ON playbook_runs(agent);
"""


def _now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Database:
    """A client-scoped handle over the internal DB.

    Every query is wrapped with a ``client_id`` filter — the SQLite
    equivalent of Postgres row-level security. ``client_id=None`` is
    reserved for agency-level tables (CRM, knowledge, audit) and is
    checked explicitly.
    """

    CLIENT_TABLES = {
        "client_accounts", "client_brand_kit", "projects", "tasks",
        "workflows", "content_assets", "content_briefs", "content_calendar",
        "media_assets", "creative_briefs", "metrics", "dashboards",
        "experiments", "agent_memory",
    }
    AGENCY_TABLES = {
        "clients", "leads", "opportunities", "proposals", "playbooks",
        "sops", "taste_library", "benchmarks", "audit_log", "egress_events",
        "playbook_runs", "trajectories", "insights",
    }

    _lock = threading.Lock()

    def __init__(self, path: Path, client_id: Optional[str] = None):
        self.path = Path(path)
        self.client_id = client_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    @classmethod
    def init(cls, path: Path) -> "Database":
        """Create schema (idempotent) and return an unscoped handle."""
        with cls._lock:
            conn = sqlite3.connect(str(path))
            conn.executescript(SCHEMA)
            conn.commit()
            conn.close()
        return cls(path)

    # -- scoped access ------------------------------------------------------
    def _guard(self, table: str) -> None:
        if table in self.CLIENT_TABLES and self.client_id is None:
            raise PermissionError(
                f"table {table!r} is client-partitioned; a scoped "
                f"connection (client_id) is required"
            )

    def _scoped_where(self, table: str, extra: Optional[str] = None) -> str:
        clauses = []
        if table in self.CLIENT_TABLES:
            clauses.append("client_id = ?")
        if extra:
            clauses.append(extra)
        return (" WHERE " + " AND ".join(clauses)) if clauses else ""

    def _scoped_params(self, table: str, params: tuple) -> tuple:
        if table in self.CLIENT_TABLES:
            return (self.client_id, *params)
        return params

    # -- query API ----------------------------------------------------------
    def query(self, table: str, where: Optional[str] = None,
              params: tuple = (), order_by: Optional[str] = None,
              limit: Optional[int] = None) -> list[dict]:
        self._guard(table)
        sql = f"SELECT * FROM {table}{self._scoped_where(table, where)}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit:
            sql += f" LIMIT {limit}"
        cur = self._conn.execute(sql, self._scoped_params(table, params))
        return [dict(r) for r in cur.fetchall()]

    def insert(self, table: str, row: dict) -> str:
        self._guard(table)
        cols = list(row.keys())
        placeholders = ", ".join("?" for _ in cols)
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
        with self._lock:
            cur = self._conn.execute(sql, tuple(row[c] for c in cols))
            self._conn.commit()
        return str(cur.lastrowid) if cur.lastrowid else None

    def update(self, table: str, row: dict, where: str,
               params: tuple = ()) -> int:
        self._guard(table)
        sets = ", ".join(f"{k} = ?" for k in row)
        sql = f"UPDATE {table} SET {sets}{self._scoped_where(table, where)}"
        with self._lock:
            cur = self._conn.execute(sql, self._scoped_params(table, (*row.values(), *params)))
            self._conn.commit()
        return cur.rowcount

    def delete(self, table: str, where: str, params: tuple = ()) -> int:
        self._guard(table)
        sql = f"DELETE FROM {table}{self._scoped_where(table, where)}"
        with self._lock:
            cur = self._conn.execute(sql, self._scoped_params(table, params))
            self._conn.commit()
        return cur.rowcount

    def count(self, table: str, where: Optional[str] = None,
              params: tuple = ()) -> int:
        self._guard(table)
        sql = f"SELECT COUNT(*) AS n FROM {table}{self._scoped_where(table, where)}"
        cur = self._conn.execute(sql, self._scoped_params(table, params))
        return int(cur.fetchone()["n"])

    def close(self) -> None:
        self._conn.close()


class Repository:
    """High-level, client-scoped data access used by tools and agents."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        Database.init(self.db_path)

    def scoped(self, client_id: Optional[str]) -> Database:
        return Database(self.db_path, client_id=client_id)

    # -- clients ------------------------------------------------------------
    def create_client(self, name: str, tier: str = "B",
                      vertical: Optional[str] = None) -> str:
        db = Database(self.db_path)
        cid = new_id("client")
        db.insert("clients", {
            "client_id": cid, "name": name, "tier": tier,
            "vertical": vertical, "status": "active", "created_at": _now(),
        })
        db.close()
        return cid

    def get_client(self, client_id: str) -> Optional[dict]:
        db = Database(self.db_path)
        rows = db.query("clients", where="client_id = ?", params=(client_id,))
        db.close()
        return rows[0] if rows else None

    def list_clients(self) -> list[dict]:
        db = Database(self.db_path)
        rows = db.query("clients", order_by="created_at")
        db.close()
        return rows

    def add_account(self, client_id: str, platform: str, vault_ref: str) -> None:
        db = Database(self.db_path, client_id=client_id)
        db.insert("client_accounts", {
            "client_id": client_id, "platform": platform,
            "vault_ref": vault_ref, "created_at": _now(),
        })
        db.close()

    def set_brand_kit(self, client_id: str, kit: dict[str, str]) -> None:
        db = Database(self.db_path, client_id=client_id)
        for key, value in kit.items():
            existing = db.query("client_brand_kit",
                                where="client_id = ? AND key = ?",
                                params=(client_id, key))
            version = (existing[0]["version"] + 1) if existing else 1
            db.insert("client_brand_kit", {
                "client_id": client_id, "key": key, "value": str(value),
                "version": version,
            })
        db.close()

    def get_brand_kit(self, client_id: str) -> dict[str, str]:
        db = Database(self.db_path, client_id=client_id)
        rows = db.query("client_brand_kit", order_by="version DESC")
        db.close()
        kit: dict[str, str] = {}
        for r in rows:  # keep latest version per key
            kit.setdefault(r["key"], r["value"])
        return kit

    # -- content ------------------------------------------------------------
    def create_brief(self, client_id: str, type_: str, keyword: str = "",
                     cta: str = "", payload: Optional[dict] = None) -> str:
        db = Database(self.db_path, client_id=client_id)
        bid = new_id("brief")
        db.insert("content_briefs", {
            "brief_id": bid, "client_id": client_id, "type": type_,
            "keyword": keyword, "cta": cta, "status": "open",
            "payload": json.dumps(payload or {}), "created_at": _now(),
        })
        db.close()
        return bid

    def create_asset(self, client_id: str, type_: str, title: str,
                     body: str, sources: Optional[list] = None,
                     status: str = "draft") -> str:
        db = Database(self.db_path, client_id=client_id)
        aid = new_id("asset")
        db.insert("content_assets", {
            "asset_id": aid, "client_id": client_id, "type": type_,
            "status": status, "title": title, "body": body,
            "sources": json.dumps(sources or []),
            "created_at": _now(), "updated_at": _now(),
        })
        db.close()
        return aid

    def update_asset_status(self, client_id: str, asset_id: str,
                            status: str) -> None:
        db = Database(self.db_path, client_id=client_id)
        db.update("content_assets", {"status": status, "updated_at": _now()},
                  where="asset_id = ?", params=(asset_id,))
        db.close()

    def get_asset(self, client_id: str, asset_id: str) -> Optional[dict]:
        db = Database(self.db_path, client_id=client_id)
        rows = db.query("content_assets", where="asset_id = ?",
                        params=(asset_id,))
        db.close()
        return rows[0] if rows else None

    # -- metrics ------------------------------------------------------------
    def record_metric(self, client_id: str, source: str, metric: str,
                      value: float, period: str) -> None:
        db = Database(self.db_path, client_id=client_id)
        db.insert("metrics", {
            "client_id": client_id, "source": source, "metric": metric,
            "value": value, "period": period, "ts": _now(),
        })
        db.close()

    def query_metrics(self, client_id: str, metric: Optional[str] = None,
                      source: Optional[str] = None) -> list[dict]:
        db = Database(self.db_path, client_id=client_id)
        where, params = [], []
        if metric:
            where.append("metric = ?"); params.append(metric)
        if source:
            where.append("source = ?"); params.append(source)
        rows = db.query("metrics",
                        where=" AND ".join(where) or None,
                        params=tuple(params), order_by="ts")
        db.close()
        return rows

    # -- CRM ----------------------------------------------------------------
    def create_lead(self, name: str, company: str = "",
                    vertical: str = "", payload: Optional[dict] = None) -> str:
        db = Database(self.db_path)
        lid = new_id("lead")
        db.insert("leads", {
            "lead_id": lid, "name": name, "company": company,
            "vertical": vertical, "score": 0, "stage": "new",
            "payload": json.dumps(payload or {}), "created_at": _now(),
            "updated_at": _now(),
        })
        db.close()
        return lid

    def update_lead(self, lead_id: str, **fields) -> None:
        db = Database(self.db_path)
        fields["updated_at"] = _now()
        db.update("leads", fields, where="lead_id = ?", params=(lead_id,))
        db.close()

    def get_lead(self, lead_id: str) -> Optional[dict]:
        db = Database(self.db_path)
        rows = db.query("leads", where="lead_id = ?", params=(lead_id,))
        db.close()
        return rows[0] if rows else None

    def list_leads(self, stage: Optional[str] = None) -> list[dict]:
        db = Database(self.db_path)
        rows = db.query("leads", where=f"stage = {stage!r}" if stage else None,
                        order_by="created_at")
        db.close()
        return rows

    def create_proposal(self, lead_id: str, body: str, price: float,
                        client_id: Optional[str] = None) -> str:
        db = Database(self.db_path)
        pid = new_id("prop")
        db.insert("proposals", {
            "proposal_id": pid, "lead_id": lead_id, "client_id": client_id,
            "status": "draft", "body": body, "price": price,
            "created_at": _now(), "updated_at": _now(),
        })
        db.close()
        return pid

    def update_proposal(self, proposal_id: str, **fields) -> None:
        db = Database(self.db_path)
        fields["updated_at"] = _now()
        db.update("proposals", fields, where="proposal_id = ?",
                  params=(proposal_id,))
        db.close()

    # -- knowledge ----------------------------------------------------------
    def save_playbook(self, agent: str, version: str, body: str) -> None:
        db = Database(self.db_path)
        db.insert("playbooks", {
            "agent": agent, "version": version, "body": body,
            "status": "active", "updated_at": _now(),
        })
        db.close()

    def add_taste(self, kind: str, payload: dict,
                  client_id: Optional[str] = None) -> str:
        db = Database(self.db_path)
        tid = new_id("taste")
        db.insert("taste_library", {
            "entry_id": tid, "client_id": client_id, "kind": kind,
            "payload": json.dumps(payload), "created_at": _now(),
        })
        db.close()
        return tid

    def list_taste(self, kind: Optional[str] = None,
                   client_id: Optional[str] = None) -> list[dict]:
        db = Database(self.db_path)
        where, params = [], []
        if kind:
            where.append("kind = ?"); params.append(kind)
        if client_id:
            where.append("client_id = ?"); params.append(client_id)
        rows = db.query("taste_library",
                        where=" AND ".join(where) or None,
                        params=tuple(params), order_by="created_at")
        db.close()
        return rows

    def add_benchmark(self, vertical: str, metric: str, value: float,
                      as_of: str, source: str = "") -> None:
        db = Database(self.db_path)
        db.insert("benchmarks", {
            "benchmark_id": new_id("bench"), "vertical": vertical,
            "metric": metric, "value": value, "as_of": as_of,
            "source": source,
        })
        db.close()

    def list_benchmarks(self, vertical: Optional[str] = None) -> list[dict]:
        db = Database(self.db_path)
        rows = db.query("benchmarks",
                        where="vertical = ?" if vertical else None,
                        params=(vertical,) if vertical else (),
                        order_by="as_of DESC")
        db.close()
        return rows

    # -- memory -------------------------------------------------------------
    def remember(self, client_id: str, agent: str, kind: str,
                 payload: dict) -> None:
        db = Database(self.db_path, client_id=client_id)
        db.insert("agent_memory", {
            "client_id": client_id, "agent": agent, "kind": kind,
            "payload": json.dumps(payload), "ts": _now(),
        })
        db.close()

    def recall(self, client_id: str, agent: Optional[str] = None,
               kind: Optional[str] = None) -> list[dict]:
        db = Database(self.db_path, client_id=client_id)
        where, params = [], []
        if agent:
            where.append("agent = ?"); params.append(agent)
        if kind:
            where.append("kind = ?"); params.append(kind)
        rows = db.query("agent_memory",
                        where=" AND ".join(where) or None,
                        params=tuple(params), order_by="ts")
        db.close()
        return rows

    # -- audit --------------------------------------------------------------
    def audit(self, actor: str, action: str, client_id: Optional[str] = None,
              detail: Optional[dict] = None, signature: str = "") -> None:
        db = Database(self.db_path)
        db.insert("audit_log", {
            "ts": _now(), "actor": actor, "action": action,
            "client_id": client_id, "detail": json.dumps(detail or {}),
            "signature": signature,
        })
        db.close()

    def egress_event(self, agent: str, tool: str, target: str,
                     data_class: str, decision: str,
                     redacted: bool = False,
                     detail: Optional[dict] = None) -> None:
        db = Database(self.db_path)
        db.insert("egress_events", {
            "ts": _now(), "agent": agent, "tool": tool, "target": target,
            "data_class": data_class, "decision": decision,
            "redacted": int(redacted), "detail": json.dumps(detail or {}),
        })
        db.close()

    def record_run(self, run: dict) -> None:
        db = Database(self.db_path)
        db.insert("playbook_runs", run)
        db.close()

    def record_trajectory(self, run_id: str, client_id: Optional[str],
                          agent: str, outcome: str, first_pass: bool,
                          payload: Optional[dict] = None) -> None:
        db = Database(self.db_path)
        db.insert("trajectories", {
            "run_id": run_id, "client_id": client_id, "agent": agent,
            "outcome": outcome, "first_pass": int(first_pass),
            "payload": json.dumps(payload or {}), "ts": _now(),
        })
        db.close()

    def trajectories(self, agent: Optional[str] = None,
                     limit: int = 500) -> list[dict]:
        db = Database(self.db_path)
        rows = db.query("trajectories",
                        where="agent = ?" if agent else None,
                        params=(agent,) if agent else (),
                        order_by="ts DESC", limit=limit)
        db.close()
        return rows

    # -- insights (L4) ------------------------------------------------------
    def save_insight(self, insight_id: str, scope: str, kind: str,
                     payload: dict, status: str = "candidate",
                     provenance: Optional[dict] = None) -> None:
        db = Database(self.db_path)
        db.insert("insights", {
            "insight_id": insight_id, "scope": scope, "kind": kind,
            "payload": json.dumps(payload), "status": status,
            "provenance": json.dumps(provenance or {}), "version": 1,
            "created_at": _now(), "updated_at": _now(),
        })
        db.close()

    def set_insight_status(self, insight_id: str, status: str) -> None:
        db = Database(self.db_path)
        db.update("insights", {"status": status, "updated_at": _now()},
                  where="insight_id = ?", params=(insight_id,))
        db.close()

    def list_insights(self, status: Optional[str] = None) -> list[dict]:
        db = Database(self.db_path)
        rows = db.query("insights",
                        where="status = ?" if status else None,
                        params=(status,) if status else (),
                        order_by="created_at")
        db.close()
        return rows