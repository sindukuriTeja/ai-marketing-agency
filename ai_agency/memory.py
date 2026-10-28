"""L2 — Memory System (persistent + shared, access-scoped).

Four memory types, partitioned by client_id (RLS):
  working    — current task state (per run, per client)
  semantic   — consolidated facts + insights
  episodic   — experiences + outcomes (feeds L4)
  procedural — versioned skills (the playbook library)

Consolidation pipeline: working -> semantic -> episodic -> procedural.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from .db import Repository


class WorkingMemory:
    """Current task context — scoped to one client, one task."""

    def __init__(self, client_id: Optional[str], task_id: str):
        self.client_id = client_id
        self.task_id = task_id
        self._state: dict[str, Any] = {}

    def set(self, key: str, value: Any) -> None:
        self._state[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def snapshot(self) -> dict:
        return {"task_id": self.task_id, "client_id": self.client_id,
                "state": dict(self._state), "ts": time.time()}


class MemorySystem:
    """The four memory types over the internal DB."""

    def __init__(self, repo: Repository):
        self.repo = repo

    # -- working (transient, per run) --------------------------------------
    def new_working(self, client_id: Optional[str], task_id: str) -> WorkingMemory:
        return WorkingMemory(client_id, task_id)

    # -- semantic (consolidated facts) -------------------------------------
    def remember_fact(self, client_id: str, agent: str, fact: dict) -> None:
        self.repo.remember(client_id, agent, "context", fact)

    def recall_facts(self, client_id: str,
                     agent: Optional[str] = None) -> list[dict]:
        return self.repo.recall(client_id, agent=agent, kind="context")

    # -- episodic (experiences + outcomes) ---------------------------------
    def remember_experience(self, client_id: str, agent: str,
                            experience: dict) -> None:
        self.repo.remember(client_id, agent, "decision", experience)

    def recall_experiences(self, client_id: str,
                           agent: Optional[str] = None) -> list[dict]:
        return self.repo.recall(client_id, agent=agent, kind="decision")

    # -- corrections (the taste library's raw feed) ------------------------
    def remember_correction(self, client_id: str, agent: str,
                            correction: dict) -> None:
        self.repo.remember(client_id, agent, "correction", correction)

    def recall_corrections(self, client_id: str,
                           agent: Optional[str] = None) -> list[dict]:
        return self.repo.recall(client_id, agent=agent, kind="correction")

    # -- consolidation (working -> semantic -> episodic -> procedural) -----
    def consolidate(self, client_id: str, agent: str,
                    working: WorkingMemory, outcome: str) -> dict:
        """End-of-run consolidation: persist what the run learned."""
        snap = working.snapshot()
        # episodic: the experience + outcome
        self.remember_experience(client_id, agent, {
            "task": snap["state"].get("task_kind", "task"),
            "outcome": outcome,
            "steps": snap["state"].get("steps_done", []),
        })
        # semantic: any facts the run established
        for fact in snap["state"].get("facts", []):
            self.remember_fact(client_id, agent, fact)
        return {"client_id": client_id, "agent": agent, "outcome": outcome,
                "consolidated_at": time.time()}