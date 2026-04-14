"""
Test configuration — mock external dependencies before they are imported.
This allows tests to run without nsclient or webapi packages installed.
"""

import sys
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

# Mock external packages that are not available in the test environment
# These must be set BEFORE any project module imports them
sys.modules["nsclient"] = MagicMock()
sys.modules["nsclient.nsclient"] = MagicMock()
sys.modules["webapi"] = MagicMock()
sys.modules["webapi.auth"] = MagicMock()
sys.modules["webapi.auth.authentication"] = MagicMock()
sys.modules["webapi.settings"] = MagicMock()
sys.modules["webapi.settings.security_cloud_platform"] = MagicMock()
sys.modules["webapi.settings.security_cloud_platform.netskope_client"] = MagicMock()
sys.modules["webapi.settings.security_cloud_platform.netskope_client.client_configuration"] = MagicMock()
sys.modules["webapi.settings.security_cloud_platform.netskope_client.devices"] = MagicMock()
sys.modules["webapi.settings.security_cloud_platform.netskope_client.users"] = MagicMock()

# Selenium — exception classes must be real Exception subclasses so
# that ``except TimeoutException:`` works at runtime.


class _TimeoutException(Exception):
    pass


class _NoSuchElementException(Exception):
    pass


_mock_exceptions = MagicMock()
_mock_exceptions.TimeoutException = _TimeoutException
_mock_exceptions.NoSuchElementException = _NoSuchElementException

sys.modules["selenium"] = MagicMock()
sys.modules["selenium.webdriver"] = MagicMock()
sys.modules["selenium.webdriver.chrome"] = MagicMock()
sys.modules["selenium.webdriver.chrome.options"] = MagicMock()
sys.modules["selenium.common"] = MagicMock()
sys.modules["selenium.common.exceptions"] = _mock_exceptions
sys.modules["selenium.webdriver.common"] = MagicMock()
sys.modules["selenium.webdriver.common.by"] = MagicMock()
sys.modules["selenium.webdriver.common.keys"] = MagicMock()
sys.modules["selenium.webdriver.support"] = MagicMock()
sys.modules["selenium.webdriver.support.ui"] = MagicMock()
sys.modules["selenium.webdriver.support.expected_conditions"] = MagicMock()


@pytest.fixture(autouse=True)
def _block_real_browser_and_network() -> None:
    """Guardrail: block real browser/network side effects in unit tests."""
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "webbrowser.open",
                side_effect=AssertionError(
                    "Real browser open() is blocked in tests"
                ),
            )
        )
        stack.enter_context(
            patch(
                "webbrowser.open_new",
                side_effect=AssertionError(
                    "Real browser open_new() is blocked in tests"
                ),
            )
        )
        stack.enter_context(
            patch(
                "webbrowser.open_new_tab",
                side_effect=AssertionError(
                    "Real browser open_new_tab() is blocked in tests"
                ),
            )
        )
        try:
            stack.enter_context(
                patch(
                    "requests.sessions.Session.request",
                    side_effect=AssertionError(
                        "Real HTTP requests are blocked in tests"
                    ),
                )
            )
        except Exception:
            # requests may be unavailable in some local test envs
            pass
        try:
            stack.enter_context(
                patch(
                    "util_email.GmailBrowser._launch_chrome",
                    side_effect=AssertionError(
                        "Real Chrome launch is blocked in tests"
                    ),
                )
            )
        except Exception:
            # util_email may not be imported in all test subsets
            pass
        yield
