"""
GitHub automation bot.

Logs into GitHub with TOTP 2FA, creates an issue with a dependent
label field, downloads a release asset, and writes a JSON manifest.
"""

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

from auth import (
    delete_session, has_saved_session, is_session_alive,
    load_session, login, save_session,
)
from evidence import collect_evidence, run_stage
from resolver import FieldDescriptor, find_on_page, format_resolve_log

load_dotenv(Path(__file__).parent / ".env")

OUTPUT_DIR = Path(__file__).parent / "output"
GITHUB = "https://github.com"


# --- Element descriptors for the issue form ---

ISSUE_TITLE = FieldDescriptor(
    name="issue_title",
    role="textbox", role_name="Add a title",
    label="Add a title",
    nearby_text="Add a title", tag="input",
    xpath="//form//input[@placeholder='Title'][1]",
    css="input[placeholder='Title']",
)

ISSUE_BODY = FieldDescriptor(
    name="issue_body",
    role="textbox", role_name="Markdown value",
    label="Markdown value",
    nearby_text="Type your description", tag="textarea",
    xpath="//form//textarea[1]",
    css="textarea[placeholder*='description'], textarea#issue_body",
)

LABELS_GEAR = FieldDescriptor(
    name="labels_gear",
    role="button", role_name="Labels",
    label="Labels",
    nearby_text="Labels", tag="summary",
    xpath="//div[contains(@class,'sidebar')]//summary[1]",
    css="[id*='label'] summary, summary:has-text('Labels')",
)

SUBMIT_BUTTON = FieldDescriptor(
    name="issue_submit",
    role="button", role_name="Create",
    label="Create",
    nearby_text="Cancel", tag="button",
    xpath="//form//button[@type='submit'][1]",
    css="button:has-text('Cancel') ~ button",
)


# --- Timer ---

class Timer:
    def __init__(self):
        self.stages = {}
        self._name = None
        self._start = None

    def start(self, name):
        self.end()
        self._name = name
        self._start = time.time()

    def end(self):
        if self._name and self._start:
            self.stages[self._name] = round(time.time() - self._start, 2)
            self._name = None

    @property
    def current_stage(self):
        return self._name or "unknown"


_stealth = Stealth()

async def new_stealth_page(context, console_messages):
    """Create a new page with fingerprint hardening and console capture."""
    page = await context.new_page()
    await _stealth.apply_stealth_async(page)
    page.on("console", lambda msg: console_messages.append(msg.text))
    return page


# --- Main flow ---

async def run():
    input_path = Path(__file__).parent / "input.json"
    with open(input_path) as f:
        inputs = json.load(f)

    repo = inputs["repo"]
    issue_config = inputs["issue"]
    release_tag = inputs["download_release"]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    downloads_dir = OUTPUT_DIR / "downloads"
    downloads_dir.mkdir(exist_ok=True)

    timer = Timer()
    pages_visited = []
    captured_files = []
    resolve_log = []
    console_messages = []
    status = "failed"
    error_message = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
            ],
        )
        context = None
        page = None

        try:
            # ---- Stage: session check ----
            timer.start("session_check")

            if has_saved_session():
                print("Loading saved session...")
                context = await load_session(browser)
                if context:
                    page = await new_stealth_page(context, console_messages)
                    alive = await is_session_alive(page)
                    if alive:
                        print("Session is valid — skipping login")
                        pages_visited.append(page.url)
                    else:
                        print("Session is stale — logging in again")
                        await page.close()
                        await context.close()
                        delete_session()
                        context = None

            if context is None:
                timer.end()
                timer.start("login")
                context = await browser.new_context()
                page = await new_stealth_page(context, console_messages)

                async def do_login():
                    await login(page, resolve_log)

                await run_stage(do_login, "login")
                await save_session(context)
                # reload context from saved state (ensures consistent cookie state)
                await page.close()
                await context.close()
                context = await browser.new_context(storage_state=str(Path(__file__).parent / "session.json"))
                page = await new_stealth_page(context, console_messages)
                print("Logged in and session saved")
                pages_visited.append("https://github.com/")

            timer.end()

            # ---- Stage: navigate to repo ----
            timer.start("navigate_to_repo")

            async def go_to_repo():
                await page.goto(f"{GITHUB}/{repo}", wait_until="domcontentloaded")

            await run_stage(go_to_repo, "navigate_to_repo")
            pages_visited.append(page.url)
            print(f"Navigated to repo: {repo}")
            timer.end()

            # ---- Stage: create issue (3+ fields, dependent label) ----
            timer.start("create_issue")

            async def create_issue():
                await page.goto(f"{GITHUB}/{repo}/issues/new", wait_until="domcontentloaded")
                pages_visited.append(page.url)

                # field 1: title
                title_result = await find_on_page(page, ISSUE_TITLE)
                resolve_log.append(title_result)
                if not title_result.found:
                    raise RuntimeError("Could not find title field")
                await title_result.element.click()
                await title_result.element.fill(issue_config["title"])
                await page.wait_for_timeout(500)
                print(f"  Title: {issue_config['title']}")

                # field 2: body
                body_result = await find_on_page(page, ISSUE_BODY)
                resolve_log.append(body_result)
                if not body_result.found:
                    raise RuntimeError("Could not find body field")
                await body_result.element.click()
                await body_result.element.fill(issue_config["body"])
                await page.wait_for_timeout(500)
                print(f"  Body: filled")

                # field 3: labels (dependent on repo — this is the dependent field quirk)
                if issue_config.get("labels"):
                    labels_result = await find_on_page(page, LABELS_GEAR)
                    resolve_log.append(labels_result)
                    if labels_result.found:
                        await labels_result.element.click()
                        await page.wait_for_timeout(1000)
                        for label_name in issue_config["labels"]:
                            label_item = page.locator(
                                f"label:has-text('{label_name}'), "
                                f"[role='menuitemcheckbox']:has-text('{label_name}'), "
                                f".select-menu-item:has-text('{label_name}')"
                            )
                            if await label_item.count() > 0:
                                await label_item.first.click()
                                print(f"  Label: {label_name}")
                            else:
                                print(f"  Label not found: {label_name}")
                        await page.keyboard.press("Escape")
                        await page.wait_for_timeout(300)

                # submit
                resolve_log.append(await find_on_page(page, SUBMIT_BUTTON))
                create_btn = page.locator("button").filter(has_text="Create").filter(has_text="⌘")
                if await create_btn.count() == 0:
                    create_btn = page.locator("button:has-text('Cancel') ~ button").first
                await create_btn.first.click()
                await page.wait_for_timeout(5000)
                await page.wait_for_load_state("domcontentloaded")

                pages_visited.append(page.url)
                print(f"Issue created: {page.url}")

            await run_stage(create_issue, "create_issue")
            timer.end()

            # ---- Stage: capture issue as PDF (quirk: print-to-PDF) ----
            timer.start("capture_pdf")

            pdf_path = downloads_dir / "issue.pdf"
            cdp = await page.context.new_cdp_session(page)
            pdf_data = await cdp.send("Page.printToPDF", {"printBackground": True})
            pdf_path.write_bytes(base64.b64decode(pdf_data["data"]))
            captured_files.append(pdf_path.name)
            print(f"Saved issue PDF: {pdf_path.name}")

            timer.end()

            # ---- Stage: download release asset (quirk: download event) ----
            timer.start("download_asset")

            async def download_asset():
                release_url = f"{GITHUB}/{repo}/releases/tag/{release_tag}"
                await page.goto(release_url, wait_until="domcontentloaded")
                pages_visited.append(page.url)
                await page.wait_for_timeout(2000)

                # expand Assets section if collapsed
                toggle = page.locator("details summary:has-text('Assets')")
                if await toggle.count() > 0:
                    parent = toggle.locator("..")
                    is_open = await parent.get_attribute("open")
                    if is_open is None:
                        await toggle.click()
                        await page.wait_for_timeout(500)

                asset_link = page.locator("a[href*='/releases/download/']")
                if await asset_link.count() > 0:
                    async with page.expect_download() as dl_info:
                        await asset_link.first.click()
                    download = await dl_info.value
                    save_path = downloads_dir / download.suggested_filename
                    await download.save_as(str(save_path))
                    captured_files.append(save_path.name)
                    print(f"Downloaded asset: {save_path.name}")
                else:
                    raise RuntimeError(f"No downloadable assets found for release {release_tag}")

            await run_stage(download_asset, "download_asset")
            timer.end()

            status = "success"

        except Exception as err:
            error_message = str(err)
            print(f"FAILED [{timer.current_stage}]: {err}", file=sys.stderr)

            if page:
                await collect_evidence(page, timer.current_stage, resolve_log, console_messages)
                print("Evidence bundle written to output/evidence/")

        finally:
            timer.end()
            await browser.close()

    # ---- Write manifest ----
    manifest = {
        "status": status,
        "inputs": inputs,
        "pages_visited": pages_visited,
        "stage_timings": timer.stages,
        "captured_files": captured_files,
        "resolver_log": format_resolve_log(resolve_log),
        "error": error_message,
    }

    manifest_path = OUTPUT_DIR / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\nManifest: {manifest_path}")
    print(json.dumps(manifest, indent=2))
    return 0 if status == "success" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
