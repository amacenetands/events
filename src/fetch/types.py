"""Fetch result types + bot-wall detection (no network code here yet)."""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class FetchStatus(str, Enum):
    """Outcome of one fetch. Only OK/NOT_MODIFIED may update the stored scrape."""
    OK = "ok"
    NOT_MODIFIED = "not_modified"
    BLOCKED = "blocked"
    EMPTY = "empty"
    ERROR = "error"


@dataclass
class FetchResult:
    """What every fetcher returns: status + body + HTTP cache validators."""
    status: FetchStatus
    body: str = ""
    content_type: str = ""
    url: str = ""
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    etag: str | None = None
    last_modified: str | None = None
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (FetchStatus.OK, FetchStatus.NOT_MODIFIED)


# Markers of interstitial/challenge pages. Still gated on short visible text: real
# pages embed e.g. Cloudflare Turnstile widgets (id="cf-chl-widget-…") in forms.
_STRONG = re.compile(
    r"cf-chl-|cf-challenge|/cdn-cgi/challenge-platform/h/[^\"']*/orchestrate/|"
    r"id=[\"']challenge-form[\"']|_Incapsula_Resource|Incapsula incident ID|"
    r"window\.awsWafCookieDomainList|/_sec/cp_challenge|"
    r"px-captcha|captcha-delivery\.com|geo\.captcha-delivery|"
    r"/\.well-known/ddos-guard/|<title>\s*DDoS-Guard\s*</title>|"
    r"/__qrator/|qrator\.net/|servicepipe\.ru/|"
    r"sucuri_cloudproxy|Sucuri WebSite Firewall|cloudproxy@sucuri\.net",
    re.I,
)
# Yandex SmartCaptcha's script is also embedded in real RU forms (Театр Наций, Еврейский
# музей), so it counts only together with challenge wording / the showcaptcha endpoint.
_SMARTCAPTCHA = re.compile(r"smartcaptcha", re.I)
_SMARTCAPTCHA_WALL = re.compile(r"showcaptcha|checkcaptcha|не робот|подтвердите, что запросы", re.I)
# <title> of a challenge / denial page.
_BLOCK_TITLE = re.compile(
    r"^\s*(?:(?:just a moment|attention required|access denied|access to this page has been denied|"
    r"let'?s confirm you are human|are you a (?:robot|human)|security check|"
    r"verify(?:ing)? you are human|request rejected|pardon our interruption|"
    r"доступ запрещ[её]н|проверка браузера|вы не робот)"
    r"|(?:403\b|forbidden\s*$))",
    re.I,
)
# Phrases that indicate a wall, but also occur on real pages (forms with reCAPTCHA,
# cookie banners...) — only trusted when the page has little visible text.
_WEAK = re.compile(
    r"captcha|access denied|attention required|verify you are (a )?human|are you a robot|"
    r"unusual traffic|checking your browser|request unsuccessful|reference #\s*\d|"
    r"enable (javascript|cookies)|доступ запрещ[её]н|проверка браузера|я не робот|"
    r"включите javascript",
    re.I,
)
_NOSCRIPT_JS = re.compile(r"<noscript[^>]*>.*?enable\s+javascript.*?</noscript>", re.I | re.S)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_DROP = re.compile(r"<(script|style|noscript|svg|template)\b.*?</\1\s*>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

STRONG_MAX_TEXT = 3000    # visible chars: walls are tiny; a full page with a widget is not
WEAK_MAX_TEXT = 1500      # visible chars: below this a weak marker counts as a wall
NOSCRIPT_MAX_TEXT = 500   # visible chars: "<noscript>enable JavaScript" shell page


def visible_text(html: str) -> str:
    """Rough visible text (scripts/styles/noscript dropped, tags stripped, entities decoded)."""
    t = _DROP.sub(" ", html)
    t = _TAG.sub(" ", t)
    return _WS.sub(" ", _html.unescape(t)).strip()


def is_blocked_page(html: str) -> bool:
    """True for bot walls: Cloudflare/Incapsula/Akamai/AWS-WAF/DataDome/DDoS-Guard/Qrator/
    Servicepipe/Sucuri/Yandex SmartCaptcha challenges,
    'Access denied', captcha pages and empty JS shells asking to enable JavaScript."""
    if not html:
        return False
    text = visible_text(html)
    if len(text) >= STRONG_MAX_TEXT:
        return False
    if _STRONG.search(html):
        return True
    if _SMARTCAPTCHA.search(html) and _SMARTCAPTCHA_WALL.search(html):
        return True
    m = _TITLE.search(html)
    if m and _BLOCK_TITLE.match(_html.unescape(m.group(1))):
        return True
    if len(text) < WEAK_MAX_TEXT and _WEAK.search(text + " " + (m.group(1) if m else "")):
        return True
    if len(text) < NOSCRIPT_MAX_TEXT and _NOSCRIPT_JS.search(html):
        return True
    return False
