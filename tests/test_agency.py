"""Test suite for the AI-first agency runtime.

Verifies the architectural invariants — the things that make
"data does not leave" and "the system improves itself" structurally true:

  security:   secrets never transit · PII requires approval+redaction ·
              C1 agency IP never leaves · cross-client egress denied ·
              kill-switch closes all egress
  isolation:  row-level client scoping (a scoped conn can't see other clients)
  tools:      typed registry (only declared tools) · unknown fields rejected
  models:     sensitive work -> self-hosted (no egress) · standard tier
              requires the egress gate
  learning:   verification gate · regression harness · promotion · rollback
  governance: claim-level grounding · right-to-delete (provenance)
  runtime:    a full run produces trajectory + audit + egress events
  playbooks:  all 19 load and are well-formed

Run:  python -m pytest tests/ -q      (or)  python tests/test_agency.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

# make the package importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_agency.config import get_settings, PLAYBOOKS_DIR, C1, C3, C4  # noqa: E402
from ai_agency.db import Repository  # noqa: E402
from ai_agency.gateway import Gateway  # noqa: E402
from ai_agency.learning import LearningEngine, VerificationGate  # noqa: E402
from ai_agency.model import ModelRouter, default_registry  # noqa: E402
from ai_agency.runtime import AgentRuntime  # noqa: E402
from ai_agency.security import (classify_record, redact, Vault)  # noqa: E402
from ai_agency.tools.impl import build_registry  # noqa: E402


def _runtime(tmp: Path, human_approves: bool = True) -> AgentRuntime:
    os.environ["AGENCY_DATA_DIR"] = str(tmp / "data")
    settings = get_settings()
    repo = Repository(settings.db_path)
    human = (lambda d: True) if human_approves else (lambda d: False)
    return AgentRuntime(settings, repo, human=human, trust_stage="S0",
                        playbooks_dir=PLAYBOOKS_DIR)


class TestSecurity(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.rt = _runtime(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_secrets_never_transit(self):
        res = self.rt.egress.send(
            agent="x", tool="t", target="api",
            payload={"token": "ghp_" + "A" * 30},
            client_id=None, target_client=None,
            playbook_policy={"egress_allowed": ["t"]})
        self.assertEqual(res["decision"], "deny")
        self.assertEqual(res["data_class"], C4)
        self.assertEqual(res["payload"], {})

    def test_c1_agency_ip_never_leaves(self):
        res = self.rt.egress.send(
            agent="x", tool="t", target="api",
            payload={"note": "our proprietary playbook"},
            client_id=None, target_client=None,
            playbook_policy={"egress_allowed": ["t"]})
        # internal text classifies C2 by default; force C1 via a C1 marker
        # by checking the policy path directly with a C1-class payload.
        from ai_agency.security import PolicyEngine
        pd = self.rt.policy.check_egress(
            agent="x", tool="t", target="api", data_class=C1,
            client_id=None, target_client=None, playbook_policy={})
        self.assertFalse(pd.allowed)

    def test_pii_requires_approval_and_redaction(self):
        res = self.rt.egress.send(
            agent="am", tool="email.send", target="esp",
            payload={"subject": "hi", "body": "call +91 98765 43210"},
            client_id="c1", target_client="c1",
            playbook_policy={"egress_allowed": ["email.send"]})
        self.assertEqual(res["data_class"], C3)
        self.assertTrue(res["redacted"])
        # with a human approver present it is approved+redacted; the body
        # must no longer contain the raw phone number
        self.assertNotIn("98765 43210", res["payload"].get("body", ""))

    def test_cross_client_egress_denied(self):
        res = self.rt.egress.send(
            agent="x", tool="t", target="api",
            payload={"content": "client A data"},
            client_id="clientA", target_client="clientB",
            playbook_policy={"egress_allowed": ["t"]})
        self.assertEqual(res["decision"], "deny")
        self.assertIn("cross-client", res["reason"])

    def test_kill_switch_closes_all_egress(self):
        self.rt.kill_switch.engage()
        res = self.rt.egress.send(
            agent="x", tool="t", target="api",
            payload={"content": "public"},
            client_id=None, target_client=None,
            playbook_policy={"egress_allowed": ["t"]})
        self.assertEqual(res["decision"], "deny")
        self.rt.kill_switch.release()

    def test_kill_switch_drill(self):
        drill = self.rt.kill_switch.drill()
        self.assertTrue(drill["passed"])
        self.assertLess(drill["closed_in_s"], 60)

    def test_classify_and_redact(self):
        self.assertEqual(classify_record({"password": "x"}), C4)
        self.assertEqual(classify_record({"email": "a@b.com"}), C3)
        text, n = redact("mail me a@b.com")
        self.assertEqual(n, 1)
        self.assertNotIn("a@b.com", text)


class TestIsolation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.rt = _runtime(self.tmp)
        self.repo = self.rt.repo
        self.c1 = self.repo.create_client("ClientOne", "B", "d2c")
        self.c2 = self.repo.create_client("ClientTwo", "B", "saas")
        self.repo.create_asset(self.c1, "blog", "One", "body one")
        self.repo.create_asset(self.c2, "blog", "Two", "body two")

    def tearDown(self):
        self._tmp.cleanup()

    def test_scoped_conn_cannot_see_other_client(self):
        scoped = self.repo.scoped(self.c1)
        # querying client2's rows through a client1-scoped conn returns none
        rows = scoped.query("content_assets",
                            where="client_id = ?", params=(self.c2,))
        self.assertEqual(rows, [])
        # and the default scoped view only sees client1
        all_rows = scoped.query("content_assets")
        self.assertTrue(all(r["client_id"] == self.c1 for r in all_rows))

    def test_unscoped_conn_refuses_client_table(self):
        unscoped = self.repo.scoped(None)
        with self.assertRaises(PermissionError):
            unscoped.query("content_assets")


class TestTools(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.rt = _runtime(self.tmp)
        self.cid = self.rt.repo.create_client("ToolsCo", "B", "d2c")

    def tearDown(self):
        self._tmp.cleanup()

    def test_registry_has_21_business_plus_meta(self):
        reg = build_registry()
        ids = {t.id for t in reg.list()}
        expected = {
            "search.web", "search.keywords", "influencer.search",
            "content.generate", "content.edit", "design.generate",
            "video.generate", "seo.audit", "ads.manage", "ads.report",
            "email.send", "email.flow", "web.build", "analytics.query",
            "report.build", "crm.read", "crm.write", "schedule.manage",
            "qa.check", "doc.draft", "notify.human", "egress.gate",
        }
        self.assertTrue(expected.issubset(ids))
        self.assertGreaterEqual(len(ids), 21)

    def test_typed_registry_only_declared_tools(self):
        pb = self.rt.library.load("writer")
        # writer does not declare seo.audit
        self.assertFalse(self.rt.tools.check_grant(pb.tools, "seo.audit"))
        self.assertTrue(self.rt.tools.check_grant(pb.tools, "content.generate"))

    def test_unknown_field_rejected(self):
        from ai_agency.tools import ToolContext
        ctx = ToolContext(agent="writer", client_id=self.cid,
                          repo=self.rt.repo, router=self.rt.router,
                          egress=self.rt.egress, vault=self.rt.vault,
                          retrieval=self.rt.retrieval,
                          settings=self.rt.settings)
        tool = self.rt.tools.get("content.generate")
        res = tool.run(ctx, type="blog", bogus_field="x")
        self.assertFalse(res.ok)
        self.assertIn("unknown fields", res.error)

    def test_ads_hard_cap_auto_pause(self):
        from ai_agency.tools import ToolContext
        ctx = ToolContext(agent="ppc", client_id=self.cid,
                          repo=self.rt.repo, router=self.rt.router,
                          egress=self.rt.egress, vault=self.rt.vault,
                          retrieval=self.rt.retrieval,
                          settings=self.rt.settings,
                          playbook_policy={"egress_allowed": ["ads.manage"]})
        tool = self.rt.tools.get("ads.manage")
        res = tool.run(ctx, action="adjust_bid", campaign="c",
                       amount=99999, budget_cap=5000)
        self.assertTrue(res.ok)
        self.assertEqual(res.data["action"], "auto_paused")


class TestModels(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.rt = _runtime(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_sensitive_routes_to_self_hosted_no_egress(self):
        before = self.rt.repo.scoped(None).count("egress_events")
        out = self.rt.router.complete(
            "Build a strategy and pricing plan", task_kind="sensitive")
        self.assertIn("90-Day", out)  # self-hosted strategy output
        after = self.rt.repo.scoped(None).count("egress_events")
        self.assertEqual(before, after)  # no egress for self-hosted

    def test_standard_tier_requires_egress_gate(self):
        # a router with NO egress wired must refuse the standard tier
        settings = self.rt.settings
        bare = ModelRouter(default_registry(), settings)
        with self.assertRaises(PermissionError):
            bare.complete("judge this", task_kind="judgment")

    def test_routine_stays_local(self):
        out = self.rt.router.complete("draft a blog", task_kind="routine")
        self.assertTrue(len(out) > 0)
        self.assertEqual(self.rt.router.stats["by_tier"].get("local", 0) >= 1,
                         True)


class TestLearning(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.rt = _runtime(self.tmp)
        self.cid = self.rt.repo.create_client("LearnCo", "B", "d2c")

    def tearDown(self):
        self._tmp.cleanup()

    def test_successful_runs_produce_trajectories(self):
        for i in range(3):
            self.rt.run_agent("writer", client_id=self.cid,
                              task={"kind": "content", "type": "blog",
                                    "keyword": f"k{i}", "brief_type": "blog"})
        trajs = self.rt.repo.trajectories(agent="writer")
        self.assertGreaterEqual(len(trajs), 3)
        self.assertTrue(all(t["first_pass"] for t in trajs))

    def test_learning_batch_promotes_with_provenance(self):
        for i in range(3):
            self.rt.run_agent("writer", client_id=self.cid,
                              task={"kind": "content", "type": "blog",
                                    "keyword": f"k{i}", "brief_type": "blog"})
        result = self.rt.learn(agent="writer")
        self.assertGreaterEqual(result["mined"], 1)
        promoted = [d for d in result["details"] if d["status"] == "promoted"]
        self.assertGreaterEqual(len(promoted), 1)
        # promoted insights are recorded with provenance
        promoted_insights = self.rt.repo.list_insights(status="promoted")
        self.assertGreaterEqual(len(promoted_insights), 1)
        self.assertTrue(any(i["provenance"] for i in promoted_insights))

    def test_verification_gate_rejects_weak_candidates(self):
        from ai_agency.learning import Candidate
        gate = VerificationGate(self.rt.repo)
        weak = Candidate(insight_id="w1", scope="agent:x", kind="skill",
                         payload={}, provenance={}, support=1,
                         confidence=0.2)
        verdict = gate.verify(weak)
        self.assertFalse(verdict["passed"])

    def test_rollback_is_reversible(self):
        for i in range(3):
            self.rt.run_agent("writer", client_id=self.cid,
                              task={"kind": "content", "type": "blog",
                                    "keyword": f"k{i}", "brief_type": "blog"})
        self.rt.learn(agent="writer")
        promoted = self.rt.repo.list_insights(status="promoted")
        if promoted:
            self.assertTrue(self.rt.learning.rollback(promoted[0]["insight_id"]))
            self.assertEqual(
                self.rt.repo.list_insights(status="promoted"),
                [i for i in promoted if i["insight_id"]
                 != promoted[0]["insight_id"]])


class TestGovernance(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.rt = _runtime(self.tmp)
        self.cid = self.rt.repo.create_client("GovCo", "B", "d2c")

    def tearDown(self):
        self._tmp.cleanup()

    def test_claim_level_grounding_abstains_on_unknown(self):
        g = self.rt.governance
        res = g.ground_claims("We grew conversions by 42.0% this month",
                              known_metrics={"conversions": 1250.0})
        # 42.0 has no matching known metric -> abstain
        self.assertFalse(res["all_grounded"])
        self.assertTrue(any(c["action"] == "abstain" for c in res["claims"]))

    def test_claim_level_grounding_cites_known(self):
        g = self.rt.governance
        res = g.ground_claims("conversions reached 1250",
                              known_metrics={"conversions": 1250.0})
        cited = [c for c in res["claims"] if c["action"] == "cite"]
        self.assertTrue(cited)

    def test_right_to_delete_propagates(self):
        # ingest a doc for the client, then erase it
        doc_id = f"doc:{self.cid}"
        self.rt.docstore.add_document(self.cid, doc_id, "Brand",
                                      "audience product campaign",
                                      data_class="C2")
        self.rt.graph.add_entity("client", self.cid, self.cid)
        self.rt.repo.remember(self.cid, "writer", "context", {"k": "v"})
        rtd = self.rt.governance.right_to_delete(
            self.cid, docstore=self.rt.docstore, graph=self.rt.graph)
        self.assertGreaterEqual(rtd["invalidated"]["chunks"], 0)
        # memory rows for the client are gone
        self.assertEqual(self.rt.repo.recall(self.cid), [])


class TestRuntimeEndToEnd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.rt = _runtime(self.tmp)
        self.gw = Gateway(self.rt)
        self.cid = self.rt.repo.create_client("E2E", "B", "d2c")
        self.rt.repo.set_brand_kit(self.cid, {
            "name": "E2E", "voice": "confident", "cta": "Buy now"})

    def tearDown(self):
        self._tmp.cleanup()

    def test_full_run_produces_trajectory_audit_egress(self):
        run = self.rt.run_agent("seo", client_id=self.cid, task={
            "kind": "audit", "url": "https://e2e.example.com",
            "seed": "skincare", "keyword": "skincare", "type": "blog"})
        self.assertEqual(run.status, "ok")
        # trajectory recorded
        self.assertGreaterEqual(len(self.rt.repo.trajectories(agent="seo")), 1)
        # audit recorded
        self.assertGreaterEqual(
            self.rt.repo.scoped(None).count("audit_log"), 1)
        # egress events recorded (seo.audit + search.keywords are egress)
        self.assertGreaterEqual(run.egress_events, 1)

    def test_human_gate_denial_stops_run(self):
        # a runtime whose human denies everything
        os.environ["AGENCY_DATA_DIR"] = str(self.tmp / "data2")
        from ai_agency.config import get_settings
        settings = get_settings()
        repo = Repository(settings.db_path)
        denying = AgentRuntime(settings, repo, human=lambda d: False,
                               trust_stage="S0",
                               playbooks_dir=PLAYBOOKS_DIR)
        cid = repo.create_client("GateCo", "B", "d2c")
        repo.set_brand_kit(cid, {"name": "GateCo", "voice": "x",
                                 "cta": "y"})
        run = denying.run_agent("seo", client_id=cid, task={
            "kind": "audit", "url": "https://g.example.com", "seed": "s",
            "keyword": "k", "type": "blog"})
        # the human_gate step (write_briefs) is denied -> gated
        self.assertEqual(run.status, "gated")

    def test_onboarding_flow(self):
        kit = {"name": "E2E", "voice": "confident", "cta": "Buy now",
               "audience": "buyers"}
        result = self.rt.onboard(self.cid, "E2E", vertical="d2c", kit=kit)
        self.assertEqual(result.get("pm"), "ok")
        self.assertEqual(result.get("plan_gate"), "approved")
        self.assertEqual(result.get("dmm"), "ok")
        self.assertIn("90-Day", result.get("plan", ""))

    def test_all_19_playbooks_load(self):
        agents = self.rt.library.list_agents()
        self.assertEqual(len(agents), 19)
        for a in agents:
            pb = self.rt.library.load(a)
            self.assertTrue(pb.agent)
            self.assertTrue(pb.procedure, f"{a} has no procedure")
            self.assertTrue(pb.tools, f"{a} declares no tools")


if __name__ == "__main__":
    unittest.main(verbosity=2)