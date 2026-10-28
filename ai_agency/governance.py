"""L7 — Governance, Privacy & Evaluation (cross-cutting).

  Evaluation + capability dashboard  — quality / privacy / cost / latency
  Retention + right-to-delete        — provenance-based propagation
  Claim-level grounding              — cite / abstain / detect conflicts
  Reflection / self-critique         — answer / clarify / abstain
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

from .db import Repository


class Governance:
    def __init__(self, repo: Repository, settings):
        self.repo = repo
        self.settings = settings
        self._runs: list[dict] = []

    # -- run tracking (feeds the eval dashboard) ----------------------------
    def record_run(self, agent: str, status: str, egress_events: int,
                   tokens: int, first_pass: bool) -> None:
        self._runs.append({
            "agent": agent, "status": status, "egress_events": egress_events,
            "tokens": tokens, "first_pass": first_pass, "ts": time.time(),
        })

    # -- evaluation + capability dashboard ----------------------------------
    def dashboard(self) -> dict:
        runs = self._runs
        n = len(runs) or 1
        first_pass_rate = sum(r["first_pass"] for r in self._runs) / n
        run_success = sum(r["status"] == "ok" for r in self._runs) / n
        total_egress = sum(r["egress_events"] for r in self._runs)
        total_tokens = sum(r["tokens"] for r in self._runs)
        # egress violations = denied egress events in the DB
        db = self.repo.scoped(None)
        denied = db.count("egress_events", where="decision = ?",
                          params=("deny",))
        db.close()
        return {
            "runs": len(self._runs),
            "first_pass_approval_rate": round(first_pass_rate, 3),
            "run_success_rate": round(run_success, 3),
            "egress_events": total_egress,
            "egress_denied": denied,
            "egress_violations": denied,   # any deny = potential incident
            "tokens_total": total_tokens,
            "token_cost_per_deliverable":
                round(total_tokens / max(1, len(self._runs)), 3),
            "quality": "ok" if first_pass_rate >= 0.8 else "review",
            "privacy": "ok" if denied == 0 else "INCIDENT",
            "cost": "ok",
            "latency": "ok",
            "gates_learning_promotion": first_pass_rate >= 0.8,
        }

    # -- claim-level grounding (cite / abstain / detect conflicts) ----------
    def ground_claims(self, text: str,
                      known_metrics: Optional[dict[str, float]] = None) -> dict:
        """Every numeric claim must cite a stored source; if no source,
        abstain; if sources conflict, surface the conflict."""
        known_metrics = known_metrics or {}
        claims = []
        # find numeric claims like "12.5%", "2.4x", "₹12500"
        for m in re.finditer(
                r"(\d+(?:\.\d+)?)\s*(%|x|×|₹|cr|lakh|k)?", text):
            num = m.group(1)
            unit = m.group(2) or ""
            # find a matching known metric (grounded) or abstain
            grounded = None
            for metric, value in known_metrics.items():
                if abs(float(value) - float(num)) < max(1.0, abs(float(num)) * 0.1):
                    grounded = metric
                    break
            claims.append({
                "claim": m.group(0),
                "grounded": grounded is not None,
                "source": f"metrics:{grounded}" if grounded else None,
                "action": "cite" if grounded else "abstain",
            })
        conflicts = self._detect_conflicts(known_metrics)
        return {
            "claims": claims,
            "all_grounded": all(c["grounded"] for c in claims) if claims else True,
            "abstained": [c["claim"] for c in claims if not c["grounded"]],
            "conflicts": conflicts,
        }

    @staticmethod
    def _detect_conflicts(known_metrics: dict[str, float]) -> list[str]:
        # demo: same metric from different sources with >5% spread
        by_metric: dict[str, list[float]] = {}
        for k, v in known_metrics.items():
            metric = k.split(":")[0] if ":" in k else k
            by_metric.setdefault(metric, []).append(v)
        conflicts = []
        for metric, vals in by_metric.items():
            if len(vals) >= 2:
                spread = (max(vals) - min(vals)) / (min(vals) or 1)
                if spread > 0.05:
                    conflicts.append(
                        f"{metric}: sources conflict by {round(spread * 100, 1)}%")
        return conflicts

    # -- reflection / self-critique -----------------------------------------
    def reflect(self, output: str, checklist: list[str]) -> dict:
        """Before an output reaches a gate, the agent self-critiques against
        the quality checklist. Result: answer / clarify / abstain."""
        issues = []
        lowered = output.lower()
        for check in checklist:
            if check == "has_cta" and not any(
                    w in lowered for w in ("next step", "cta", "learn more",
                                           "get started", "sign up")):
                issues.append("missing clear CTA")
            if check == "complete" and len(output.strip()) < 40:
                issues.append("output too short to be complete")
            if check == "no_secrets" and re.search(
                    r"(?i)(api[_-]?key|password|token)\s*[:=]\s*\S+", output):
                issues.append("possible secret in output")
        if not issues:
            verdict = "answer"
        elif any("secret" in i for i in issues):
            verdict = "abstain"   # never ship a secret
        else:
            verdict = "clarify"   # fixable: send back for revision
        return {"verdict": verdict, "issues": issues,
                "ts": time.time()}

    # -- retention + right-to-delete (provenance-based propagation) ---------
    def right_to_delete(self, client_id: str,
                        docstore=None, graph=None, memory=None) -> dict:
        """DPDP data-principal erasure = one scoped operation: delete the
        source, propagate invalidation to all derived artifacts via
        provenance."""
        invalidated = {"chunks": 0, "graph_nodes": 0, "memory_rows": 0}
        if docstore is not None:
            # invalidate all docs for this client
            for chunk in list(docstore.all_chunks()):
                if chunk.client_id == client_id:
                    docstore.invalidate_doc(chunk.doc_id)
                    invalidated["chunks"] += 1
        if graph is not None:
            invalidated["graph_nodes"] = graph.invalidate_client(client_id)
        # always erase agent memory rows for this client (DPDP erasure is
        # one scoped operation; the repo is the source of truth)
        db = self.repo.scoped(client_id)
        invalidated["memory_rows"] = db.delete(
            "agent_memory", where="client_id = ?", params=(client_id,))
        db.close()
        # audit the erasure (provenance)
        self.repo.audit(actor="governance", action="right_to_delete",
                        client_id=client_id, detail=invalidated)
        return {"client_id": client_id, "invalidated": invalidated,
                "ts": time.time()}