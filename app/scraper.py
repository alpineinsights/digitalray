"""
Scraper: drives a headless browser to log into digitalray.ai and send a message.

Flow (mirrors the proven Axiom.ai automation):
    1. Go to digitalray.ai/login
    2. Wait 5 seconds for the SPA to render
    3. Click "Chat with Digital Ray" button (leads to principlesyou.com login)
    4. Fill email -> click Continue (this reveals the Terms checkbox)
    5. Tick Terms checkbox -> click Continue again
    6. On password page, fill password (with keystroke delay) -> click Continue
    7. Redirected to digitalray.ai/home
    8. Type question in chat textarea
    9. Click send button
    10. Wait 35 seconds for the AI reply to complete
    11. Scrape the reply text
"""
import asyncio
import logging
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

from app.config import settings

logger = logging.getLogger(__name__)


# ============================================================================
# SELECTORS - matching the exact selectors used by the working Axiom automation
# ============================================================================

# On https://www.digitalray.ai/login
CHAT_WITH_DIGITAL_RAY_BUTTON = 'button.el-button.btn:has-text("Chat with Digital Ray")'

# On https://principlesyou.com/session_types (email page)
EMAIL_INPUT = '#email_address'
EMAIL_CONTINUE_BUTTON = '#signInEmail'            # <input type="submit">
TERMS_CHECKBOX = '#accept_terms'                  # <input type="checkbox">

# On https://principlesyou.com/session/password (password page)
PASSWORD_INPUT = '#password'
PASSWORD_CONTINUE_BUTTON = 'button[type="submit"]'  # <button type="submit">

# On https://www.digitalray.ai/home
CHAT_INPUT = '#v-step-8'                          # <textarea>
SEND_BUTTON = '#v-step-10'                        # <button>
# Reply is scraped by grabbing all nested divs - we use a broader selector
REPLY_TEXT_AREA = '[class*="answer"], [class*="message"], [class*="reply"]'


async def ask_digitalray(message: str) -> str:
    """
    Opens a browser, logs into digitalray.ai, sends a message, returns the reply.
    """
    logger.info(f"Processing message: {message[:60]}...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
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
    Mirrors the working Axiom automation step-for-step.
    """
    # Step 1: Go to the login landing page
    logger.info("Opening digitalray.ai/login")
    await page.goto(settings.login_page_url, wait_until="networkidle", timeout=30000)

    # Step 2: Wait 5 seconds for SPA rendering (same as Axiom does)
    logger.info("Waiting 5 seconds for page to settle")
    await page.wait_for_timeout(5000)

    # Step 3: Click "Chat with Digital Ray" button (optional - may redirect
    # automatically on some sessions). This leads to the principlesyou.com login.
    logger.info("Clicking 'Chat with Digital Ray' button")
    try:
        await page.click(CHAT_WITH_DIGITAL_RAY_BUTTON, timeout=10000)
    except PlaywrightTimeout:
        logger.info("'Chat with Digital Ray' button not found, continuing anyway "
                    "(may have auto-redirected)")

    # Step 4: Wait for the principlesyou.com login form to appear
    logger.info("Waiting for principlesyou.com email form")
    try:
        await page.wait_for_selector(EMAIL_INPUT, timeout=20000, state="visible")
    except PlaywrightTimeout:
        raise RuntimeError(
            f"Email form never appeared on principlesyou.com. "
            f"Current URL: {page.url}"
        )

    # Step 5: Fill email and click Continue (first time)
    logger.info("Filling email address")
    await page.fill(EMAIL_INPUT, settings.digitalray_email)

    logger.info("Clicking Continue (email page, first click)")
    await page.click(EMAIL_CONTINUE_BUTTON, timeout=10000)

    # Step 6: Tick the Terms of Service checkbox
    # Axiom clicks Continue first, which reveals the Terms requirement.
    # We give the page a moment to show the checkbox, then tick it.
    await page.wait_for_timeout(1000)
    logger.info("Ticking Terms of Service checkbox")
    try:
        await page.check(TERMS_CHECKBOX, timeout=5000)
    except Exception as e:
        logger.warning(f"Couldn't tick Terms checkbox: {e}")

    # Step 7: Click Continue again to submit the email + terms
    logger.info("Clicking Continue (email page, second click after ticking Terms)")
    await page.click(EMAIL_CONTINUE_BUTTON, timeout=10000)

    # Step 8: Wait for the password page
    logger.info("Waiting for password field")
    try:
        await page.wait_for_selector(PASSWORD_INPUT, timeout=20000, state="visible")
    except PlaywrightTimeout:
        raise RuntimeError(
            f"Password field never appeared. Current URL: {page.url}"
        )

    # Step 9: Type the password with small keystroke delay (as Axiom does).
    # The 3ms/keystroke delay helps avoid bot detection.
    logger.info("Typing password (with keystroke delay)")
    await page.click(PASSWORD_INPUT)  # focus the field first
    await page.type(PASSWORD_INPUT, settings.digitalray_password, delay=3)

    # Step 10: Click Continue on the password page
    logger.info("Clicking Continue (password page)")
    await page.click(PASSWORD_CONTINUE_BUTTON, timeout=10000)

    # Step 11: Wait for redirect to authenticated digitalray.ai/home
    logger.info("Waiting for redirect to digitalray.ai/home")
    try:
        await page.wait_for_url(
            lambda url: "digitalray.ai" in url
                        and "/home" in url,
            timeout=30000,
        )
        await page.wait_for_load_state("networkidle", timeout=30000)
    except PlaywrightTimeout:
        # Diagnostic dump
        try:
            body_text = await page.locator("body").inner_text()
            logger.error(f"Post-login page body (first 1500 chars): {body_text[:1500]}")
        except Exception:
            pass
        raise RuntimeError(
            f"Login didn't redirect to digitalray.ai/home. Current URL: {page.url}. "
            f"Possible causes: wrong credentials, 2FA, or email verification."
        )

    logger.info(f"Login successful. Current URL: {page.url}")


async def _send_message_and_get_reply(page, message: str) -> str:
    """
    Types the question, clicks send, waits 35 seconds, scrapes the reply.
    Mirrors the Axiom approach: simple fixed wait instead of polling.
    """
    # Wait for the chat textarea to be visible
    logger.info("Waiting for chat input textarea")
    await page.wait_for_selector(CHAT_INPUT, timeout=20000, state="visible")

    # Type the message
    logger.info("Typing question into chat")
    await page.fill(CHAT_INPUT, message)

    # Click the send button
    logger.info("Clicking send")
    await page.click(SEND_BUTTON, timeout=10000)

    # Wait 35 seconds for the reply to finish streaming (Axiom's approach)
    logger.info("Waiting 35 seconds for AI reply to complete")
    await page.wait_for_timeout(35000)

    # Scrape the reply. Digital Ray renders replies in nested divs on /home.
    # Strategy: grab all text elements, find the longest one that looks like
    # an AI reply (longer than ~50 chars, not the question we asked).
    logger.info("Scraping reply text")
    reply_text = await _extract_latest_reply(page, user_message=message)

    if not reply_text:
        raise RuntimeError(
            "No reply text could be extracted from the page. The answer "
            "selector may need adjustment."
        )

    logger.info(f"Got reply: {len(reply_text)} chars")
    return reply_text


async def _extract_latest_reply(page, user_message: str) -> str:
    """
    Extracts the most recent AI reply from the chat page.

    Strategy: scrape the full visible text of the chat area, then find the
    block that comes AFTER our question. The last meaningful block of text
    is the AI's reply.
    """
    # Grab all text content from the page
    try:
        # Target the main chat area - avoid sidebar and header
        # The chat messages are usually in divs further down the DOM
        full_text = await page.locator("body").inner_text()
    except Exception as e:
        logger.warning(f"Couldn't read page text: {e}")
        return ""

    # Split into lines and clean
    lines = [line.strip() for line in full_text.split("\n") if line.strip()]

    # Find the line containing our question - the reply is after it
    reply_lines = []
    found_question = False
    for line in lines:
        if found_question:
            # Stop if we hit a UI element (short lines, nav items)
            if line in ("New Chat", "Principles", "My Principles", "Chat History",
                        "Ask me anything", "Type Your Questions Here"):
                break
            reply_lines.append(line)
        elif user_message.lower() in line.lower() and len(line) < len(user_message) + 50:
            found_question = True

    if reply_lines:
        return "\n".join(reply_lines).strip()

    # Fallback: return the longest line on the page that isn't a nav item
    candidates = [line for line in lines if len(line) > 100]
    if candidates:
        return max(candidates, key=len)

    return ""
