"""
Unit tests for util_email.py — GmailBrowser email automation.
All Selenium calls are mocked — no real Chrome or Gmail needed.

Selenium is mocked at the sys.modules level in conftest.py. Since
util_email.py uses deferred imports (inside methods), we configure
the mock behavior via sys.modules references, not @patch on module
attributes.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from util_email import (
    GmailBrowser,
    DEFAULT_DEBUG_PORT,
)

# References to conftest-mocked selenium modules.  The deferred imports
# in util_email.py resolve through these exact objects.
_mock_selenium = sys.modules["selenium"]
_mock_wait_ui = sys.modules["selenium.webdriver.support.ui"]
_mock_exceptions = sys.modules["selenium.common.exceptions"]
TimeoutException = _mock_exceptions.TimeoutException


# ── _unwrap_google_redirect ──────────────────────────────────────


class TestUnwrapGoogleRedirect:
    """Tests for the static Google redirect URL unwrap helper."""

    def test_unwraps_google_redirect(self) -> None:
        """Extracts real URL from a Google redirect wrapper."""
        redirect = (
            "https://www.google.com/url?"
            "q=https://download-tenant.example.com/dlr/win/TOKEN"
            "&sa=D&source=gmail"
        )
        result = GmailBrowser._unwrap_google_redirect(redirect)
        assert result == "https://download-tenant.example.com/dlr/win/TOKEN"

    def test_non_redirect_passthrough(self) -> None:
        """Non-Google URL is returned unchanged."""
        url = "https://download-tenant.example.com/dlr/win/TOKEN"
        assert GmailBrowser._unwrap_google_redirect(url) == url

    def test_google_url_without_q_param(self) -> None:
        """Google URL without q param is returned unchanged."""
        url = "https://www.google.com/url?sa=D&source=gmail"
        assert GmailBrowser._unwrap_google_redirect(url) == url


# ── connect ──────────────────────────────────────────────────────


class TestConnect:
    """Tests for attaching Selenium to Chrome."""

    def setup_method(self) -> None:
        """Reset the selenium.webdriver.Chrome mock before each test."""
        _mock_selenium.webdriver.Chrome.reset_mock()
        _mock_selenium.webdriver.Chrome.side_effect = None

    @patch.object(GmailBrowser, "_is_port_open", return_value=True)
    def test_connect_success(self, _mock_port: MagicMock) -> None:
        """Successfully attaches to Chrome on debug port."""
        browser = GmailBrowser(email_address="user@example.com")
        browser.connect()
        _mock_selenium.webdriver.Chrome.assert_called_once()
        assert browser._driver is not None

    @patch.object(GmailBrowser, "_launch_chrome")
    @patch.object(GmailBrowser, "_is_port_open", return_value=False)
    def test_connect_failure_raises(
        self, _mock_port: MagicMock, _mock_launch: MagicMock,
    ) -> None:
        """Raises RuntimeError with helpful message on connection failure."""
        _mock_selenium.webdriver.Chrome.side_effect = Exception(
            "Connection refused"
        )
        browser = GmailBrowser(email_address="user@example.com")
        with pytest.raises(RuntimeError, match="Could not connect"):
            browser.connect()

    @patch.object(GmailBrowser, "_is_port_open", return_value=True)
    def test_connect_custom_port(self, _mock_port: MagicMock) -> None:
        """Uses the specified debug port."""
        browser = GmailBrowser(
            email_address="user@example.com", debug_port=9333,
        )
        browser.connect()
        _mock_selenium.webdriver.Chrome.assert_called_once()

    @patch.object(GmailBrowser, "_launch_chrome")
    @patch.object(GmailBrowser, "_is_port_open", return_value=False)
    def test_connect_launches_chrome_when_port_closed(
        self, _mock_port: MagicMock, mock_launch: MagicMock,
    ) -> None:
        """Launches Chrome when nothing is listening on the debug port."""
        browser = GmailBrowser(email_address="user@example.com")
        browser.connect()
        mock_launch.assert_called_once()
        _mock_selenium.webdriver.Chrome.assert_called_once()


# ── get_download_link ────────────────────────────────────────────


class TestGetDownloadLink:
    """Tests for the Gmail navigation and link extraction flow."""

    def setup_method(self) -> None:
        """Reset WebDriverWait mock before each test."""
        _mock_wait_ui.WebDriverWait.reset_mock()

    def _make_browser(
        self, is_64_bit: bool = True,
    ) -> GmailBrowser:
        """Create a GmailBrowser with a mock driver attached."""
        browser = GmailBrowser(
            email_address="user@example.com",
            is_64_bit=is_64_bit,
        )
        browser._driver = MagicMock()
        browser._driver.current_url = (
            "https://mail.google.com/mail/u/0/"
        )
        return browser

    def test_not_connected_raises(self) -> None:
        """Raises RuntimeError when connect() hasn't been called."""
        browser = GmailBrowser(email_address="user@example.com")
        with pytest.raises(RuntimeError, match="Not connected"):
            browser.get_download_link()

    def test_get_download_link_success(self) -> None:
        """Successfully extracts download link from email."""
        browser = self._make_browser(is_64_bit=True)
        download_url = (
            "https://download-tenant.example.com/dlr/win/TOKEN123"
        )

        mock_search_box = MagicMock()
        mock_row = MagicMock()
        mock_link = MagicMock()
        mock_link.get_attribute.return_value = download_url

        mock_wait = MagicMock()
        mock_wait.until.side_effect = [
            mock_search_box,  # search input
            mock_row,         # first result row
            mock_link,        # download link
        ]
        _mock_wait_ui.WebDriverWait.return_value = mock_wait

        result = browser.get_download_link(timeout=10)
        assert result == download_url

    def test_unwraps_google_redirect_in_link(self) -> None:
        """Google redirect URLs are unwrapped to the real download URL."""
        browser = self._make_browser()
        real_url = "https://download-tenant.example.com/dlr/win/TOKEN"
        redirect_url = (
            f"https://www.google.com/url?q={real_url}"
            "&sa=D&source=gmail"
        )

        mock_search_box = MagicMock()
        mock_row = MagicMock()
        mock_link = MagicMock()
        mock_link.get_attribute.return_value = redirect_url

        mock_wait = MagicMock()
        mock_wait.until.side_effect = [
            mock_search_box, mock_row, mock_link,
        ]
        _mock_wait_ui.WebDriverWait.return_value = mock_wait

        result = browser.get_download_link(timeout=10)
        assert result == real_url

    def test_retries_on_no_results(self) -> None:
        """Retries when search results are not found (email not arrived)."""
        browser = self._make_browser()
        download_url = (
            "https://download-tenant.example.com/dlr/win/TOKEN"
        )

        mock_search_box = MagicMock()
        mock_row = MagicMock()
        mock_link = MagicMock()
        mock_link.get_attribute.return_value = download_url

        # 1st attempt: search OK, row times out → retry
        # 2nd attempt: all OK
        call_count = 0

        def until_side_effect(condition):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_search_box
            if call_count == 2:
                raise TimeoutException("no results")
            if call_count == 3:
                return mock_search_box
            if call_count == 4:
                return mock_row
            if call_count == 5:
                return mock_link
            return MagicMock()

        mock_wait = MagicMock()
        mock_wait.until.side_effect = until_side_effect
        _mock_wait_ui.WebDriverWait.return_value = mock_wait

        # time.monotonic: deadline calc, first check (not past), loop
        import util_email
        original_monotonic = util_email.time.monotonic
        original_sleep = util_email.time.sleep
        mono_values = iter([0, 5, 10])
        util_email.time.monotonic = lambda: next(mono_values)
        util_email.time.sleep = lambda _: None
        try:
            result = browser.get_download_link(timeout=120)
        finally:
            util_email.time.monotonic = original_monotonic
            util_email.time.sleep = original_sleep

        assert result == download_url

    def test_timeout_raises(self) -> None:
        """Raises TimeoutError when email never arrives."""
        browser = self._make_browser()
        mock_search_box = MagicMock()

        mock_wait = MagicMock()
        mock_wait.until.side_effect = [
            mock_search_box,
            TimeoutException("no results"),
        ]
        _mock_wait_ui.WebDriverWait.return_value = mock_wait

        import util_email
        original_monotonic = util_email.time.monotonic
        original_sleep = util_email.time.sleep
        mono_values = iter([0, 200])
        util_email.time.monotonic = lambda: next(mono_values)
        util_email.time.sleep = lambda _: None
        try:
            with pytest.raises(TimeoutError, match="Email not found"):
                browser.get_download_link(timeout=10)
        finally:
            util_email.time.monotonic = original_monotonic
            util_email.time.sleep = original_sleep


# ── close / context manager ─────────────────────────────────────


class TestClose:
    """Tests for detach behavior."""

    def test_close_uses_quit(self) -> None:
        """close() calls driver.quit() to end the WebDriver session."""
        browser = GmailBrowser(email_address="user@example.com")
        mock_driver = MagicMock()
        browser._driver = mock_driver
        browser.close()
        mock_driver.quit.assert_called_once()
        assert browser._driver is None

    def test_context_manager(self) -> None:
        """Context manager calls close() on exit."""
        browser = GmailBrowser(email_address="user@example.com")
        mock_driver = MagicMock()
        browser._driver = mock_driver
        with browser:
            pass
        assert browser._driver is None


# ── constructor defaults ─────────────────────────────────────────


class TestDefaults:
    """Tests for constructor defaults and config."""

    def test_default_port(self) -> None:
        browser = GmailBrowser(email_address="user@example.com")
        assert browser._debug_port == DEFAULT_DEBUG_PORT

    def test_default_64bit(self) -> None:
        browser = GmailBrowser(email_address="user@example.com")
        assert browser._is_64_bit is True

    def test_custom_config(self) -> None:
        browser = GmailBrowser(
            email_address="test@example.com",
            is_64_bit=False,
            debug_port=9333,
        )
        assert browser._email_address == "test@example.com"
        assert browser._is_64_bit is False
        assert browser._debug_port == 9333
