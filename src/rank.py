"""Two-stage ranking: score → top 50 → LLM picks 5."""
import json
import os
import re
import asyncio
import httpx
from .models import Event

_RANKING_BACKEND = os.environ.get("RANKING_BACKEND", "ollama").lower()
_RANKING_MODEL = os.environ.get("RANKING_MODEL", "claude-haiku-4-5-20251001")
_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
_OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")
_OLLAMA_CTX = int(os.environ.get("OLLAMA_CTX", "32768"))


def score(event: Event) -> float:
    s = 0.0
    if event.is_one_off:
        s += 10.0
    if event.is_kids:
        s += 4.0
    if event.is_family:
        s += 3.0
    if event.category == "concert":
        s += 3.0
    if event.category in ("gallery", "theatre"):
        s += 2.0
    if event.time and int(event.time.split(":")[0]) >= 18:
        s += 1.0
    return s

_RANK_PROMPT = """\
You are curating a weekly London events digest. From the numbered list below, select the best 5 events.

Selection criteria (in order of priority):
1. Prefer unusual/non-mainstream venues (church concerts, small galleries, experimental spaces)
2. Ensure category variety (not all concerts)
3. Prefer evening/weekend slots
4. Prefer free or low-cost events when quality is similar

Return ONLY a JSON array of the 5 selected event titles in ranked order, best first:
["Title 1", "Title 2", ...]

No prose, no explanation — raw JSON array only.

EVENTS:
{events}"""


def pre_filter(events: list[Event]) -> list[Event]:
    """Score and return top 50 candidates for Claude ranking."""
    sorted_events = sorted(events, key=score, reverse=True)
    return sorted_events[:50]


async def _run_claude_async(prompt: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt, "--model", _RANKING_MODEL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    return stdout.decode().strip()


async def _run_ollama_async(prompt: str) -> str:
    payload = {
        "model": _OLLAMA_MODEL,
        "prompt": "/no_think\n" + prompt,
        "stream": True,
        "options": {"temperature": 0, "num_ctx": _OLLAMA_CTX},
    }
    chunks: list[str] = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=120.0)) as client:
        async with client.stream("POST", f"{_OLLAMA_URL}/api/generate", json=payload) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if line:
                    try:
                        chunks.append(json.loads(line).get("response", ""))
                    except json.JSONDecodeError:
                        pass
    raw = "".join(chunks)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    return raw


async def _run_llm_async(prompt: str) -> str:
    if _RANKING_BACKEND == "ollama":
        return await _run_ollama_async(prompt)
    return await _run_claude_async(prompt)


def rank_with_heuristic(events: list[Event], n: int = 10) -> list[Event]:
    """Score-based ranking with venue + category diversity."""
    scored = sorted(events, key=score, reverse=True)
    selected: list[Event] = []
    venue_counts: dict[str, int] = {}
    cat_counts: dict[str, int] = {}

    for e in scored:
        if len(selected) >= n:
            break
        if venue_counts.get(e.venue, 0) >= 2:
            continue
        if cat_counts.get(e.category, 0) >= 3:
            continue
        selected.append(e)
        venue_counts[e.venue] = venue_counts.get(e.venue, 0) + 1
        cat_counts[e.category] = cat_counts.get(e.category, 0) + 1

    # fill remaining with relaxed constraints (venue cap only)
    if len(selected) < n:
        selected_ids = {id(e) for e in selected}
        for e in scored:
            if len(selected) >= n:
                break
            if id(e) not in selected_ids and venue_counts.get(e.venue, 0) < 2:
                selected.append(e)
                venue_counts[e.venue] = venue_counts.get(e.venue, 0) + 1
    # final fill with no constraints
    if len(selected) < n:
        selected_ids = {id(e) for e in selected}
        for e in scored:
            if len(selected) >= n:
                break
            if id(e) not in selected_ids:
                selected.append(e)

    return selected


async def rank_with_claude(events: list[Event]) -> list[Event]:
    """Heuristic ranking with venue + category diversity. LLM no longer used."""
    return rank_with_heuristic(events)
