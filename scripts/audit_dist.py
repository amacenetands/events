"""Distribution audit: parse cached HTML (family pipeline) -> classify -> kind/audience/category/time stats."""
import asyncio, sys
from collections import Counter
sys.path.insert(0, ".")
from src.sources.html_sites import parse_sites
from src.classify import classify

async def main(tag):
    evs = classify(await parse_sites(pipeline=tag, force_parse=False))
    n = len(evs) or 1
    print("events", len(evs))
    for name, f in (("kind", lambda e: e.kind), ("audience", lambda e: e.audience), ("category", lambda e: e.category)):
        c = Counter(f(e) for e in evs)
        print(name, {k: f"{v} ({100*v/n:.1f}%)" for k, v in c.most_common()})
    print(f"time coverage {100*sum(e.time is not None for e in evs)/n:.1f}%  min_age set {100*sum(e.min_age is not None for e in evs)/n:.1f}%")
    print(f"is_kids {100*sum(e.is_kids for e in evs)/n:.1f}%  is_family {100*sum(e.is_family for e in evs)/n:.1f}%")

asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
