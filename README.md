# The AI-First Digital Marketing Agency — Complete Project

A **runnable implementation** of the self-learning, local-first agent
ecosystem that delivers a full-service digital marketing agency: **19 AI
agents** (YAML playbooks) on **one shared runtime**, **one internal
database**, **one 21-tool library**, and **one gated door out** (the Egress
Gate) — with a small human team owning strategy, client trust, and final
decisions.

This is the code for the architecture in
`ai-marketing-agency-complete-project.md` / `.pdf`.

## The four design laws (enforced in code)

| Law | Where it's enforced |
|-----|---------------------|
| 1. Write once, reuse everywhere | `tools/` — 21 shared tools; `PlaybookLibrary` — agents are YAML, not code |
| 2. Internal database first | `db.py` — client-scoped repository (RLS stand-in); `sql/schema.sql` — real Postgres RLS |
| 3. Every agent = a playbook | `playbooks/*.yaml` — 19 versioned playbooks; `runtime.py` — one executor |
| 4. Data does not leave | `security.py` — Egress Gate, PolicyEngine, Vault, KillSwitch, signed audit |

## Quick start

```bash
cd ai-marketing-agency
pip install -r requirements.txt        # PyYAML + numpy (demo path is otherwise stdlib)

# the full end-to-end demo (lead -> contract -> onboard -> deliver -> measure -> learn -> govern)
python -m ai_agency.demo

# the test suite (architectural invariants)
python -m pytest tests/ -q             # or: python tests/test_agency.py

# CLI
python -m ai_agency.cli agents         # list the 19 agents
python -m ai_agency.cli tools          # list the 21 tools
python -m ai_agency.cli health         # system health dashboard
python -m ai_agency.cli onboard --name "Acme" --vertical d2c
python -m ai_agency.cli run writer --client <client_id> --type blog --keyword "clean skincare"
python -m ai_agency.cli learn          # run a learning batch
python -m ai_agency.cli killswitch     # run the kill-switch drill
```

No API keys, no network, no GPU required for the demo path — the local
model tier is a deterministic on-prem stand-in so every architectural path
(egress, RLS, learning, grounding) is exercised and testable. Swap in real
backends (below) to go live.

## Layout (mapped to the 8-layer architecture)

```
ai_agency/
  config.py            settings, data classes C0-C4, trust stages S0-S3
  db.py                L2 internal DB (SQLite stand-in) + client-scoped repo (RLS)
  security.py          per-agent zone: classify/redact/minimize, Vault,
                       PolicyEngine, ApprovalService, EgressGate, KillSwitch
  model.py             L1: ModelRegistry, Local/Standard/SelfHosted backends,
                       ModelRouter, prompt + semantic cache
  memory.py            L2: working/semantic/episodic/procedural + consolidation
  knowledge.py         L3: VersionedDocStore, VectorIndex (dense+BM25),
                       KnowledgeGraph, Reranker, RetrievalAPI
  ingestion.py         L5: parse -> normalize -> chunk -> version -> classify
                       -> embed -> entity extraction -> graph
  learning.py          L4: trajectory store, insight miner, VerificationGate,
                       RegressionHarness, promotion + rollback
  tools/               L3 tool library: contract + registry + the 21 tools
  runtime.py           L6: Playbook, Blackboard, EventBus, Scheduler,
                       TaskRouter, WorkflowEngine, AgentRuntime (the executor)
  governance.py        L7: eval dashboard, claim-level grounding, reflection,
                       right-to-delete
  gateway.py           center: User/API gateway (identity, tenant, policy)
  demo.py              end-to-end demo
  cli.py               command-line interface
playbooks/             the 19 agent playbooks (YAML)
sql/schema.sql         production PostgreSQL schema with real RLS
tests/test_agency.py   architectural-invariant test suite
```

## What the demo proves

Running `python -m ai_agency.demo` walks one client (GlowSkincare, D2C)
through the whole lifecycle and prints the security proofs:

1. **Lead → BDE** — enrichment, pitch, proposal, outreach (egress-gated).
2. **Human gate** — proposal sign-off is a consequence gate (always human).
3. **Onboarding** — PM kickoff → ingestion (chunks + entities) → Strategist
   90-day plan on the **self-hosted** tier → **human gate** → DMM kickoff.
4. **Delivery** — SEO, PPC (hard cap), Writer, Social, Email agents run per
   client, each: DB → tools → QA gate → human gate → publish.
5. **Measurement** — Data agent reconciliation (platform vs internal), AM
   agent MBR with **claim-level grounded** numbers.
6. **Learning** — the engine mines trajectories, verifies, and promotes a
   skill/LoRA with provenance (reversible).
7. **Governance** — eval dashboard + the security proofs:
   - (a) cross-client isolation (scoped conn can't see another client)
   - (b) PII egress → approval + redaction
   - (c) secret egress → denied (C4 never transits)
   - (d) kill-switch drill (closes all egress < 60s)
   - (e) right-to-delete (DPDP) via provenance

## Going to production

The demo path is deliberately dependency-free. To go live, swap the stand-ins:

| Stand-in (demo) | Production |
|-----------------|------------|
| SQLite (`db.py`) | PostgreSQL + pgvector (`sql/schema.sql`) — real RLS |
| `LocalBackend` (hash-embed + rule draft) | vLLM/Ollama quantized model (L1), OpenAI-compatible API |
| `StandardBackend` (falls back to local) | Real frontier API behind the Egress Gate, zero-retention contract |
| `SelfHostedBackend` (template plan) | Model on an India-region VM inside the boundary |
| `ApprovalService` (deterministic) | Real human approval UI (per-request / approved class) |
| `Scheduler` (on-demand) | Cron engine (sprints, MBRs, renewals) |
| Hash embedding | `sentence-transformers` local embedding model |
| HMAC audit signature | KMS-signed append-only audit |

The **contracts don't change** — only the backends. That's the point of the
architecture: the 19 playbooks, the 21 tools, and the egress policy are
identical whether the models are local or frontier.

## The moat (what compounds)

- **Procedural memory** — the playbook library (versioned, tested, promoted)
- **Taste library** — every human correction becomes a few-shot example
- **LoRA adapters** — vertical-specific drafters fine-tuned on approved work
- **Benchmarks + knowledge graph** — cross-client, anonymized, time-stamped

Each client adds trajectories → better insights/skills/models → higher
first-pass approval → fewer human minutes per deliverable. Delivery cost
stays flat as the client base grows: **more clients → more compute, not more
headcount.**

## Safety model (one line each)

- **C4 secrets** never transit — vault-only, injected at the tool layer.
- **C1 agency IP** never leaves.
- **C2 client data** egresses only to that client's own platforms.
- **C3 confidential** egresses only with per-case human approval + redaction.
- **Cross-client** access is structurally impossible (RLS) and red-team tested.
- **Kill-switch** closes all egress instantly; **signed append-only audit**
  makes "what left, when, where" always answerable.

See `../ai-marketing-agency-complete-project.pdf` for the full design,
business case, and 36-month roadmap.