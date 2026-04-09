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
from urllib.parse import parse_qs, urlparse

GMAIL_URL = "https://mail.google.com/"
DEFAULT_DEBUG_PORT = 9222
SEARCH_RETRY_INTERVAL = 5  # seconds between retries
DEFAULT_TIMEOUT = 300  # total seconds to wait for email
SUBJECT_TEMPLATE = '[EXTERNAL] Netskope New User Onboarding for "{email}"'
# Gmail search-safe version — avoids nested quotes that break
# subject:("...") syntax.  The email address is matched separately.
SEARCH_SUBJECT = "Netskope New User Onboarding"
LINK_TEXTS_64 = ["Windows Client (64-bit)", "Windows Client"]
LINK_TEXTS_32 = ["Windows Client"]

LOCAL_PROFILE_DIR = Path(__file__).parent / "local_profile"
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

log = logging.getLogger(__name__)


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
    ) -> None:
        self._email_address = email_address
        self._is_64_bit = is_64_bit
        self._debug_port = debug_port
        self._tenant_hostname = tenant_hostname
        self._stop_event = stop_event
        self._driver: Optional[Any] = None

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
            driver.get(GMAIL_URL)

        self._dismiss_overlays(driver, By)

        try:
            search_box = WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR,
                     'input[aria-label="Search mail"]')
                )
            )
        except TimeoutException:
            search_box = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, 'input[name="q"]')
                )
            )

        search_query = (
            f'is:unread subject:("{SEARCH_SUBJECT}") '
            f'"{self._email_address}"'
        )
        log.info("Marking old unread emails as read: %s", search_query)
        search_box.clear()
        search_box.send_keys(search_query)
        search_box.send_keys(Keys.RETURN)

        # Wait for results
        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "tr.zA")
                )
            )
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
        except TimeoutException:
            log.warning(
                "Could not find select-all checkbox "
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
        Poll Gmail until the unread count exceeds *baseline*.

        Lightweight — only searches and counts DOM rows, never opens
        any email.  Call with *baseline* captured **before** sending
        the invite so that the new email is detected reliably.

        :param baseline: Unread count before the invite was sent.
        :param timeout: Max seconds to poll.
        :return: True when a new unread email is detected.
        """
        if self._driver is None:
            raise RuntimeError("Not connected — call connect() first")

        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        driver = self._driver
        deadline = time.monotonic() + timeout
        search_query = (
            f'is:unread subject:("{SEARCH_SUBJECT}") '
            f'"{self._email_address}"'
        )

        log.info(
            "Polling for new unread email "
            "(baseline=%d, timeout=%ds)",
            baseline, timeout,
        )

        while time.monotonic() < deadline:
            if self._stop_event and self._stop_event.is_set():
                log.warning("Stop event — aborting email wait")
                return False

            if "mail.google.com" not in (
                driver.current_url or ""
            ):
                driver.get(GMAIL_URL)

            self._dismiss_overlays(driver, By)

            try:
                search_box = WebDriverWait(driver, 15).until(
                    EC.element_to_be_clickable(
                        (By.CSS_SELECTOR,
                         'input[aria-label="Search mail"]')
                    )
                )
            except TimeoutException:
                search_box = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable(
                        (By.CSS_SELECTOR, 'input[name="q"]')
                    )
                )

            search_box.clear()
            search_box.send_keys(search_query)
            search_box.send_keys(Keys.RETURN)

            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "tr.zA")
                    )
                )
                count = int(driver.execute_script(
                    "return document.querySelectorAll("
                    "'tr.zA, tr.zE').length;"
                ))
            except TimeoutException:
                count = 0

            if count > baseline:
                log.info(
                    "New unread email detected (%d > %d)",
                    count, baseline,
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

    def count_unread_emails(self) -> int:
        """
        Count unread emails matching the invite subject.

        :return: Number of matching unread rows (0 if none).
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
            driver.get(GMAIL_URL)

        self._dismiss_overlays(driver, By)

        try:
            search_box = WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR,
                     'input[aria-label="Search mail"]')
                )
            )
        except TimeoutException:
            search_box = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, 'input[name="q"]')
                )
            )

        search_query = (
            f'is:unread subject:("{SEARCH_SUBJECT}") '
            f'"{self._email_address}"'
        )
        log.info("Counting unread emails: %s", search_query)
        search_box.clear()
        search_box.send_keys(search_query)
        search_box.send_keys(Keys.RETURN)

        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "tr.zA")
                )
            )
            count: int = driver.execute_script(
                "return document.querySelectorAll("
                "'tr.zA, tr.zE').length;"
            )
        except TimeoutException:
            count = 0

        log.info("Found %d unread email(s)", count)
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
            driver.get(GMAIL_URL)

        self._dismiss_overlays(driver, By)

        try:
            search_box = WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, 'input[aria-label="Search mail"]')
                )
            )
        except TimeoutException:
            search_box = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, 'input[name="q"]')
                )
            )

        search_query = (
            f'subject:("{SEARCH_SUBJECT}") '
            f'"{self._email_address}"'
        )
        log.info("Counting existing emails: %s", search_query)
        search_box.clear()
        search_box.send_keys(search_query)
        search_box.send_keys(Keys.RETURN)

        try:
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "tr.zA"))
            )
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
        Search for unread invite emails and return the download URL.

        Uses ``is:unread`` filter so that emails opened in previous
        calls are automatically skipped (Gmail marks opened emails
        as read).  Retries until *timeout* seconds have elapsed.

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
                driver.get(GMAIL_URL)

            # Dismiss notification prompts that block clicks
            self._dismiss_overlays(driver, By)

            # Step 2: Wait for search input
            log.info("Waiting for Gmail search input")
            try:
                search_box = WebDriverWait(driver, 30).until(
                    EC.element_to_be_clickable(
                        (By.CSS_SELECTOR,
                         'input[aria-label="Search mail"]')
                    )
                )
            except TimeoutException:
                # Fallback selector
                search_box = WebDriverWait(driver, 10).until(
                    EC.element_to_be_clickable(
                        (By.CSS_SELECTOR, 'input[name="q"]')
                    )
                )

            # Step 3: Search for unread emails matching the subject
            search_query = (
                f'is:unread subject:("{SEARCH_SUBJECT}") '
                f'"{self._email_address}"'
            )
            log.info("Searching Gmail: %s", search_query)
            search_box.clear()
            search_box.send_keys(search_query)
            search_box.send_keys(Keys.RETURN)

            # Step 4: Wait for results and count rows
            try:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "tr.zA")
                    )
                )
                row_count = int(driver.execute_script(
                    "return document.querySelectorAll("
                    "'tr.zA, tr.zE').length;"
                ))
            except TimeoutException:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"No unread email found within "
                        f"{timeout}s: {subject}"
                    )
                log.info(
                    "No unread email yet — retrying in %ds",
                    SEARCH_RETRY_INTERVAL,
                )
                time.sleep(SEARCH_RETRY_INTERVAL)
                continue

            # Step 5: Iterate unread rows (newest first).
            # Opening an email marks it as read, so the next
            # is:unread search will skip it automatically.
            check_count = row_count
            if max_rows is not None:
                check_count = min(row_count, max_rows)
            log.info(
                "%d unread email(s) found (checking %d)",
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
                    "No unread email with matching download "
                    f"link found within {timeout}s "
                    f"(tenant={self._tenant_hostname})"
                )
            log.info(
                "No matching link in unread emails "
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
                link_el = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, xpath))
                )
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
                            link_el = driver.find_element(
                                By.XPATH, xpath,
                            )
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

    # -- helpers --------------------------------------------------------

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
            self._driver.get(GMAIL_URL)
            WebDriverWait(self._driver, 15).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR,
                     'input[aria-label="Search mail"],'
                     ' input[name="q"]')
                )
            )
            return True
        except Exception:
            return False

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

        LOCAL_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        cmd = (
            f'start "" "{chrome_exe}"'
            f' --user-data-dir="{LOCAL_PROFILE_DIR}"'
            f" --remote-debugging-port={self._debug_port}"
            " --remote-allow-origins=*"
            f' "{GMAIL_URL}"'
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
