"""Field-level extraction compare: qwen3:8b vs qwen3:14b vs cached haiku.
Reads HTML from cache (no fetch), runs ollama models on the SAME prompt, dumps full fields."""
import asyncio, os, json
os.environ.setdefault("LLM_BACKEND", "ollama")
os.environ.setdefault("OLLAMA_CTX", "8192")
from datetime import date
from src.sources.html_sites import _load_sites, _html_to_site_data
from src.dedup import get_cached_html_any
from src.store import _conn
from src.extract import _make_batch_prompt, _parse_object_strict
from src.llm import run_llm

VENUES = ["Esse Jazz Club", "Gnessin Academy of Music"]
MODELS = ["qwen3:8b", "qwen3:14b"]


def _dump(tag, evs):
    print(f"\n=== {tag} | {len(evs)} events ===")
    for e in evs[:6]:
        print(f"  • title={str(e.get('title'))[:50]!r}")
        print(f"    date={e.get('date')} time={e.get('time')!r} cat={e.get('category')!r} price={e.get('price')!r}")
        print(f"    desc={str(e.get('description'))[:90]!r}")


def _haiku_cached(name):
    row = _conn.execute(
        "SELECT events_json FROM extractions WHERE site_name=? AND model='haiku'", (name,)
    ).fetchone()
    return json.loads(row["events_json"]) if row else []


async def main():
    sites = _load_sites(pipeline="moscow")
    for name in VENUES:
        site = next(s for s in sites if s["name"] == name)
        html = get_cached_html_any(site["url"])
        if not html:
            print(f"!! {name}: no cached html"); continue
        sd = _html_to_site_data(site, html)
        prompt = _make_batch_prompt([sd], date.today().isoformat())
        print("\n" + "#" * 70 + f"\n# {name}\n" + "#" * 70)
        _dump(f"{name} | HAIKU (cached)", _haiku_cached(name))
        for m in MODELS:
            try:
                raw = await run_llm(prompt, backend="ollama", model=m, timeout=260)
                evs = _parse_object_strict(raw).get(name, [])
                _dump(f"{name} | {m}", evs)
            except Exception as e:
                print(f"\n=== {name} | {m} | ERROR: {e} ===")


asyncio.run(main())
