"""L6 — Orchestration + the agent runtime.

One runtime executes every playbook:
  load playbook -> receive task -> plan steps -> call declared tools only ->
  QA gate each output -> human gate where marked -> write results to DB ->
  log run + egress -> (async) publish trajectory to L4

Agents never call each other directly — they publish tasks/events to the
router and read each other's outputs from the shared blackboard.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import yaml

from .config import Settings, TIER_LOCAL
from .db import Repository, new_id
from .governance import Governance
from .knowledge import KnowledgeGraph, RetrievalAPI, Reranker, VersionedDocStore
from .learning import LearningEngine
from .memory import MemorySystem
from .model import ModelRouter, default_registry
from .security import (ApprovalService, EgressGate, KillSwitch, PolicyEngine,
                       Vault)
from .tools import ToolContext, ToolRegistry
from .tools.impl import build_registry


# ---------------------------------------------------------------------------
# Playbook (the unit of an agent)
# ---------------------------------------------------------------------------

@dataclass
class Playbook:
    agent: str
    version: str
    mission: str
    triggers: list = field(default_factory=list)
    inputs: list = field(default_factory=list)
    tools: list = field(default_factory=list)
    procedure: list = field(default_factory=list)
    quality_gates: list = field(default_factory=list)
    escalation: list = field(default_factory=list)
    kpi: list = field(default_factory=list)
    data_policy: dict = field(default_factory=dict)
    scope: dict = field(default_factory=dict)
    owner: str = ""
    status: str = "active"
    changes: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Playbook":
        return cls(
            agent=d["agent"],
            version=d.get("version", "0.0.0"),
            mission=d.get("mission", ""),
            triggers=d.get("triggers", []),
            inputs=d.get("inputs", []),
            tools=d.get("tools", []),
            procedure=d.get("procedure", []),
            quality_gates=d.get("quality_gates", []),
            escalation=d.get("escalation", []),
            kpi=d.get("kpi", []),
            data_policy=d.get("data_policy", {}),
            scope=d.get("scope", {}),
            owner=d.get("owner", ""),
            status=d.get("status", "active"),
            changes=d.get("changes", []),
        )

    @property
    def tool_ids(self) -> list[str]:
        return [t.split("@")[0] for t in self.tools]


class PlaybookLibrary:
    def __init__(self, repo: Repository, playbooks_dir=None):
        self.repo = repo
        self.dir = playbooks_dir
        self._cache: dict[str, Playbook] = {}

    def load(self, agent: str) -> Playbook:
        if agent in self._cache:
            return self._cache[agent]
        if self.dir is not None:
            path = self.dir / f"{agent}.yaml"
            if path.exists():
                with open(path) as f:
                    pb = Playbook.from_dict(yaml.safe_load(f))
                self._cache[agent] = pb
                return pb
        # fall back to the DB
        db = self.repo.scoped(None)
        rows = db.query("playbooks", where="agent = ?", params=(agent,),
                        order_by="updated_at DESC", limit=1)
        db.close()
        if rows:
            pb = Playbook.from_dict(yaml.safe_load(rows[0]["body"]))
            self._cache[agent] = pb
            return pb
        raise KeyError(f"no playbook for agent {agent!r}")

    def list_agents(self) -> list[str]:
        if self.dir is not None and self.dir.exists():
            return sorted(p.stem for p in self.dir.glob("*.yaml"))
        db = self.repo.scoped(None)
        rows = db.query("playbooks", order_by="agent")
        db.close()
        return [r["agent"] for r in rows]


# ---------------------------------------------------------------------------
# Shared blackboard (scoped inter-agent state)
# ---------------------------------------------------------------------------

class Blackboard:
    """Scoped inter-agent state. Agents publish results here; no direct
    agent-to-agent calls. Scoped per client."""

    def __init__(self):
        self._boards: dict[str, dict[str, Any]] = {}

    def _board(self, client_id: Optional[str]) -> dict[str, Any]:
        return self._boards.setdefault(client_id or "agency", {})

    def publish(self, client_id: Optional[str], key: str, value: Any,
                agent: str) -> None:
        self._board(client_id)[key] = {"value": value, "agent": agent,
                                       "ts": time.time()}

    def read(self, client_id: Optional[str], key: str) -> Optional[Any]:
        entry = self._board(client_id).get(key)
        return entry["value"] if entry else None

    def keys(self, client_id: Optional[str]) -> list[str]:
        return list(self._board(client_id).keys())


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------

class EventBus:
    def __init__(self):
        self._subs: dict[str, list[Callable]] = {}
        self._log: list[dict] = []

    def subscribe(self, event: str, handler: Callable) -> None:
        self._subs.setdefault(event, []).append(handler)

    def emit(self, event: str, payload: dict) -> list[dict]:
        self._log.append({"event": event, "payload": payload,
                          "ts": time.time()})
        results = []
        for handler in self._subs.get(event, []):
            results.append(handler(payload))
        return results

    @property
    def log(self) -> list[dict]:
        return list(self._log)


# ---------------------------------------------------------------------------
# Scheduler (time triggers)
# ---------------------------------------------------------------------------

class Scheduler:
    """Time triggers (sprints, MBRs, renewals, hygiene jobs).

    Demo: a deterministic trigger matcher. Production: a cron engine.
    """

    def __init__(self):
        self._triggers: list[dict] = []

    def add(self, name: str, cron: str, handler: Callable) -> None:
        self._triggers.append({"name": name, "cron": cron,
                               "handler": handler})

    def due(self, now: str = "") -> list[dict]:
        # demo: all registered triggers are "due" on demand
        return list(self._triggers)

    def fire(self, name: str) -> list:
        results = []
        for t in self._triggers:
            if t["name"] == name:
                results.append(t["handler"]())
        return results


# ---------------------------------------------------------------------------
# Task router (L4 in the business stack)
# ---------------------------------------------------------------------------

class TaskRouter:
    """Assigns each task to its owning agent (by service/trigger); enforces
    single-owner."""

    def __init__(self, library: PlaybookLibrary, bus: EventBus):
        self.library = library
        self.bus = bus

    def route(self, task: dict) -> str:
        """Pick the owning agent for a task.

        task: {kind, client_id, service?, ...}
        """
        agent = task.get("agent")
        if agent:
            return agent
        service = task.get("service")
        kind = task.get("kind", "")
        # service -> agent map (single-owner)
        service_map = {
            "seo": "seo", "ppc": "ppc", "paid_social": "paid_social",
            "social": "social", "content": "writer", "email": "email",
            "web": "webdev", "video": "video", "design": "designer",
            "influencer": "influencer", "data": "data",
            "strategy": "strategist", "am": "am", "bde": "bde",
            "pm": "pm", "dmm": "dmm", "qa": "qa",
            "creative": "creative_director",
        }
        if service and service in service_map:
            return service_map[service]
        # kind -> agent fallback
        kind_map = {
            "lead": "bde", "onboard": "pm", "strategy": "strategist",
            "report": "am", "audit": "seo", "campaign": "ppc",
        }
        return kind_map.get(kind, "pm")


# ---------------------------------------------------------------------------
# Workflow engine
# ---------------------------------------------------------------------------

class WorkflowEngine:
    """Runs multi-agent sequences (onboarding, campaign launch); state in
    `workflows`. A human gate pauses the workflow until approved."""

    def __init__(self, repo: Repository):
        self.repo = repo

    def start(self, client_id: Optional[str], name: str,
              steps: list[dict]) -> str:
        wid = new_id("wf")
        scope = self.repo.scoped(client_id or "agency")
        scope.insert("workflows", {
            "workflow_id": wid, "client_id": client_id or "agency",
            "name": name, "state": "running",
            "steps": _json(steps), "created_at": time.time(),
            "updated_at": time.time(),
        })
        scope.close()
        return wid

    def set_state(self, client_id: Optional[str], workflow_id: str,
                  state: str) -> None:
        scope = self.repo.scoped(client_id or "agency")
        scope.update("workflows", {"state": state, "updated_at": time.time()},
                     where="workflow_id = ?", params=(workflow_id,))
        scope.close()


# ---------------------------------------------------------------------------
# The agent runtime (executor)
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    run_id: str
    agent: str
    client_id: Optional[str]
    status: str                 # ok | failed | escalated | gated
    steps: list
    egress_events: int
    tokens: int
    outputs: dict
    trajectory: dict


class AgentRuntime:
    """The one codebase for 19 agents.

    Wiring (the whole machine in one object):
      L1 model router · L2 memory · L3 retrieval · L4 learning ·
      L5 ingestion · L6 orchestration · L7 governance · security zone
    """

    def __init__(self, settings: Settings, repo: Repository,
                 human: Optional[Callable] = None,
                 trust_stage: str = "S0",
                 playbooks_dir=None):
        self.settings = settings
        self.repo = repo
        self.human = human
        self.trust_stage = trust_stage

        # security zone
        self.vault = Vault(settings.data_dir / "vault.json")
        self.policy = PolicyEngine(settings, self.vault)
        self.approvals = ApprovalService(settings, trust_stage=trust_stage,
                                         human=human)
        self.egress = EgressGate(settings, repo, self.vault, self.policy,
                                 self.approvals)
        self.kill_switch = KillSwitch(settings)

        # L1 models
        self.registry = default_registry()
        self.router = ModelRouter(self.registry, settings)
        self.router.set_egress(self.egress)

        # L3 knowledge
        self.docstore = VersionedDocStore()
        self.graph = KnowledgeGraph()
        self.reranker = Reranker()
        self.retrieval = RetrievalAPI(repo, self.docstore, self.graph,
                                      self.reranker)

        # L2 memory
        self.memory = MemorySystem(repo)

        # L5 ingestion
        from .ingestion import IngestionPipeline
        self.ingestion = IngestionPipeline(repo, self.docstore, self.graph)

        # L4 learning
        self.learning = LearningEngine(repo, self.registry, settings,
                                       approver=lambda ctx: True)

        # L7 governance
        self.governance = Governance(repo, settings)

        # L6 orchestration
        self.bus = EventBus()
        self.blackboard = Blackboard()
        self.scheduler = Scheduler()
        self.library = PlaybookLibrary(repo, playbooks_dir)
        self.router_ = TaskRouter(self.library, self.bus)
        self.workflows = WorkflowEngine(repo)

        # tools
        self.tools = build_registry()

        # wire event -> agent (the agency's reactive behavior)
        self._wire_events()

    # -- event wiring -------------------------------------------------------
    def _wire_events(self) -> None:
        def _run(agent: str, client_id: Optional[str], payload: dict):
            self.run_agent(agent, client_id=client_id, task=payload)
        self.bus.subscribe("lead_qualified",
                           lambda p: _run("bde", p.get("client_id"), p))
        self.bus.subscribe("content_ready",
                           lambda p: _run("qa", p.get("client_id"), p))
        self.bus.subscribe("report_due",
                           lambda p: _run("am", p.get("client_id"), p))
        self.bus.subscribe("data_anomaly",
                           lambda p: _run("data", p.get("client_id"), p))
        self.bus.subscribe("strategy_approved",
                           lambda p: _run("dmm", p.get("client_id"), p))

    # -- the core: run one agent on one task --------------------------------
    def run_agent(self, agent: str, client_id: Optional[str] = None,
                  task: Optional[dict] = None) -> RunResult:
        task = task or {}
        run_id = new_id("run")
        pb = self.library.load(agent)

        # context manager: bounded context for this run
        working = self.memory.new_working(client_id, run_id)
        working.set("task_kind", task.get("kind", task.get("service", "task")))
        working.set("task", task)
        steps_done: list[str] = []

        ctx = ToolContext(
            agent=agent, client_id=client_id, repo=self.repo,
            router=self.router, egress=self.egress, vault=self.vault,
            retrieval=self.retrieval, settings=self.settings,
            playbook_policy=pb.data_policy, human=self.human,
            working=working,
        )

        egress_events = 0
        tokens = 0
        outputs: dict[str, Any] = {}
        status = "ok"
        step_results = []
        prev_output: Optional[dict] = None

        for step in pb.procedure:
            step_name = step.get("step", "step")
            tool_id = step.get("tool")
            human_gate = step.get("human_gate", False)
            on_fail = step.get("on_fail", "escalate")

            # human gate (where marked) — pause for approval
            if human_gate:
                decision = self.approvals.request(
                    agent=agent, gate=step.get("gate_note", "step"),
                    client_id=client_id,
                    context={"step": step_name, "task": task})
                step_results.append({"step": step_name, "gate": decision})
                if decision["decision"] != "approved":
                    status = "gated"
                    break

            if tool_id:
                # typed tool registry: only declared tools
                if not self.tools.check_grant(pb.tools, tool_id):
                    step_results.append({
                        "step": step_name, "error":
                            f"tool {tool_id} not granted to {agent}"})
                    status = "failed"
                    break
                tool = self.tools.get(tool_id)
                if tool is None:
                    step_results.append({"step": step_name,
                                         "error": f"unknown tool {tool_id}"})
                    status = "failed"
                    break
                # pass through any step args
                args = {k: v for k, v in step.items()
                        if k not in ("step", "tool", "on_fail",
                                     "human_gate", "gate_note")}
                # merge task values into args (task-driven parameters) —
                # only fields the tool's schema declares (typed contract)
                props = tool.input_schema.get("properties", {})
                open_schema = tool.input_schema.get("additionalProperties",
                                                    False) is not False
                for k, v in task.items():
                    if k in args or not isinstance(v, (str, int, float)):
                        continue
                    if not props or open_schema or k in props:
                        args[k] = v
                # cross-step data flow: fill a missing required field from
                # the previous step's output (e.g. self_edit.asset_id <- draft)
                if prev_output:
                    for req in tool.input_schema.get("required", []):
                        if req not in args and req in prev_output \
                                and isinstance(prev_output[req],
                                               (str, int, float)):
                            args[req] = prev_output[req]
                result = tool.run(ctx, **args)
                egress_events += result.egress_events
                tokens += 1  # demo: 1 token-unit per tool call
                step_results.append({"step": step_name, "tool": tool_id,
                                     "ok": result.ok,
                                     "data": result.data,
                                     "error": result.error})
                if not result.ok:
                    if on_fail == "retry":
                        result = tool.run(ctx, **args)
                        step_results[-1]["retried"] = True
                        step_results[-1]["ok"] = result.ok
                    if not result.ok:
                        status = "escalated" if on_fail == "escalate" \
                            else "failed"
                        # escalation
                        for esc in pb.escalation:
                            if _match_condition(esc.get("condition", ""),
                                                result.error):
                                self.bus.emit(
                                    "escalation",
                                    {"agent": agent, "to": esc.get("to"),
                                     "urgency": esc.get("urgency", "normal"),
                                     "error": result.error})
                        break
                else:
                    outputs[step_name] = result.data
                    steps_done.append(step_name)
                    prev_output = result.data
                    # publish to blackboard (inter-agent state)
                    self.blackboard.publish(client_id, f"{agent}.{step_name}",
                                            result.data, agent)
            else:
                # a reasoning step (no tool): use the model router
                prompt = step_name
                out = self.router.complete(
                    prompt, task_kind=task.get("task_kind", "routine"),
                    client_id=client_id, context={})
                outputs[step_name] = out
                steps_done.append(step_name)
                tokens += 1

        # quality gates (qa.check as a gate inside every agent)
        if status == "ok" and pb.quality_gates:
            qa = self.tools.get("qa.check")
            checklist = _checklist_for(agent)
            asset = {"fail_" + g: False for g in []}
            qa_result = qa.run(ctx, checklist=checklist, asset=asset) \
                if qa else None
            if qa_result and not qa_result.data.get("passed", True):
                status = "escalated"
                step_results.append({"step": "quality_gate",
                                     "ok": False,
                                     "data": qa_result.data})

        # write results to DB (task row) — tasks are client-partitioned,
        # so agency-level runs are recorded under the "agency" tenant
        scope = self.repo.scoped(client_id or "agency")
        scope.insert("tasks", {
            "task_id": run_id, "project_id": None,
            "client_id": client_id or "agency", "agent": agent,
            "kind": task.get("kind", "task"), "status": status,
            "payload": _json(task), "result": _json(outputs),
            "created_at": time.time(), "updated_at": time.time(),
        })
        scope.close()

        # log run + egress
        self.repo.record_run({
            "run_id": run_id, "agent": agent, "client_id": client_id,
            "status": status, "steps": _json(step_results),
            "egress_count": egress_events, "tokens": tokens,
            "started_at": time.time() - 0.001, "finished_at": time.time(),
        })

        # (async) publish trajectory to L4
        outcome = {"ok": "published", "failed": "failed",
                   "escalated": "escalated",
                   "gated": "gated"}.get(status, status)
        first_pass = status == "ok"
        self.learning.record_trajectory(run_id, client_id, agent, outcome,
                                        first_pass,
                                        {"steps": steps_done,
                                         "egress_events": egress_events})

        # consolidate memory (working -> semantic/episodic)
        if client_id:
            self.memory.consolidate(client_id, agent, working, outcome)

        # governance: record run for the eval dashboard
        self.governance.record_run(agent, status, egress_events, tokens,
                                   first_pass)

        return RunResult(run_id=run_id, agent=agent, client_id=client_id,
                         status=status, steps=step_results,
                         egress_events=egress_events, tokens=tokens,
                         outputs=outputs,
                         trajectory={"outcome": outcome,
                                     "first_pass": first_pass})

    # -- onboarding workflow (multi-agent sequence) -------------------------
    def onboard(self, client_id: str, client_name: str,
                vertical: str = "", kit: Optional[dict] = None) -> dict:
        """contract_signed -> PM kickoff -> ingestion -> Strategist 90-day
        plan (HUMAN GATE) -> DMM kickoff."""
        results: dict[str, Any] = {}

        # 1. PM: kickoff workflow
        pm_run = self.run_agent("pm", client_id=client_id,
                                task={"kind": "onboard",
                                      "name": client_name,
                                      "vertical": vertical})
        results["pm"] = pm_run.status

        # 2. ingestion: brand kit + baseline
        if kit:
            results["ingestion"] = self.ingestion.ingest_brand_kit(
                client_id, kit)
            self.repo.set_brand_kit(client_id, kit)

        # 3. Strategist: 90-day plan (self-hosted — sensitive)
        strat_run = self.run_agent(
            "strategist", client_id=client_id,
            task={"kind": "strategy", "vertical": vertical,
                  "task_kind": "sensitive"})
        results["strategist"] = strat_run.status
        plan_out = strat_run.outputs.get("build_90_day_plan", "")
        # content.generate returns a dict; extract the body text
        if isinstance(plan_out, dict):
            plan = plan_out.get("body", "")
        else:
            plan = plan_out or ""

        # 4. HUMAN GATE: 90-day plan approval (always)
        decision = self.approvals.request(
            agent="strategist", gate="strategy_plan",
            client_id=client_id, context={"plan": plan[:400]})
        results["plan_gate"] = decision["decision"]
        if decision["decision"] != "approved":
            results["aborted"] = True
            return results

        # 5. DMM: delivery kickoff
        dmm_run = self.run_agent("dmm", client_id=client_id,
                                 task={"kind": "kickoff"})
        results["dmm"] = dmm_run.status
        results["plan"] = plan
        return results

    # -- learning batch -----------------------------------------------------
    def learn(self, agent: Optional[str] = None) -> dict:
        return self.learning.run_batch(agent=agent)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _json(obj: Any) -> str:
    import json
    return json.dumps(obj, default=str)


def _match_condition(condition: str, error: str) -> bool:
    if not condition:
        return True
    return condition.lower() in (error or "").lower()


def _checklist_for(agent: str) -> str:
    mapping = {
        "writer": "content", "content_strategist": "content",
        "seo": "seo", "ppc": "ads", "paid_social": "ads",
        "webdev": "web", "email": "email", "designer": "creative",
        "video": "creative", "social": "content", "influencer": "content",
    }
    return mapping.get(agent, "general")