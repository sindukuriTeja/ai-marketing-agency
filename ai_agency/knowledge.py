"""L3 — Local Knowledge Substrate.

  Retrieval API        — hybrid search across structured + vector + graph
  Structured store     — the internal DB (see db.py)
  Knowledge graph      — entities + relations + time
  Vector index         — dense (hash-embed) + BM25/keyword
  Versioned doc store  — raw sources + chunk provenance
  Local reranker       — cross-encoder stand-in; returns
                         evidence + source + sensitivity + freshness

All local: embedding happens on-prem (client text never leaves to be
embedded), and every result carries provenance so L7 can ground claims
and propagate right-to-delete.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .db import Repository, new_id
from .security import classify_text, C0, C1, C2, C3, C4

# ---------------------------------------------------------------------------
# Embedding (local model stand-in — deterministic hash embedding)
# ---------------------------------------------------------------------------

def embed(text: str, dim: int = 32) -> list[float]:
    vec = []
    for i in range(dim):
        h = hashlib.sha256(f"{i}:{text.lower()}".encode()).digest()
        vec.append((h[0] - 128) / 128.0)
    norm = sum(x * x for x in vec) ** 0.5 or 1.0
    return [x / norm for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _tf(tokens: list[str]) -> dict[str, float]:
    freq: dict[str, int] = {}
    for t in tokens:
        freq[t] = freq.get(t, 0) + 1
    total = len(tokens) or 1
    return {t: c / total for t, c in freq.items()}


# ---------------------------------------------------------------------------
# Versioned document store (raw sources + chunk provenance)
# ---------------------------------------------------------------------------

@dataclass
class Chunk:
    chunk_id: str
    client_id: Optional[str]
    doc_id: str
    doc_title: str
    text: str
    data_class: str
    version: int
    embedding: list[float]
    tokens: list[str]
    ts: float
    source: str = ""


class VersionedDocStore:
    def __init__(self):
        self._chunks: list[Chunk] = []
        self._by_id: dict[str, Chunk] = {}
        self._docs: dict[str, dict] = {}

    def add_document(self, client_id: Optional[str], doc_id: str,
                     title: str, text: str, source: str = "",
                     data_class: Optional[str] = None) -> list[str]:
        """Ingest a document: chunk, classify, embed, store with provenance."""
        version = self._docs.get(doc_id, {}).get("version", 0) + 1
        self._docs[doc_id] = {"title": title, "client_id": client_id,
                              "version": version, "source": source,
                              "ts": time.time()}
        chunks = self._chunk(text)
        ids = []
        for i, chunk_text in enumerate(chunks):
            cls = data_class or classify_text(chunk_text)
            c = Chunk(
                chunk_id=new_id("chunk"), client_id=client_id,
                doc_id=doc_id, doc_title=title, text=chunk_text,
                data_class=cls, version=version,
                embedding=embed(chunk_text), tokens=_tokens(chunk_text),
                ts=time.time(), source=source,
            )
            self._chunks.append(c)
            self._by_id[c.chunk_id] = c
            ids.append(c.chunk_id)
        return ids

    @staticmethod
    def _chunk(text: str, size: int = 400) -> list[str]:
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return []
        parts = []
        for i in range(0, len(text), size):
            part = text[i:i + size].strip()
            if part:
                parts.append(part)
        return parts or [text]

    def invalidate_doc(self, doc_id: str) -> int:
        """Right-to-delete / version invalidation (L7 provenance)."""
        doomed = [c for c in self._chunks if c.doc_id == doc_id]
        for c in doomed:
            self._by_id.pop(c.chunk_id, None)
        self._chunks = [c for c in self._chunks if c.doc_id != doc_id]
        self._docs.pop(doc_id, None)
        return len(doomed)

    def all_chunks(self) -> list[Chunk]:
        return list(self._chunks)


# ---------------------------------------------------------------------------
# Vector index (dense + BM25)
# ---------------------------------------------------------------------------

class VectorIndex:
    def __init__(self, store: VersionedDocStore):
        self.store = store

    def _bm25(self, query_tokens: list[str], chunk: Chunk,
              avgdl: float, N: int, df: dict[str, int], k1: float = 1.5,
              b: float = 0.75) -> float:
        tf = _tf(chunk.tokens)
        dl = len(chunk.tokens) or 1
        score = 0.0
        for q in query_tokens:
            if q not in tf:
                continue
            idf = math.log(1 + (N - df.get(q, 0) + 0.5) / (df.get(q, 0) + 0.5))
            score += idf * tf[q] * (k1 + 1) / (tf[q] + k1 * (1 - b + b * dl / avgdl))
        return score

    def search(self, query: str, client_id: Optional[str] = None,
               top_k: int = 5, data_class_max: str = C3) -> list[dict]:
        """Hybrid dense + BM25 retrieval, client-scoped."""
        chunks = self.store.all_chunks()
        if client_id is not None:
            chunks = [c for c in chunks if c.client_id == client_id
                      or c.client_id is None]
        if not chunks:
            return []
        qv = embed(query)
        qtokens = _tokens(query)
        N = len(chunks)
        avgdl = sum(len(c.tokens) for c in chunks) / N or 1
        df: dict[str, int] = {}
        for c in chunks:
            for t in set(c.tokens):
                df[t] = df.get(t, 0) + 1

        scored = []
        for c in chunks:
            dense = cosine(qv, c.embedding)
            bm25 = self._bm25(qtokens, c, avgdl, N, df)
            # normalize bm25 into ~[0,1]
            bm25n = min(1.0, bm25 / 3.0)
            score = 0.6 * dense + 0.4 * bm25n
            scored.append((score, c))
        scored.sort(key=lambda x: -x[0])
        out = []
        for score, c in scored[:top_k]:
            out.append({
                "chunk_id": c.chunk_id, "doc_id": c.doc_id,
                "doc_title": c.doc_title, "text": c.text,
                "score": round(score, 4), "data_class": c.data_class,
                "version": c.version, "source": c.source,
                "client_id": c.client_id,
            })
        return out


# ---------------------------------------------------------------------------
# Knowledge graph (entities, relations, time)
# ---------------------------------------------------------------------------

class KnowledgeGraph:
    def __init__(self):
        self._nodes: dict[str, dict] = {}
        self._edges: list[dict] = []

    def add_entity(self, etype: str, name: str, client_id: Optional[str],
                   props: Optional[dict] = None) -> str:
        eid = f"{etype}:{client_id or 'global'}:{name}".lower()
        self._nodes[eid] = {"id": eid, "type": etype, "name": name,
                            "client_id": client_id, "props": props or {},
                            "as_of": time.time()}
        return eid

    def add_relation(self, src: str, rel: str, dst: str,
                     props: Optional[dict] = None) -> None:
        self._edges.append({"src": src, "rel": rel, "dst": dst,
                            "props": props or {}, "as_of": time.time()})

    def neighbors(self, eid: str) -> list[dict]:
        out = []
        for e in self._edges:
            if e["src"] == eid:
                out.append({**e, "node": self._nodes.get(e["dst"])})
            elif e["dst"] == eid:
                out.append({**e, "node": self._nodes.get(e["src"])})
        return out

    def entities(self, etype: Optional[str] = None,
                 client_id: Optional[str] = None) -> list[dict]:
        out = []
        for n in self._nodes.values():
            if etype and n["type"] != etype:
                continue
            if client_id is not None and n["client_id"] not in (client_id, None):
                continue
            out.append(n)
        return out

    def invalidate_client(self, client_id: str) -> int:
        before = len(self._nodes)
        self._nodes = {k: v for k, v in self._nodes.items()
                       if v["client_id"] != client_id}
        self._edges = [e for e in self._edges
                       if client_id not in (e["src"], e["dst"])]
        return before - len(self._nodes)


# ---------------------------------------------------------------------------
# Local cross-encoder reranker
# ---------------------------------------------------------------------------

class Reranker:
    """Stand-in cross-encoder. Returns evidence + source + sensitivity +
    freshness for each candidate."""

    def rerank(self, query: str, candidates: list[dict]) -> list[dict]:
        qv = embed(query)
        for c in candidates:
            cv = embed(c["text"])
            c["rerank_score"] = round(cosine(qv, cv), 4)
            c["evidence"] = c["text"][:200]
            c["source"] = c.get("source") or c.get("doc_title")
            c["sensitivity"] = c.get("data_class", C2)
            c["freshness"] = self._freshness(c)
        candidates.sort(key=lambda x: -x["rerank_score"])
        return candidates

    @staticmethod
    def _freshness(c: dict) -> str:
        # demo: no explicit date -> "unknown"; production reads as_of
        as_of = c.get("as_of")
        if not as_of:
            return "unknown"
        age_days = (time.time() - as_of) / 86400
        if age_days < 30:
            return "fresh"
        if age_days < 180:
            return "aging"
        return "stale"


# ---------------------------------------------------------------------------
# Retrieval API (the single query surface)
# ---------------------------------------------------------------------------

class RetrievalAPI:
    """Hybrid, filtered retrieval over vector + graph + structured store.
    Agents call this; they never query the stores directly."""

    def __init__(self, repo: Repository, store: VersionedDocStore,
                 graph: KnowledgeGraph, reranker: Reranker):
        self.repo = repo
        self.vector = VectorIndex(store)
        self.store = store
        self.graph = graph
        self.reranker = reranker

    def retrieve(self, query: str, client_id: Optional[str] = None,
                 top_k: int = 5, rerank: bool = True) -> list[dict]:
        cands = self.vector.search(query, client_id=client_id, top_k=top_k * 2)
        if rerank and cands:
            cands = self.reranker.rerank(query, cands)[:top_k]
        return cands

    def retrieve_brand_context(self, client_id: str) -> dict:
        """The RAG context for a client: brand kit + voice + approved work."""
        kit = self.repo.get_brand_kit(client_id)
        approved = self.vector.search(
            "approved high-performing content", client_id=client_id, top_k=3)
        return {"brand_kit": kit, "approved_examples": approved}

    def retrieve_benchmarks(self, vertical: str) -> list[dict]:
        return self.repo.list_benchmarks(vertical)

    def graph_view(self, client_id: str) -> dict:
        return {
            "entities": self.graph.entities(client_id=client_id),
        }