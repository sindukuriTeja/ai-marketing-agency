"""L1 — Local Inference Runtime.

Model registry + serving + routing + prompt/semantic caching.

Backends:
  - LocalBackend:      deterministic on-prem "model" (rule-based). The demo
                       workhorse — zero network, zero cost, reproducible.
                       In production this is a quantized model served by
                       vLLM/Ollama behind an OpenAI-compatible API.
  - StandardBackend:   external frontier model, zero-retention, and it may
                       ONLY be reached through the Egress Gate.
  - SelfHostedBackend: a model that runs inside the boundary for sensitive
                       work (strategy, pricing, contracts).

The ModelRouter picks the smallest model that can do the job (Part 2.3):
routine -> local, judgment -> standard (egress-gated), sensitive ->
self-hosted. Prompt caching and semantic caching keep token usage low.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .config import TIER_LOCAL, TIER_STANDARD, TIER_SELF_HOSTED


# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

@dataclass
class ModelSpec:
    name: str
    tier: str
    kind: str                 # chat | code | embed | guardrail | design | video
    config: dict = field(default_factory=dict)
    version: str = "1.0.0"
    sandbox_policy: str = "local"


class ModelRegistry:
    """type + model + config + version. L4 stages improved models here."""

    def __init__(self):
        self._models: dict[str, ModelSpec] = {}
        self._staged: list[dict] = []

    def register(self, spec: ModelSpec) -> None:
        self._models[spec.name] = spec

    def get(self, name: str) -> Optional[ModelSpec]:
        return self._models.get(name)

    def by_tier_kind(self, tier: str, kind: str = "chat") -> Optional[ModelSpec]:
        for spec in self._models.values():
            if spec.tier == tier and spec.kind == kind:
                return spec
        return None

    def stage(self, name: str, adapter: dict) -> None:
        """L4 -> registry: a verified model/adapter, pending activation."""
        self._staged.append({"name": name, "adapter": adapter,
                             "ts": time.time()})

    def activate_staged(self, name: str) -> bool:
        for entry in self._staged:
            if entry["name"] == name:
                spec = self._models.get(name)
                if spec:
                    spec.config.update(entry["adapter"].get("config", {}))
                    spec.version = entry["adapter"].get("version",
                                                        spec.version)
                self._staged.remove(entry)
                return True
        return False

    def list(self) -> list[ModelSpec]:
        return list(self._models.values())


def default_registry() -> ModelRegistry:
    reg = ModelRegistry()
    reg.register(ModelSpec("local-chat-7b", TIER_LOCAL, "chat",
                           config={"quant": "q4", "ctx": 8192}))
    reg.register(ModelSpec("local-code-7b", TIER_LOCAL, "code"))
    reg.register(ModelSpec("local-embed", TIER_LOCAL, "embed"))
    reg.register(ModelSpec("local-guardrail", TIER_LOCAL, "guardrail"))
    reg.register(ModelSpec("local-design", TIER_LOCAL, "design"))
    reg.register(ModelSpec("local-video", TIER_LOCAL, "video"))
    reg.register(ModelSpec("frontier-judgment", TIER_STANDARD, "chat",
                           config={"zero_retention": True}))
    reg.register(ModelSpec("self-hosted-strategy", TIER_SELF_HOSTED, "chat",
                           config={"region": "in", "inside_boundary": True}))
    return reg


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class BaseBackend:
    tier = TIER_LOCAL
    egress_required = False  # True => may only be called via EgressGate

    def complete(self, prompt: str, system: str = "",
                 kind: str = "chat", context: Optional[dict] = None) -> str:
        raise NotImplementedError


class LocalBackend(BaseBackend):
    """Deterministic on-prem model.

    Produces structured, on-brand output from the retrieved context. This
    is what makes the whole pipeline runnable and testable with no network
    and no API keys, while exercising every architectural path.
    """
    tier = TIER_LOCAL
    egress_required = False

    def complete(self, prompt: str, system: str = "",
                 kind: str = "chat", context: Optional[dict] = None) -> str:
        context = context or {}
        voice = context.get("voice", "clear and confident")
        cta = context.get("cta", "Learn more")
        keyword = context.get("keyword", "")
        brand = context.get("brand", "")

        if kind == "code":
            return self._code(prompt, context)
        if kind == "guardrail":
            return self._guardrail(prompt)
        if kind == "embed":
            return json.dumps(self._embed(prompt))
        if kind in ("design", "video"):
            return self._creative(prompt, kind, context)

        # chat / reasoning / drafting
        return self._draft(prompt, system, voice, cta, keyword, brand)

    @staticmethod
    def _draft(prompt, system, voice, cta, keyword, brand) -> str:
        title = re.search(r"title[:=]\s*(.+)", prompt, re.I)
        title = title.group(1).strip() if title else (
            keyword.title() + " Guide" if keyword else "Draft")
        lines = [
            f"# {title}",
            "",
            f"Written in a {voice} voice for {brand or 'the client'}.",
            "",
            "## Why it matters",
            f"Teams searching for '{keyword or 'this topic'}' want a direct, "
            "practical answer — not filler. This piece leads with the outcome.",
            "",
            "## What to do",
            "1. Start from the customer's current state and the gap they feel.",
            "2. Show the mechanism: what changes and why it works.",
            "3. Give one concrete next step they can take today.",
            "",
            f"**Next step:** {cta}.",
        ]
        return "\n".join(lines)

    @staticmethod
    def _code(prompt, context) -> str:
        return (
            "<!doctype html>\n<html lang=\"en\">\n<head>\n"
            "  <meta charset=\"utf-8\">\n"
            "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            f"  <title>{context.get('brand', 'Client')}</title>\n"
            "  <style>body{font-family:system-ui;margin:0}header{padding:2rem}</style>\n"
            "</head>\n<body>\n"
            "  <header><h1>" + context.get("brand", "Client") + "</h1></header>\n"
            "  <main><section class=\"hero\"><h2>" +
            context.get("headline", "Grow faster") +
            "</h2><a class=\"cta\" href=\"#\">" +
            context.get("cta", "Get started") + "</a></section></main>\n"
            "</body>\n</html>\n"
        )

    @staticmethod
    def _guardrail(text: str) -> str:
        # prompt-injection + PII screen (the L1 guardrail model)
        flags = []
        lowered = text.lower()
        for marker in ("ignore previous", "disregard", "you are now",
                       "system prompt", "reveal your"):
            if marker in lowered:
                flags.append("prompt_injection")
        if re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text):
            flags.append("pii_email")
        if re.search(r"\b\d{16}\b", text):
            flags.append("pii_number")
        return json.dumps({"untrusted": True, "flags": flags,
                           "treat_as": "data"})

    @staticmethod
    def _embed(text: str) -> list[float]:
        # deterministic hash-based embedding (stand-in for local embed model)
        vec = []
        for i in range(16):
            h = hashlib.sha256(f"{i}:{text}".encode()).digest()
            vec.append((h[0] - 128) / 128.0)
        norm = sum(x * x for x in vec) ** 0.5 or 1.0
        return [round(x / norm, 5) for x in vec]

    @staticmethod
    def _creative(prompt, kind, context) -> str:
        brand = context.get("brand", "Client")
        if kind == "design":
            return json.dumps({
                "template": context.get("template", "social-post"),
                "variants": [
                    {"id": "v1", "layout": "headline-left",
                     "headline": context.get("headline", "Grow faster"),
                     "cta": context.get("cta", "Learn more"),
                     "palette": context.get("palette", ["#111", "#fff"])},
                    {"id": "v2", "layout": "center-stack",
                     "headline": context.get("headline", "Grow faster"),
                     "cta": context.get("cta", "Learn more"),
                     "palette": context.get("palette", ["#111", "#fff"])},
                ],
                "brand_compliant": True,
                "brand": brand,
            })
        # video
        return json.dumps({
            "hook": context.get("hook", "Stop scrolling — this changes everything."),
            "hook_variants": [
                context.get("hook", "Stop scrolling — this changes everything."),
                "You're doing this wrong (and it's fixable in a day).",
                "The 3-second reason your results stalled.",
            ],
            "duration_s": 30,
            "captions": True,
            "audio_licensed": True,
            "brand_lut": brand,
            "exports": ["9:16", "1:1", "16:9"],
        })


class StandardBackend(BaseBackend):
    """External frontier model. egress_required=True: the runtime only
    calls this through the Egress Gate, which classifies/redacts/minimizes
    the prompt and logs it. Zero-retention contract assumed."""
    tier = TIER_STANDARD
    egress_required = True

    def __init__(self, egress_gate, agent: str):
        self.gate = egress_gate
        self.agent = agent

    def complete(self, prompt: str, system: str = "",
                 kind: str = "chat", context: Optional[dict] = None,
                 client_id: Optional[str] = None) -> str:
        # The prompt itself is egress: it passes the gate like any byte.
        result = self.gate.send(
            agent=self.agent, tool="model.frontier",
            target="external-model",
            payload={"prompt": prompt, "system": system},
            client_id=client_id,
            target_client=client_id,
            field_allowlist=["prompt", "system"],
        )
        if not result["ok"]:
            raise PermissionError(
                f"frontier model egress denied: {result['reason']}")
        # In production: call the external API here. In the demo, fall back
        # to the local generator so the pipeline stays runnable.
        return LocalBackend().complete(prompt, system, kind, context)


class SelfHostedBackend(BaseBackend):
    """Model inside the boundary for sensitive work. No egress at all —
    the prompt never leaves, so this path is the strongest privacy tier."""
    tier = TIER_SELF_HOSTED
    egress_required = False

    def complete(self, prompt: str, system: str = "",
                 kind: str = "chat", context: Optional[dict] = None) -> str:
        # sensitive work: strategy, pricing, contracts
        if "strategy" in prompt.lower() or "plan" in prompt.lower():
            return (
                "# 90-Day Growth Plan (self-hosted)\n\n"
                "## Phase 1 (Days 1–30): Foundation\n"
                "- Baseline audit + tracking verification\n"
                "- Quick wins: technical fixes, top-3 content briefs\n\n"
                "## Phase 2 (Days 31–60): Activation\n"
                "- Launch 2 channels with measured hypotheses\n"
                "- CRO on the highest-intent landing page\n\n"
                "## Phase 3 (Days 61–90): Scale\n"
                "- Shift budget to the winning channel (evidence-based)\n"
                "- Document what worked -> playbook update\n\n"
                "Budget allocation: 40% proven channel, 40% test, 20% reserve."
            )
        return LocalBackend().complete(prompt, system, kind, context)


# ---------------------------------------------------------------------------
# Prompt cache + semantic cache (Part 2.3)
# ---------------------------------------------------------------------------

class PromptCache:
    """Stable context (playbook + SOPs + brand kit) is cached by hash."""

    def __init__(self):
        self._cache: dict[str, str] = {}

    def key(self, *parts) -> str:
        return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()

    def get(self, key: str) -> Optional[str]:
        return self._cache.get(key)

    def put(self, key: str, value: str) -> None:
        self._cache[key] = value

    def stats(self) -> dict:
        return {"entries": len(self._cache)}


class SemanticCache:
    """Near-identical requests return cached responses.

    Demo: exact-match on a normalized key (same brief type + client).
    Production: embedding similarity threshold.
    """

    def __init__(self):
        self._cache: dict[str, dict] = {}

    @staticmethod
    def _norm(brief_type: str, client_id: Optional[str], kind: str) -> str:
        return f"{kind}:{brief_type}:{client_id or '-'}"

    def get(self, brief_type: str, client_id: Optional[str], kind: str):
        return self._cache.get(self._norm(brief_type, client_id, kind))

    def put(self, brief_type: str, client_id: Optional[str], kind: str,
            response: str) -> None:
        self._cache[self._norm(brief_type, client_id, kind)] = {
            "response": response, "ts": time.time()}

    def stats(self) -> dict:
        return {"entries": len(self._cache)}


# ---------------------------------------------------------------------------
# Model router
# ---------------------------------------------------------------------------

class ModelRouter:
    """Picks the smallest model that can do the job.

    routing(task) -> tier:
      sensitive (strategy/pricing/contract) -> self-hosted
      judgment (Tier-A creative, final strategy) -> standard (egress)
      routine (everything else) -> local
    """

    SENSITIVE_MARKERS = ("strategy", "pricing", "contract", "financial",
                         "legal", "confidential")

    def __init__(self, registry: ModelRegistry, settings,
                 egress_gate=None, agent: str = "router"):
        self.registry = registry
        self.settings = settings
        self.local = LocalBackend()
        self.self_hosted = SelfHostedBackend()
        self._standard: Optional[StandardBackend] = None
        self._egress = egress_gate
        self.agent = agent
        self.prompt_cache = PromptCache()
        self.semantic_cache = SemanticCache()
        self.stats = {"calls": 0, "by_tier": {}, "cache_hits": 0,
                      "egress_calls": 0}

    def set_egress(self, gate) -> None:
        self._egress = gate
        self._standard = StandardBackend(gate, self.agent) if gate else None

    def route(self, prompt: str, *, task_kind: str = "routine",
              tier: Optional[str] = None) -> str:
        if tier:
            return tier
        lowered = prompt.lower()
        if any(m in lowered for m in self.SENSITIVE_MARKERS) \
                or task_kind == "sensitive":
            return TIER_SELF_HOSTED
        if task_kind in ("judgment", "tier_a"):
            return TIER_STANDARD
        return TIER_LOCAL

    def complete(self, prompt: str, *, system: str = "",
                 task_kind: str = "routine", kind: str = "chat",
                 context: Optional[dict] = None,
                 client_id: Optional[str] = None,
                 brief_type: Optional[str] = None,
                 tier: Optional[str] = None) -> str:
        self.stats["calls"] += 1

        # semantic cache (near-identical requests)
        if brief_type:
            hit = self.semantic_cache.get(brief_type, client_id, kind)
            if hit:
                self.stats["cache_hits"] += 1
                return hit["response"]

        chosen = self.route(prompt, task_kind=task_kind, tier=tier)
        self.stats["by_tier"][chosen] = self.stats["by_tier"].get(chosen, 0) + 1

        if chosen == TIER_SELF_HOSTED:
            out = self.self_hosted.complete(prompt, system, kind, context)
        elif chosen == TIER_STANDARD:
            if self._standard is None:
                # no egress wired: refuse to leave the boundary
                raise PermissionError(
                    "standard tier requires an Egress Gate; refusing to "
                    "eject data without the gate")
            self.stats["egress_calls"] += 1
            out = self._standard.complete(prompt, system, kind, context,
                                          client_id=client_id)
        else:
            out = self.local.complete(prompt, system, kind, context)

        if brief_type:
            self.semantic_cache.put(brief_type, client_id, kind, out)
        return out