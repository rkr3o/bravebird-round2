# GitHub Automation Bot

Browser automation that logs into GitHub with TOTP-based two-factor authentication, creates an issue with dependent fields, downloads a release asset, and captures a page as PDF. Built with Playwright (Python) against a live GitHub account.

## File Structure

```
bot.py                    Main orchestrator — runs all stages in sequence
auth.py                   TOTP code provider, login flow, session persistence
resolver.py               Multi-strategy element finder (online + offline modes)
evidence.py               Retry with exponential backoff, failure evidence bundle
api.py                    REST API wrapper (extra credit)
test_resolver_offline.py  Offline resolver tests against mutated HTML
input.json                Task inputs (repo, issue fields, release tag)
setup.sh                  One-command environment setup
.env.example              Required environment variables
requirements.txt          Python dependencies
```

## How to Run

```bash
# 1. Setup
chmod +x setup.sh && ./setup.sh

# 2. Configure credentials
cp .env.example .env
# Fill in GITHUB_USERNAME, GITHUB_PASSWORD, GITHUB_TOTP_SECRET

# 3. Run
source venv/bin/activate
python3 bot.py
```

The bot writes all output to `output/`:
- `manifest.json` — run result, stage timings, resolver log, pages visited
- `downloads/issue.pdf` — PDF capture of the created issue
- `downloads/<asset>` — downloaded release asset
- `evidence/` — screenshot, HTML, and metadata on failure

### Environment Variables

| Variable | Purpose |
|---|---|
| `GITHUB_USERNAME` | GitHub account username |
| `GITHUB_PASSWORD` | GitHub account password |
| `GITHUB_TOTP_SECRET` | Base32 TOTP secret (the seed, not a one-time code) |

### Offline Tests

```bash
python3 test_resolver_offline.py
```

Runs the resolver against saved HTML with four mutation types (renamed classes, wrapper divs, reordered siblings, iframe wrapping) to verify element-finding survives DOM changes without a live browser.

---

## How the TOTP Code Arrives and What Happens When It Is Wrong

The `get_code()` function in `auth.py` reads the TOTP secret from the `GITHUB_TOTP_SECRET` environment variable and generates a fresh 6-digit code using `pyotp.TOTP.now()`. The secret is the base32 seed — the same value you scan from a QR code during 2FA setup. The function never logs the generated code.

During login, the bot enters the TOTP code and submits the form. If the code is rejected (the page stays on the two-factor URL instead of redirecting), the bot waits 3 seconds for the TOTP window to roll over, generates a new code, and tries once more. This handles the common edge case where a code is submitted in the last few seconds of its 30-second window. After two failed attempts, the bot raises `RuntimeError` and stops — there is no point retrying further since that likely indicates a wrong secret, not a timing issue.

GitHub sometimes shows a WebAuthn or SMS prompt before the TOTP input. The bot detects this and navigates to the authenticator app page by expanding the "More options" dropdown and clicking the TOTP link.

---

## How a Dead Session Is Detected

Session validation in `is_session_alive()` uses two independent signals, not just a redirect check:

1. **DOM avatar check** — Navigate to `github.com` and look for a user avatar element in the header (`img.avatar`, `[data-login]`, or `.AppHeader-user`). If any of these are present, the session is alive.

2. **API check** — If the DOM check fails (GitHub may lazy-load the avatar or restructure the header), the bot fetches `/settings/profile` via `fetch()` with `redirect: 'manual'`. A `200` means the session cookie is still valid. Any other status (typically `302` to `/login`) means it is stale.

If both checks fail, the bot deletes the stored `session.json` and performs a full login. This two-signal approach avoids false negatives from transient DOM changes or layout shifts that could hide the avatar element.

---

## Locator Strategy Order and Why

The resolver in `resolver.py` tries five strategies in a fixed order. Each element is described by a `FieldDescriptor` that carries hints for every strategy, so the resolver can fall through gracefully.

| Priority | Strategy | What it does | Why this position |
|---|---|---|---|
| 1 | **role + name** | `get_by_role("textbox", name="Add a title")` | The most stable signal. ARIA roles and accessible names survive CSS refactors, class renames, and DOM restructuring. If the site is accessible, this wins. |
| 2 | **label** | `get_by_label("Password")` | Label associations (`<label for="...">`) are the next most durable — they change less often than markup structure. |
| 3 | **nearby text** | Find text node, walk up, find target tag | Handles cases where labels exist as plain text (not `<label>` elements) near the target input. Less precise but useful when accessibility markup is missing. |
| 4 | **structure (XPath)** | `//form//input[1]` — positional index | Positional selectors are fragile — they break when siblings are added or reordered — but they work when all semantic signals are absent. |
| 5 | **id or class (CSS)** | `#login_field`, `input[type='submit']` | Last resort. IDs and classes are the most likely to change across deploys, especially with CSS-in-JS or obfuscated builds. |

This order means the resolver prefers semantic, accessibility-based selectors and only falls back to structural or presentational selectors when semantic ones fail. The offline test suite (`test_resolver_offline.py`) verifies that elements are still found after class renames, wrapper div injection, sibling reordering, and iframe wrapping.

---

## Quirks Handled

### Download event (release asset)

GitHub serves release assets through a redirect chain. Clicking the download link does not navigate the page — the browser fires a download event instead. The bot uses Playwright's `page.expect_download()` context manager to intercept the download event, wait for the file, and save it to disk. Before clicking, it also expands the "Assets" section if it is collapsed (GitHub hides assets behind a `<details>` toggle).

### Print to PDF (issue page)

After creating the issue, the bot captures the page as a PDF. Playwright's built-in `page.pdf()` only works in headless Chromium, and the bot needs the full rendered page. It opens a CDP session and calls `Page.printToPDF` directly, which produces a faithful print-layout PDF including background colors and images. The result is base64-decoded and written to `output/downloads/issue.pdf`.

### Dependent field (labels depend on repo)

The Labels dropdown on the issue form is a dependent field — its options are loaded from the repository's label set, not hardcoded in the form HTML. The bot opens the dropdown by clicking the gear icon, waits for the label list to populate (1-second pause for the async fetch), then searches for each requested label by text. It handles three different DOM structures GitHub has used for label items (`<label>`, `[role='menuitemcheckbox']`, `.select-menu-item`). After selection, it presses Escape to close the dropdown.

---

## Which Failures Retry

The retry logic is in `evidence.py`. The `run_stage()` wrapper classifies errors into two buckets:

**Retried (transient):**
- `playwright.async_api.TimeoutError` — page load or element wait exceeded its timeout
- `ConnectionError` — network dropped mid-request
- `OSError` — socket-level failure

These retry up to 2 times with exponential backoff (2s, 4s).

**Not retried (terminal):**
- `RuntimeError` — element not found, credentials missing, TOTP verification failed
- Any other `Exception` subclass not in the retryable set

Terminal errors fail immediately because retrying will not fix a missing element or a wrong password.

On final failure, `collect_evidence()` writes a bundle to `output/evidence/`: a full-page screenshot, the page HTML, and a metadata JSON file containing the current URL, last 50 console messages, and the resolver attempt log. All output is run through `redact()` which strips credentials, TOTP codes, email addresses, and any environment variable values from the evidence files.

---

## What I Would Change First for a Hundred Portals

**Site config files.** Each portal gets a YAML or JSON config that declares its login flow (URL, field descriptors, 2FA type), the tasks to run, and any portal-specific quirks. The bot reads a config and executes generically — no per-site Python code. The `FieldDescriptor` dataclass already supports this: serialize it to JSON and load at runtime.

**Parallel execution.** Run portal sessions concurrently with `asyncio.gather()` or a worker pool. Each session gets its own browser context, so cookies and state are isolated. A shared task queue with configurable concurrency (e.g., 10 browsers at once) keeps resource usage bounded. Failed portals go back in the queue for retry without blocking others.

**Shared resolver registry.** Common UI patterns (login forms, search bars, dropdown menus, file upload dialogs) get registered once as reusable descriptor sets. Portal configs reference these by name rather than redefining field descriptors for every standard component. When a site updates its markup, fixing the registry fixes it for every portal that uses that pattern.

---

## Evaluation Test Matrix

| Test | What it verifies | Expected behavior |
|---|---|---|
| Login with TOTP | Credentials + 2FA flow completes | Bot reaches authenticated state, session.json saved |
| Session reuse | Saved session skips login | `is_session_alive()` returns true, login stage skipped |
| Stale session | Expired cookies trigger re-login | DOM/API check fails, session deleted, fresh login |
| Create issue (3+ fields) | Title, body, labels filled and submitted | Issue appears at `/{repo}/issues/{n}`, URL in manifest |
| Dependent field (labels) | Label dropdown loads repo-specific options | Requested labels selected from dynamically loaded list |
| Download release asset | Binary file saved via download event | File appears in `output/downloads/`, name in manifest |
| PDF capture | Issue page rendered to PDF | `issue.pdf` written to `output/downloads/` |
| Retry on timeout | Transient failure triggers backoff | Stage retried up to 2x, delay doubles each attempt |
| Fail on bad credentials | Wrong password fails immediately | No retry, `RuntimeError` raised, evidence bundle written |
| Evidence on failure | Screenshot + HTML + metadata saved | `output/evidence/` populated, credentials redacted |
| Resolver fallback | Primary strategy fails, next one succeeds | Resolver log shows attempted strategies and winner |
| Offline resolver | HTML mutations do not break element finding | `test_resolver_offline.py` passes all mutation variants |

---

## Extra Credit

### Fingerprint Hardening

The bot uses `playwright-stealth` to reduce detection by anti-bot systems. Every page is created through `new_stealth_page()` which applies evasions:

- Overrides `navigator.webdriver` to return `false`
- Patches `navigator.plugins`, `navigator.languages`, and `navigator.permissions`
- Removes Chrome automation indicators (`chrome.runtime`, `chrome.csi`, `chrome.loadTimes`)
- Spoofs WebGL vendor/renderer strings
- Patches `Error.prototype` stack traces to hide Playwright internals
- Disables the `AutomationControlled` Blink feature flag via Chrome launch args

These measures make the headless browser harder to distinguish from a real user session. They won't defeat sophisticated fingerprinting (canvas hashing, behavioral analysis), but they handle the most common bot-detection checks that sites like GitHub and Cloudflare use.

### REST API

`api.py` wraps the bot runner as a FastAPI service:

```bash
uvicorn api:app --port 8000
```

| Endpoint | Method | Description |
|---|---|---|
| `/run` | POST | Trigger a bot run. Accepts optional overrides for repo, title, body, labels, release tag. |
| `/status` | GET | Returns the last run's manifest, or "running" if a run is in progress. |
| `/download/{file}` | GET | Download an output file (PDF, asset, manifest). |

The API enforces single-run concurrency — a second `/run` while one is active returns `409 Conflict`. Inputs are written to `input.json` before the run, so the same JSON-driven flow applies.

---

## Dependencies

- **playwright** — browser automation
- **pyotp** — TOTP code generation
- **python-dotenv** — `.env` file loading
- **beautifulsoup4** — offline HTML parsing for the resolver
- **playwright-stealth** — fingerprint hardening (extra credit)
- **fastapi** / **uvicorn** — REST API (extra credit)
