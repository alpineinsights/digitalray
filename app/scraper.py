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
    """Performs the full login flow via principlesyou.com OAuth."""
    logger.info("Opening login page")
    await page.goto(settings.login_page_url, wait_until="networkidle", timeout=30000)

    logger.info("Waiting for login form")
    try:
        await page.wait_for_selector(EMAIL_INPUT, timeout=20000)
    except PlaywrightTimeout:
        raise RuntimeError(
            f"Login form never appeared. Selector '{EMAIL_INPUT}' may be wrong. "
            f"Current URL: {page.url}"
        )

    logger.info("Filling in credentials")
    await page.fill(EMAIL_INPUT, settings.digitalray_email)
    await page.fill(PASSWORD_INPUT, settings.digitalray_password)

    logger.info("Submitting login form")
    await page.click(LOGIN_SUBMIT_BUTTON)

    logger.info("Waiting for redirect back to digitalray.ai")
    try:
        await page.wait_for_url("**/digitalray.ai/**", timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=30000)
    except PlaywrightTimeout:
        raise RuntimeError(
            f"Login submit succeeded but we never got back to digitalray.ai. "
            f"Current URL: {page.url}. Possible causes: wrong credentials or 2FA."
        )

    logger.info("Login successful")
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
    
