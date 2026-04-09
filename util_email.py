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
        then attach.
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
                return
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
        subject = SUBJECT_TEMPLATE.format(email=self._email_address)

        if "mail.google.com" not in (driver.current_url or ""):
            log.info("Navigating to Gmail")
            driver.get(GMAIL_URL)

        self._dismiss_overlays(driver, By)

        try:
            search_box = WebDriverWait(driver, 30).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'input[aria-label="Search mail"]')
                )
            )
        except TimeoutException:
            search_box = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, 'input[name="q"]')
                )
            )

        search_query = f'subject:("{subject}")'
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
        skip_count: int = 0,
    ) -> str:
        """
        Navigate Gmail, find the invite email, and return the download URL.

        Retries if the email has not arrived yet (up to *timeout* seconds).

        :param timeout: Max seconds to wait for the email to appear.
        :param skip_count: Number of old emails to ignore (newest-first).
            Pass the value returned by :meth:`count_matching_emails`.
        :return: Download URL string.
        :raises TimeoutError: If the email or link is not found in time.
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
        subject = SUBJECT_TEMPLATE.format(email=self._email_address)
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
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR,
                         'input[aria-label="Search mail"]')
                    )
                )
            except TimeoutException:
                # Fallback selector
                search_box = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, 'input[name="q"]')
                    )
                )

            # Step 3: Search for the email
            search_query = f'subject:("{subject}")'
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
                    "return document.querySelectorAll('tr.zA, tr.zE').length;"
                ))
            except TimeoutException:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Email not found within {timeout}s: {subject}"
                    )
                log.info(
                    "Email not found yet — retrying in %ds",
                    SEARCH_RETRY_INTERVAL,
                )
                time.sleep(SEARCH_RETRY_INTERVAL)
                continue

            # Gmail shows newest first — only check new rows
            new_count = row_count - skip_count
            if new_count <= 0:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"No new email arrived within {timeout}s: {subject}"
                    )
                log.info(
                    "No new email yet (%d old) — retrying in %ds",
                    skip_count, SEARCH_RETRY_INTERVAL,
                )
                time.sleep(SEARCH_RETRY_INTERVAL)
                continue

            # Step 5: Iterate only new rows (indices 0..new_count-1)
            log.info(
                "%d new email(s) found (%d total, %d skipped)",
                new_count, row_count, skip_count,
            )
            for row_idx in range(new_count):
                # Click the row by index (no offsetParent guard —
                # Gmail rows can report null offsetParent while visible)
                clicked = driver.execute_script(f"""
                    var rows = document.querySelectorAll('tr.zA, tr.zE');
                    if ({row_idx} < rows.length) {{
                        rows[{row_idx}].click();
                        return true;
                    }}
                    return false;
                """)
                if not clicked:
                    log.info(
                        "Row %d not present in DOM — skipping", row_idx,
                    )
                    continue
                log.info("Opened email row %d/%d", row_idx + 1, new_count)

                # Find download link in email body
                url = self._extract_link_from_body(
                    driver, By, EC, WebDriverWait, link_texts,
                )
                if not url:
                    log.info("No download link in row %d — skipping", row_idx + 1)
                    driver.back()
                    time.sleep(1)
                    continue

                # Check tenant hostname in the link
                if self._tenant_hostname and self._tenant_hostname not in url:
                    log.info(
                        "Link tenant mismatch (expected %s, got %s) "
                        "— skipping row %d",
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
                    "No email with matching tenant download link found "
                    f"within {timeout}s (tenant={self._tenant_hostname})"
                )
            log.info(
                "No matching email found — retrying in %ds",
                SEARCH_RETRY_INTERVAL,
            )
            time.sleep(SEARCH_RETRY_INTERVAL)

    def _extract_link_from_body(
        self, driver: Any, By: Any, EC: Any,
        WebDriverWait: Any, link_texts: list[str],
    ) -> str:
        """
        Extract the download URL from the currently opened email body.

        :return: Unwrapped URL, or empty string if not found.
        """
        from selenium.common.exceptions import TimeoutException

        for link_text in link_texts:
            xpath = f'//a[contains(text(), "{link_text}")]'
            try:
                link_el = WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.XPATH, xpath))
                )
                raw_href = link_el.get_attribute("href") or ""
                return self._unwrap_google_redirect(raw_href)
            except TimeoutException:
                log.info("Link text %r not found, trying next", link_text)
                continue
        return ""

    def close(self) -> None:
        """
        Detach from Chrome without closing the user's browser.

        Kills any leftover chromedriver.exe processes spawned by
        Selenium to prevent accumulation across runs.
        """
        if self._driver is not None:
            pid = self._driver.service.process.pid if self._driver.service else None
            self._driver = None
            if pid:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)],
                        capture_output=True,
                    )
                    log.info("Killed chromedriver.exe (PID %d)", pid)
                except Exception:
                    pass
            self._kill_stale_chromedrivers()

    @staticmethod
    def _kill_stale_chromedrivers() -> None:
        """Kill any remaining chromedriver.exe processes."""
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", "chromedriver.exe"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                log.info("Killed stale chromedriver.exe processes")
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
