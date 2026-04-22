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
# The "CONTINUE" button on both the email and password pages of principlesyou.com.
# Matches by visible text (case-insensitive) to be resilient.
LOGIN_SUBMIT_BUTTON = 'button:has-text("Continue"), button:has-text("CONTINUE")'

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
    Performs the full authenticated login flow.

    Pages visited:
      1. digitalray.ai/login  (click "Log In" link, NOT "Chat with Digital Ray")
      2. principlesyou.com Welcome Back page (tick Terms checkbox, fill email, CONTINUE)
      3. principlesyou.com Enter Password page (fill password, CONTINUE)
      4. digitalray.ai/home  (authenticated, ready to chat)
    """
    logger.info("Opening landing page")
    await page.goto(settings.login_page_url, wait_until="networkidle", timeout=30000)

    # --- Step 1b: wait for /guest page, click user avatar, then click Log In ---
    logger.info("Waiting to arrive at /guest page")
    try:
        await page.wait_for_url("**/guest**", timeout=15000)
    except PlaywrightTimeout:
        raise RuntimeError(
            f"Button click didn't lead to /guest. Current URL: {page.url}"
        )

    # Let /guest finish rendering
    await page.wait_for_timeout(2000)

    # Click the user avatar icon (top-right) to open the user menu dropdown.
    # The avatar has a generic person icon and is the only element in that
    # position. We target it via its likely parent structures.
    logger.info("Opening user menu (top-right avatar)")
    avatar_locators = [
        page.get_by_role("button", name="user"),
        page.get_by_role("button", name="profile"),
        page.get_by_role("button", name="menu"),
        page.locator('[class*="avatar"]').first,
        page.locator('[class*="user-icon"]').first,
        page.locator('[class*="profile"]').first,
        # Fallback: the avatar is usually an img or svg at the top-right corner
        page.locator('header img, header svg, [class*="header"] img, [class*="header"] svg').last,
    ]

    avatar_clicked = False
    for i, locator in enumerate(avatar_locators):
        try:
            await locator.click(timeout=3000)
            logger.info(f"Clicked user avatar using strategy #{i + 1}")
            avatar_clicked = True
            break
        except Exception:
            continue

    if not avatar_clicked:
        # If the avatar can't be found, try the sidebar "Log In" as fallback
        logger.warning("Couldn't click avatar, trying sidebar 'Log In' as fallback")

    # After clicking avatar, the dropdown should appear. Give it a moment.
    await page.wait_for_timeout(1000)

    # Now click "Log In" - this should now be visible either from the
    # opened dropdown OR from the sidebar (whichever renders)
    logger.info("Clicking 'Log In'")
    login_locators = [
        page.get_by_role("link", name="Log In"),
        page.get_by_role("button", name="Log In"),
        page.get_by_text("Log In", exact=True),
        page.locator("a:has-text('Log In'), button:has-text('Log In'), span:has-text('Log In'), div:has-text('Log In')"),
        page.locator("text=/^\\s*Log In\\s*$/i"),
    ]

    clicked = False
    for i, locator in enumerate(login_locators):
        try:
            await locator.first.click(timeout=5000)
            logger.info(f"Clicked 'Log In' using strategy #{i + 1}")
            clicked = True
            break
        except Exception:
            continue

    if not clicked:
        # Diagnostic dump
        try:
            body_text = await page.locator("body").inner_text()
            logger.error(f"/guest page body text (first 2000 chars): {body_text[:2000]}")
        except Exception:
            pass
        raise RuntimeError(
            f"Couldn't find 'Log In' on /guest page (tried avatar dropdown and sidebar). "
            f"Current URL: {page.url}. See prior log line for visible page text."
        )

    # --- Step 2: wait for principlesyou.com Welcome Back page ---
    logger.info("Waiting for principlesyou.com Welcome Back page")
    try:
        await page.wait_for_url("**/principlesyou.com/**", timeout=20000)
        await page.wait_for_selector(EMAIL_INPUT, timeout=20000, state="visible")
    except PlaywrightTimeout:
        raise RuntimeError(
            f"Never reached principlesyou.com email form. Current URL: {page.url}"
        )

    # --- Step 3: tick the Terms of Service checkbox (REQUIRED) ---
    # The checkbox is mandatory - without it the CONTINUE button won't work
    # and you get a red error: "You must accept the Terms of Service..."
    logger.info("Ticking Terms of Service checkbox")
    try:
        # Checkboxes on principlesyou.com are input[type=checkbox]. There
        # might be multiple on the page, so we target the one near the
        # Terms of Service text.
        checkbox = page.locator('input[type="checkbox"]').first
        if not await checkbox.is_checked():
            await checkbox.check(timeout=5000)
    except Exception as e:
        logger.warning(f"Couldn't tick Terms checkbox (may not be required): {e}")

    # --- Step 4: fill email and click CONTINUE ---
    logger.info("Filling email address")
    await page.fill(EMAIL_INPUT, settings.digitalray_email)

    logger.info("Clicking CONTINUE on email page")
    await page.click(LOGIN_SUBMIT_BUTTON, timeout=10000)

    # --- Step 5: wait for Enter Your Password page ---
    logger.info("Waiting for password page")
    try:
        await page.wait_for_selector(PASSWORD_INPUT, timeout=20000, state="visible")
    except PlaywrightTimeout:
        raise RuntimeError(
            f"Password page never appeared after clicking CONTINUE. "
            f"Current URL: {page.url}. Possible causes: wrong email, "
            f"Terms checkbox wasn't ticked, or CONTINUE button selector is wrong."
        )

    # --- Step 6: fill password and click CONTINUE ---
    logger.info("Filling password")
    await page.fill(PASSWORD_INPUT, settings.digitalray_password)

    logger.info("Clicking CONTINUE on password page")
    await page.click(LOGIN_SUBMIT_BUTTON, timeout=10000)

    # --- Step 7: wait for redirect back to authenticated digitalray.ai ---
    # We specifically want /home (not /login, not /guest). Hitting /guest
    # means authentication silently failed and we fell through to guest mode.
    logger.info("Waiting for redirect to authenticated digitalray.ai")
    try:
        await page.wait_for_url(
            lambda url: "digitalray.ai" in url
                        and "/guest" not in url
                        and "/login" not in url,
            timeout=30000,
        )
        await page.wait_for_load_state("networkidle", timeout=30000)
    except PlaywrightTimeout:
        raise RuntimeError(
            f"Login submit didn't redirect to authenticated digitalray.ai. "
            f"Current URL: {page.url}. Possible causes: wrong password, "
            f"2FA required, or email verification needed."
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
    
