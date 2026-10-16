"""
Authentication: TOTP code provider, GitHub login, session persistence.
"""

import os
from pathlib import Path

import pyotp

from resolver import FieldDescriptor, find_on_page

SESSION_PATH = Path(__file__).parent / "session.json"

# --- Element descriptors for the login flow ---

USERNAME_FIELD = FieldDescriptor(
    name="login_username",
    role="textbox", role_name="Username or email address",
    label="Username or email address",
    nearby_text="Username", tag="input",
    xpath="//form//input[1]",
    css="#login_field",
)

PASSWORD_FIELD = FieldDescriptor(
    name="login_password",
    role="textbox", role_name="Password",
    label="Password",
    nearby_text="Password", tag="input",
    xpath="//form//input[2]",
    css="#password",
)

SIGN_IN_BUTTON = FieldDescriptor(
    name="login_submit",
    role="button", role_name="Sign in",
    label="Sign in",
    nearby_text="Sign in", tag="input",
    xpath="//form//input[@type='submit'][1]",
    css="input[type='submit'][value='Sign in']",
)

TOTP_FIELD = FieldDescriptor(
    name="totp_input",
    role="textbox", role_name="Authentication code",
    label="Authentication code",
    nearby_text="Authentication code", tag="input",
    xpath="//form//input[@type='text'][1]",
    css="#app_totp, #otp, input[name='app_totp']",
)


# --- Code provider (exact interface from the assignment) ---

async def get_code(challenge: str = "") -> str:
    """Generate a TOTP code from the secret stored in the environment.
    Never logs the code.
    """
    secret = os.environ.get("GITHUB_TOTP_SECRET")
    if not secret:
        raise RuntimeError("GITHUB_TOTP_SECRET is not set")
    return pyotp.TOTP(secret).now()


# --- Login flow ---

async def login(page, resolve_log):
    """Log into GitHub with username + password + TOTP.
    Retries the TOTP code once if the first attempt fails.
    """
    username = os.environ.get("GITHUB_USERNAME")
    password = os.environ.get("GITHUB_PASSWORD")
    if not username or not password:
        raise RuntimeError("GITHUB_USERNAME and GITHUB_PASSWORD must be set")

    # step 1: credentials
    await page.goto("https://github.com/login")
    await page.wait_for_load_state("domcontentloaded")

    user_field = await find_on_page(page, USERNAME_FIELD)
    resolve_log.append(user_field)
    if not user_field.found:
        raise RuntimeError("Could not find username field")
    await user_field.element.fill(username)

    pass_field = await find_on_page(page, PASSWORD_FIELD)
    resolve_log.append(pass_field)
    if not pass_field.found:
        raise RuntimeError("Could not find password field")
    await pass_field.element.fill(password)

    sign_in = await find_on_page(page, SIGN_IN_BUTTON)
    resolve_log.append(sign_in)
    if not sign_in.found:
        raise RuntimeError("Could not find Sign in button")
    await sign_in.element.click()
    await page.wait_for_load_state("networkidle", timeout=15000)

    if _is_logged_in(page.url):
        return

    # step 2: navigate to TOTP page (GitHub shows WebAuthn/SMS first)
    await _navigate_to_totp_page(page)

    # step 3: enter TOTP — retry once if wrong
    for attempt in range(2):
        totp_field = await find_on_page(page, TOTP_FIELD)
        resolve_log.append(totp_field)
        if not totp_field.found:
            if _is_logged_in(page.url):
                return
            raise RuntimeError("Could not find TOTP input field")

        code = await get_code()
        await totp_field.element.fill(code)

        # click Verify or press Enter
        verify = page.locator("button:has-text('Verify')")
        try:
            async with page.expect_navigation(timeout=15000):
                if await verify.count() > 0:
                    await verify.first.click()
                else:
                    await page.keyboard.press("Enter")
        except Exception:
            pass

        if _is_logged_in(page.url):
            return

        if attempt == 0:
            await page.wait_for_timeout(3000)

    raise RuntimeError("TOTP verification failed after 2 attempts")


async def _navigate_to_totp_page(page):
    """From any 2FA page, get to the authenticator app input."""
    if "two-factor/app" in page.url:
        return

    # expand "More options" dropdown
    more_btn = page.locator("button:has-text('More options'), summary:has-text('More options')")
    if await more_btn.count() > 0:
        await more_btn.first.click()
        await page.wait_for_timeout(800)

    # click "Authenticator app" link
    totp_link = page.locator("a[href*='two-factor/app']")
    if await totp_link.count() > 0:
        await totp_link.first.click(timeout=5000)
        await page.wait_for_load_state("domcontentloaded")


def _is_logged_in(url):
    blocked = ["/login", "/session", "/sessions", "/two-factor"]
    return not any(part in url for part in blocked)


# --- Session persistence ---

async def save_session(context):
    await context.storage_state(path=str(SESSION_PATH))


def has_saved_session():
    return SESSION_PATH.exists()


async def load_session(browser):
    if not SESSION_PATH.exists():
        return None
    try:
        return await browser.new_context(storage_state=str(SESSION_PATH))
    except Exception:
        return None


async def is_session_alive(page):
    """Check if the session is still valid.
    Uses two signals — not just a redirect check:
    1. DOM: look for the user avatar on github.com
    2. API: fetch /user endpoint and check for 200
    """
    try:
        await page.goto("https://github.com", wait_until="domcontentloaded", timeout=15000)

        # signal 1: avatar visible in the header
        avatar = page.locator("img.avatar, [data-login], .AppHeader-user")
        if await avatar.count() > 0:
            return True

        # signal 2: API returns authenticated
        status = await page.evaluate("""async () => {
            try {
                const r = await fetch('/settings/profile', {redirect: 'manual'});
                return r.status;
            } catch { return 0; }
        }""")
        return status == 200

    except Exception:
        return False


def delete_session():
    if SESSION_PATH.exists():
        SESSION_PATH.unlink()
