"""L5 — Specialist Agent Cores (Ingestion).

Everything a client gives the agency flows through ONE pipeline:
  parse -> normalize -> chunk -> version -> classify sensitivity +
  provenance -> (vector index + versioned doc store) -> entity/relation
  extraction -> knowledge graph

Sensitivity is classified at the door, before storage, so the egress
policy applies from the first second.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from .db import Repository, new_id
from .knowledge import KnowledgeGraph, VersionedDocStore
from .security import classify_text


class IngestionPipeline:
    def __init__(self, repo: Repository, store: VersionedDocStore,
                 graph: KnowledgeGraph):
        self.repo = repo
        self.store = store
        self.graph = graph

    def ingest(self, client_id: str, doc_id: str, title: str, text: str,
               source: str = "upload",
               data_class: Optional[str] = None) -> dict:
        """Ingest one document end-to-end. Returns a summary."""
        # 1. parse + normalize
        normalized = self._normalize(text)

        # 2. chunk + version + classify + embed (store does this)
        chunk_ids = self.store.add_document(
            client_id=client_id, doc_id=doc_id, title=title,
            text=normalized, source=source, data_class=data_class)

        # 3. entity / relation extraction -> knowledge graph
        entities = self._extract_entities(client_id, title, normalized)

        # 4. provenance record in the structured store (audit)
        self.repo.audit(
            actor="ingestion", action="ingest.document", client_id=client_id,
            detail={"doc_id": doc_id, "title": title,
                    "chunks": len(chunk_ids), "entities": entities,
                    "source": source})

        return {"doc_id": doc_id, "chunks": len(chunk_ids),
                "entities": entities, "ts": time.time()}

    @staticmethod
    def _normalize(text: str) -> str:
        text = re.sub(r"\r\n?", "\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _extract_entities(client_id: str, title: str, text: str) -> list[str]:
        """Lightweight entity extraction (demo heuristics).

        Production: a local NER model. Here we pull capitalized phrases and
        known marketing entity keywords so the graph is non-trivial.
        """
        found: dict[str, str] = {}
        # keyword -> entity type
        keyword_map = {
            "audience": "audience", "customer": "audience",
            "competitor": "competitor", "product": "product",
            "campaign": "campaign", "keyword": "keyword",
            "brand": "brand", "sku": "product",
        }
        lowered = text.lower()
        for kw, etype in keyword_map.items():
            if kw in lowered:
                found[f"{etype}:{kw}"] = etype
        # capitalized multi-word phrases (naive proper-noun extraction)
        for m in re.finditer(r"\b([A-Z][a-z]+(?: [A-Z][a-z]+){0,2})\b", text):
            phrase = m.group(1)
            if len(phrase) > 3 and phrase not in ("The", "This", "That"):
                found.setdefault(f"entity:{phrase.lower()}", "entity")
        return sorted(found.keys())[:20]

    def ingest_brand_kit(self, client_id: str, kit: dict[str, str]) -> dict:
        """Ingest a client's brand kit as a versioned document + graph node."""
        doc_id = f"brand_kit:{client_id}"
        text = "\n".join(f"{k}: {v}" for k, v in kit.items())
        result = self.ingest(client_id, doc_id, f"Brand Kit", text,
                             source="brand_kit", data_class="C2")
        # graph: client -> brand
        self.graph.add_entity("client", client_id, client_id)
        self.graph.add_entity("brand", kit.get("name", client_id), client_id,
                             {"voice": kit.get("voice")})
        self.graph.add_relation(
            f"client:{client_id}", "has_brand",
            f"brand:{client_id or 'global'}:{kit.get('name', client_id)}".lower())
        return result

    def ingest_metrics_snapshot(self, client_id: str, metrics: list[dict]) -> dict:
        """Ingest an analytics export into the structured store + graph."""
        for m in metrics:
            self.repo.record_metric(
                client_id, m.get("source", "internal"),
                m.get("metric", "value"), float(m.get("value", 0)),
                m.get("period", "current"))
        self.graph.add_entity("campaign", "current", client_id,
                             {"metrics": len(metrics)})
        return {"ingested": len(metrics)}