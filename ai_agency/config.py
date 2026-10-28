"""Central configuration for the agency runtime.

Everything is local-first and dependency-free by default. Optional
production backends (Postgres, pgvector, sentence-transformers, vLLM)
are enabled via environment variables and degrade gracefully.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# --- paths -----------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLAYBOOKS_DIR = PROJECT_ROOT / "playbooks"


def _data_dir() -> Path:
    """Resolve the data dir from the environment *at call time* so that
    tests (which set AGENCY_DATA_DIR in setUp) get an isolated store. The
    old import-time constants froze the first value and leaked state across
    tests / runs, so paths are now computed per Settings instance."""
    return Path(os.environ.get("AGENCY_DATA_DIR", PROJECT_ROOT / ".data"))

# --- model tiers (L1) -------------------------------------------------------
TIER_LOCAL = "local"            # on-prem quantized model (routine 80%)
TIER_STANDARD = "standard"      # external frontier, zero-retention, egress-gated
TIER_SELF_HOSTED = "self-hosted"  # inside the boundary (sensitive work)

# --- data classes (Part 7) --------------------------------------------------
C0 = "C0"  # public
C1 = "C1"  # internal (agency IP) — never leaves
C2 = "C2"  # client — only to that client's own platforms
C3 = "C3"  # confidential — per-case human approval + redaction
C4 = "C4"  # secrets — never transit

DATA_CLASSES = (C0, C1, C2, C3, C4)

# --- trust ladder (Part 6.2) ------------------------------------------------
S0 = "S0"  # supervised: 100% human review
S1 = "S1"  # sampled: 20-30% review
S2 = "S2"  # class-approved: exceptions only
S3 = "S3"  # exception-based

TRUST_STAGES = (S0, S1, S2, S3)

# Consequence gates never loosen, at any trust stage.
CONSEQUENCE_GATES = {
    "money", "contracts", "sensitive_data_egress",
    "crises", "offboarding", "playbook_changes",
}


@dataclass
class Settings:
    """Runtime settings. Defaults are safe for the local demo path."""
    data_dir: Path = field(default_factory=_data_dir)
    # Egress
    egress_enabled: bool = True
    # Human approval: in the demo, approvals are auto-resolved by a
    # deterministic policy (see security.approval). Set to False to require
    # an explicit human decision object at every gate.
    auto_approve_routine: bool = True
    # Model routing
    default_tier: str = TIER_LOCAL
    # Learning
    learning_enabled: bool = True
    learning_job_budget: int = 8          # max learning jobs per batch
    # Kill-switch
    kill_switch_engaged: bool = field(default=False, repr=False)

    # derived paths (computed per instance, so env changes are honored)
    @property
    def db_path(self) -> Path:
        return self.data_dir / "agency.db"

    @property
    def vault_path(self) -> Path:
        return self.data_dir / "vault.json"

    @property
    def egress_log_path(self) -> Path:
        return self.data_dir / "egress_events.jsonl"

    @property
    def audit_log_path(self) -> Path:
        return self.data_dir / "audit_log.jsonl"

    @property
    def run_log_path(self) -> Path:
        return self.data_dir / "playbook_runs.jsonl"

    @property
    def trajectory_path(self) -> Path:
        return self.data_dir / "trajectories.jsonl"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.data_dir / "learned",
                  self.data_dir / "memory", self.data_dir / "cache",
                  self.data_dir / "objects"):
            p.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s