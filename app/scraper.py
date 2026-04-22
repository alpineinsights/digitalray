"""
Scraper: drives a headless browser to log into digitalray.ai and send a message.

Why Playwright and not pure HTTP?
---------------------------------
digitalray.ai uses Firebase Auth + OAuth through principlesyou.com. The JWT
token ends up in an httpOnly cookie (invisible to JavaScript). Replicating
this entire flow in pure HTTP is fragile. A real browser handles all the
redirects, cookies, and token exchange automatically - just like a human.

This module has ONE public function: ask_digitalray(message).
It opens a browser, logs in, sends the message, waits for the streamed reply
to finish, extracts the answer text, and closes the browser.

Performance note
----------------
Each call takes 15-25 seconds because it includes a full browser startup and
login. For the requested use case (a few requests/day) this is fine. If
traffic grows, the next optimization is to keep the browser logged in between
requests (see TODO at the bottom).
"""
import logging
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

from app.config import settings

logger = logging.getLogger(__name__)


# ============================================================================
# SELECTORS - these tell Playwright which HTML elements to interact with.
#
# Each one is a "CSS selector" - the same kind of pattern you'd see in a
# stylesheet. If any selector breaks (e.g. digitalray.ai redesigns their UI),
# only this section needs to be updated.
#
# How to find/verify a selector:
#   1. Open the page in Chrome, right-click the element (e.g. the email
#      input field) and choose "Inspect".
#   2. In DevTools, right-click the highlighted HTML line -> Copy -> Copy selector.
#   3. Paste it below, replacing the placeholder.
# ============================================================================

# --- On https://principlesyou.com/session_types login page ---

# The email input field. Usually `input[type="email"]` or `input[name="email"]`.
EMAIL_INPUT = 'input[type="email"]'

# The password input field. Usually `input[type="password"]`.
PASSWORD_INPUT = 'input[type="password"]'

# The submit button. Often `button[type="submit"]` but could be specific like
# `button:has-text("Sign in")` or `button:has-text("Log in")`.
LOGIN_SUBMIT_BUTTON = 'button[type="submit"]'


# --- On https://www.digitalray.ai (once logged in) ---

# The chat input textarea. Usually `textarea` or more specific like
# `textarea[placeholder*="message"]` if there are multiple.
CHAT_INPUT = 'textarea'

# The send button. Often has an icon, try `button[aria-label*="send" i]`
# (case-insensitive match on "send") or inspect the actual button.
SEND_BUTTON = 'button[aria-label*="send" i]'

# The container that holds the latest AI reply. Critical - we watch this
# to know when streaming is done. Usually the LAST element with a class
# like "message", "assistant-message", "bot-reply". Verify by inspecting.
LATEST_REPLY_SELECTOR = '.message-ai:last-of-type, [class*="assistant"]:last-of-type'


# ============================================================================


async def ask_digitalray(message: str) -> str:
    """
    Opens a browser, logs into digitalray.ai, sends a message, and returns
    the AI's reply as a string.
    """
    logger.info(f"Processing message: {message[:60]}...")

    async with async_playwright() as p:
        # headless=True runs Chrome invisibly (required for servers).
        # Set to False locally to watch what's happening for debugging.
        browser = await p.chromium.launch(headless=True)

        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                # A realistic user agent helps avoid bot-detection systems
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            # Step 1: Log in
