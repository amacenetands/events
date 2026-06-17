"""Batch translation of event description fields via LLM."""
import json
import re
from .llm import run_llm


async def translate_descriptions(events: list, target_lang: str = "ru") -> None:
    """Translate description fields in-place. Venue/title/URL left untouched."""
    to_translate = [(i, e) for i, e in enumerate(events) if e.description]
    if not to_translate:
        return

    items = [{"i": i, "d": e.description} for i, e in to_translate]
    prompt = (
        f"Translate these short event descriptions to Russian. "
        f"Keep proper nouns, venue names, and URLs as-is. "
        f"Return ONLY a JSON array preserving the same 'i' keys with translated 'd' values. No prose.\n\n"
        + json.dumps(items, ensure_ascii=False)
    )

    raw = await run_llm(prompt)
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return
    try:
        translated = json.loads(m.group())
    except Exception:
        return

    idx_map = {t["i"]: t["d"] for t in translated if "i" in t and "d" in t}
    for i, e in to_translate:
        if i in idx_map:
            e.description = idx_map[i]
