"""
Test configuration — mock external dependencies before they are imported.
This allows tests to run without nsclient or webapi packages installed.
"""

import sys
from unittest.mock import MagicMock

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
