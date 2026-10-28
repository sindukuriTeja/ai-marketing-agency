"""L3 Tool Library (Law 1 — write once).

Every capability is a tool with a fixed MCP-style contract:
  id · version · description · input_schema (rejects unknown fields) ·
  output_schema · egress (bool -> routes via Egress Gate) · data_class ·
  required_scope · idempotent · rate_limit · retry · audit · tests

Agents declare which tools they need; the runtime enforces it. No agent
may ship a private tool.

21 business tools + 1 meta-tool (egress.gate).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..config import C0, C1, C2, C3, C4
from ..db import Repository
from ..model import ModelRouter
from ..security import EgressGate, Vault


@dataclass
class ToolContext:
    """Everything a tool may touch, scoped to the calling agent + client."""
    agent: str
    client_id: Optional[str]
    repo: Repository
    router: ModelRouter
    egress: EgressGate
    vault: Vault
    retrieval: Any                      # RetrievalAPI
    settings: Any
    playbook_policy: dict = field(default_factory=dict)
    human: Optional[Callable] = None    # human gate for sensitive egress
    working: Any = None                 # WorkingMemory (current task)


@dataclass
class ToolResult:
    ok: bool
    tool: str
    data: dict = field(default_factory=dict)
    egress_events: int = 0
    error: str = ""


class Tool:
    """Base tool. Subclasses set the contract fields and implement _run."""
    id: str = ""
    version: str = "1.0.0"
    description: str = ""
    egress: bool = False
    data_class: str = C2
    required_scope: str = ""
    idempotent: bool = False
    rate_limit: int = 60
    retry: int = 2
    audit: bool = True
    tests: str = ""
    input_schema: dict = field(default_factory=dict)  # type: ignore
    output_schema: dict = field(default_factory=dict)  # type: ignore

    def _run(self, ctx: ToolContext, **args) -> ToolResult:
        raise NotImplementedError

    def _map_args(self, args: dict) -> dict:
        """Map schema property names onto the _run signature (keyword args)."""
        import inspect
        sig = inspect.signature(self._run)
        params = {name: param for name, param in sig.parameters.items()
                  if param.kind in (param.POSITIONAL_OR_KEYWORD,
                                    param.KEYWORD_ONLY)}
        mapped: dict = {}
        for k, v in args.items():
            if k in params:
                mapped[k] = v
            elif k + "_" in params:      # "type" -> "type_"
                mapped[k + "_"] = v
            else:
                mapped[k] = v
        return mapped

    def run(self, ctx: ToolContext, **args) -> ToolResult:
        # contract: reject unknown fields
        props = self.input_schema.get("properties", {})
        if props and self.input_schema.get("additionalProperties", False) is False:
            unknown = set(args) - set(props)
            if unknown:
                return ToolResult(False, self.id,
                                  error=f"unknown fields: {sorted(unknown)}")
        # required fields
        for req in self.input_schema.get("required", []):
            if req not in args:
                return ToolResult(False, self.id,
                                  error=f"missing required field: {req}")
        # translate schema property names -> _run signature names (e.g.
        # "type" -> "type_", "for" -> "for_") so the typed contract and the
        # implementation can't drift apart
        args = self._map_args(args)
        t0 = time.time()
        try:
            result = self._run(ctx, **args)
        except PermissionError as e:
            result = ToolResult(False, self.id, error=f"egress denied: {e}")
        except Exception as e:  # noqa: BLE001
            result = ToolResult(False, self.id, error=f"{type(e).__name__}: {e}")
        # audit (always true)
        if self.audit:
            ctx.repo.audit(
                actor=f"tool:{self.id}", action=f"tool.{self.id}",
                client_id=ctx.client_id,
                detail={"ok": result.ok, "ms": round((time.time() - t0) * 1000),
                        "egress_events": result.egress_events,
                        "error": result.error})
        return result


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.id] = tool

    def get(self, tool_id: str) -> Optional[Tool]:
        return self._tools.get(tool_id)

    def has(self, tool_id: str) -> bool:
        return tool_id in self._tools

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def check_grant(self, agent_tools: list[str], tool_id: str) -> bool:
        """The typed tool registry: an agent may only call declared tools."""
        return any(t.split("@")[0] == tool_id for t in agent_tools)