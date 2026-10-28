"""
ai_agency — The AI-First Digital Marketing Agency runtime.

A self-learning, local-first agent ecosystem that delivers a full-service
digital marketing agency: 19 AI agents (YAML playbooks) on one shared
runtime, one internal database, one 21-tool library, and one gated door
out (the Egress Gate).

Layer map (see the project document, Part 1):
  L1  local inference runtime     -> ai_agency.model
  L2  memory system               -> ai_agency.memory
  L3  local knowledge substrate   -> ai_agency.knowledge
  L4  learning engine             -> ai_agency.learning
  L5  specialist agent cores      -> ai_agency.ingestion
  L6  orchestration               -> ai_agency.runtime (router/workflow/scheduler)
  L7  governance & evaluation     -> ai_agency.governance
  zone  per-agent security        -> ai_agency.security
  center  user/api gateway        -> ai_agency.gateway
"""

__version__ = "1.0.0"