# Test Plan: Auto-Upgrade Resilience (With/Without Reboot) – NSC Windows

## 1. Objective

Validate that Netskope Client auto-upgrade:

1. Never leaves the endpoint without a functional client (old or new).
2. Correctly uses the installation monitor / watchdog to:
   - Recover from upgrade interruptions (forced shutdown/reboot).
   - Recover from monitor process kills.
3. Correctly upgrades the watchdog / monitor service and its binary path so that:
   - `stAgentSvcMon` always starts successfully.
   - Upgrade can resume or roll back after reboot.

This plan is focused on the regression space of ENG‑949874 / ENG‑951409:
- Incorrect monitor service binpath.
- No recovery when upgrade is interrupted.
- Windows Installer error 1603 during driver/service install.

## 2. Scope

- **Products / Components**
  - Netskope Client (NSC) Windows
  - NSC installer/auto-upgrade (MSI)
  - Services:
    - `stAgentSvc` (main agent service)
    - `stAgentSvcMon` / watchdog / installation monitor
  - Drivers installed by NSC

- **Upgrade Paths**
  - **From:** 135.0.0.2631 (R135.0) or an equivalent pre-fix build.
  - **To:** 135.1.x / 136.x / 137.x **with ENG‑951409 fix**.
  - Include at least:
    - 135.0.0 → 135.1.10 (EHF/fixed 135.1 build)
    - 135.0.0 → 136.0.0
    - 135.0.0 → 137.0.0

- **OS Versions**
  - Windows 11 (primary focus)
  - Windows 10 (supported and common)
  - (Optional separate section) Windows 8.1 for informational coverage, clearly marked as unsupported.

## 3. Test Environment & Prerequisites

1. **Lab Setup**
   - Clean Windows VMs with snapshots:
     - Win 11
     - Win 10
   - Local admin access.

2. **Software/Config**
   - Install **old NSC version** (e.g., 135.0.0.2631).
   - Configure **auto-upgrade** to target **fixed build**:
     - Ensure client fetches upgrade and uses
       ```bash
       msiexec /l*v+ "<path>\STAUpdate-<machine>.txt" /i "<path>\STAgent.msi" /qn
       ```
   - Ensure **watchdog / monitor feature flag** is:
     - Tested **enabled** and **disabled** where applicable.
   - Ability to run:
     - `shutdown /r /f /t 0`
     - `taskkill /F /IM stagentsvcmon.exe` (or service stop via `sc stop stAgentSvcMon`).

3. **Instrumentation**
   - Collect:
     - `STAUpdate-*.txt` (MSI logs)
     - `nsdebuglog.txt`
     - `nsInstallation.log`
     - Windows Event Viewer: System + Application logs
   - Command line tools:
     - `sc query stAgentSvc`
     - `sc query stAgentSvcMon`
     - `sc qc stAgentSvcMon` (to check binary path)
     - `tasklist | findstr stagent`
   - Ability to snapshot/restore VMs between tests.

## 4. General Verification Checklist (for Each Test)

After each scenario, always verify:

1. **Service Status**
   - `stAgentSvc` exists and is **Running**.
   - `stAgentSvcMon` / watchdog service exists and is **Running**.
   - `sc qc stAgentSvcMon` shows **correct binary path**.

2. **Client State**
   - NSC UI is present and running (`stAgentUI.exe`/equivalent).
   - Client is visible in **Programs & Features / Apps** with:
     - Either old version (rollback) or new version (upgrade complete).
   - Device is **protected** (policy applied, traffic intercepted).

3. **Logs**
   - No final MSI status `1603` without recovery.
   - No repeated errors:
     - `stAgentSvcMon service failed to start ... The system cannot find the file specified.`
     - `Config failed to start Installation Monitor Service for auto-upgrade but continue the installtion`
   - If failures occur during upgrade:
     - Confirm **rollback or resume** path is executed.
     - Confirm old or new version ends up fully installed.

---

## 5. Test Scenarios

### 5.1 Baseline: Normal Auto-Upgrade (No Interruptions)

**Goal:** Ensure fixed build behaves correctly with no interruptions (sanity check).

**Steps:**
1. Install **135.0.0.2631** on clean VM.
2. Confirm:
   - `stAgentSvc` and `stAgentSvcMon` running.
3. Trigger auto-upgrade to target fixed build (e.g., 135.1.10) and let it **complete naturally**:
   - Do not reboot or kill processes.
4. After upgrade completes and system stabilizes (3–5 minutes):

**Expected:**
- MSI log shows successful upgrade, no terminal error 1603.
- New version installed and running.
- Monitor/watchdog service:
  - Present and running.
  - Binary path is correct.
- No loss of protection at any point.

---

### 5.2 Forced Reboot During Old Service/Driver Removal

**Reference from `init.txt`:**  
> Inject force shutdown via "shutdown /r /f /t 0"?

**Goal:** Simulate worst-case: reboot while old client is being uninstalled (previously left machine without NSC).

**Steps:**
1. Install **135.0.0.2631**.
2. Enable logs collection.
3. Trigger auto-upgrade.
4. **Carefully time** the reboot:
   - Monitor `STAUpdate-*.txt` in real-time or use Process Monitor.
   - When log shows actions like:
     - `RemoveExistingProducts action executed`
     - `stAgentSvc` stop pending / old driver removal (from nsInstallation/logs),
   - Immediately run:
     ```bash
     shutdown /r /f /t 0
     ```
5. Allow machine to reboot.
6. After reboot:
   - Wait for 5–10 minutes for any resume logic.
   - Check:
     - Services (`stAgentSvc`, `stAgentSvcMon`).
     - Installed NSC version.
     - Logs for monitor/rollback activity.

**Expected:**
- Post-reboot, **at least one working NSC version** is present:
  - Either:
    - Upgrade resumes and completes (new version running), or
    - Rollback restores old version and it is running.
- `stAgentSvcMon` starts automatically; no “file not found” errors.
- MSI log shows:
  - Either resumed install to success, or successful rollback.
- No long-term state where both old and new are missing.

---

### 5.3 Forced Reboot During New Service/Driver Installation

**Goal:** Validate resilience when reboot occurs while installing the **new** NSC version.

**Steps:**
1. Install **135.0.0.2631**.
2. Trigger auto-upgrade.
3. Wait until old version is fully removed (check logs for completion of `RemoveExistingProducts`).
4. During **driver or service installation** of the new version, run:
   ```bash
   shutdown /r /f /t 0