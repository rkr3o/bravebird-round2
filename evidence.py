"""
Retry logic and failure evidence collection.

Provides bounded retries with exponential backoff.
On a final failure, writes an evidence bundle: screenshot, page HTML,
URL, console log, stage key, resolver attempts — all redacted.
"""

import asyncio
import json
import os
import re
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeout

from resolver import format_resolve_log

EVIDENCE_DIR = Path(__file__).parent / "output" / "evidence"

# errors worth retrying (transient)
RETRYABLE_ERRORS = (PlaywrightTimeout, ConnectionError, OSError)


async def run_stage(fn, stage_name, max_retries=2, base_delay=2):
    """Run a stage function with bounded retries and exponential backoff.
    Retryable errors (timeouts, network) get retried.
    Everything else fails immediately.
    """
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return await fn()
        except RETRYABLE_ERRORS as err:
            last_error = err
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                print(f"  Retrying {stage_name} in {delay}s (attempt {attempt}/{max_retries})")
                await asyncio.sleep(delay)
        except Exception:
            raise  # non-retryable — fail immediately
    raise last_error


async def collect_evidence(page, stage, resolve_log, console_messages):
    """Write a failure evidence bundle to disk. All sensitive values are redacted."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    # screenshot
    try:
        await page.screenshot(path=str(EVIDENCE_DIR / "screenshot.png"), full_page=True)
    except Exception:
        pass

    # page HTML (redacted)
    try:
        html = await page.content()
        (EVIDENCE_DIR / "page.html").write_text(redact(html))
    except Exception:
        pass

    # metadata
    metadata = {
        "stage": stage,
        "url": redact(page.url),
        "console_log": [redact(msg) for msg in console_messages[-50:]],
        "resolver_attempts": redact(format_resolve_log(resolve_log)),
    }
    (EVIDENCE_DIR / "metadata.json").write_text(json.dumps(metadata, indent=2))


def redact(text):
    """Replace credentials, TOTP codes, and account identifiers with [REDACTED]."""
    if not text:
        return text

    patterns = []

    for var in ("GITHUB_USERNAME", "GITHUB_PASSWORD", "GITHUB_TOTP_SECRET"):
        val = os.environ.get(var)
        if val:
            patterns.append(re.escape(val))

    # standalone 6-digit codes (TOTP)
    patterns.append(r"\b\d{6}\b")

    # email addresses
    patterns.append(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

    combined = "|".join(f"({p})" for p in patterns)
    return re.sub(combined, "[REDACTED]", text)
