"""
Installation logic for the Netskope Client Upgrade Tool.

Handles finding, resolving, downloading, and installing the base
client MSI — including the Gmail-based email invite flow and MSI
retry logic for bad tokens (exit code 1603).
"""

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse
import threading

_PROFILE_BASE_DIR = Path(__file__).parent

from util_client import LocalClient
from util_webui import WebUIClient

BASE_VERSION_DIR = Path(__file__).parent / "data" / "base_version"
INSTALLER_JSON = Path(__file__).parent / "data" / "installer.json"

log = logging.getLogger(__name__)


def resolve_email_profile(
    email: str,
    email_profiles: dict[str, str],
    save_fn: Optional[Callable[[], None]] = None,
) -> Path:
    """
    Look up or auto-assign a Chrome user-data-dir for *email*.

    If *email* is already in *email_profiles*, return its profile directory.
    Otherwise assign the next available slot (local_profile →
    local_profile2 → local_profile3 …), update *email_profiles* in-place,
    and call *save_fn* to persist the new mapping.

    :param email: Email address to resolve a profile for.
    :param email_profiles: Mutable mapping of email → profile dir name.
    :param save_fn: Called after a new assignment to persist config.
    :return: Path to the Chrome user-data-dir for *email*.
    """
    if email in email_profiles:
        name = email_profiles[email]
        log.info("Using existing Chrome profile %r for %s", name, email)
        return _PROFILE_BASE_DIR / name

    used = set(email_profiles.values())
    if "local_profile" not in used:
        name = "local_profile"
    else:
        n = 2
        while f"local_profile{n}" in used:
            n += 1
        name = f"local_profile{n}"

    email_profiles[email] = name
    log.info("Assigned new Chrome profile %r for %s", name, email)

    if save_fn:
        try:
            save_fn()
            log.info("Saved email_profiles mapping to config")
        except Exception as exc:
            log.warning("Failed to save email_profiles mapping: %s", exc)

    return _PROFILE_BASE_DIR / name


class InstallerManager:
    """
    Manages client installation: finding, resolving, downloading,
    and installing the base MSI — including the email invite flow.

    Created by :class:`UpgradeRunner` and holds installer-specific
    state (Gmail browser, cloned installer, old email count).
    """

    def __init__(
        self,
        client: LocalClient,
        webui: WebUIClient,
        source_64_bit: bool,
        stop_event: threading.Event,
        log_dir: Optional[Path] = None,
        init_nsclient_fn: Optional[Callable[[], bool]] = None,
        email_profiles: Optional[dict[str, str]] = None,
        save_config_fn: Optional[Callable[[], None]] = None,
    ) -> None:
        self.client = client
        self.webui = webui
        self.source_64_bit = source_64_bit
        self.stop_event = stop_event
        self.log_dir = log_dir
        self._init_nsclient = init_nsclient_fn or (lambda: False)
        self._gmail_browser: Any = None
        self._cloned_installer: Optional[Path] = None
        self._email_profiles: dict[str, str] = (
            email_profiles if email_profiles is not None else {}
        )
        self._save_config_fn = save_config_fn

    # ── Public API ───────────────────────────────────────────────────

    def ensure_client_installed(
        self,
        from_version: Optional[str] = None,
        invite_email: Optional[str] = None,
    ) -> None:
        """
        Ensure the Netskope Client is installed at the correct base
        version and running.

        Compares the installed version (from registry) against the base
        MSI's Subject field:

        - **(0) Different version installed** — uninstall via msiexec /x,
          then do full install flow.
        - **(1) Same version and service running** — skip install, go
          straight to upgrade.
        - **(2) Same version but service not running** — uninstall via
          msiexec /x, then do full install flow.
        - **(3) Not installed** — do full install flow.

        The install flow: send email invite (if requested), resolve
        installer (with optional tenant-specific rename), install via
        msiexec, wait for service.

        :param from_version: Build version for download fallback
                             (e.g. '123.0.0').
        :param invite_email: Email to send enrollment invite before install.
        """
        # Step 1: Find base installer for version comparison
        base_filename = self.client.get_installer_filename(
            is_64_bit=self.source_64_bit,
        )
        base_installer = find_base_installer(base_filename)

        # Step 2: Read MSI subject to get base version
        msi_version = ""
        if base_installer:
            raw_subject = self.client.get_msi_subject(base_installer)
            if raw_subject:
                msi_version = (
                    raw_subject.rsplit(" ", 1)[-1]
                    if " " in raw_subject
                    else raw_subject
                )
                log.info(
                    "Base MSI version (subject): %s (raw: %s)",
                    msi_version, raw_subject,
                )

        # Step 3: Check current installation state
        uninstall_info = self.client.check_uninstall_registry()
        service_running = self.client.is_service_running()

        if uninstall_info.found and msi_version:
            installed_version = uninstall_info.display_version
            log.info(
                "Installed: %s (running=%s), base MSI: %s",
                installed_version, service_running, msi_version,
            )
            if installed_version == msi_version and service_running:
                log.info(
                    "Installed version matches base MSI and running "
                    "— skipping install"
                )
                return
            elif installed_version == msi_version:
                log.info(
                    "Same version but not running — uninstalling "
                    "before reinstall"
                )
                self.client.uninstall_msi(uninstall_info.product_code)
                time.sleep(10)
            else:
                log.info(
                    "Installed version %s differs from base MSI %s "
                    "— uninstalling first",
                    installed_version, msi_version,
                )
                self.client.uninstall_msi(uninstall_info.product_code)
                time.sleep(10)
        elif uninstall_info.found:
            if service_running:
                log.info(
                    "Client running (no MSI version to compare) "
                    "— skipping install"
                )
                return
            log.info(
                "Client installed but not running (no MSI version) "
                "— uninstalling"
            )
            self.client.uninstall_msi(uninstall_info.product_code)
            time.sleep(10)
        else:
            log.info("No existing installation found")

        # Step 4: Full install flow
        log.info("Installing base client")

        # Get download link from email invite (also sends the invite)
        download_link = ""
        installer_name = None
        if invite_email:
            download_link = self._fetch_download_link_from_gmail(
                invite_email
            )
            installer_name = self._get_installer_name(download_link)
            if installer_name:
                print(f"Installer name: {installer_name}")

        # Resolve base installer and copy to tenant-specific name
        installer = resolve_installer(base_filename, installer_name)
        if installer_name and installer:
            self._cloned_installer = installer

        if not installer and from_version:
            log.info(
                "No local installer — downloading build "
                "(requires nsclient)"
            )
            self._init_nsclient()
            build_version = (
                f"release-{from_version}"
                if not from_version.startswith("release-")
                else from_version
            )
            info = self.client.download_build(
                build_version=build_version,
                installer_filename=base_filename,
            )
            installer = Path(info["location"])

        if not installer:
            raise FileNotFoundError(
                f"No installer found in {BASE_VERSION_DIR} and "
                "--from-version not provided"
            )

        # Wait for the tenant to register the token before installing
        if installer_name:
            log.info("Waiting 15s for tenant to accept the token")
            time.sleep(15)

        # Install with msiexec (retry on 1603 with next email)
        self._install_msi_with_email_retry(
            installer, base_filename, download_link,
            invite_email=invite_email or "",
        )

        # Wait for service to start
        if not self.client.wait_for_service():
            raise RuntimeError(
                "Client service (stAgentSvc) did not start "
                "after installation"
            )

    def cleanup(self) -> None:
        """Close Gmail browser and delete cloned installer."""
        self._close_gmail_browser()
        if self._cloned_installer and self._cloned_installer.is_file():
            try:
                self._cloned_installer.unlink()
                log.info(
                    "Cleanup: deleted cloned installer %s",
                    self._cloned_installer.name,
                )
            except Exception as exc:
                log.warning(
                    "Failed to delete cloned installer: %s", exc,
                )
            self._cloned_installer = None

    # ── Gmail Email Flow ─────────────────────────────────────────────

    def _fetch_download_link_from_gmail(
        self, invite_email: str,
    ) -> str:
        """
        Send an email invite and auto-extract the download link from Gmail.

        Uses ``is:unread`` filtering — previously opened emails are
        automatically skipped since Gmail marks them as read when
        viewed.

        Returns the URL on success, or an empty string on any failure
        (caller falls back to the manual input prompt).
        """
        from util_email import GmailBrowser

        profile_dir = resolve_email_profile(
            invite_email,
            self._email_profiles,
            self._save_config_fn,
        )
        self._gmail_browser = GmailBrowser(
            email_address=invite_email,
            is_64_bit=self.source_64_bit,
            tenant_hostname=self.webui.hostname,
            stop_event=self.stop_event,
            profile_dir=profile_dir,
        )
        self._gmail_browser.connect()

        # Phase 1: Clear old unread emails and capture baseline
        try:
            self._gmail_browser.mark_all_as_read()
        except Exception:
            log.warning(
                "mark_all_as_read failed — continuing with "
                "baseline count",
                exc_info=True,
            )
        baseline = self._gmail_browser.count_unread_emails()

        log.info("Sending email invite to %s", invite_email)
        self.webui.send_email_invite(invite_email)

        # Phase 2: Poll until new unread email arrives (cheap)
        if not self._gmail_browser.wait_for_new_unread(
            baseline=baseline, timeout=30,
        ):
            log.warning(
                "Polling did not detect new email "
                "— proceeding to search (may be threaded)"
            )

        # Phase 3: Open the newest email and extract link
        url = self._gmail_browser.get_download_link(
            timeout=60, max_rows=1,
        )
        log.info("Auto-extracted download link: %s", url)
        return url

    def _close_gmail_browser(self) -> None:
        """Close the Gmail browser session if one is open."""
        if self._gmail_browser is not None:
            try:
                self._gmail_browser.close()
            except Exception:
                pass
            self._gmail_browser = None

    # ── MSI Install with Retry ───────────────────────────────────────

    def _install_msi_with_email_retry(
        self,
        installer: Path,
        base_filename: str,
        initial_url: str,
        invite_email: str = "",
    ) -> None:
        """
        Install via msiexec.  On exit code 1603 (wrong email token),
        re-send the invite to get a fresh token and retry.

        Uses ``is:unread`` filtering — the first email was already
        opened (and marked as read in Gmail), so the next
        ``get_download_link`` call only returns newer unread emails.

        :param installer: Path to the MSI to install.
        :param base_filename: Base installer name for re-resolving.
        :param initial_url: Download URL used for *installer* (may be
            empty if no email flow was used).
        :param invite_email: Email address for re-sending the invite
            on 1603 retry.
        """
        try:
            self.client.install_msi(str(installer), log_dir=self.log_dir)
            return
        except RuntimeError as exc:
            if "exit code 1603" not in str(exc):
                raise
            if self._gmail_browser is None or not invite_email:
                raise
            log.warning(
                "Install failed (1603) — wrong email token, "
                "will re-send invite and retry",
            )

        # Clean up the bad cloned installer
        if self._cloned_installer and self._cloned_installer.is_file():
            self._cloned_installer.unlink()
            self._cloned_installer = None

        # The old email is already read (we opened it to extract
        # the link).  Capture baseline, re-send invite, then poll
        # until the fresh email arrives.
        baseline = self._gmail_browser.count_unread_emails()
        log.info("Re-sending email invite to %s", invite_email)
        self.webui.send_email_invite(invite_email)

        if not self._gmail_browser.wait_for_new_unread(
            baseline=baseline, timeout=60,
        ):
            raise RuntimeError(
                "Install failed (1603) — no fresh unread email "
                "arrived within 60s after re-send"
            )

        url = self._gmail_browser.get_download_link(
            timeout=10, max_rows=1,
        )

        log.info("Found fresh download link: %s", url)
        name = self._get_installer_name(url)
        if not name:
            raise RuntimeError(
                "Install failed (1603) — could not compose "
                "installer name from fresh link"
            )

        new_installer = resolve_installer(base_filename, name)
        if not new_installer:
            raise RuntimeError(
                "Install failed (1603) — could not resolve "
                "installer from fresh link"
            )
        self._cloned_installer = new_installer

        # Wait for the tenant to register the new token
        log.info("Waiting 15s for tenant to accept the token")
        time.sleep(15)

        self.client.install_msi(
            str(new_installer), log_dir=self.log_dir,
        )

    # ── Installer Name Resolution ────────────────────────────────────

    def _get_installer_name(
        self, download_link: str,
    ) -> Optional[str]:
        """
        Compose the tenant-specific installer name from installer.json
        and the download token extracted from the email invite link.

        The JSON maps tenant hostnames to an installer name prefix.
        The full filename is: {prefix}_{token}_.msi

        :param download_link: Full download URL from the email invite.
        :return: Full installer filename, or None if no config for tenant.
        """
        if not INSTALLER_JSON.is_file():
            log.info(
                "No installer.json found — using base installer name"
            )
            return None

        try:
            token = extract_token_from_url(download_link)
            data = json.loads(
                INSTALLER_JSON.read_text(encoding="utf-8")
            )
            tenant = self.webui.hostname
            entry = data.get(tenant)
            if entry and "installer_name" in entry:
                prefix = entry["installer_name"]
                name = f"{prefix}_{token}_.msi"
                log.info("Composed installer name: %s", name)
                return name
            log.info(
                "No installer config for tenant %s in installer.json",
                tenant,
            )
            return None
        except Exception as exc:
            log.warning("Failed to read installer.json: %s", exc)
            return None


# ── Module-level helpers ─────────────────────────────────────────────


def find_base_installer(base_filename: str) -> Optional[Path]:
    """
    Find the base installer file in data/base_version/ without
    renaming or copying anything.

    :param base_filename: Expected installer name (e.g. 'STAgent.msi').
    :return: Path to the installer, or None if not found.
    """
    if not BASE_VERSION_DIR.is_dir():
        return None
    base = BASE_VERSION_DIR / base_filename
    if base.is_file():
        return base
    # Single-file fallback
    files = [f for f in BASE_VERSION_DIR.iterdir() if f.is_file()]
    if len(files) == 1:
        return files[0]
    return None


def extract_token_from_url(download_link: str) -> str:
    """
    Extract the download token from an email invite download link.

    Example URL:
      https://download-exploratory2.stg.boomskope.com/dlr/win/QO848Vt80sc...
    Returns:
      'QO848Vt80sc...'

    :param download_link: Full download URL from the email invite.
    :return: Token string (last path segment).
    :raises ValueError: If the URL has no extractable token.
    """
    path = urlparse(download_link.strip()).path.rstrip("/")
    token = path.rsplit("/", 1)[-1] if "/" in path else ""
    if not token:
        raise ValueError(
            f"Could not extract token from download link: "
            f"{download_link}"
        )
    return token


def resolve_installer(
    base_filename: str,
    installer_name: Optional[str] = None,
) -> Optional[Path]:
    """
    Resolve the installer file from data/base_version/.

    If installer_name is provided (from installer.json), copies the
    base installer to that name. Otherwise falls back to finding the
    base installer directly.

    :param base_filename: Base installer name (e.g. 'STAgent.msi').
    :param installer_name: Tenant-specific installer name, or None.
    :return: Path to the installer ready for msiexec, or None.
    """
    if not BASE_VERSION_DIR.is_dir():
        return None

    # Find the base installer file
    base = BASE_VERSION_DIR / base_filename
    if not base.is_file():
        files = [f for f in BASE_VERSION_DIR.iterdir() if f.is_file()]
        if len(files) == 1:
            source = files[0]
            log.info("Copying %s -> %s", source.name, base_filename)
            shutil.copy2(str(source), str(base))
        else:
            return None

    # If tenant-specific name given, copy base to that name
    if installer_name:
        target = BASE_VERSION_DIR / installer_name
        log.info(
            "Copying base installer %s -> %s",
            base_filename, installer_name,
        )
        shutil.copy2(str(base), str(target))
        return target

    return base
