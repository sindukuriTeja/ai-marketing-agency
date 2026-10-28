"""Center — User / API Gateway (the single entry point).

One door in: the client portal, the internal team UI, and the API all enter
through the same gateway, which resolves identity (which human/agent),
tenant (which client), and policy (what they may do) before anything else
runs. One door out: the only path to external models or web search is the
egress gateway's approved output.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .runtime import AgentRuntime


class Gateway:
    """The thin front door. Clients see dashboards, approve content, and
    receive reports — they do not see the machine."""

    def __init__(self, runtime: AgentRuntime):
        self.runtime = runtime

    # -- identity / tenant / policy check -----------------------------------
    def _check(self, identity: str, client_id: Optional[str]) -> dict:
        # demo: any identity with a valid client (or agency) is allowed.
        # production: JWT/OIDC + tenant resolution + RBAC.
        if client_id:
            client = self.runtime.repo.get_client(client_id)
            if not client:
                return {"allowed": False, "reason": "unknown client"}
        return {"allowed": True, "identity": identity, "client_id": client_id}

    # -- client-facing operations -------------------------------------------
    def submit_lead(self, identity: str, name: str, company: str = "",
                    vertical: str = "") -> dict:
        chk = self._check(identity, None)
        if not chk["allowed"]:
            return chk
        lead_id = self.runtime.repo.create_lead(name, company, vertical)
        # enrich + score (local model) then qualify -> event -> BDE agent
        score = self._score_lead(name, company, vertical)
        self.runtime.repo.update_lead(lead_id, score=score,
                                      stage="qualified" if score >= 60 else "new")
        if score >= 60:
            self.runtime.bus.emit("lead_qualified",
                                  {"lead_id": lead_id, "score": score,
                                   "client_id": None})
        return {"lead_id": lead_id, "score": score,
                "stage": "qualified" if score >= 60 else "new"}

    @staticmethod
    def _score_lead(name: str, company: str, vertical: str) -> float:
        # demo BANT-style scoring (local model stand-in)
        score = 40.0
        if company:
            score += 20
        if vertical in ("d2c", "saas", "edtech", "healthcare", "real_estate"):
            score += 20
        if len(name) > 3:
            score += 10
        return min(100.0, score)

    def approve_content(self, identity: str, client_id: str,
                        asset_id: str, approve: bool = True) -> dict:
        chk = self._check(identity, client_id)
        if not chk["allowed"]:
            return chk
        status = "live" if approve else "draft"
        self.runtime.repo.update_asset_status(client_id, asset_id, status)
        if approve:
            self.runtime.bus.emit("content_ready",
                                  {"client_id": client_id,
                                   "asset_id": asset_id})
        return {"asset_id": asset_id, "status": status}

    def get_dashboard(self, identity: str, client_id: str) -> dict:
        chk = self._check(identity, client_id)
        if not chk["allowed"]:
            return chk
        metrics = self.runtime.repo.query_metrics(client_id)
        by_metric: dict[str, list[float]] = {}
        for m in metrics:
            by_metric.setdefault(m["metric"], []).append(m["value"])
        summary = {k: round(sum(v) / len(v), 2) for k, v in by_metric.items()}
        return {"client_id": client_id, "metrics": summary,
                "health": self._health(client_id)}

    def _health(self, client_id: str) -> str:
        # AM health score (demo): based on metric volume + recency
        metrics = self.runtime.repo.query_metrics(client_id)
        if len(metrics) >= 4:
            return "healthy"
        if metrics:
            return "watch"
        return "onboarding"

    def request_mbr(self, identity: str, client_id: str) -> dict:
        chk = self._check(identity, client_id)
        if not chk["allowed"]:
            return chk
        run = self.runtime.run_agent("am", client_id=client_id,
                                     task={"kind": "report", "service": "am"})
        return {"run_id": run.run_id, "status": run.status,
                "report": run.outputs.get("build_mbr", {})}

    # -- internal team operations -------------------------------------------
    def run_agent(self, identity: str, agent: str,
                  client_id: Optional[str] = None,
                  task: Optional[dict] = None) -> dict:
        chk = self._check(identity, client_id)
        if not chk["allowed"]:
            return chk
        run = self.runtime.run_agent(agent, client_id=client_id, task=task)
        return {"run_id": run.run_id, "agent": run.agent,
                "status": run.status, "outputs": run.outputs,
                "egress_events": run.egress_events}

    def system_health(self, identity: str) -> dict:
        return self.runtime.governance.dashboard()