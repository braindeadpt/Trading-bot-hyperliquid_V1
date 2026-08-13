# Dependency CVE Audit — requirements.txt (2026-08-13)

Scan via **OSV.dev API** (`api.osv.dev/v1/querybatch`, PyPI ecosystem) of all
37 pinned deps in `requirements.txt` (39 after the additions below). Before
this sweep, **8 packages** carried known advisories — including several
**HIGH** severity. All have been upgraded and re-scanned **clean**.

## What was vulnerable

| Package | Was pinned | Advisories | Severity highlights | Fixed in |
|---|---|---|---|---|
| `aiohttp` | 3.11.16 | 66 | **HIGH** OOB heap read (parser error path), zip-bomb decompression, HTTP request smuggling (WS upgrade), pipeline queue DoS | 3.14.3 |
| `cryptography` | 44.0.2 | 11 | **HIGH** PKCS#7 EnvelopedData Bleichenbacher oracle, subgroup attack, duplicate self-signed intermediates, vulnerable OpenSSL in wheels | 50.0.0 |
| `urllib3` | 2.4.0 | 12 | **HIGH** decompression-bomb bypass on redirects, unbounded decompression chain, sensitive headers across proxied redirects | 2.7.0 |
| `requests` | 2.32.3 | 4 | MODERATE .netrc credential leak via malicious URLs, insecure temp-file reuse | 2.34.2 |
| `python-socketio` | 5.13.0 | 4 | **HIGH** binary-attachment DoS; MODERATE arbitrary code execution via malformed packets | 5.16.4 |
| `pytest` | 8.4.2 | 2 | MODERATE vulnerable tmpdir handling | 9.1.1 |
| `idna` | 3.10 | 2 | MODERATE crafted-input CPU DoS | 3.18 |
| `click` | 8.1.8 | 1 | PYSEC-2026-2132 | 8.4.2 |

## Transitive companions (required by the fixes)

| Package | Was | Now | Why |
|---|---|---|---|
| `aiosignal` | 1.3.2 | 1.4.0 | `aiohttp>=3.14` requires `>=1.4.0` |
| `propcache` | — | 0.5.2 | new hard dep of `aiohttp 3.14` |
| `python-engineio` | 4.13.1 | 4.13.5 | `python-socketio 5.16` requires `>=4.13.2` |
| `cffi` | 1.17.1 | 2.1.1 | `cryptography 50` requires `>=2.0.0` |
| `pycparser` | 2.22 | 3.0 | `cffi 2.x` companion |
| `pytest-asyncio` | 0.26.0 | 1.4.0 | `pytest 9` requires `pytest-asyncio>=1.4` (`pytest<9` was the old constraint) |

## Compatibility notes

- `cryptography 44 → 50` is the largest jump. Only consumer in-tree is
  `src/security/vault.py` (Fernet + PBKDF2-HMAC), whose APIs are stable —
  round-trip verified after upgrade.
- `flask-socketio 5.6.1` (the Flask 3.1.3 session fix) is compatible with
  `python-socketio 5.16.4` (`>=5.12.0` required). Socket.IO construction
  smoke-tested.
- `pytest 8 → 9` + `pytest-asyncio 0.26 → 1.4`: `asyncio_mode = auto`
  (pytest.ini) still honoured; full CI suite passes unchanged.
- Hyperliquid SDK pins (`eth-account<0.14`, `requests<3.0`) are satisfied.

## Validation

- Full CI suite: **956 passed, 1 skipped, 12 deselected** (69s).
- Re-scan of the upgraded `requirements.txt`: **0 advisories** on all 39
  pinned deps.
- Live bot: healthy on `/health` (still running the old in-memory stack —
  new deps load on next restart, as with prior dependency work).

## Command to re-run

```bash
python - <<'EOF'
import json, re, urllib.request
deps = []
for line in open("requirements.txt", encoding="utf-8"):
    line = line.split("#")[0].strip()
    m = re.match(r"^([A-Za-z0-9_.\-]+)==([A-Za-z0-9_.\-]+)$", line)
    if m:
        deps.append((m.group(1).lower().replace("_", "-"), m.group(2)))
queries = [{"package": {"name": n, "ecosystem": "PyPI"}, "version": v} for n, v in deps]
req = urllib.request.Request("https://api.osv.dev/v1/querybatch",
    data=json.dumps({"queries": queries}).encode(),
    headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=60) as r:
    resp = json.load(r)
bad = [(q["package"]["name"], q["version"], len(r.get("vulns") or []))
       for q, r in zip(queries, resp.get("results", [])) if r.get("vulns")]
print("CLEAN" if not bad else bad)
EOF
```
