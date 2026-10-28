#!/usr/bin/env python3
"""CLI for the AI-first agency runtime.

Examples:
  python -m ai_agency.cli demo                 # full end-to-end demo
  python -m ai_agency.cli agents               # list the 19 agents
  python -m ai_agency.cli tools                # list the 21 tools
  python -m ai_agency.cli health               # system health dashboard
  python -m ai_agency.cli run writer --client <id> --type blog --keyword "x"
  python -m ai_agency.cli onboard --name "Acme" --vertical d2c
  python -m ai_agency.cli learn                # run a learning batch
  python -m ai_agency.cli killswitch           # run the kill-switch drill
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .config import get_settings, PLAYBOOKS_DIR
from .db import Repository
from .gateway import Gateway
from .runtime import AgentRuntime
from .tools.impl import build_registry


def _runtime(human_approves: bool = True) -> AgentRuntime:
    settings = get_settings()
    repo = Repository(settings.db_path)
    human = (lambda d: True) if human_approves else (lambda d: False)
    return AgentRuntime(settings, repo, human=human, trust_stage="S1",
                        playbooks_dir=PLAYBOOKS_DIR)


def cmd_demo(_args) -> int:
    from .demo import main
    return main()


def cmd_agents(_args) -> int:
    rt = _runtime()
    agents = rt.library.list_agents()
    print(f"{len(agents)} agents:")
    for a in agents:
        pb = rt.library.load(a)
        print(f"  {a:<20} v{pb.version}  tools={len(pb.tool_ids)}  "
              f"tier={pb.data_policy.get('model_tier', 'standard')}")
    return 0


def cmd_tools(_args) -> int:
    reg = build_registry()
    tools = reg.list()
    print(f"{len(tools)} tools:")
    for t in tools:
        egress = "egress" if t.egress else "local "
        print(f"  {t.id:<20} v{t.version}  [{egress}] {t.description[:50]}")
    return 0


def cmd_health(_args) -> int:
    rt = _runtime()
    print(json.dumps(rt.governance.dashboard(), indent=2))
    return 0


def cmd_run(args) -> int:
    rt = _runtime()
    task = {"kind": "task"}
    for k in ("type", "keyword", "title", "cta", "campaign", "url", "seed",
              "flow", "template", "hook", "query", "subject", "body",
              "checklist", "kind", "service"):
        v = getattr(args, k, None)
        if v is not None:
            task[k] = v
    r = rt.run_agent(args.agent, client_id=args.client, task=task)
    print(json.dumps({"run_id": r.run_id, "agent": r.agent,
                      "status": r.status, "egress_events": r.egress_events,
                      "outputs": r.outputs}, indent=2, default=str))
    return 0 if r.status == "ok" else 1


def cmd_onboard(args) -> int:
    rt = _runtime()
    repo = rt.repo
    cid = repo.create_client(args.name, tier=args.tier,
                             vertical=args.vertical)
    kit = {"name": args.name, "voice": "clear and confident",
           "cta": "Learn more", "audience": args.audience or "target market"}
    result = rt.onboard(cid, args.name, vertical=args.vertical, kit=kit)
    print(json.dumps({"client_id": cid, **{k: v for k, v in result.items()
                                            if k != "plan"}},
                     indent=2, default=str))
    print("\n--- 90-day plan ---")
    print(result.get("plan", ""))
    return 0


def cmd_learn(_args) -> int:
    rt = _runtime()
    print(json.dumps(rt.learn(), indent=2, default=str))
    return 0


def cmd_killswitch(_args) -> int:
    rt = _runtime()
    print(json.dumps(rt.kill_switch.drill(), indent=2))
    return 0


def cmd_dashboard(args) -> int:
    rt = _runtime()
    gw = Gateway(rt)
    print(json.dumps(gw.get_dashboard("client", args.client), indent=2))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ai-agency",
                                description="AI-first digital marketing agency")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("demo", help="run the end-to-end demo").set_defaults(
        fn=cmd_demo)
    sub.add_parser("agents", help="list the 19 agents").set_defaults(
        fn=cmd_agents)
    sub.add_parser("tools", help="list the 21 tools").set_defaults(
        fn=cmd_tools)
    sub.add_parser("health", help="system health dashboard").set_defaults(
        fn=cmd_health)
    sub.add_parser("learn", help="run a learning batch").set_defaults(
        fn=cmd_learn)
    sub.add_parser("killswitch", help="run the kill-switch drill").set_defaults(
        fn=cmd_killswitch)

    pr = sub.add_parser("run", help="run one agent on a task")
    pr.add_argument("agent")
    pr.add_argument("--client", default=None)
    for k in ("type", "keyword", "title", "cta", "campaign", "url", "seed",
              "flow", "template", "hook", "query", "subject", "body",
              "checklist", "kind", "service"):
        pr.add_argument(f"--{k}", default=None)
    pr.set_defaults(fn=cmd_run)

    po = sub.add_parser("onboard", help="onboard a client")
    po.add_argument("--name", required=True)
    po.add_argument("--vertical", default="")
    po.add_argument("--tier", default="B")
    po.add_argument("--audience", default="")
    po.set_defaults(fn=cmd_onboard)

    pd = sub.add_parser("dashboard", help="client dashboard")
    pd.add_argument("--client", required=True)
    pd.set_defaults(fn=cmd_dashboard)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())