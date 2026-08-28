#!/usr/bin/env python3
"""
lt-advisory-watch — detect changes in foreign governments' travel advisories for Lithuania.

Design:
  fetch -> normalise -> classify -> diff against last snapshot -> alert -> persist

Stdlib only. No dependency soup, fast cold start in CI.

Env:
  WATCH_COUNTRY_ISO2   default LT
  NTFY_TOPIC           e.g. https://ntfy.sh/lt-advisory-<random>
  TELEGRAM_BOT_TOKEN   optional
  TELEGRAM_CHAT_ID     optional
  CONTACT              contact string put in the User-Agent (be a good citizen)
  STATE_DIR            default ./state
  DRY_RUN              "1" to skip sending alerts
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from xml.etree import ElementTree

COUNTRY_NAME = "Lithuania"
ISO2 = os.environ.get("WATCH_COUNTRY_ISO2", "LT")
ISO3 = "LTU"
STATE_DIR = os.environ.get("STATE_DIR", "state")
CONTACT = os.environ.get("CONTACT", "lt-advisory-watch (personal monitoring bot)")
USER_AGENT = f"lt-advisory-watch/1.0 (+{CONTACT})"
# Some gov CDNs 503 anything that does not look like a browser.
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 30
DRY_RUN = os.environ.get("DRY_RUN") == "1"


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def fetch(url: str, accept: str = "*/*", retries: int = 3, browser_ua: bool = False) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": BROWSER_UA if browser_ua else USER_AGENT,
                "Accept": accept,
                "Accept-Language": "en",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - we retry everything
            last = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"fetch failed: {url}: {last}")


def fetch_json(url: str) -> Any:
    return json.loads(fetch(url, accept="application/json").decode("utf-8", "replace"))


# --------------------------------------------------------------------------
# html -> text
# --------------------------------------------------------------------------

class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}
    BLOCK = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "tr", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - malformed markup is normal
        pass
    return normalise(" ".join(p.out))


def normalise(text: str) -> str:
    """Collapse whitespace and strip volatile junk so hashes are stable."""
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def deep_find(obj: Any, wanted: set[str]) -> dict[str, Any]:
    """Recursively pull the first value for each wanted key. Survives schema drift."""
    found: dict[str, Any] = {}
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k in wanted and k not in found and isinstance(v, (str, int, float, bool)):
                    found[k] = v
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return found


# --------------------------------------------------------------------------
# snapshot model
# --------------------------------------------------------------------------

@dataclass
class Snapshot:
    source: str            # stable id, e.g. "us-state"
    label: str             # human name, e.g. "US State Department"
    url: str               # page a human should open
    level: str             # normalised advisory level / alert status
    official_updated: str  # timestamp the source itself claims
    body: str              # normalised advisory text (the thing we diff)
    extra: dict = field(default_factory=dict)

    @property
    def body_hash(self) -> str:
        return digest(self.body)

    def to_state(self) -> dict:
        d = asdict(self)
        d["body_hash"] = self.body_hash
        d["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return d


# --------------------------------------------------------------------------
# source adapters
# --------------------------------------------------------------------------

def src_us_state() -> Snapshot:
    """US State Dept. All-country RSS; level lives in the item title."""
    feed = "https://travel.state.gov/_res/rss/TAsTWs.xml"
    root = ElementTree.fromstring(fetch(feed, accept="application/rss+xml"))
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title.lower().startswith(COUNTRY_NAME.lower()):
            continue
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        desc = html_to_text(item.findtext("description") or "")
        m = re.search(r"Level\s*(\d)", title)
        level = f"Level {m.group(1)}" if m else title
        return Snapshot(
            source="us-state",
            label="US State Department",
            url=link or "https://travel.state.gov/content/travel/en/traveladvisories/traveladvisories/lithuania-travel-advisory.html",
            level=level,
            official_updated=pub,
            body=normalise(f"{title}\n{desc}"),
        )
    raise RuntimeError(f"{COUNTRY_NAME} not present in US State RSS feed")


def src_uk_fcdo() -> Snapshot:
    """UK FCDO via the GOV.UK Content API. Gives an explicit change_description."""
    api = "https://www.gov.uk/api/content/foreign-travel-advice/lithuania"
    data = fetch_json(api)
    details = data.get("details", {}) or {}
    alert = details.get("alert_status") or []
    parts = details.get("parts") or []
    body = "\n".join(
        f"## {p.get('title', '')}\n{html_to_text(p.get('body', ''))}" for p in parts
    )
    if not body:
        body = html_to_text(json.dumps(details))
    return Snapshot(
        source="uk-fcdo",
        label="UK FCDO",
        url="https://www.gov.uk/foreign-travel-advice/lithuania",
        level=", ".join(alert) if alert else "no alert status",
        official_updated=str(data.get("public_updated_at", "")),
        body=normalise(body),
        extra={
            "change_description": details.get("change_description", ""),
            "reviewed_at": details.get("reviewed_at", ""),
            "last_note": (details.get("change_history") or [{}])[0].get("note", ""),
            "email_signup": details.get("email_signup_link", ""),
        },
    )


def src_ca_gac() -> Snapshot:
    """Global Affairs Canada open data.

    NOTE: metadata.generated.timestamp changes on every request - hashing the
    whole payload would fire an alert on every poll. Diff only data.eng.
    """
    api = f"https://data.international.gc.ca/travel-voyage/cta-cap-{ISO2}.json"
    data = fetch_json(api)
    d = data.get("data", {}) or {}
    eng = d.get("eng", {}) or {}

    sections = ("advisories", "security", "entry-exit", "health",
                "laws-culture", "disasters-climate")
    body = "\n".join(
        f"## {name}\n{html_to_text(eng.get(name, '') or '')}" for name in sections
    )
    state = d.get("advisory-state", "?")
    return Snapshot(
        source="ca-gac",
        label="Global Affairs Canada",
        url="https://travel.gc.ca/destinations/lithuania",
        level=f"state={state} ({eng.get('advisory-text', '?')})",
        official_updated=str(eng.get("friendly-date", "")),
        body=normalise(body),
        extra={
            "recent_updates": str(eng.get("recent-updates", ""))[:400],
            "update_type": str(d.get("update-metadata", "")),
            "has_advisory_warning": str(d.get("has-advisory-warning", "")),
            "has_regional_advisory": str(d.get("has-regional-advisory", "")),
        },
    )


def src_de_aa() -> Snapshot:
    """German Auswärtiges Amt OpenData. Index first, then the content item."""
    index = fetch_json("https://www.auswaertiges-amt.de/opendata/travelwarning")
    entries = index.get("response", index)
    match_id, meta = None, {}
    for key, val in entries.items():
        if not isinstance(val, dict):
            continue
        if val.get("iso3CountryCode") == ISO3 or val.get("countryName") in ("Litauen", COUNTRY_NAME):
            match_id, meta = key, val
            break
    if match_id is None:
        raise RuntimeError(f"{COUNTRY_NAME} not present in Auswärtiges Amt index")

    detail = fetch_json(f"https://www.auswaertiges-amt.de/opendata/travelwarning/{match_id}")
    node = (detail.get("response") or {}).get(str(match_id), {})
    body = html_to_text(node.get("content", "") or json.dumps(detail, ensure_ascii=False))

    flags = [
        name for name, key in (
            ("Reisewarnung", "warning"),
            ("Teilreisewarnung", "partialWarning"),
            ("Situationshinweis", "situationWarning"),
        )
        if meta.get(key)
    ]
    ts = meta.get("lastModified")
    when = (
        datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(timespec="seconds")
        if isinstance(ts, (int, float)) else str(ts or "")
    )
    return Snapshot(
        source="de-aa",
        label="Auswärtiges Amt (DE)",
        url=str(meta.get("effectiveFrom") or "https://www.auswaertiges-amt.de/de/service/laender/litauen-node/litauensicherheit"),
        level=", ".join(flags) if flags else "keine Warnung",
        official_updated=when,
        body=normalise(body),
        extra={"contentId": match_id, "title": meta.get("title", "")},
    )


# Nederland Wereldwijd stamps two dates into the page body. "Nog steeds geldig
# op" is re-stamped every single day even when nothing was edited, so hashing it
# means an alert every day. Keep both out of the hashed body; they are recorded
# in extra, which is never diffed.
NL_VOLATILE_DATES = re.compile(
    r"^(Laatst gewijzigd op|Nog steeds geldig op):\s*(.*)$", re.MULTILINE
)


def generic_html(source: str, label: str, url: str,
                 start: str | None = None, end: str | None = None,
                 volatile: re.Pattern[str] | None = None) -> Callable[[], Snapshot]:
    """Fallback adapter for sources with no structured feed.

    start/end are literal markers used to slice out the advisory region, so
    nav chrome, cookie banners and rotating promo blocks don't cause false
    positives. Inspect the page once and set them.

    volatile is a line-anchored regex with two groups (label, value) matching
    text the page re-stamps on its own schedule. Matches are lifted out of the
    body before it is hashed and parked in extra, so they can never fire an
    alert on their own.
    """
    def _run() -> Snapshot:
        raw = fetch(url, accept="text/html", browser_ua=True).decode("utf-8", "replace")
        if start and start in raw:
            raw = raw.split(start, 1)[1]
        if end and end in raw:
            raw = raw.split(end, 1)[0]
        text = html_to_text(raw)
        extra: dict[str, str] = {}
        if volatile is not None:
            for key, val in volatile.findall(text):
                extra[key] = val.strip()
            text = normalise(volatile.sub("", text))
        return Snapshot(
            source=source, label=label, url=url,
            level="(unparsed - html source)",
            official_updated="",
            body=text,
            extra=extra,
        )
    return _run


SOURCES: dict[str, Callable[[], Snapshot]] = {
    "us-state": src_us_state,
    "uk-fcdo": src_uk_fcdo,
    "ca-gac": src_ca_gac,
    "de-aa": src_de_aa,
    # Best-effort HTML sources. Set slice markers after inspecting each page.
    #"au-smartraveller": generic_html(
    #    "au-smartraveller", "Smartraveller (AU)",
    #    "https://www.smartraveller.gov.au/destinations/europe/lithuania",
    #),
    "nl-bz": generic_html(
        "nl-bz", "Nederland Wereldwijd",
        "https://www.nederlandwereldwijd.nl/reizen/reisadviezen/litouwen",
        volatile=NL_VOLATILE_DATES,
    ),
}


# --------------------------------------------------------------------------
# diffing
# --------------------------------------------------------------------------

@dataclass
class Change:
    kind: str      # LEVEL | CONTENT | FIRST_RUN | STALE_SOURCE | ERROR
    priority: int  # ntfy priority 1..5
    snapshot: Snapshot | None
    summary: str
    detail: str = ""


def classify(old: dict | None, new: Snapshot) -> Change | None:
    if old is None:
        return Change("FIRST_RUN", 2, new, f"{new.label}: baseline recorded ({new.level})")

    if old.get("level") != new.level:
        return Change(
            "LEVEL", 5, new,
            f"{new.label}: LEVEL CHANGED for {COUNTRY_NAME}",
            f"{old.get('level')}  ->  {new.level}",
        )

    if old.get("body_hash") != new.body_hash:
        note = new.extra.get("change_description") or ""
        return Change(
            "CONTENT", 4, new,
            f"{new.label}: advisory text changed ({new.level})",
            note or f"Body hash {old.get('body_hash')} -> {new.body_hash}",
        )

    # Source says it republished but nothing we track actually moved.
    if old.get("official_updated") != new.official_updated and new.official_updated:
        return Change(
            "CONTENT", 2, new,
            f"{new.label}: republished, no text change",
            f"{old.get('official_updated')} -> {new.official_updated}",
        )
    return None


def check_staleness(old: dict | None, source: str, label: str, max_hours: int = 12) -> Change | None:
    """A source going dark is itself a signal - alert rather than fail silently."""
    if not old or not old.get("checked_at"):
        return None
    last = datetime.fromisoformat(old["checked_at"])
    age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    if age_h > max_hours:
        return Change(
            "STALE_SOURCE", 3, None,
            f"{label}: no successful fetch for {age_h:.0f}h",
            f"source id: {source}",
        )
    return None


# --------------------------------------------------------------------------
# alerting
# --------------------------------------------------------------------------

def send_ntfy(change: Change) -> None:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    tags = {"LEVEL": "rotating_light", "CONTENT": "pencil2",
            "STALE_SOURCE": "warning", "ERROR": "x", "FIRST_RUN": "seedling"}
    body = change.detail or "(no detail)"
    if change.snapshot:
        body += f"\n\n{change.snapshot.url}"
    req = urllib.request.Request(
        topic,
        data=body.encode("utf-8"),
        headers={
            "Title": change.summary,
            "Priority": str(change.priority),
            "Tags": tags.get(change.kind, "bell"),
            "User-Agent": USER_AGENT,
            **({"Click": change.snapshot.url} if change.snapshot else {}),
        },
    )
    urllib.request.urlopen(req, timeout=TIMEOUT).read()


def send_telegram(change: Change) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    text = f"*{change.summary}*\n{change.detail}"
    if change.snapshot:
        text += f"\n{change.snapshot.url}"
    payload = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "parse_mode": "Markdown",
        "disable_web_page_preview": "false",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
    )
    urllib.request.urlopen(req, timeout=TIMEOUT).read()


def notify(change: Change) -> None:
    print(f"[{change.kind}] {change.summary} :: {change.detail}", flush=True)
    if DRY_RUN:
        return
    for fn in (send_ntfy, send_telegram):
        try:
            fn(change)
        except Exception as exc:  # noqa: BLE001 - never let alerting kill the run
            print(f"  ! alert transport {fn.__name__} failed: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def state_path(source: str) -> str:
    return os.path.join(STATE_DIR, f"{source}.json")


def load_state(source: str) -> dict | None:
    try:
        with open(state_path(source), encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None


def save_state(snap: Snapshot) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(state_path(snap.source), "w", encoding="utf-8") as fh:
        json.dump(snap.to_state(), fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")


# --------------------------------------------------------------------------

def main() -> int:
    only = set(sys.argv[1:]) or set(SOURCES)
    failures = 0
    changes: list[Change] = []

    for source, adapter in SOURCES.items():
        if source not in only:
            continue
        old = load_state(source)
        try:
            snap = adapter()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  x {source}: {exc}", file=sys.stderr)
            stale = check_staleness(old, source, source)
            if stale:
                changes.append(stale)
            continue

        change = classify(old, snap)
        print(f"  . {source}: level={snap.level!r} hash={snap.body_hash} "
              f"updated={snap.official_updated!r} bytes={len(snap.body)}")
        if change:
            changes.append(change)
        save_state(snap)

    for change in changes:
        notify(change)

    real = [c for c in changes if c.kind in ("LEVEL", "CONTENT")]
    print(f"\n{len(real)} change(s), {failures} fetch failure(s)")
    return 1 if failures and not real else 0


if __name__ == "__main__":
    sys.exit(main())
