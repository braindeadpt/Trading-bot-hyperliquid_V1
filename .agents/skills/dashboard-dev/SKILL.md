---
name: dashboard-dev
description: Dashboard development conventions — Flask routes, Socket.IO emitters, the bindSocketHandlers closure gotcha, authFetch, TTL cache, read-only research panels.
triggers:
  - user
  - model
---

# Dashboard development

Files: `src/dashboard/web.py` (Flask routes + Socket.IO emitters),
`src/dashboard/templates/index.html` (single-page UI, all JS inline),
`src/dashboard/static/dashboard.css`.

## The #1 gotcha — bindSocketHandlers closure

Almost all JS render functions (`render*`, `updateKPIs`, `statusUpdate`,
`statusReceivedAt`) live INSIDE `bindSocketHandlers()`. Consequences:

- A `function foo()` declared inside that scope is invisible outside it —
  calling it at script top-level throws ReferenceError when auth gates the
  connect path.
- `pollAll`/`pollResearch` are outer `let` placeholders REPLACED inside
  `bindSocketHandlers` — follow this pattern for new polled fetches: add the
  fetch inside `pollResearch = function() {...}` rather than creating a
  standalone timer at top level.
- Top-level `let` vars (e.g. `socket`) ARE reachable in page-realm evaluates,
  but closure functions are not.

## Adding a new panel/endpoint — the established pattern

1. Backend: `@app.route("/api/<name>")` in `web.py`. Expensive research
   payloads use the existing TTL cache helper (see `/api/research_watchdogs`,
   60s). Read-only SQLite: `sqlite3.connect("file:...?mode=ro", uri=True)`.
   Return a JSON error payload on failure — never crash the dashboard on a
   missing manifest/DB.
2. Frontend: fetch inside `pollAll` (5s cadence, operational data) or
   `pollResearch` (60s cadence, research panels) with `.catch(() => {})`.
3. Styles: `dashboard.css`; bump the `?v=N` cache-buster on the
   `<link>` tag in `index.html` whenever CSS changes.
4. Auth: all fetches go through `authFetch()` (adds `X-Dashboard-Token`).

## Conventions

- Research panels are READ-ONLY — they display evidence, never trigger
  promotion/execution actions.
- Semantic color: green=pass/healthy, red=fail/critical, yellow=warning,
  blue=info, gray(`badge-dim`)=neutral-off. A neutral OFF state must not look
  like a warning.
- `#alert-banner` surfaces critical states above the fold (CB on, engine
  stopped, socket offline, stale feed) — update it via `updateAlertBanner()`
  inside the status/disconnect handlers.
- Compact dense terminal aesthetic: ~9–11px body, SF Mono/Consolas numerals,
  `var(--up)/--down/--warn/--accent/--muted/--dim` tokens.

## Verify

After edits: restart the bot process (dashboard is served by `main.py`), then
hit the route directly (`curl.exe http://localhost:5000/api/<name>`) and
check the rendered page — visual check via the kimi-webbridge skill when the
browser extension is connected.
