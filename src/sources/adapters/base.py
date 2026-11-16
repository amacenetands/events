"""Adapter base: Rec record, registry, Adapter ABC. See README.md."""
from __future__ import annotations
import datetime as dt
import dataclasses as dc

REGISTRY: dict[str, type["Adapter"]] = {}


def register(kind: str):
    """Class decorator: make an Adapter available as `adapter: {kind: <kind>}` in sites/<city>.yaml."""
    def deco(cls):
        REGISTRY[kind] = cls
        cls.kind = kind
        return cls
    return deco


@dc.dataclass
class Rec:
    """One event-ish record. `raw` set => unstructured text line (LLM must interpret)."""
    title: str = ""
    date: str = ""
    time: str = ""
    url: str = ""
    desc: str = ""
    raw: str = ""
    price: str = ""     # price text as found

    def line(self) -> str:
        if self.raw:
            return self.raw
        body = " | ".join(x for x in (self.title, self.date, self.time, self.desc, self.price) if x)
        return body + (f" -> {self.url}" if self.url else "")


class Adapter:
    """Subclass, set `direct=True` if records are trustworthy enough to skip the LLM."""
    kind = ""
    direct = False   # fully structured records -> build Events without the LLM

    def records(self, html: str, site_cfg: dict, today: dt.date | None = None) -> list[Rec]:
        """Records of one page. `today` (crawl date; None = wall clock) for adapters that drop
        past dates or infer years."""
        raise NotImplementedError

    @staticmethod
    def dedupe(recs: list[Rec]) -> list[Rec]:
        seen: dict = {}
        for r in recs:
            seen.setdefault((r.title, r.date, r.time, r.raw), r)
        return list(seen.values())
