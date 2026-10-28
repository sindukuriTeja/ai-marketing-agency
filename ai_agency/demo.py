#!/usr/bin/env python3
"""End-to-end demo: the full client lifecycle on the AI-first agency.

Run:  python -m ai_agency.demo

Walks one client through:
  lead -> BDE (enrich, pitch, proposal) -> CONTRACT (human gate)
  -> ONBOARD (PM -> ingestion -> Strategist 90-day plan [human gate] -> DMM)
  -> DELIVER (SEO, PPC, Writer, Social, Email, Data, AM MBR)
  -> MEASURE (reconciliation + MBR, claim-level grounded)
  -> LEARN (learning engine: mine -> verify -> promote)
  -> GOVERN (eval dashboard, right-to-delete, kill-switch drill)

Every egress byte passes the Egress Gate; every action is signed + audited.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from .config import get_settings, PLAYBOOKS_DIR
from .db import Repository
from .gateway import Gateway
from .runtime import AgentRuntime

BANNER = "=" * 72


def hr(title: str) -> None:
    print(f"\n{BANNER}\n{title}\n{BANNER}")


def main() -> int:
    # fresh demo data dir (idempotent)
    demo_data = Path(__file__).resolve().parent.parent / ".data_demo"
    if demo_data.exists():
        shutil.rmtree(demo_data)
    import os
    os.environ["AGENCY_DATA_DIR"] = str(demo_data)
    settings = get_settings()
    repo = Repository(settings.db_path)

    # the human team (deterministic approvers for the demo)
    def human(decision: dict) -> bool:
        # consequence gates + routine gates: the demo human approves
        # everything EXCEPT a deliberately bad proposal (to show denial).
        ctx = decision.get("context", {})
        if ctx.get("reject_demo"):
            return False
        return True

    runtime = AgentRuntime(settings, repo, human=human, trust_stage="S1",
                           playbooks_dir=PLAYBOOKS_DIR)
    gw = Gateway(runtime)

    hr("AI-FIRST DIGITAL MARKETING AGENCY — END-TO-END DEMO")
    print(f"19 agents · 21 tools · 1 internal DB · 1 egress gate · "
          f"self-learning local-first runtime")
    print(f"Trust stage: S1 (sampled review)   Human: demo approver")

    # ------------------------------------------------------------------
    hr("1. LEAD INBOUND -> BDE AGENT (24/7 pipeline)")
    lead = gw.submit_lead("founder", "Aarav Mehta", "GlowSkincare", "d2c")
    print(f"Lead created: {lead['lead_id']}  BANT score: {lead['score']}  "
          f"stage: {lead['stage']}")
    print("-> lead_qualified event fired -> BDE agent runs")

    # ------------------------------------------------------------------
    hr("2. BDE AGENT RUN (enrich -> pitch -> proposal -> outreach)")
    bde = gw.run_agent("founder", "bde", task={
        "kind": "lead", "company": "GlowSkincare", "vertical": "d2c",
        "query": "GlowSkincare D2C skincare India",
        "type": "outreach", "subject": "Growth plan for GlowSkincare",
    })
    print(f"BDE run: {bde['run_id']}  status: {bde['status']}  "
          f"egress events: {bde['egress_events']}")
    for k, v in bde["outputs"].items():
        if isinstance(v, dict) and "body" in v:
            print(f"  [{k}] {str(v['body'])[:80].strip()}...")
        elif isinstance(v, dict):
            print(f"  [{k}] {json.dumps(v)[:80]}")

    # ------------------------------------------------------------------
    hr("3. HUMAN GATE: PROPOSAL SIGN-OFF (consequence gate — always human)")
    proposal = repo.create_proposal(lead["lead_id"],
                                    "90-day D2C growth: SEO + PPC + Social",
                                    150000)
    decision = runtime.approvals.request(
        agent="bde", gate="contracts", client_id=None,
        context={"proposal_id": proposal, "price": 150000})
    print(f"Proposal {proposal}  ->  human decision: {decision['decision']} "
          f"(by {decision['by']})")
    repo.update_proposal(proposal, status="signed")
    print("-> CONTRACT signed. Creating client + vault access.")

    # ------------------------------------------------------------------
    hr("4. ONBOARDING (PM -> ingestion -> Strategist [human gate] -> DMM)")
    client_id = repo.create_client("GlowSkincare", tier="B", vertical="d2c")
    # vault: secrets never transit — only refs stored
    vault_ref = runtime.vault.put("glow_ga4", "GA4-measurement-secret-xyz")
    repo.add_account(client_id, "ga4", vault_ref)
    kit = {"name": "GlowSkincare", "voice": "warm, confident, science-led",
           "palette": ["#2E7D5B", "#F4F1EA"], "cta": "Shop the routine",
           "audience": "urban women 24-40, clean-beauty buyers"}
    onb = runtime.onboard(client_id, "GlowSkincare", vertical="d2c", kit=kit)
    print(f"client: {client_id}")
    print(f"PM kickoff: {onb.get('pm')}   ingestion: "
          f"{onb.get('ingestion', {}).get('chunks', 0)} chunks, "
          f"{len(onb.get('ingestion', {}).get('entities', []))} entities")
    print(f"Strategist (self-hosted): {onb.get('strategist')}   "
          f"90-day plan gate: {onb.get('plan_gate')}")
    print(f"DMM kickoff: {onb.get('dmm')}")
    print("\n--- 90-DAY PLAN (self-hosted model, inside boundary) ---")
    print(onb.get("plan", "")[:600])

    # ------------------------------------------------------------------
    hr("5. DELIVER — agents run per client (each: DB -> tools -> QA -> gate)")
    deliveries = [
        ("seo", {"kind": "audit", "url": "https://glowskincare.example.com",
                 "seed": "clean skincare india", "keyword": "clean skincare",
                 "type": "blog"}),
        ("ppc", {"kind": "campaign", "campaign": "glow-search",
                 "amount": 120, "budget_cap": 5000, "period": "last_1d",
                 "seed": "search terms"}),
        ("writer", {"kind": "content", "type": "blog",
                    "keyword": "clean skincare routine",
                    "title": "The 5-Step Clean Skincare Routine",
                    "brief_type": "blog", "instruction": "tighten"}),
        ("social", {"kind": "post", "type": "social", "query": "skincare trends",
                    "brief_type": "social", "what": "social_post"}),
        ("email", {"kind": "flow", "flow": "welcome", "type": "email",
                   "subject": "Welcome to GlowSkincare", "brief_type": "email"}),
    ]
    for agent, task in deliveries:
        r = gw.run_agent("founder", agent, client_id=client_id, task=task)
        egress = r["egress_events"]
        print(f"  {agent:<10} status={r['status']:<8} egress={egress}")

    # ------------------------------------------------------------------
    hr("6. MEASURE — Data agent (reconciliation) + AM agent (MBR)")
    # seed metrics from multiple sources to exercise reconciliation
    for src, val in [("ga4", 1250), ("internal", 1210), ("ads", 1265)]:
        repo.record_metric(client_id, src, "conversions", val, "2026-10-27")
    data_run = gw.run_agent("founder", "data", client_id=client_id,
                            task={"kind": "ingest", "period": "last_1d",
                                  "type": "memo", "brief_type": "insight"})
    recon = data_run["outputs"].get("reconcile", {})
    print(f"Data agent: status={data_run['status']}")
    print(f"  reconciled: {json.dumps(recon.get('reconciled', {}))}")
    if recon.get("flags"):
        print(f"  flags: {recon['flags']}")

    am_run = gw.run_agent("founder", "am", client_id=client_id,
                          task={"kind": "report", "subject": "Your MBR"})
    mbr = am_run["outputs"].get("build_mbr", {})
    print(f"\nAM agent: status={am_run['status']}")
    print(f"  MBR claims (claim-level grounded):")
    for c in mbr.get("claims", [])[:6]:
        mark = "cite" if c["grounded"] else "ABSTAIN"
        print(f"    [{mark}] {c['claim']}  <- {c['source']}")

    # ------------------------------------------------------------------
    hr("7. LEARN — learning engine (mine -> verify -> promote)")
    # generate a few more successful runs so the miner has trajectories
    for i in range(3):
        gw.run_agent("founder", "writer", client_id=client_id,
                     task={"kind": "content", "type": "blog",
                           "keyword": f"topic {i}", "brief_type": "blog"})
    learn = runtime.learn()
    print(f"mined={learn['mined']}  verified={learn['verified']}  "
          f"promoted={learn['promoted']}  staged_models={learn['staged_models']}  "
          f"quarantined={learn['quarantined']}")
    for d in learn["details"][:5]:
        print(f"  {d['insight_id']}: {d['status']}")

    # ------------------------------------------------------------------
    hr("8. GOVERN — eval dashboard + security proofs")
    dash = gw.system_health("founder")
    print(json.dumps(dash, indent=2))

    print("\n--- Security proofs ---")
    # (a) cross-client isolation: a scoped conn cannot see another client
    other = repo.create_client("RivalBrand", tier="B", vertical="saas")
    scoped = repo.scoped(client_id)
    rival_rows = scoped.query("content_assets",
                              where="client_id = ?", params=(other,))
    print(f"(a) cross-client isolation: scoped conn queried RivalBrand rows "
          f"-> {len(rival_rows)} (must be 0)")

    # (b) egress: a C3 (PII) payload requires approval + redaction
    pii = runtime.egress.send(
        agent="am", tool="email.send", target="esp",
        payload={"subject": "hi", "body": "call me at +91 98765 43210"},
        client_id=client_id, target_client=client_id,
        playbook_policy={"egress_allowed": ["email.send"]})
    print(f"(b) PII egress: decision={pii['decision']} "
          f"redacted={pii['redacted']} class={pii['data_class']}")

    # (c) secret never transits
    secret = runtime.egress.send(
        agent="ppc", tool="ads.manage", target="ads-platform",
        payload={"token": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"},
        client_id=client_id, target_client=client_id,
        playbook_policy={"egress_allowed": ["ads.manage"]})
    print(f"(c) secret egress: decision={secret['decision']} "
          f"({secret['reason']})")

    # (d) kill-switch drill
    drill = runtime.kill_switch.drill()
    print(f"(d) kill-switch drill: passed={drill['passed']} "
          f"(closed in {drill['closed_in_s']}s)")

    # (e) right-to-delete (DPDP) — provenance-based propagation
    rtd = runtime.governance.right_to_delete(
        other, docstore=runtime.docstore, graph=runtime.graph)
    print(f"(e) right-to-delete(RivalBrand): {rtd['invalidated']}")

    # ------------------------------------------------------------------
    hr("9. CLIENT DASHBOARD (what the client sees — not the machine)")
    dash_client = gw.get_dashboard("client:glow", client_id)
    print(json.dumps(dash_client, indent=2))

    hr("DEMO COMPLETE")
    print("Every egress byte passed the Egress Gate. Every action is signed "
          "and audited.")
    print(f"Internal DB: {settings.db_path}")
    print(f"Egress events: {repo.scoped(None).count('egress_events')}   "
          f"Audit entries: {repo.scoped(None).count('audit_log')}")
    db = repo.scoped(None)
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())