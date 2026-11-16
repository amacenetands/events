"""scripts/migrate_sites.py (kept for history): legacy sites.yaml -> sites/<city>.yaml, notes -> about/hint."""
import importlib.util
from pathlib import Path

import pytest
import yaml

from src import config

_spec = importlib.util.spec_from_file_location(
    "migrate_sites", Path(__file__).resolve().parent.parent / "scripts" / "migrate_sites.py")
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)

LEGACY = """\
html_sites:
- name: Hall A
  map: https://maps.google.com/?q=Hall+A+London
  url: https://a.example/whats-on
  category_hint: concert
  js_required: true
  pipeline: evening
  notes: 'Camden 120-cap folk venue — daily gigs, doors 7pm [test: yes]'
- name: Hall B
  # DISABLED: 403 blocks automated clients
  disabled: true
  map: https://maps.google.com/?q=Somewhere+Else
  url: https://b.example/
  category_hint: theatre
  pipeline: [family, theatres]
  notes: Pantomime and family shows. Flag family events.
- name: Музей
  url: https://m.example/afisha
  category_hint: exhibition
  pipeline: moscow
  notes: Museum on Prechistenka. RU. Titles+dates in HTML.
- name: Театр
  url: https://t.example/
  category_hint: theatre
  pipeline: moscow-theatre
  stealth: true  # Cloudflare
  notes: 'Drama theatre. RU server-rendered schedule with dates.'
"""
CITIES = {
    "london": {"label": "L", "display_name": "London", "tz": "Europe/London", "sites": "sites/london.yaml",
               },
    "moscow": {"label": "M", "display_name": "Москва", "tz": "Europe/Moscow", "lang": "ru",
               "sites": "sites/moscow.yaml", "require_tags": ["moscow", "moscow-theatre"],
               "parked_tags": ["moscow-theatre"], "crawl_tags": ["moscow"]},
    "paris": {"label": "P", "display_name": "Paris", "tz": "Europe/Paris", "sites": "sites/paris.yaml",
              "require_tags": ["paris"]},                                       # no entries: no file
}


@pytest.fixture
def tmp_layout(tmp_path, monkeypatch):
    (tmp_path / "sites.yaml").write_text(LEGACY)
    (tmp_path / "cities.yaml").write_text(yaml.safe_dump(CITIES, allow_unicode=True))
    monkeypatch.setattr(config, "CITIES_YAML", tmp_path / "cities.yaml")
    config.clear_cache()
    yield tmp_path
    monkeypatch.undo()
    config.clear_cache()


def test_split_notes_rules():
    # default is about; hint only for imperative / page-format text
    assert mig.split_notes("Camden 120-cap folk venue — daily gigs [test: yes]") == (
        "Camden 120-cap folk venue — daily gigs [test: yes]", None, [])
    assert mig.split_notes("Camden folk venue — daily gigs, doors 7pm") == (
        "Camden folk venue", "daily gigs, doors 7pm", [])
    assert mig.split_notes("Shoreditch music venue — doors 18:00") == ("Shoreditch music venue", "doors 18:00", [])
    about, hint, _ = mig.split_notes("Smaller gallery. Often free talks. Flag family events.")
    assert (about, hint) == ("Smaller gallery. Often free talks.", "Flag family events.")
    assert mig.split_notes("Classical. Listing hides per-event dates — follow detail links for dates.")[1] == \
        "Listing hides per-event dates — follow detail links for dates."
    about, hint, flags = mig.split_notes("Old URL — skip until new URL confirmed")
    assert hint is None and flags == ["recon+hint→about: 'Old URL — skip until new URL confirmed'"]
    assert mig.split_notes(None) == (None, None, [])


def test_migrate(tmp_layout, capsys):
    assert mig.main(["--src", str(tmp_layout / "sites.yaml")]) == 0
    out = capsys.readouterr().out
    assert "london: 2 venues" in out and "moscow: 2 venues" in out and "paris" not in out
    assert not (tmp_layout / "sites/paris.yaml").exists()                    # only cities with entries
    lon = yaml.safe_load((tmp_layout / "sites/london.yaml").read_text())["venues"]
    a, b = lon
    assert "map" not in a and b["map"] == "https://maps.google.com/?q=Somewhere+Else"     # derivable map dropped
    assert a["fetch"] == {"mode": "browser"} and a["tags"] == ["evening"] and "notes" not in a
    assert b["disabled_reason"] == "403 blocks automated clients"
    assert (b["about"], b["hint"]) == ("Pantomime and family shows.", "Flag family events.")
    mos = yaml.safe_load((tmp_layout / "sites/moscow.yaml").read_text())["venues"]
    assert mos[1]["about"].endswith("(Cloudflare)")                          # inline comment kept in about
    assert config.main(["--check"]) == 1                                     # paris file missing
    assert [v.name for v in config.load_venues("london", include_disabled=True)] == ["Hall A", "Hall B"]
    assert [v.name for v in config.load_venues("moscow")] == ["Музей"]
    assert [v.name for v in config.load_venues("moscow", tags=["moscow-theatre"])] == ["Театр"]
    assert config.load_venues("london")[0].map_url("London") == "https://maps.google.com/?q=Hall+A+London"
    # a second run refuses to overwrite without --force
    assert mig.main(["--src", str(tmp_layout / "sites.yaml")]) == 1
    assert "refusing to overwrite" in capsys.readouterr().out
    assert mig.main(["--src", str(tmp_layout / "sites.yaml"), "--force"]) == 0
