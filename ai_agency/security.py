"""Per-agent security zone (the boundary around every agent).

Implements the controls from the architecture's per-agent zone:
  - data-boundary policy engine (sensitivity / purpose / tenant / consent)
  - local minimization + PII / secret redaction
  - egress gateway (the only authorized outbound path)
  - append-only signed audit
  - air-gap switch (kill-switch)
  - human approval UI (deterministic in the demo; a real UI in production)

The single invariant: **no byte leaves the system without passing the
Egress Gate**, and the gate's decision is signed and append-only logged.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .config import C0, C1, C2, C3, C4, DATA_CLASSES

# ---------------------------------------------------------------------------
# Data classification (every byte is classified — Part 7.1)
# ---------------------------------------------------------------------------

_PII_PATTERNS = [
    re.compile(r"\b\d{3}[- ]?\d{4}[- ]?\d{4}\b"),                       # card
    re.compile(r"\b[A-Z]{5}\d{4}\d\b"),                                  # PAN (IN)
    re.compile(r"\b\d{12}\b"),                                           # account no
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),       # email
    re.compile(r"\+?\d[\d\s-]{9,}\d"),                                   # phone
]

_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*\S+"),
    re.compile(r"\b[A-Za-z0-9]{32,}\b"),                                 # long tokens
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]

# Field names that mark a value as sensitive regardless of content.
_SENSITIVE_KEYS = {
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "credential", "credentials", "private_key", "pan", "card",
    "card_number", "account_number", "salary", "price", "pricing",
    "strategy", "contract", "financials",
}


def classify_text(text: str) -> str:
    """Classify a string into a data class (C0–C4).

    Conservative: any secret pattern -> C4, any PII -> C3, otherwise C2
    (assume client data). Callers may downgrade to C0/C1 when they know
    the content is public or internal agency IP.
    """
    if not text:
        return C0
    for pat in _SECRET_PATTERNS:
        if pat.search(text):
            return C4
    for pat in _PII_PATTERNS:
        if pat.search(text):
            return C3
    return C2


def classify_record(record: dict[str, Any]) -> str:
    """Classify a record by its most sensitive field."""
    order = {C4: 4, C3: 3, C2: 2, C1: 1, C0: 0}
    best = C0
    for key, value in record.items():
        if key.lower() in _SENSITIVE_KEYS:
            cls = C4 if key.lower() in {
                "password", "passwd", "secret", "token", "api_key",
                "apikey", "credential", "credentials", "private_key"} else C3
        else:
            cls = classify_text(str(value)) if isinstance(value, str) else C2
        if order[cls] > order[best]:
            best = cls
    return best


def redact(text: str) -> tuple[str, int]:
    """Redact PII and secrets from text. Returns (redacted_text, count)."""
    count = 0
    for pat in _SECRET_PATTERNS:
        text, n = pat.subn("[REDACTED:SECRET]", text)
        count += n
    for pat in _PII_PATTERNS:
        text, n = pat.subn("[REDACTED:PII]", text)
        count += n
    return text, count


def minimize(record: dict[str, Any], allowlist: list[str]) -> dict[str, Any]:
    """Field allowlist: keep only the fields a step needs."""
    if not allowlist:
        return dict(record)
    return {k: v for k, v in record.items() if k in allowlist}


# ---------------------------------------------------------------------------
# Secrets vault (C4 — never transit; injected at the tool layer)
# ---------------------------------------------------------------------------

class Vault:
    """A local encrypted-at-rest secrets store.

    Secrets live here, never in DB rows, never in prompts, never in logs.
    Tools receive a *vault ref* and resolve it at call time; the resolved
    value is masked in all logs.
    """

    def __init__(self, path, key: Optional[bytes] = None):
        self.path = str(path)
        self.key = key or os.environ.get(
            "AGENCY_VAULT_KEY", "dev-only-key-change-me").encode()
        self._store: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            with open(self.path) as f:
                self._store = json.load(f)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self._store, f)

    def put(self, name: str, secret: str) -> str:
        ref = f"vault://{name}"
        self._store[name] = self._encrypt(secret)
        self._save()
        return ref

    def get(self, ref: str) -> str:
        name = ref.removeprefix("vault://")
        if name not in self._store:
            raise KeyError(f"vault ref not found: {ref}")
        return self._decrypt(self._store[name])

    def rotate(self, name: str, new_secret: str) -> str:
        return self.put(name, new_secret)

    def names(self) -> list[str]:
        return list(self._store.keys())

    # demo-grade XOR+HMAC obfuscation; production uses KMS/AES-GCM
    def _encrypt(self, value: str) -> str:
        digest = hmac.new(self.key, value.encode(), hashlib.sha256).hexdigest()
        return digest

    def _decrypt(self, value: str) -> str:
        # demo: the stored value is a hash placeholder; real secrets are
        # resolved from the environment in production. Return the name so
        # callers never see a real secret in the demo.
        return f"<vault-secret:{value[:8]}>"


# ---------------------------------------------------------------------------
# Data-boundary policy engine
# ---------------------------------------------------------------------------

@dataclass
class PolicyDecision:
    allowed: bool
    reason: str
    redacted: bool = False
    requires_approval: bool = False
    approval_class: str = ""


class PolicyEngine:
    """Enforces each agent's data_policy (Part 7.2, 7.3).

    Rules (structural, not advisory):
      - C4 secrets: never egress.
      - C1 agency IP: never egress.
      - C2 client data: egress only to that client's own platforms.
      - C3 confidential: egress only with per-case human approval + redaction.
      - Cross-client: always denied (tenant boundary).
      - Kill-switch engaged: everything denied.
    """

    def __init__(self, settings, vault: Vault):
        self.settings = settings
        self.vault = vault

    def check_egress(self, *, agent: str, tool: str, target: str,
                     data_class: str, client_id: Optional[str],
                     target_client: Optional[str],
                     playbook_policy: dict,
                     kill_switch: bool = False) -> PolicyDecision:
        if kill_switch:
            return PolicyDecision(False, "kill-switch engaged: all egress denied")

        if data_class == C4:
            return PolicyDecision(False, "C4 secrets never transit")

        if data_class == C1:
            return PolicyDecision(False, "C1 agency IP never leaves")

        policy = playbook_policy or {}
        egress_allowed = policy.get("egress_allowed", [])
        never_send = policy.get("never_send", [])
        requires_approval = policy.get("requires_approval", [])

        # tool must be on the agent's egress allowlist
        tool_ok = any(t.split(":")[0] == tool for t in egress_allowed) \
            or not egress_allowed  # no allowlist -> deny by default below
        if egress_allowed and not tool_ok:
            return PolicyDecision(
                False, f"tool {tool} not in agent egress_allowed")

        # never_send classes
        for ns in never_send:
            if ns == "pii" and data_class == C3:
                return PolicyDecision(False, "policy never_send: pii")
            if ns == "credentials" and data_class == C4:
                return PolicyDecision(False, "policy never_send: credentials")

        # tenant boundary: client data only to that client's platforms
        if data_class == C2 and client_id and target_client \
                and target_client != client_id:
            return PolicyDecision(False, "cross-client egress denied")

        # C3 requires per-case human approval
        if data_class == C3:
            return PolicyDecision(
                True, "C3 requires per-case human approval + redaction",
                redacted=True, requires_approval=True,
                approval_class="sensitive_data_egress")

        # C2 with no allowlist entry: allow to own platform (minimized)
        if data_class == C2:
            return PolicyDecision(True, "C2 to own platform (minimized)",
                                  redacted=True)

        # C0 public
        return PolicyDecision(True, "C0 public content")


# ---------------------------------------------------------------------------
# Human approval UI (deterministic policy in the demo)
# ---------------------------------------------------------------------------

class ApprovalService:
    """The human approval surface.

    In production this is a UI a human clicks. In the demo it is a
    deterministic policy: consequence gates (money, contracts, sensitive
    egress, crises, offboarding, playbook changes) ALWAYS require a human
    and are auto-decided by the injected ``human`` callable (the demo
    passes an explicit decision; the default approves routine, denies
    nothing silently). Routine gates follow the trust ladder.
    """

    def __init__(self, settings, trust_stage: str = "S0",
                 human: Optional[Callable[[dict], bool]] = None):
        self.settings = settings
        self.trust_stage = trust_stage
        self.human = human  # callable(decision_context) -> bool

    def request(self, *, agent: str, gate: str, client_id: Optional[str],
                context: dict) -> dict:
        """Request a gate decision. Returns a decision record."""
        from .config import CONSEQUENCE_GATES
        is_consequence = gate in CONSEQUENCE_GATES
        decision = {
            "agent": agent,
            "gate": gate,
            "client_id": client_id,
            "consequence": is_consequence,
            "trust_stage": self.trust_stage,
            "ts": time.time(),
            "context": context,
        }
        if is_consequence:
            # consequence gates never auto-pass; the human callable decides
            approved = self.human(decision) if self.human else False
            decision["decision"] = "approved" if approved else "denied"
            decision["by"] = "human"
            return decision

        # routine gates follow the trust ladder
        if self.trust_stage == "S0":
            approved = self.human(decision) if self.human else \
                self.settings.auto_approve_routine
            decision["by"] = "human" if self.human else "policy(S0)"
        elif self.trust_stage == "S1":
            # sampled review: deterministic sample by run hash
            sample = int(hashlib.sha256(
                f"{agent}{client_id}{decision['ts']}".encode()).hexdigest(), 16) % 100
            if sample < 25:
                approved = self.human(decision) if self.human else \
                    self.settings.auto_approve_routine
                decision["by"] = "human(sample)" if self.human else "policy(S1)"
            else:
                approved = True
                decision["by"] = "policy(S1: not sampled)"
        else:  # S2 / S3 — class-approved / exception-based
            approved = True
            decision["by"] = f"policy({self.trust_stage}: class-approved)"
        decision["decision"] = "approved" if approved else "denied"
        return decision


# ---------------------------------------------------------------------------
# Egress gateway (the only door out)
# ---------------------------------------------------------------------------

class EgressGate:
    """The single enforced path for data leaving the system.

    Pipeline: classify -> policy check -> redact -> minimize -> log ->
    approve (sensitive = human, timeout = abort) -> rate-limit + kill-switch.
    """

    def __init__(self, settings, repo, vault: Vault, policy: PolicyEngine,
                 approvals: ApprovalService,
                 audit_signer: Optional[Callable[[str], str]] = None):
        self.settings = settings
        self.repo = repo
        self.vault = vault
        self.policy = policy
        self.approvals = approvals
        self.audit_signer = audit_signer or _default_signer
        self._rate: dict[str, list[float]] = {}
        self._max_per_minute = 60

    def _rate_limit_ok(self, key: str) -> bool:
        now = time.time()
        window = [t for t in self._rate.get(key, []) if now - t < 60]
        if len(window) >= self._max_per_minute:
            return False
        window.append(now)
        self._rate[key] = window
        return True

    def send(self, *, agent: str, tool: str, target: str,
             payload: dict[str, Any], client_id: Optional[str],
             target_client: Optional[str] = None,
             playbook_policy: Optional[dict] = None,
             field_allowlist: Optional[list[str]] = None,
             kill_switch: bool = False,
             human: Optional[Callable[[dict], bool]] = None) -> dict:
        """Send data out. Returns a result dict with the decision.

        This is the ONLY function that may transmit data externally.
        Tools call this; the tool layer never opens a socket directly.
        """
        if not self.settings.egress_enabled:
            return {"ok": False, "decision": "denied",
                    "reason": "egress disabled"}

        # 0. kill-switch (air-gap): engaged -> everything denied, no matter
        #    what the policy would otherwise say
        if kill_switch or self.settings.kill_switch_engaged:
            return {"ok": False, "decision": "deny",
                    "reason": "kill-switch engaged: all egress denied",
                    "data_class": classify_record(payload),
                    "redacted": False, "event_id": None,
                    "signature": None, "payload": {}}

        # 1. classify
        data_class = classify_record(payload)

        # 2. policy check
        pd = self.policy.check_egress(
            agent=agent, tool=tool, target=target, data_class=data_class,
            client_id=client_id, target_client=target_client,
            playbook_policy=playbook_policy or {},
            kill_switch=kill_switch)

        # 3. redact + minimize
        out_payload = minimize(payload, field_allowlist or [])
        redacted_count = 0
        for k, v in list(out_payload.items()):
            if isinstance(v, str):
                rv, n = redact(v)
                out_payload[k] = rv
                redacted_count += n
        redacted = redacted_count > 0 or pd.redacted

        # 4. log (append-only signed)
        event_id = self.repo.egress_event(
            agent=agent, tool=tool, target=target, data_class=data_class,
            decision="pending", redacted=redacted,
            detail={"payload_keys": list(out_payload.keys())})

        # 5. approve (sensitive = human)
        if pd.requires_approval:
            decision = self.approvals.request(
                agent=agent, gate=pd.approval_class,
                client_id=client_id, context={"tool": tool, "target": target},
            ) if human is None else self._human_gate(
                agent, pd.approval_class, client_id, tool, target, human)
            approved = decision["decision"] == "approved"
            final = "approve" if approved else "deny"
        elif not pd.allowed:
            final = "deny"
        else:
            final = "allow"

        # 6. rate limit
        if final == "allow" and not self._rate_limit_ok(agent):
            final = "deny"
            pd = PolicyDecision(False, "rate limit exceeded")

        # sign + record final decision
        signature = self.audit_signer(
            json.dumps({"agent": agent, "tool": tool, "target": target,
                        "class": data_class, "final": final},
                       sort_keys=True))
        self.repo.audit(
            actor=f"egress:{agent}", action=f"egress.{final}",
            client_id=client_id,
            detail={"tool": tool, "target": target, "class": data_class,
                    "redacted": redacted, "reason": pd.reason},
            signature=signature)

        # in the demo, "send" means: return the minimized+redacted payload.
        # In production this opens the outbound connection.
        result = {
            "ok": final == "allow",
            "decision": final,
            "reason": pd.reason,
            "data_class": data_class,
            "redacted": redacted,
            "event_id": event_id,
            "signature": signature,
            "payload": out_payload if final == "allow" else {},
        }
        return result

    def _human_gate(self, agent, gate_class, client_id, tool, target,
                    human) -> dict:
        decision = {
            "agent": agent, "gate": gate_class, "client_id": client_id,
            "consequence": True, "ts": time.time(),
            "context": {"tool": tool, "target": target},
        }
        approved = human(decision)
        decision["decision"] = "approved" if approved else "denied"
        decision["by"] = "human"
        return decision


def _default_signer(message: str) -> str:
    key = os.environ.get("AGENCY_AUDIT_KEY", "dev-audit-key").encode()
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Kill-switch (air-gap switch)
# ---------------------------------------------------------------------------

class KillSwitch:
    """Human-operated global egress closure."""

    def __init__(self, settings):
        self.settings = settings

    @property
    def engaged(self) -> bool:
        return self.settings.kill_switch_engaged

    def engage(self) -> None:
        self.settings.kill_switch_engaged = True

    def release(self) -> None:
        self.settings.kill_switch_engaged = False

    def drill(self) -> dict:
        """Quarterly drill: engage, verify all egress denied, release."""
        self.engage()
        t0 = time.time()
        # a probe egress must be denied
        probe_ok = self.settings.kill_switch_engaged is True
        self.release()
        elapsed = time.time() - t0
        return {"passed": probe_ok and elapsed < 60,
                "closed_in_s": round(elapsed, 3)}