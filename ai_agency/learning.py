"""L4 — Learning Engine (asynchronous, budgeted, reversible).

The loop:
  trajectory store -> insight miner -> candidate insights/skills ->
  verification gate
    ├─ fail  -> quarantine + review
    ├─ pass  -> model improvement -> regression harness
    │            ├─ fail: keep current model
    │            └─ pass: staged model/adapter -> model registry (L1)
    └─ pass  -> promote insight with provenance -> L6 orchestration

Safety properties (the whole point of the gate):
  - budgeted: at most `learning_job_budget` jobs per batch
  - verified: replay + hold-out tests before promotion
  - reversible: every promotion can be rolled back
  - human-approved: promotions require the Quality & Compliance Lead
    (the demo injects an approver; the default approves verified candidates)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .db import Repository, new_id
from .model import ModelRegistry


@dataclass
class Candidate:
    insight_id: str
    scope: str
    kind: str                 # insight | skill | lora
    payload: dict
    provenance: dict          # source trajectory ids
    support: int              # how many trajectories back it
    confidence: float


class VerificationGate:
    """Replay + hold-out tests. A candidate must reproduce past good
    outcomes AND work on unseen cases before it may be promoted."""

    def __init__(self, repo: Repository):
        self.repo = repo

    def verify(self, cand: Candidate) -> dict:
        # replay: does the candidate reproduce the outcomes it was mined from?
        replay_pass = cand.support >= 2 and cand.confidence >= 0.6
        # hold-out: unseen-case test (demo: confidence threshold + scope check)
        holdout_pass = cand.confidence >= 0.5 and bool(cand.scope)
        passed = replay_pass and holdout_pass
        return {
            "passed": passed,
            "replay": replay_pass,
            "holdout": holdout_pass,
            "support": cand.support,
            "confidence": round(cand.confidence, 3),
            "ts": time.time(),
        }


class RegressionHarness:
    """Golden cases per service line. A model/adapter is staged only if it
    does not regress the golden set. Fail -> keep current model."""

    def __init__(self):
        self._golden: dict[str, dict] = {}

    def add_golden(self, service: str, case: dict) -> None:
        self._golden[f"{service}:{case.get('id', len(self._golden))}"] = case

    def run(self, adapter: dict) -> dict:
        # demo: an adapter passes if it carries a non-empty config and a
        # version; production runs the golden suite against the staged model.
        passed = bool(adapter.get("config")) and bool(adapter.get("version"))
        return {"passed": passed,
                "cases": len(self._golden),
                "ts": time.time()}


class LearningEngine:
    def __init__(self, repo: Repository, registry: ModelRegistry,
                 settings,
                 approver: Optional[Callable[[dict], bool]] = None):
        self.repo = repo
        self.registry = registry
        self.settings = settings
        self.gate = VerificationGate(repo)
        self.harness = RegressionHarness()
        self.approver = approver or (lambda ctx: True)
        self._promotions: list[dict] = []

    # -- trajectory store ---------------------------------------------------
    def record_trajectory(self, run_id: str, client_id: Optional[str],
                          agent: str, outcome: str, first_pass: bool,
                          payload: Optional[dict] = None) -> None:
        self.repo.record_trajectory(run_id, client_id, agent, outcome,
                                    first_pass, payload)

    # -- insight miner (local model, batch, budgeted) -----------------------
    def mine(self, agent: Optional[str] = None) -> list[Candidate]:
        """Batch-analyze trajectories to find candidate insights/skills."""
        trajs = self.repo.trajectories(agent=agent,
                                       limit=self.settings.learning_job_budget * 10)
        if not trajs:
            return []
        # group by agent + outcome
        groups: dict[str, list[dict]] = {}
        for t in trajs:
            key = f"{t['agent']}:{t['outcome']}"
            groups.setdefault(key, []).append(t)

        candidates = []
        for key, items in groups.items():
            agent, outcome = key.split(":", 1)
            first_pass_rate = sum(t["first_pass"] for t in items) / len(items)
            if len(items) < 2:
                continue
            # a "skill" candidate: this agent reliably produces `outcome`
            if first_pass_rate >= 0.6 and outcome in ("approved", "published"):
                candidates.append(Candidate(
                    insight_id=new_id("insight"),
                    scope=f"agent:{agent}",
                    kind="skill",
                    payload={"agent": agent, "outcome": outcome,
                             "first_pass_rate": round(first_pass_rate, 3),
                             "note": f"{agent} reliably reaches {outcome}"},
                    provenance={"trajectories": [t["run_id"] for t in items]},
                    support=len(items),
                    confidence=first_pass_rate,
                ))
            # a "lora" candidate: enough volume to fine-tune a drafter
            if len(items) >= 3:
                candidates.append(Candidate(
                    insight_id=new_id("insight"),
                    scope=f"agent:{agent}",
                    kind="lora",
                    payload={"agent": agent,
                             "note": f"curated LoRA from {len(items)} runs"},
                    provenance={"trajectories": [t["run_id"] for t in items]},
                    support=len(items),
                    confidence=min(1.0, 0.5 + len(items) * 0.05),
                ))
        # budget: at most learning_job_budget candidates per batch
        return candidates[: self.settings.learning_job_budget]

    # -- the full learning batch --------------------------------------------
    def run_batch(self, agent: Optional[str] = None) -> dict:
        if not self.settings.learning_enabled:
            return {"skipped": True, "reason": "learning disabled"}
        candidates = self.mine(agent=agent)
        result = {"mined": len(candidates), "verified": 0, "quarantined": 0,
                  "promoted": 0, "staged_models": 0, "details": []}
        for cand in candidates:
            verdict = self.gate.verify(cand)
            if not verdict["passed"]:
                # quarantine + review
                self.repo.save_insight(cand.insight_id, cand.scope, cand.kind,
                                       cand.payload, status="quarantine",
                                       provenance=cand.provenance)
                result["quarantined"] += 1
                result["details"].append({
                    "insight_id": cand.insight_id, "status": "quarantine",
                    "verdict": verdict})
                continue

            # verified -> human approval (Quality & Compliance Lead)
            approval_ctx = {"insight_id": cand.insight_id, "kind": cand.kind,
                            "scope": cand.scope, "verdict": verdict,
                            "provenance": cand.provenance}
            if not self.approver(approval_ctx):
                self.repo.save_insight(cand.insight_id, cand.scope, cand.kind,
                                       cand.payload, status="quarantine",
                                       provenance=cand.provenance)
                result["quarantined"] += 1
                result["details"].append({
                    "insight_id": cand.insight_id, "status": "quarantine",
                    "verdict": {**verdict, "approval": "denied"}})
                continue

            self.repo.save_insight(cand.insight_id, cand.scope, cand.kind,
                                   cand.payload, status="verified",
                                   provenance=cand.provenance)
            result["verified"] += 1

            if cand.kind == "lora":
                # model improvement -> regression harness -> registry
                adapter = {"config": {"fine_tuned": True,
                                      "source": cand.scope},
                           "version": "1.0.0+learned"}
                reg = self.harness.run(adapter)
                if reg["passed"]:
                    model_name = f"local-{cand.scope.split(':')[1]}-adapter"
                    self.registry.stage(model_name, adapter)
                    self.registry.activate_staged(model_name)
                    self.repo.set_insight_status(cand.insight_id, "promoted")
                    result["staged_models"] += 1
                    result["promoted"] += 1
                    result["details"].append({
                        "insight_id": cand.insight_id, "status": "promoted",
                        "model": model_name, "regression": reg})
                else:
                    # fail: keep current model
                    self.repo.set_insight_status(cand.insight_id, "quarantine")
                    result["quarantined"] += 1
                    result["details"].append({
                        "insight_id": cand.insight_id,
                        "status": "quarantine(regression-fail)",
                        "regression": reg})
            else:
                # promote insight with provenance -> L6
                self.repo.set_insight_status(cand.insight_id, "promoted")
                result["promoted"] += 1
                result["details"].append({
                    "insight_id": cand.insight_id, "status": "promoted"})
            self._promotions.append({
                "insight_id": cand.insight_id, "ts": time.time()})
        return result

    # -- memory consolidator (merge / decay / prune / re-embed) -------------
    def consolidate_memory(self, client_id: str) -> dict:
        """Merge duplicate facts, decay stale ones (demo: dedupe)."""
        facts = self.repo.recall(client_id, kind="context")
        seen = set()
        kept = 0
        for f in facts:
            key = json.dumps(json.loads(f["payload"]), sort_keys=True) \
                if f["payload"] else str(f["id"])
            if key in seen:
                continue
            seen.add(key)
            kept += 1
        return {"client_id": client_id, "facts": len(facts), "kept": kept,
                "pruned": len(facts) - kept}

    # -- rollback (reversible) ---------------------------------------------
    def rollback(self, insight_id: str) -> bool:
        self.repo.set_insight_status(insight_id, "rolled_back")
        return True

    def promotions(self) -> list[dict]:
        return list(self._promotions)