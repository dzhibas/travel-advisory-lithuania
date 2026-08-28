# lt-advisory-watch

Detects changes in foreign governments' travel advisories **for Lithuania** and pushes
an alert. Stdlib Python, no dependencies.

Every state file is committed, so `git log -p state/uk-fcdo.json` is your full change
archive for free — you can see exactly which sentence moved, months later.

## Sources

| id | Government | Endpoint | Type |
|---|---|---|---|
| `us-state` | US State Dept | `travel.state.gov/_res/rss/TAsTWs.xml` | RSS, all countries |
| `uk-fcdo` | UK FCDO | `gov.uk/api/content/foreign-travel-advice/lithuania` | JSON, has changelog |
| `ca-gac` | Global Affairs Canada | `data.international.gc.ca/travel-voyage/cta-cap-LT.json` | JSON |
| `de-aa` | Auswärtiges Amt | `auswaertiges-amt.de/opendata/travelwarning` | JSON |
| `nl-bz` | Nederland Wereldwijd | `nederlandwereldwijd.nl/.../litouwen` | HTML (set slice markers) |
| `au-smartraveller` | Smartraveller | `smartraveller.gov.au/...` | HTML, **blocks datacenter IPs** |

The first four are verified working. Smartraveller returns 503 to cloud egress
(Akamai bot rules) — it needs a residential IP or a headless browser, and for
Lithuania it is low-value anyway. Leave it out unless you have somewhere to run it.

## Run

```bash
DRY_RUN=1 python3 watch.py              # all sources, no alerts
DRY_RUN=1 python3 watch.py uk-fcdo      # one source
NTFY_TOPIC=https://ntfy.sh/<your-topic> python3 watch.py
```

Alerts go to ntfy and/or Telegram — set whichever env vars you want:
`NTFY_TOPIC`, `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`.

## Change classes

| kind | priority | meaning |
|---|---|---|
| `LEVEL` | 5 | advisory level / alert status moved — the thing you actually care about |
| `CONTENT` | 4 | advisory prose changed at the same level |
| `CONTENT` | 2 | source republished, tracked text identical |
| `STALE_SOURCE` | 3 | no successful fetch in 12h — a source going dark is itself a signal |

## Gotchas already handled

- **GOV.UK has two timestamps.** `updated_at` bumps on unrelated site republishes
  (currently 2026-08-26); `public_updated_at` is the real edit (2026-08-11). Diff the
  latter. `details.change_history` gives you a human-written note per change.
- **Canada's payload embeds a generation timestamp.** `metadata.generated.timestamp`
  changes on *every* request. Hashing the whole document alerts you every 15 minutes.
- **The Dutch page re-stamps a date daily.** The body carries both `Laatst gewijzigd
  op:` (the real edit date) and `Nog steeds geldig op:` (today's date, bumped every
  day regardless). Both are pulled out of the body before hashing and parked in
  `extra`, which is never diffed — see `NL_VOLATILE_DATES` and the `volatile=`
  argument to `generic_html`.
  Only `data.eng.*` sections are diffed.
- **Germany's `lastModified` is epoch seconds**, not milliseconds.
- **US level lives in the RSS item title** (`Lithuania - Level 1: ...`), not the body.
- Never diff raw HTML — nonces, cookie banners and rotating promo blocks will page
  you at 03:00. Text-extract first, and set slice markers on any HTML source.
