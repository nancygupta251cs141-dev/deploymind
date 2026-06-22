"""Memory store for DeployMind.

Persists past (error_message, category, fix, reasoning) tuples to a local JSON
file so the fix_suggestion step can reference previously successful fixes instead
of always researching from scratch.

Similarity is keyword-based (no embeddings needed for this scale) — good enough
for matching recurring deployment errors like missing scripts, wrong Node versions,
missing dependencies, etc.
"""
import json
import os
import re
from pathlib import Path
from typing import Optional

MEMORY_FILE = os.environ.get("MEMORY_FILE", "deploymind_memory.json")
MAX_RESULTS = 3  # max past fixes to surface per query


def _load() -> list[dict]:
    path = Path(MEMORY_FILE)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save(entries: list[dict]) -> None:
    Path(MEMORY_FILE).write_text(
        json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _keywords(text: str) -> set[str]:
    """Extract lowercase alpha-numeric tokens of length >= 3 for simple overlap scoring."""
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w}


def save_fix(error_message: str, category: str, fix: str, reasoning: str) -> None:
    """Persist a validated fix to memory so it can be retrieved in future runs."""
    entries = _load()
    # Avoid exact duplicates (same error_message already stored)
    for e in entries:
        if e.get("error_message") == error_message:
            # Update in place with the latest fix instead of duplicating
            e.update({"category": category, "fix": fix, "reasoning": reasoning})
            _save(entries)
            return
    entries.append({"error_message": error_message, "category": category, "fix": fix, "reasoning": reasoning})
    _save(entries)


def retrieve_similar_fixes(error_message: str, category: str, top_k: int = MAX_RESULTS) -> list[dict]:
    """Return the top_k most relevant past fixes for the given error.

    Scoring: keyword overlap between the query (error_message + category) and
    stored entries (error_message + category). Category match doubles the score.
    """
    entries = _load()
    if not entries:
        return []

    query_kw = _keywords(f"{error_message} {category}")
    scored = []
    for e in entries:
        entry_kw = _keywords(f"{e.get('error_message', '')} {e.get('category', '')}")
        overlap = len(query_kw & entry_kw)
        if e.get("category") == category:
            overlap *= 2
        if overlap > 0:
            scored.append((overlap, e))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:top_k]]


def format_memory_context(past_fixes: list[dict]) -> str:
    """Format retrieved past fixes into a readable context string for the agent prompt."""
    if not past_fixes:
        return ""
    lines = ["RELEVANT PAST FIXES FROM MEMORY:"]
    for i, fix in enumerate(past_fixes, 1):
        lines.append(
            f"\n[{i}] Error: {fix['error_message']}\n"
            f"    Category: {fix['category']}\n"
            f"    Fix: {fix['fix']}\n"
            f"    Reasoning: {fix['reasoning']}"
        )
    return "\n".join(lines)