"""Sequential CV evidence ranking; identifiers and token budgets remain authoritative."""

import hashlib
import json
import re
from dataclasses import dataclass

from recruitment.domain.contracts import EvidenceInput
from recruitment.domain.tokenization import token_count


def embedding_fingerprint(
    provider: str, endpoint: str, model: str, dimensions: int, revision: str
) -> str:
    value = [provider, endpoint.rstrip("/"), model, dimensions, revision]
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


def embedding_batches(texts: list[str], size: int, tokens: int) -> list[list[str]]:
    result: list[list[str]] = []
    batch: list[str] = []
    used = 0
    for text in texts:
        count = token_count(text)
        if count > tokens:
            raise ValueError("An embedding input exceeds the batch token ceiling.")
        if batch and (len(batch) >= size or used + count > tokens):
            result.append(batch)
            batch, used = [], 0
        batch.append(text)
        used += count
    if batch:
        result.append(batch)
    return result


def technical_terms(text: str) -> set[str]:
    # Keep C++, C#, .NET, AZ-104 and dotted version numbers as complete identifiers.
    return {
        term.rstrip(".-")
        for term in re.findall(r"(?<![\w.])(?:\.[a-z][\w.]*|[\w][\w.+#-]*)", text.casefold())
    }


@dataclass(frozen=True)
class RankedChunk:
    id: str
    position: int
    text: str


def keyword_rank(chunks: list[RankedChunk], criterion: str, limit: int) -> list[str]:
    terms = technical_terms(criterion)
    scores = [(len(terms & technical_terms(c.text)), c) for c in chunks]
    return [c.id for score, c in sorted(scores, key=lambda p: (-p[0], p[1].id)) if score][:limit]


def evidence_selection(
    chunks: list[RankedChunk],
    semantic: list[str],
    lexical: list[str],
    *,
    top_k: int,
    neighbor_window: int,
    budget: int,
    rrf_k: int,
) -> list[EvidenceInput]:
    by_id = {chunk.id: chunk for chunk in chunks}
    scores: dict[str, float] = {}
    for ranking in (semantic, lexical):
        for rank, identifier in enumerate(dict.fromkeys(ranking), 1):
            if identifier in by_id:
                scores[identifier] = scores.get(identifier, 0) + 1 / (rrf_k + rank)
    primary = sorted(scores, key=lambda identifier: (-scores[identifier], identifier))[:top_k]
    chosen: dict[str, RankedChunk] = {}

    def evidence() -> list[EvidenceInput]:
        return [
            EvidenceInput(chunk_id=c.id, text=c.text)
            for c in sorted(chosen.values(), key=lambda c: (c.position, c.id))
        ]

    def add(chunk: RankedChunk) -> None:
        if chunk.id in chosen:
            return
        chosen[chunk.id] = chunk
        serialized = json.dumps([item.model_dump() for item in evidence()], ensure_ascii=False)
        if token_count(serialized) > budget:
            del chosen[chunk.id]

    for identifier in primary:
        add(by_id[identifier])
    reserved = list(chosen.values())
    for main in reserved:
        for chunk in sorted(chunks, key=lambda c: (abs(c.position - main.position), c.position)):
            if abs(chunk.position - main.position) <= neighbor_window:
                add(chunk)
    return evidence()
