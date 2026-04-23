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

    Flow:
      1. Navigate directly to digitalray.ai/guest
      2. Click user avatar (top-right) to open menu dropdown
      3. Click "Log In" from the dropdown
      4. On principlesyou.com email form: fill email, click Continue,
         tick Terms, click Continue again
      5. On password page: type password with keystroke delay, click Continue
      6. Redirect to digitalray.ai/home
    """
    # --- Step 1: go directly to /guest ---
    guest_url = "https://www.digitalray.ai/guest"
    logger.info(f"Navigating to {guest_url}")
    await page.goto(guest_url, wait_until="networkidle", timeout=30000)
    await page.wait_for_timeout(3000)

    # --- Step 2: click the user avatar to open the menu dropdown ---
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
    for i, locator in enumerate(avatar_locators):
        try:
            await locator.click(timeout=3000)
            logger.info(f"Clicked user avatar using strategy #{i + 1}")
            break
        except Exception:
            continue
    await page.wait_for_timeout(1000)

    # --- Step 3: click "Log In" (from dropdown or sidebar) ---
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
            logger.error(f"/guest page body: {body_text[:1500]}")
        except Exception:
            pass
        raise RuntimeError(
            f"Couldn't find 'Log In' on /guest page. Current URL: {page.url}"
        )

    # --- Step 4: wait for principlesyou.com email form ---
    logger.info("Waiting for principlesyou.com email form")
    try:
        await page.wait_for_url("**/principlesyou.com/**", timeout=20000)
        await page.wait_for_selector(EMAIL_INPUT, timeout=20000, state="visible")
    except PlaywrightTimeout:
        raise RuntimeError(
            f"Email form never appeared on principlesyou.com. Current URL: {page.url}"
        )

    # --- Step 5: fill email, click Continue, tick Terms, click Continue again ---
    logger.info("Filling email address")
    await page.fill(EMAIL_INPUT, settings.digitalray_email)

    logger.info("Clicking Continue (email page, first click)")
    await page.click(EMAIL_CONTINUE_BUTTON, timeout=10000)

    await page.wait_for_timeout(1000)
    logger.info("Ticking Terms of Service checkbox")
    try:
        await page.check(TERMS_CHECKBOX, timeout=5000)
    except Exception as e:
        logger.warning(f"Couldn't tick Terms checkbox: {e}")

    logger.info("Clicking Continue (email page, second click)")
    await page.click(EMAIL_CONTINUE_BUTTON, timeout=10000)

    # --- Step 6: wait for password field, type password with delay, Continue ---
    logger.info("Waiting for password field")
    try:
        await page.wait_for_selector(PASSWORD_INPUT, timeout=20000, state="visible")
    except PlaywrightTimeout:
        raise RuntimeError(
            f"Password field never appeared. Current URL: {page.url}"
        )

    logger.info("Typing password (with keystroke delay)")
    await page.click(PASSWORD_INPUT)
    await page.type(PASSWORD_INPUT, settings.digitalray_password, delay=3)

    logger.info("Clicking Continue (password page)")
    await page.click(PASSWORD_CONTINUE_BUTTON, timeout=10000)

    # --- Step 7: wait for redirect to digitalray.ai/home ---
    logger.info("Waiting for redirect to digitalray.ai/home")
    try:
        await page.wait_for_url(
            lambda url: "digitalray.ai" in url and "/home" in url,
            timeout=30000,
        )
        await page.wait_for_load_state("networkidle", timeout=30000)
    except PlaywrightTimeout:
        try:
            body_text = await page.locator("body").inner_text()
            logger.error(f"Post-login body: {body_text[:1500]}")
        except Exception:
            pass
        raise RuntimeError(
            f"Login didn't redirect to /home. Current URL: {page.url}. "
            f"Possible causes: wrong credentials, 2FA, verification email needed."
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
    Extracts ONLY the AI analysis text from the chat page, stripping out
    UI chrome (header, echoed question, sources list, disclaimer, suggested
    follow-ups, footer, voice chat button).
    """
    # Get the full visible text of the page
    try:
        full_text = await page.locator("body").inner_text()
    except Exception as e:
        logger.warning(f"Couldn't read page text: {e}")
        return ""

    # Split into non-empty lines
    all_lines = [line.strip() for line in full_text.split("\n") if line.strip()]

    # --- Find the END boundary via disclaimer markers ---
    end_markers = [
        "This response includes information from external sources",
        "may contain inaccuracies",
        "DigitalRay may produce",
        "Feel free to ask",
    ]
    end_idx = None
    for i, line in enumerate(all_lines):
        if any(marker in line for marker in end_markers):
            end_idx = i
            break

    # --- Find the echoed-question line ---
    question_idx = None
    user_msg_lower = user_message.lower().strip()
    for i, line in enumerate(all_lines):
        line_lower = line.lower().strip()
        if user_msg_lower in line_lower and len(line) < len(user_message) + 30:
            question_idx = i
            break

    # --- Collect candidate analysis lines between question and disclaimer ---
    # A line is part of the analysis if:
    #   - It's NOT a URL/domain
    #   - It's NOT a source title (heuristic: ends in "..." or "..." in middle,
    #     or contains " - " pattern like "Title - Publication")
    #   - It's NOT a short UI string (nav, button, etc.)
    start_scan = (question_idx + 1) if question_idx is not None else 0
    end_scan = end_idx if end_idx is not None else len(all_lines)

    ui_chrome_patterns = (
        "Hello,", "How can I help", "Voice Chat with",
        "Type Your Questions",
        "New Chat", "Chat History", "My Principles",
        "Principle of the Day", "Register Now", "Log In",
    )

    analysis_lines = []
    for i in range(start_scan, end_scan):
        line = all_lines[i]

        # Skip UI chrome
        if any(pat in line for pat in ui_chrome_patterns):
            continue
        # Skip URL-like lines
        if _looks_like_url_or_domain(line):
            continue
        # Skip source titles - heuristics:
        # 1. Line ends with "..." (truncated title)
        # 2. Line contains " - " AND is short (< 100 chars) - article title pattern
        # 3. Line is followed by a URL/domain line (original heuristic)
        if line.endswith("...") or line.endswith("…"):
            continue
        if " - " in line and len(line) < 100 and i + 1 < len(all_lines):
            # Check if next line looks source-listy (short, no full sentence)
            next_line = all_lines[i + 1] if i + 1 < len(all_lines) else ""
            if _looks_like_url_or_domain(next_line) or (len(next_line) < 60 and " - " in next_line):
                continue
        if i + 1 < end_scan and _looks_like_url_or_domain(all_lines[i + 1]):
            continue
        # Skip the echoed question variant (partial match)
        if user_msg_lower in line.lower() and len(line) < len(user_message) + 30:
            continue

        analysis_lines.append(line)

    # --- Join all analysis lines into one block ---
    # We deliberately do NOT split on short lines here - that was the bug.
    # The analysis can have short sentences or line-break artifacts in it.
    cleaned = "\n".join(analysis_lines).strip()

    # Sanity check: analysis should be at least ~200 chars
    if len(cleaned) >= 200:
        logger.info(f"Extracted analysis: {len(cleaned)} chars (end_marker_found={end_idx is not None})")
        return cleaned

    # --- Last resort: return everything between question and disclaimer ---
    if question_idx is not None and end_idx is not None and end_idx > question_idx + 1:
        raw = "\n".join(all_lines[question_idx + 1 : end_idx]).strip()
        logger.warning(f"Using last-resort extraction: {len(raw)} chars")
        return raw

    # If we still have nothing usable, return whatever we got
    logger.warning(f"Extraction produced only {len(cleaned)} chars")
    return cleaned


def _looks_like_url_or_domain(line: str) -> bool:
    """Heuristic: is this line a URL or a bare domain like 'www.example.com'?"""
    if not line:
        return False
    line_lower = line.lower().strip()
    # Full URLs
    if line_lower.startswith(("http://", "https://", "www.")):
        return True
    # Bare domains ending in a common TLD and without spaces
    if " " not in line_lower and "." in line_lower:
        for tld in (".com", ".ai", ".org", ".co", ".net", ".io", ".gov", ".edu"):
            if line_lower.endswith(tld) or line_lower.endswith(tld + "/"):
                return True
    return False
