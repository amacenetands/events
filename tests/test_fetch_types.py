from pathlib import Path

import pytest

from src.fetch import FetchResult, FetchStatus, is_blocked_page

FIX = Path(__file__).parent / "fixtures" / "fetch"


def _read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", [
    "southbank_cloudflare.html", "incapsula.html", "aws_waf.html",            # real, from cache
    "ddos_guard.html", "qrator.html", "servicepipe.html", "yandex_smartcaptcha.html",
    "access_denied_ru.html", "sucuri.html",                                   # synthetic
])
def test_walls_blocked(name):
    assert is_blocked_page(_read(name))


@pytest.mark.parametrize("name", ["sadlers_wells.html", "teatr_natsiy_real.html"])
def test_real_pages_not_blocked(name):
    # Театр Наций: short real RU page that embeds the Yandex SmartCaptcha script
    assert not is_blocked_page(_read(name))


@pytest.mark.parametrize("title", ["Forbidden Planet Megastore — Events", "Forbidden Fruit Festival"])
def test_forbidden_title_anchored(title):
    assert not is_blocked_page(f"<html><head><title>{title}</title></head><body>Signings this week</body></html>")


@pytest.mark.parametrize("title", ["403 Forbidden", "Forbidden"])
def test_forbidden_title_blocked(title):
    assert is_blocked_page(f"<html><head><title>{title}</title></head><body>nginx</body></html>")


@pytest.mark.parametrize("html", [
    "<html><head><title>Attention Required! | Cloudflare</title></head><body>Sorry, you have been blocked</body></html>",
    "<html><head><title>Access Denied</title></head><body><h1>Access Denied</h1>You don't have permission. Reference #18.1234</body></html>",
    "<html><body><div id='px-captcha'></div></body></html>",
    "<html><body><h1>Please complete the captcha to continue</h1></body></html>",
    "<html><head><title>App</title></head><body><noscript>You need to enable JavaScript to run this app.</noscript><div id=root></div></body></html>",
])
def test_synthetic_walls_blocked(html):
    assert is_blocked_page(html)


def test_turnstile_widget_on_real_page_not_blocked():
    # EartH Hackney: real listing with a Cloudflare Turnstile form widget (cf-chl-widget-…)
    body = "<p>" + "Gig listing text. " * 400 + "</p>"
    html = (f"<html><head><title>Events</title></head><body>{body}"
            '<input type="hidden" name="cf-turnstile-response" id="cf-chl-widget-q39hi_response">'
            "<noscript>Please enable JavaScript</noscript> captcha</body></html>")
    assert not is_blocked_page(html)


def test_empty_and_plain():
    assert not is_blocked_page("")
    assert not is_blocked_page("<html><body><h1>Concerts</h1><p>Sat 22 Nov 7.30pm</p></body></html>")


def test_fetch_result_defaults():
    r = FetchResult(status=FetchStatus.NOT_MODIFIED, url="https://x")
    assert r.ok and r.body == "" and r.etag is None and r.fetched_at.tzinfo is not None
    assert not FetchResult(status=FetchStatus.BLOCKED).ok
    assert FetchStatus("blocked") is FetchStatus.BLOCKED
