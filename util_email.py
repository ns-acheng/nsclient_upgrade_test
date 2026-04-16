"""
Gmail email automation for extracting Netskope download links.

Uses Selenium to attach to a Chrome browser launched with
--remote-debugging-port and --user-data-dir (local profile).
If Chrome is not already running on the debug port, it is
launched automatically.

External imports (selenium) are deferred to connect() so the module
can be imported without selenium installed (e.g. during testing).
"""

import logging
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote_plus, urlparse

GMAIL_URL = "https://mail.google.com/"
DEFAULT_DEBUG_PORT = 9222
PAGE_LOAD_TIMEOUT = 30    # seconds for driver.get() before TimeoutException
SEARCH_RETRY_INTERVAL = 5  # seconds between retries
DEFAULT_TIMEOUT = 300  # total seconds to wait for email
SUBJECT_TEMPLATE = '[EXTERNAL] Netskope New User Onboarding for "{email}"'
# Gmail search-safe version — avoids nested quotes that break
# subject:("...") syntax.  The email address is matched separately.
SEARCH_SUBJECT = "Netskope New User Onboarding"
SEARCH_LABEL = "Email Invite"
LINK_TEXTS_64 = ["Windows Client (64-bit)", "Windows Client"]
LINK_TEXTS_32 = ["Windows Client"]

LOCAL_PROFILE_DIR = Path(__file__).parent / "local_profile"
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

log = logging.getLogger(__name__)


RETRYABLE_BROWSER_ERROR_SNIPPETS = (
    "invalid session id",
    "session deleted as the browser has closed the connection",
    "unable to receive message from renderer",
    "disconnected",
    "target window already closed",
    "chrome not reachable",
)


class GmailBrowser:
    """
    Attaches to a running Chrome instance and extracts a Netskope
    download link from a Gmail invite email.

    Usage::

        with GmailBrowser(email_address="user@example.com") as gb:
            gb.connect()
            url = gb.get_download_link()
    """

    def __init__(
        self,
        email_address: str,
        is_64_bit: bool = True,
        debug_port: int = DEFAULT_DEBUG_PORT,
        tenant_hostname: str = "",
        stop_event: Optional[threading.Event] = None,
        profile_dir: Optional[Path] = None,
    ) -> None:
        self._email_address = email_address
        self._is_64_bit = is_64_bit
        self._debug_port = debug_port
        self._tenant_hostname = tenant_hostname
        self._stop_event = stop_event
        self._driver: Optional[Any] = None
        self._profile_dir = profile_dir or LOCAL_PROFILE_DIR

    # -- context manager ------------------------------------------------

    def __enter__(self) -> "GmailBrowser":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    # -- public API -----------------------------------------------------

    def connect(self) -> None:
        """
        Attach Selenium to Chrome on the debug port.

        If no Chrome is reachable on the port, launch a new instance
        with a local profile (``local_profile/``) and the debug port,
        then attach.  After attaching, verifies Gmail is actually
        usable; if not, closes Chrome via DevTools and relaunches.
        """
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        options = Options()
        options.debugger_address = f"localhost:{self._debug_port}"

        # Probe port first — webdriver.Chrome() hangs if nothing is
        # listening, so skip straight to launch when port is closed.
        if self._is_port_open(self._debug_port):
            try:
                self._driver = webdriver.Chrome(options=options)
                self._driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
                log.info(
                    "Connected to Chrome on port %d",
                    self._debug_port,
                )
                if self._is_gmail_ready():
                    return
                log.info(
                    "Gmail not usable on existing Chrome "
                    "— closing and relaunching"
                )
                self._close_chrome_via_cdp()
            except Exception:
                log.info(
                    "Chrome on port %d not usable — relaunching",
                    self._debug_port,
                )

        else:
            log.info(
                "Nothing listening on port %d — launching Chrome",
                self._debug_port,
            )

        # Launch Chrome with local profile + debug port
        self._launch_chrome()

        # Retry attach after launch
        try:
            self._driver = webdriver.Chrome(options=options)
            self._driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
            log.info(
                "Connected to Chrome on port %d", self._debug_port
            )
        except Exception as exc:
            raise RuntimeError(
                f"Could not connect to Chrome on port "
                f"{self._debug_port} even after launch. "
                "Close all Chrome windows and retry."
            ) from exc

    def mark_all_as_read(self) -> int:
        """
        Mark all unread emails matching the invite subject as read.

        Searches for ``is:unread subject:("...")``, selects all results,
        and clicks the "Mark as read" toolbar button.  This clears stale
        unread emails so that only freshly sent invites appear in
        subsequent :meth:`get_download_link` calls.

        :return: Number of emails that were marked as read (0 if none).
        :raises RuntimeError: If the browser is not connected.
        """
        if self._driver is None:
            raise RuntimeError("Not connected — call connect() first")

        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        driver = self._driver
        if "mail.google.com" not in (driver.current_url or ""):
            log.info("Navigating to Gmail")
            driver.get(self._gmail_start_url())

        self._dismiss_overlays(driver, By)

        try:
            search_box = self._find_search_box(driver, By, EC, WebDriverWait)
        except TimeoutException:
            log.info(
                "Search box not found — inbox may be empty, "
                "nothing to mark as read"
            )
            return 0

        search_query = (
            self._build_invite_search_query(unread=True)
        )
        log.info("Marking old unread emails as read: %s", search_query)
        self._set_search_query(search_box, search_query, submit=True)

        # Wait for results
        try:
            self._wait_for_email_rows(driver, By, WebDriverWait, timeout=15)
            count: int = driver.execute_script(
                "return document.querySelectorAll("
                "'tr.zA, tr.zE').length;"
            )
        except TimeoutException:
            log.info("No unread emails to mark as read")
            return 0

        if count == 0:
            log.info("No unread emails to mark as read")
            return 0

        # Click the "Select all" checkbox in the toolbar
        try:
            select_all = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR,
                     'div[role="toolbar"] '
                     'span[role="checkbox"],'
                     ' div[gh="mtb"] '
                     'span[role="checkbox"]')
                )
            )
            select_all.click()
            time.sleep(0.5)
        except (TimeoutException, Exception):
            log.warning(
                "Could not click select-all checkbox "
                "— skipping mark as read"
            )
            return 0

        # Click "Mark as read" button (open-envelope icon)
        try:
            mark_read = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR,
                     'div[act="1"] button[aria-label],'
                     ' div[role="toolbar"] '
                     'button[aria-label="Mark as read"]')
                )
            )
            mark_read.click()
            time.sleep(1)
            log.info(
                "Marked %d unread email(s) as read", count,
            )
        except TimeoutException:
            # Fallback: use keyboard shortcut Shift+I
            from selenium.webdriver.common.action_chains import (
                ActionChains,
            )
            ActionChains(driver).key_down(
                Keys.SHIFT
            ).send_keys("i").key_up(Keys.SHIFT).perform()
            time.sleep(1)
            log.info(
                "Marked %d unread email(s) as read "
                "(via keyboard shortcut)",
                count,
            )

        return count

    def wait_for_new_unread(
        self,
        baseline: int = 0,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> bool:
        """
        Poll Gmail inbox DOM until unread count exceeds *baseline*.

        Scans inbox rows directly (``tr.zE`` = unread) with a
        subject-text check instead of using Gmail search, which
        can lag behind the actual inbox by 30+ seconds.

        :param baseline: Unread count before the invite was sent.
        :param timeout: Max seconds to poll.
        :return: True when a new unread email is detected.
        """
        if self._driver is None:
            raise RuntimeError("Not connected — call connect() first")

        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        driver = self._driver
        deadline = time.monotonic() + timeout

        # Fingerprint tracks row snippet text so we can detect
        # new messages that Gmail threads into an existing row
        # (row count stays the same but content changes).
        baseline_fp: Optional[str] = None

        log.info(
            "Polling inbox DOM for new unread email "
            "(baseline=%d, timeout=%ds)",
            baseline, timeout,
        )

        while time.monotonic() < deadline:
            if self._stop_event and self._stop_event.is_set():
                log.warning("Stop event — aborting email wait")
                return False

            # Navigate to inbox to get fresh DOM
            try:
                driver.get(self._gmail_start_url())
            except TimeoutException:
                remaining = deadline - time.monotonic()
                log.warning(
                    "Gmail page load timed out (%ds) "
                    "— retrying in %ds (%.0fs left)",
                    PAGE_LOAD_TIMEOUT,
                    SEARCH_RETRY_INTERVAL, remaining,
                )
                time.sleep(SEARCH_RETRY_INTERVAL)
                continue
            self._dismiss_overlays(driver, By)

            # Wait for inbox rows to load
            try:
                self._wait_for_email_rows(driver, By, WebDriverWait, timeout=15)
            except TimeoutException:
                remaining = deadline - time.monotonic()
                log.info(
                    "No inbox rows loaded "
                    "— polling in %ds (%.0fs left)",
                    SEARCH_RETRY_INTERVAL, remaining,
                )
                time.sleep(SEARCH_RETRY_INTERVAL)
                continue

            # Count unread rows (tr.zE) whose text contains
            # the invite subject fragment, and capture a content
            # fingerprint to detect threaded replies.
            result = driver.execute_script(
                "var rows = document.querySelectorAll('tr.zE');"
                "var frag = arguments[0].toLowerCase();"
                "var c = 0, fp = [];"
                "for (var i = 0; i < rows.length; i++) {"
                "  var txt = (rows[i].textContent || '');"
                "  if (txt.toLowerCase().indexOf(frag)"
                "      !== -1) {"
                "    c++;"
                "    fp.push(txt.substring(0, 200));"
                "  }"
                "}"
                "return {count: c, fp: fp.join('|||')};",
                SEARCH_SUBJECT,
            ) or {"count": 0, "fp": ""}

            count = int(result.get("count", 0))
            fp = result.get("fp", "")

            if baseline_fp is None:
                baseline_fp = fp

            if count > baseline:
                log.info(
                    "New unread email detected in inbox "
                    "(%d > %d)",
                    count, baseline,
                )
                return True

            if fp != baseline_fp:
                log.info(
                    "Inbox content changed (likely threaded "
                    "reply) — treating as new email"
                )
                return True

            remaining = deadline - time.monotonic()
            log.info(
                "No new unread email yet (%d, baseline %d) "
                "— polling in %ds (%.0fs left)",
                count, baseline,
                SEARCH_RETRY_INTERVAL, remaining,
            )
            time.sleep(SEARCH_RETRY_INTERVAL)

        log.warning(
            "Timed out waiting for new unread email after %ds",
            timeout,
        )
        return False

    def wait_for_new_matching_email(
        self,
        baseline: int = 0,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> bool:
        """
        Wait until the count of matching invite emails increases.

        This uses a single Gmail search query (label + subject + email)
        and avoids unread-state dependence.

        :param baseline: Matching row count before invite send.
        :param timeout: Max seconds to wait.
        :return: True when a newer matching email is detected.
        """
        if self._driver is None:
            raise RuntimeError("Not connected — call connect() first")

        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        driver = self._driver
        deadline = time.monotonic() + timeout

        log.info(
            "Polling matching invite emails (baseline=%d, timeout=%ds)",
            baseline,
            timeout,
        )

        while time.monotonic() < deadline:
            if self._stop_event and self._stop_event.is_set():
                log.warning("Stop event — aborting matching email wait")
                return False

            if "mail.google.com" not in (driver.current_url or ""):
                try:
                    driver.get(self._gmail_start_url())
                except TimeoutException:
                    time.sleep(SEARCH_RETRY_INTERVAL)
                    continue

            self._dismiss_overlays(driver, By)
            search_box = self._find_search_box(driver, By, EC, WebDriverWait)
            search_query = self._build_invite_search_query(unread=False)
            self._set_search_query(search_box, search_query, submit=True)

            try:
                self._wait_for_email_rows(driver, By, WebDriverWait, timeout=15)
                count = int(driver.execute_script(
                    "return document.querySelectorAll('tr.zA, tr.zE').length;"
                ) or 0)
            except TimeoutException:
                count = 0

            if count > baseline:
                log.info(
                    "New matching invite email detected (%d > %d)",
                    count,
                    baseline,
                )
                return True

            remaining = deadline - time.monotonic()
            log.info(
                "No new matching email yet (%d, baseline %d) — "
                "polling in %ds (%.0fs left)",
                count,
                baseline,
                SEARCH_RETRY_INTERVAL,
                remaining,
            )
            time.sleep(SEARCH_RETRY_INTERVAL)

        log.warning(
            "Timed out waiting for new matching invite email after %ds",
            timeout,
        )
        return False

    def count_unread_emails(self) -> int:
        """
        Count unread inbox rows matching the invite subject.

        Scans the inbox DOM directly (``tr.zE`` with subject text)
        instead of using Gmail search to avoid index lag.

        :return: Number of matching unread rows (0 if none).
        :raises RuntimeError: If the browser is not connected.
        """
        if self._driver is None:
            raise RuntimeError("Not connected — call connect() first")

        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        driver = self._driver

        if "mail.google.com" not in (driver.current_url or ""):
            driver.get(self._gmail_start_url())

        self._dismiss_overlays(driver, By)

        try:
            self._wait_for_email_rows(driver, By, WebDriverWait, timeout=15)
        except TimeoutException:
            log.info("No inbox rows found")
            return 0

        count: int = int(driver.execute_script(
            "var rows = document.querySelectorAll('tr.zE');"
            "var frag = arguments[0].toLowerCase();"
            "var c = 0;"
            "for (var i = 0; i < rows.length; i++) {"
            "  if ((rows[i].textContent || '')"
            "      .toLowerCase().indexOf(frag) !== -1) c++;"
            "}"
            "return c;",
            SEARCH_SUBJECT,
        ) or 0)

        log.info("Found %d unread email(s) in inbox", count)
        return count

    def count_matching_emails(self) -> int:
        """
        Count how many emails currently match the invite subject.

        Call this *before* sending the invite so
        :meth:`get_download_link` can skip stale results.

        :return: Number of matching email rows (0 if none).
        :raises RuntimeError: If the browser is not connected.
        """
        if self._driver is None:
            raise RuntimeError("Not connected — call connect() first")

        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        driver = self._driver

        if "mail.google.com" not in (driver.current_url or ""):
            log.info("Navigating to Gmail")
            driver.get(self._gmail_start_url())

        self._dismiss_overlays(driver, By)

        search_box = self._find_search_box(driver, By, EC, WebDriverWait)

        search_query = (
            self._build_invite_search_query(unread=False)
        )
        log.info("Counting existing emails: %s", search_query)
        self._set_search_query(search_box, search_query, submit=True)

        try:
            self._wait_for_email_rows(driver, By, WebDriverWait, timeout=15)
            count: int = driver.execute_script(
                "return document.querySelectorAll('tr.zA, tr.zE').length;"
            )
        except TimeoutException:
            count = 0

        log.info("Found %d existing email(s)", count)
        return count

    def get_download_link(
        self,
        timeout: int = DEFAULT_TIMEOUT,
        max_rows: Optional[int] = None,
    ) -> str:
        """
        Search for invite emails and return the download URL.

        Uses label + subject + email query and checks newest row first.
        Retries until *timeout* seconds have elapsed.

        :param timeout: Max seconds to wait for the email to appear.
        :param max_rows: Maximum email rows to check per search pass.
        :return: Download URL string.
        :raises TimeoutError: If no usable link is found in time.
        :raises RuntimeError: If the browser is not connected.
        """
        if self._driver is None:
            raise RuntimeError("Not connected — call connect() first")

        from selenium.common.exceptions import (
            NoSuchElementException,
            TimeoutException,
        )
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        driver = self._driver
        deadline = time.monotonic() + timeout
        link_texts = LINK_TEXTS_64 if self._is_64_bit else LINK_TEXTS_32

        while True:
            if self._stop_event and self._stop_event.is_set():
                raise TimeoutError("Stopped by user (ESC)")

            # Step 1: Navigate to Gmail
            if "mail.google.com" not in (driver.current_url or ""):
                log.info("Navigating to Gmail")
                driver.get(self._gmail_start_url())

            # Dismiss notification prompts that block clicks
            self._dismiss_overlays(driver, By)

            # Step 2: Wait for search input
            log.info("Waiting for Gmail search input")
            search_box = self._find_search_box(
                driver, By, EC, WebDriverWait,
            )

            # Step 3: Search for unread emails matching the subject
            search_query = (
                self._build_invite_search_query(unread=False)
            )
            log.info("Searching Gmail: %s", search_query)
            self._set_search_query(search_box, search_query, submit=True)

            # Step 4: Wait for results and count rows
            try:
                self._wait_for_email_rows(driver, By, WebDriverWait, timeout=15)
                row_count = int(driver.execute_script(
                    "return document.querySelectorAll("
                    "'tr.zA, tr.zE').length;"
                ))
            except TimeoutException:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"No unread email found within "
                        f"{timeout}s: {SEARCH_SUBJECT}"
                    )
                log.info(
                    "No matching email yet — retrying in %ds",
                    SEARCH_RETRY_INTERVAL,
                )
                time.sleep(SEARCH_RETRY_INTERVAL)
                continue

            # Step 5: Iterate matched rows (newest first).
            check_count = row_count
            if max_rows is not None:
                check_count = min(row_count, max_rows)
            log.info(
                "%d matching email(s) found (checking %d)",
                row_count, check_count,
            )
            for row_idx in range(check_count):
                # Click the row by index (no offsetParent guard —
                # Gmail rows can report null offsetParent while
                # visible)
                clicked = driver.execute_script(f"""
                    var rows = document.querySelectorAll(
                        'tr.zA, tr.zE'
                    );
                    if ({row_idx} < rows.length) {{
                        rows[{row_idx}].click();
                        return true;
                    }}
                    return false;
                """)
                if not clicked:
                    log.info(
                        "Row %d not present in DOM — skipping",
                        row_idx,
                    )
                    continue
                log.info(
                    "Opened email row %d/%d (marked as read)",
                    row_idx + 1, row_count,
                )

                # Find download link in email body
                url = self._extract_link_from_body(
                    driver, By, EC, WebDriverWait, link_texts,
                )
                if not url:
                    log.info(
                        "No download link in row %d — skipping",
                        row_idx + 1,
                    )
                    driver.back()
                    time.sleep(1)
                    continue

                # Check tenant hostname in the link
                if (
                    self._tenant_hostname
                    and self._tenant_hostname not in url
                ):
                    log.info(
                        "Link tenant mismatch (expected %s, "
                        "got %s) — skipping row %d",
                        self._tenant_hostname, url, row_idx + 1,
                    )
                    driver.back()
                    time.sleep(1)
                    continue

                log.info("Found download link: %s", url)
                return url

            # No matching row found in this pass
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "No matching email with download "
                    f"link found within {timeout}s "
                    f"(tenant={self._tenant_hostname})"
                )
            log.info(
                "No matching link in latest matching emails "
                "— retrying in %ds",
                SEARCH_RETRY_INTERVAL,
            )
            time.sleep(SEARCH_RETRY_INTERVAL)

    def _extract_link_from_body(
        self, driver: Any, By: Any, EC: Any,
        WebDriverWait: Any, link_texts: list[str],
    ) -> str:
        """
        Extract the download URL from the currently opened email body.

        Waits for the email body container to load, then searches
        for download links by text.  Also logs all ``<a>`` tags
        in the body on first failure for debugging.

        :return: Unwrapped URL, or empty string if not found.
        """
        from selenium.common.exceptions import (
            StaleElementReferenceException,
            TimeoutException,
        )

        for link_text in link_texts:
            xpath = f'//a[contains(text(), "{link_text}")]'
            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, xpath))
                )
                # Use find_elements and take the last match so that
                # in threaded views the newest message's link wins.
                elements = driver.find_elements(By.XPATH, xpath)
                if not elements:
                    continue
                link_el = elements[-1]
                # Retry once on stale element (DOM may refresh after
                # navigating back from a previous email)
                for attempt in range(2):
                    try:
                        raw_href = link_el.get_attribute("href") or ""
                        return self._unwrap_google_redirect(raw_href)
                    except StaleElementReferenceException:
                        if attempt == 0:
                            log.info(
                                "Stale element — re-finding link"
                            )
                            time.sleep(1)
                            elements = driver.find_elements(
                                By.XPATH, xpath,
                            )
                            if not elements:
                                break
                            link_el = elements[-1]
                        else:
                            raise
            except TimeoutException:
                log.info("Link text %r not found, trying next", link_text)
                continue

        # Dump email body HTML to file for debugging
        try:
            info = driver.execute_script("""
                var body = document.querySelector(
                    'div.a3s, div.ii.gt'
                );
                var html = '';
                if (body) {
                    html = body.innerHTML;
                } else {
                    var msg = document.querySelector(
                        'div.adn, div[role="main"]'
                    );
                    html = msg
                        ? '<!-- fallback -->' + msg.innerHTML
                        : '<!-- no email body found -->';
                }
                // Try to grab the email subject line
                var subj = '';
                var h2 = document.querySelector(
                    'h2.hP, span[data-thread-perm-id]'
                );
                if (h2) subj = h2.textContent.trim();
                return {html: html, subject: subj};
            """)
            html = info.get("html", "") if info else ""
            subject = info.get("subject", "") if info else ""
            stamp = time.strftime("%Y%m%d_%H%M%S")
            fname = f"email_debug_{stamp}.html"
            dump_path = (
                Path(__file__).parent / "log" / fname
            )
            dump_path.parent.mkdir(parents=True, exist_ok=True)
            header = (
                f"<!-- subject: {subject} -->\n"
                f"<!-- timestamp: {stamp} -->\n"
            )
            dump_path.write_text(
                header + html, encoding="utf-8",
            )
            log.info(
                "Dumped email body to %s "
                "(subject=%r, %d bytes)",
                dump_path, subject, len(html),
            )
        except Exception as exc:
            log.warning("Failed to dump email body: %s", exc)
        return ""

    def close(self) -> None:
        """
        Detach from Chrome without closing the user's browser.

        Uses ``quit()`` to cleanly end the WebDriver session.
        Falls back to ``taskkill`` only if ``quit()`` fails.
        """
        if self._driver is None:
            return
        driver = self._driver
        self._driver = None
        try:
            driver.quit()
            log.info("WebDriver session closed via quit()")
        except Exception:
            # quit() failed — force-kill the chromedriver process
            pid = None
            try:
                if driver.service and driver.service.process:
                    pid = driver.service.process.pid
            except Exception:
                pass
            if pid:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)],
                        capture_output=True,
                    )
                    log.info(
                        "quit() failed — killed chromedriver.exe "
                        "(PID %d)",
                        pid,
                    )
                except Exception:
                    pass

    def restart(self) -> None:
        """Force-close the current Chrome session and reconnect."""
        log.info("Restarting Gmail browser on port %d", self._debug_port)
        self._close_chrome_via_cdp()
        self.connect()

    @staticmethod
    def is_retryable_disconnect(exc: Exception) -> bool:
        """Return True for transient browser/session disconnect errors."""
        message = str(exc).lower()

        try:
            from selenium.common.exceptions import (
                InvalidSessionIdException,
                WebDriverException,
            )

            if isinstance(exc, InvalidSessionIdException):
                return True
            if isinstance(exc, WebDriverException):
                return any(
                    snippet in message
                    for snippet in RETRYABLE_BROWSER_ERROR_SNIPPETS
                )
        except Exception:
            pass

        return any(
            snippet in message
            for snippet in RETRYABLE_BROWSER_ERROR_SNIPPETS
        )

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _find_search_box(
        driver: Any, By: Any, EC: Any, WebDriverWait: Any,
        timeout: int = 30,
    ) -> Any:
        """
        Locate the Gmail search input.

        Uses a resilient XPath-first locator chain to tolerate Gmail UI
        variations and delayed rendering.

        :param timeout: Seconds to wait for the primary selector.
        :return: The search box WebElement.
        :raises TimeoutException: If no suitable input is found in time.
        """
        from selenium.common.exceptions import TimeoutException

        locators: list[tuple[str, str]] = [
            (
                By.XPATH,
                '//input[@name="q" and '
                '(@aria-label="Search mail" or contains(@aria-label, "Search"))]',
            ),
            (
                By.XPATH,
                '//input[@name="q" and @placeholder="Search mail"]',
            ),
            (
                By.XPATH,
                '//input[@name="q" and @type="text"]',
            ),
            (
                By.XPATH,
                '//form//input[@name="q"]',
            ),
        ]

        end_time = time.monotonic() + timeout
        while time.monotonic() < end_time:
            GmailBrowser._dismiss_overlays(driver, By)
            for by, selector in locators:
                try:
                    elements = driver.find_elements(by, selector)
                except Exception:
                    continue
                if not elements:
                    continue
                for element in elements:
                    try:
                        if element.is_displayed() and element.is_enabled():
                            return element
                    except Exception:
                        continue
            time.sleep(0.5)

        raise TimeoutException("Gmail search box not found")

    @staticmethod
    def _set_search_query(
        search_box: Any,
        query: str,
        submit: bool = True,
    ) -> None:
        """
        Set Gmail search query robustly without relying on clear() only.

        Some Gmail UI states expose a present-but-not-interactable input;
        this method tries a normal clear/send path first, then a select-all
        replacement path as fallback.
        """
        from selenium.common.exceptions import ElementNotInteractableException
        from selenium.webdriver.common.keys import Keys

        try:
            search_box.click()
            search_box.clear()
            search_box.send_keys(query)
            if submit:
                search_box.send_keys(Keys.RETURN)
            return
        except ElementNotInteractableException:
            pass

        # Fallback path for transient non-interactable states.
        search_box.click()
        search_box.send_keys(Keys.CONTROL, "a")
        search_box.send_keys(Keys.BACKSPACE)
        search_box.send_keys(query)
        if submit:
            search_box.send_keys(Keys.RETURN)

    def _build_invite_search_query(self, unread: bool = True) -> str:
        """Build Gmail query for invite emails using label + subject + email."""
        parts: list[str] = []
        if unread:
            parts.append("is:unread")
        if SEARCH_LABEL:
            parts.append(f'label:"{SEARCH_LABEL}"')
        parts.append(f'subject:("{SEARCH_SUBJECT}")')
        parts.append(f'"{self._email_address}"')
        return " ".join(parts)

    @staticmethod
    def _wait_for_email_rows(
        driver: Any, By: Any, WebDriverWait: Any, timeout: int = 15,
    ) -> Any:
        """Wait for email rows using resilient selectors (XPath first)."""
        locators: list[tuple[str, str]] = [
            (
                By.XPATH,
                '//tr[contains(@class,"zA") or contains(@class,"zE")]',
            ),
            (
                By.CSS_SELECTOR,
                'tr.zA, tr.zE',
            ),
        ]

        def _pick_row(drv: Any) -> Any:
            for by, selector in locators:
                rows = drv.find_elements(by, selector)
                if rows:
                    return rows[0]
            return False

        return WebDriverWait(driver, timeout).until(_pick_row)

    @staticmethod
    def _is_port_open(port: int, timeout: float = 2.0) -> bool:
        """Quick check whether anything is listening on localhost:port."""
        try:
            with socket.create_connection(
                ("localhost", port), timeout=timeout,
            ):
                return True
        except (ConnectionRefusedError, TimeoutError, OSError):
            return False

    @staticmethod
    def _dismiss_overlays(driver: Any, by_cls: Any) -> None:
        """Dismiss Gmail notification/promotion overlays if present."""
        try:
            no_thanks = driver.find_elements(
                by_cls.XPATH,
                '//button[contains(text(), "No thanks")]',
            )
            for btn in no_thanks:
                btn.click()
                log.info("Dismissed notification overlay")
        except Exception:
            pass

    def _is_gmail_ready(self) -> bool:
        """Navigate to Gmail and verify the search box is reachable."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        try:
            self._driver.get(self._gmail_start_url())
            self._find_search_box(
                self._driver,
                By,
                EC,
                WebDriverWait,
                timeout=15,
            )
            return True
        except Exception:
            return False

    def _gmail_start_url(self) -> str:
        """Return Gmail start URL, preferring label deep-link when configured."""
        if SEARCH_LABEL:
            return (
                "https://mail.google.com/mail/u/0/?pli=1#label/"
                f"{quote_plus(SEARCH_LABEL)}"
            )
        return GMAIL_URL

    def _close_chrome_via_cdp(self) -> None:
        """Close Chrome via DevTools Protocol and disconnect WebDriver."""
        if self._driver is None:
            return
        try:
            self._driver.execute_cdp_cmd("Browser.close", {})
            log.info("Closed Chrome via DevTools Protocol")
        except Exception:
            pass
        try:
            self._driver.quit()
        except Exception:
            pass
        self._driver = None
        time.sleep(3)

    def _launch_chrome(self) -> None:
        """Launch Chrome with a local profile and remote debugging."""
        chrome_exe = ""
        for path in CHROME_PATHS:
            if Path(path).is_file():
                chrome_exe = path
                break
        if not chrome_exe:
            raise FileNotFoundError(
                "Chrome not found. Install Chrome or set the path."
            )

        self._profile_dir.mkdir(parents=True, exist_ok=True)
        start_url = self._gmail_start_url()
        cmd = (
            f'start "" "{chrome_exe}"'
            f' --user-data-dir="{self._profile_dir}"'
            f" --remote-debugging-port={self._debug_port}"
            " --remote-allow-origins=*"
            f' "{start_url}"'
        )
        log.info("Launching Chrome: %s", cmd)
        subprocess.Popen(f"cmd /c {cmd}", shell=True)

        # Wait for Chrome to start accepting connections
        log.info("Waiting for Chrome to start...")
        time.sleep(5)

    @staticmethod
    def _unwrap_google_redirect(url: str) -> str:
        """
        Extract the real URL from a Gmail redirect wrapper.

        Gmail rewrites outbound links as::

            https://www.google.com/url?q=REAL_URL&sa=D&...

        If the URL is not a Google redirect, return it unchanged.
        """
        parsed = urlparse(url)
        if parsed.hostname and "google.com" in parsed.hostname:
            q_values = parse_qs(parsed.query).get("q")
            if q_values:
                return q_values[0]
        return url
