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
# The "CONTINUE" button on principlesyou.com email and password pages.
# We target it multiple ways because it may be a <button>, <input type=submit>,
# or a styled element with the text "CONTINUE" (case-insensitive).
# Helper _click_continue() in _log_in() handles the actual clicking.
LOGIN_SUBMIT_BUTTON = 'CONTINUE_BUTTON_SEE_HELPER'  # sentinel - see _click_continue

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

    Navigation path:
      1. Navigate directly to digitalray.ai/guest (contains the Log In link)
      2. Click "Log In" (via user avatar dropdown OR sidebar - both tried)
      3. principlesyou.com Welcome Back page: tick Terms checkbox, fill
         email, click CONTINUE
      4. principlesyou.com Enter Password page: fill password, click CONTINUE
      5. Redirect back to digitalray.ai/home (authenticated, ready to chat)
    """
    # --- Step 1: go directly to /guest (skips the landing page entirely) ---
    guest_url = "https://www.digitalray.ai/guest"
    logger.info(f"Navigating directly to {guest_url}")
    await page.goto(guest_url, wait_until="networkidle", timeout=30000)

    # Give the SPA time to finish rendering
    await page.wait_for_timeout(3000)

    # --- Step 2a: open the user menu (try clicking the avatar) ---
    logger.info("Opening user menu (top-right avatar)")
    avatar_locators = [
        page.get_by_role("button", name="user"),
        page.get_by_role("button", name="profile"),
        page.get_by_role("button", name="menu"),
        page.locator('[class*="avatar"]').first,
        page.locator('[class*="user-icon"]').first,
        page.locator('[class*="profile"]').first,
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
        logger.warning("Couldn't click avatar; will try sidebar 'Log In' directly")

    await page.wait_for_timeout(1000)

    # --- Step 2b: click "Log In" (from dropdown if opened, or from sidebar) ---
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
        try:
            body_text = await page.locator("body").inner_text()
            logger.error(f"/guest page body text (first 2000 chars): {body_text[:2000]}")
        except Exception:
            pass
        raise RuntimeError(
            f"Couldn't find 'Log In' on /guest page. Current URL: {page.url}. "
            f"See prior log line for visible page text."
        )

    # --- Step 3: wait for principlesyou.com Welcome Back page ---
    logger.info("Waiting for principlesyou.com Welcome Back page")
    try:
        await page.wait_for_url("**/principlesyou.com/**", timeout=20000)
        await page.wait_for_selector(EMAIL_INPUT, timeout=20000, state="visible")
    except PlaywrightTimeout:
        raise RuntimeError(
            f"Never reached principlesyou.com email form. Current URL: {page.url}"
        )

    # The OneTrust cookie banner may be covering buttons - dismiss it
    await _dismiss_cookie_banner(page)

    # --- Step 4: tick Terms of Service checkbox (mandatory) ---
    logger.info("Ticking Terms of Service checkbox")
    try:
        checkbox = page.locator('input[type="checkbox"]').first
        if not await checkbox.is_checked():
            await checkbox.check(timeout=5000)
    except Exception as e:
        logger.warning(f"Couldn't tick Terms checkbox (may not be required): {e}")

    # --- Step 5: fill email and click CONTINUE ---
    logger.info("Filling email address")
    await page.fill(EMAIL_INPUT, settings.digitalray_email)

    # Capture URL before submit so we can detect the navigation
    url_before_email_submit = page.url

    logger.info("Clicking CONTINUE on email page")
    await _click_continue(page)

    # --- Step 6: wait for password page ---
    # On principlesyou.com, the email and password forms share the same URL,
    # so we can't wait for URL change. Instead, wait for the password field
    # to become visible (it's not present on the email form).
    logger.info("Waiting for password page to appear")
    try:
        await page.wait_for_selector(PASSWORD_INPUT, timeout=15000, state="visible")
        logger.info(f"Password page loaded. Current URL: {page.url}")
    except PlaywrightTimeout:
        raise RuntimeError(
            f"Password page never appeared after email CONTINUE. "
            f"Current URL: {page.url}."
        )

    # --- Step 7: fill password and submit ---
    logger.info("Filling password")
    await page.fill(PASSWORD_INPUT, settings.digitalray_password)

    # Try submitting three different ways: press Enter, click CONTINUE, submit form.
    # Whichever triggers navigation first wins.
    logger.info("Submitting password - trying Enter key first")
    try:
        # Pressing Enter on the password field submits most HTML forms natively
        await page.press(PASSWORD_INPUT, "Enter")
    except Exception as e:
        logger.warning(f"Enter press failed: {e}")

    # Give it a moment to start navigating
    await page.wait_for_timeout(2000)

    # If we're still on principlesyou.com and password field still visible,
    # the Enter didn't work - try clicking CONTINUE
    if "principlesyou.com" in page.url:
        try:
            still_on_password = await page.locator(PASSWORD_INPUT).is_visible(timeout=1000)
        except Exception:
            still_on_password = False
        if still_on_password:
            logger.info("Enter didn't submit - trying CONTINUE click")
            try:
                await _click_continue(page)
            except Exception as e:
                logger.warning(f"CONTINUE click failed too: {e}")

            # If still stuck, try direct form submission via JS
            await page.wait_for_timeout(2000)
            try:
                still_on_password = await page.locator(PASSWORD_INPUT).is_visible(timeout=1000)
            except Exception:
                still_on_password = False
            if still_on_password:
                logger.info("Click didn't submit either - trying form.submit() via JS")
                try:
                    await page.evaluate("""
                        () => {
                            const pwd = document.querySelector('input[type="password"]');
                            if (pwd && pwd.form) pwd.form.submit();
                        }
                    """)
                except Exception as e:
                    logger.warning(f"JS form.submit() failed: {e}")

    # --- Step 8: wait for redirect to authenticated digitalray.ai ---
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
        # Diagnostic dump
        try:
            body_text = await page.locator("body").inner_text()
            logger.error(f"Post-password page body: {body_text[:2000]}")
        except Exception:
            pass
        raise RuntimeError(
            f"Password submit didn't redirect to authenticated digitalray.ai. "
            f"Current URL: {page.url}. Possible causes: wrong password, 2FA "
            f"required, email verification needed, or cookie banner blocking submit."
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


async def _click_continue(page) -> None:
    """
    Clicks the 'CONTINUE' button on principlesyou.com login pages.

    The button may be a <button>, an <input type=submit>, or a styled
    element. We try several strategies in order.
    """
    # Give the form a moment to enable the button after filling the last field
    await page.wait_for_timeout(500)

    # Strategies from most-specific to most-permissive
    strategies = [
        page.get_by_role("button", name="Continue", exact=False),
        page.locator('button:has-text("CONTINUE")'),
        page.locator('button:has-text("Continue")'),
        page.locator('input[type="submit"]'),
        page.locator('[type="submit"]'),
        page.get_by_text("CONTINUE", exact=True),
        page.get_by_text("Continue", exact=True),
        page.locator('a:has-text("CONTINUE"), a:has-text("Continue")'),
    ]

    last_error = None
    for i, locator in enumerate(strategies):
        try:
            await locator.first.click(timeout=5000)
            logger.info(f"Clicked CONTINUE using strategy #{i + 1}")
            return
        except Exception as e:
            last_error = e
            continue

    # All strategies failed - dump body text for diagnostics
    try:
        body_text = await page.locator("body").inner_text()
        logger.error(f"principlesyou.com page body text (first 2000 chars): {body_text[:2000]}")
    except Exception:
        pass
    raise RuntimeError(
        f"Couldn't click CONTINUE button on principlesyou.com. "
        f"Current URL: {page.url}. Last error: {last_error}. "
        f"See prior log line for visible page text."
    )


async def _dismiss_cookie_banner(page) -> None:
    """
    Dismisses the OneTrust cookie consent banner if visible.
    Safe to call even if no banner exists.
    """
    banner_buttons = [
        page.get_by_role("button", name="Reject All"),
        page.get_by_role("button", name="Accept All"),
        page.get_by_role("button", name="Ok"),
        page.locator('#onetrust-reject-all-handler'),
        page.locator('#onetrust-accept-btn-handler'),
        page.locator('button:has-text("Reject All")'),
        page.locator('button:has-text("Ok")'),
    ]
    for locator in banner_buttons:
        try:
            await locator.first.click(timeout=2000)
            logger.info("Dismissed cookie banner")
            await page.wait_for_timeout(500)
            return
        except Exception:
            continue
    # No banner found - that's fine
    logger.info("No cookie banner to dismiss")
    
