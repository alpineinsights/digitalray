"""
Scraper: drives a headless browser to log into digitalray.ai and send a message.

Why Playwright and not pure HTTP?
---------------------------------
digitalray.ai uses Firebase Auth + OAuth through principlesyou.com. The JWT
token ends up in an httpOnly cookie (invisible to JavaScript). Replicating
this entire flow in pure HTTP is fragile. A real browser handles all the
redirects, cookies, and token exchange automatically - just like a human.

This module has ONE public function: ask_digitalray(message).
"""
import asyncio
import logging
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

from app.config import settings

logger = logging.getLogger(__name__)


# ============================================================================
# SELECTORS - verify each one on the actual site before running.
# How to find: right-click element in Chrome -> Inspect -> right-click HTML
# line -> Copy -> Copy selector. Paste below replacing the guess.
# ============================================================================

# On https://principlesyou.com/session_types login page
EMAIL_INPUT = 'input[type="email"]'
PASSWORD_INPUT = 'input[type="password"]'
LOGIN_SUBMIT_BUTTON = 'button[type="submit"]'

# On https://www.digitalray.ai (once logged in)
CHAT_INPUT = 'textarea'
SEND_BUTTON = 'button[aria-label*="send" i]'
LATEST_REPLY_SELECTOR = '.message-ai:last-of-type, [class*="assistant"]:last-of-type'


async def ask_digitalray(message: str) -> str:
    """
    Opens a browser, logs into digitalray.ai, sends a message, and returns
    the AI's reply as a string.
    """
    logger.info(f"Processing message: {message[:60]}...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()
            await _log_in(page)
            reply = await _send_message_and_get_reply(page, message)
            return reply
        finally:
            await browser.close()


async def _log_in(page) -> None:
    """
    Performs authenticated login via principlesyou.com OAuth.

    Flow:
      1. Open digitalray.ai/login (landing page)
      2. Click the 'Log In' link in the sidebar (NOT the big red 'Chat with
         Digital Ray' button - that leads to guest mode)
      3. Follow OAuth redirect to principlesyou.com/session_types
      4. If there's a 'Continue with email' button, click it
      5. Fill email + password, submit
      6. Wait for redirect back to digitalray.ai (authenticated)
    """
    logger.info("Opening landing page")
    await page.goto(settings.login_page_url, wait_until="networkidle", timeout=30000)

    # The landing page has TWO login entry points:
    #  - Big red 'Chat with Digital Ray' button -> GUEST mode (don't click)
    #  - 'Log In' link in sidebar -> Authenticated OAuth flow (click this!)
    logger.info("Looking for 'Log In' link (authenticated path)")
    try:
        # Match any element whose visible text is exactly "Log In" or "Login"
        # Using Playwright's text selector, case-insensitive
        await page.get_by_text("Log In", exact=True).first.click(timeout=15000)
        logger.info("Clicked 'Log In' link, waiting for OAuth redirect")
    except PlaywrightTimeout:
        raise RuntimeError(
            f"Couldn't find 'Log In' link on digitalray.ai landing page. "
            f"Current URL: {page.url}"
        )

    # Wait for the redirect chain to land on principlesyou.com
    logger.info("Waiting to reach principlesyou.com")
    try:
        await page.wait_for_url("**/principlesyou.com/**", timeout=20000)
    except PlaywrightTimeout:
        raise RuntimeError(
            f"Clicked Log In but never reached principlesyou.com. "
            f"Current URL: {page.url}"
        )

    # principlesyou.com/session_types may show login method picker first.
    # If the email field isn't immediately visible, try clicking a
    # "Continue with email" option.
    logger.info("Waiting for email field on principlesyou.com")
    email_visible = False
    try:
        await page.wait_for_selector(EMAIL_INPUT, timeout=5000, state="visible")
        email_visible = True
    except PlaywrightTimeout:
        logger.info("Email field not visible yet, trying 'Continue with email' button")
        # Try common labels for an email-login option
        for label in ["Continue with email", "Sign in with email",
                      "Log in with email", "Email"]:
            try:
                await page.get_by_text(label, exact=False).first.click(timeout=3000)
                logger.info(f"Clicked '{label}'")
                break
            except PlaywrightTimeout:
                continue

        # Now wait for the email field to appear
        try:
            await page.wait_for_selector(EMAIL_INPUT, timeout=15000, state="visible")
            email_visible = True
        except PlaywrightTimeout:
            pass

    if not email_visible:
        raise RuntimeError(
            f"Email field never appeared on principlesyou.com. "
            f"Current URL: {page.url}. The session_types page may have changed."
        )

    logger.info("Filling in credentials")
    await page.fill(EMAIL_INPUT, settings.digitalray_email)
    await page.fill(PASSWORD_INPUT, settings.digitalray_password)

    logger.info("Submitting login form")
    await page.click(LOGIN_SUBMIT_BUTTON)

    logger.info("Waiting for redirect back to digitalray.ai")
    try:
        # Wait for URL to contain digitalray.ai AND NOT be /guest or /login
        await page.wait_for_url(
            lambda url: "digitalray.ai" in url
                        and "/guest" not in url
                        and "/login" not in url,
            timeout=30000,
        )
        await page.wait_for_load_state("networkidle", timeout=30000)
    except PlaywrightTimeout:
        raise RuntimeError(
            f"Login submit didn't redirect back to authenticated digitalray.ai. "
            f"Current URL: {page.url}. Possible causes: wrong credentials, 2FA, "
            f"or email verification required."
        )

    logger.info(f"Login successful. Current URL: {page.url}")


async def _send_message_and_get_reply(page, message: str) -> str:
    """Types the message, clicks send, waits for the streamed reply to complete."""
    logger.info("Waiting for chat interface")
    await page.wait_for_selector(CHAT_INPUT, timeout=20000)

    logger.info("Typing message")
    await page.fill(CHAT_INPUT, message)

    logger.info("Clicking send")
    await page.click(SEND_BUTTON)

    logger.info("Waiting for reply to finish streaming")
    reply_text = await _wait_for_reply_to_stabilize(page)

    if not reply_text:
        raise RuntimeError(
            "Message sent but no reply detected. LATEST_REPLY_SELECTOR "
            "may need to be updated."
        )

    logger.info(f"Got reply: {len(reply_text)} chars")
    return reply_text


async def _wait_for_reply_to_stabilize(page, max_wait_seconds: int = 90) -> str:
    """
    Polls the latest reply element every second. Returns once its text
    stops changing for 2 consecutive seconds (meaning streaming finished).
    """
    last_text = ""
    stable_checks = 0
    required_stable_checks = 2

    for _ in range(max_wait_seconds):
        await asyncio.sleep(1)

        try:
            elements = await page.query_selector_all(LATEST_REPLY_SELECTOR)
            if not elements:
                continue
            current_text = (await elements[-1].inner_text()).strip()
        except Exception:
            continue

        if current_text and current_text == last_text:
            stable_checks += 1
            if stable_checks >= required_stable_checks:
                return current_text
        else:
            stable_checks = 0
            last_text = current_text

    return last_text
    
