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
   
   
### 5.3 Forced Reboot During New Service/Driver Installation
**Goal:** Validate resilience when reboot occurs while installing the **new** NSC version (after old version is already removed).
**Steps:**
1. Install **135.0.0.2631** (or other source build defined for this plan) on a clean VM.
2. Confirm:
   - `stAgentSvc` is running.
   - `stAgentSvcMon` (installation monitor / watchdog service) is present and running.
3. Start log collection:
   - Ensure `STAUpdate-<machine>.txt` MSI log is enabled (per your standard logging command).
   - Ensure NSC logs (e.g., `nsdebuglog.txt`, `nsInstallation.log`) are being collected.
4. Trigger auto-upgrade to the **fixed build** (e.g., 135.1.10 / 136.x / 137.x).
5. Monitor the MSI log (`STAUpdate-*.txt`) in real time. Wait until:
   - Old product removal has completed (look for completion of `RemoveExistingProducts` or similar).
   - New service/driver installation has started (look for entries like:
     - `InstallAgentService: Installing service stAgentSvc`
     - `InstallDriver` / `CA_InstallDriver` / driver-related custom action names.
6. **During the new install phase**, immediately trigger forced reboot:
   ```cmd
   shutdown /r /f /t 0
Allow the system to reboot and stabilize (wait at least 5–10 minutes after logon).

Post-reboot verification:

Check services:



sc query stAgentSvc
sc query stAgentSvcMon
sc qc stAgentSvcMon
Check installed NSC version in:

Programs & Features / Apps list.

Confirm the endpoint is protected:

NSC tray/UI present.

Policy applied and traffic intercepted (basic connectivity test).

Review logs (MSI + NSC) for:

Resume/repair actions.

Rollback, if triggered.

Any terminal errors.

Expected Result:

After reboot, the system converges to a working NSC state:

Either the new version installation completes successfully, or

The old version is restored via rollback and is functional.

stAgentSvcMon:

Exists and is in Running state.

sc qc shows a valid, correct binary path.

There is no state where both old and new NSC are missing.

MSI logs show either:

Successful completion of the upgrade after reboot, or

Clean rollback with success status.

No unrecoverable MSI error 1603 that leaves the host unprotected.

5.4 Kill stAgentSvcMon During Upgrade, Then Reboot
Goal: Validate that killing the installation monitor / watchdog during upgrade, then rebooting, does not reintroduce the RCA behavior (ENG‑949874 / ENG‑951409) and that the new custom actions and service configuration still guarantee recovery.

Steps:

Install 135.0.0.2631 on a clean VM.

Verify:

stAgentSvc is running.

stAgentSvcMon is present and running.

Start collecting logs (MSI + NSC logs as in 5.3).

Trigger auto-upgrade to the fixed build.

Once the upgrade has started (you see upgrade-related activity in logs, and/or CPU / installer activity):

Kill the monitor process:



taskkill /F /IM stagentsvcmon.exe
or stop the service:



sc stop stAgentSvcMon
Immediately after killing/stopping the monitor service, force reboot:



shutdown /r /f /t 0
After reboot, wait 5–10 minutes to allow any resume/repair actions to complete.

Post-reboot verification:

Check services:



sc query stAgentSvc
sc query stAgentSvcMon
sc qc stAgentSvcMon
Confirm:

stAgentSvcMon exists.

stAgentSvcMon is Running.

Binary path is valid and points to the correct monitor binary.

Check installed NSC version (old vs new).

Confirm the endpoint has a working, protected NSC client.

Review logs for:

Custom action to (re)create and start installation monitor.

Any “file not found” / SCM errors related to stAgentSvcMon.

Expected Result:

After reboot:

stAgentSvcMon is installed correctly and running with a valid binary path.

The NSC client is in a consistent, protected state (either old or new version).

No recurrence of errors such as:

stAgentSvcMon service failed to start due to the following error: The system cannot find the file specified.

Logs confirm that the new custom action for starting/maintaining the installation monitor executed successfully.

5.5 Repeated Upgrade Attempts with Forced Shutdown (Stress / Repeatability)
Goal: Systematically stress the auto-upgrade flow to verify repeatability and to catch intermittent issues when:

Upgrades run normally (no interference).

Upgrades are interrupted by forced reboot at specific timings.

Target: At least 10 runs per OS / version combination, split between normal and interrupted flows.

5.5.1 Repeated Normal Upgrades (No Interruptions)
Goal: Ensure the “happy path” remains stable and free of regressions.

Steps:

Prepare a VM snapshot with:

Old NSC version installed (e.g., 135.0.0.2631).

Auto-upgrade pointing to a fixed build.

For run i = 1..5:

Restore snapshot.

Start log collection.

Trigger auto-upgrade.

Let the upgrade complete with no reboots, no process kills.

After completion, verify:

New version is installed and functional.

stAgentSvc and stAgentSvcMon are Running.

sc qc stAgentSvcMon shows correct, valid binary path.

Record:

OS version.

Old → new version.

Any warnings/errors in logs.

Expected Result:

All 5 runs complete successfully with:

New NSC installed.

Monitor service healthy.

No MSI 1603 or related installer errors.

No intermittent failures or flakiness.

5.5.2 Repeated Interrupted Upgrades (With Forced Shutdown)
Goal: Verify consistent behavior when upgrades are interrupted at specific phases.

Steps:

Prepare a VM snapshot as in 5.5.1 with old NSC installed.

Define interruption patterns (shuffle between them across runs):

Pattern A: Reboot during old uninstall (see 5.2).

Pattern B: Reboot during new install (see 5.3).

For run i = 1..5:

Restore snapshot.

Start log collection.

Trigger auto-upgrade.

Apply the selected interruption pattern:

A: Reboot during old version removal.

B: Reboot during new service/driver install.

Allow machine to reboot and stabilize.

Verify:

Final NSC version (old or new).

stAgentSvc and stAgentSvcMon state.

Monitor service binary path (via sc qc).

Logs for upgrade/rollback behavior and monitor involvement.

Record:

OS, old → target version.

Interruption pattern (A/B).

Final state.

Key errors (if any).

Expected Result:

All 5 interrupted runs end in a protected and functional NSC state:

New version successfully installed, or

Old version successfully rolled back and running.

No run leaves the host without a working NSC.

Behavior across runs is consistent (no “sometimes broken, sometimes okay” behavior).

5.6 Watchdog Feature Flag Variants
Goal: Confirm correct behavior with different watchdog / monitor feature flag configurations so that:

Enabling watchdog does not break auto-upgrade.

Disabling watchdog still maintains resilience via the installer monitor.

Variants:
For selected core scenarios (e.g., 5.2, 5.3, 5.4):

Watchdog OFF (if configurable):

Run scenarios with watchdog disabled.

Focus on installer’s own installation monitor behavior.

Watchdog ON:

Run the same scenarios with watchdog feature flag enabled.

Observe interactions between watchdog and installer.

Steps (high-level):

For each chosen scenario:

Configure watchdog FF to OFF:

Run scenario, collect results.

Configure watchdog FF to ON:

Run the same scenario again, collect results.

For each run, check:

Final NSC state (protected/unprotected, old/new version).

Service stability:

No restart loops or flapping of stAgentSvc.

CPU / memory impact of watchdog.

Monitor service presence and binary path correctness.

Expected Result:

In both configurations (ON/OFF):

Device is not left unprotected after upgrade or interruption.

With watchdog ON:

Watchdog correctly restarts stAgentSvc if needed.

No interference with installer/rollback flows.

No new instability or resource issues.

6. Negative / Edge Tests
6.1 Intentional Driver Failure Simulation (If Test Harness Available)
Goal: Validate behavior when driver installation fails (e.g., custom action failure) and verify:

Correct rollback.

No reappearance of the “silent removal” issue.

Prerequisite: Internal test MSI or flags that can simulate driver install failure (e.g., forced failure in CA_InstallDriver).

Steps:

Install old NSC version on test VM.

Configure test build / flags to force driver installation to fail during upgrade.

Start log collection.

Trigger auto-upgrade with no reboot:

Observe behavior when driver fails.

Repeat with reboot timed at/around failure point:

Trigger failure.

Immediately:



shutdown /r /f /t 0
After each run:

Check installed NSC version.

Verify service states (stAgentSvc, stAgentSvcMon).

Verify host is protected.

Analyze MSI and NSC logs for:

Failure reason.

Rollback/repair actions.

Expected Result:

For driver failure cases:

System either:

Rolls back to old version completely and cleanly, or

Clearly fails but does not leave both old and new NSC absent.

Installation monitor / watchdog remains installed and working.

No new errors similar to ENG‑949874/ENG‑951409.

6.2 Unsupported OS Coverage (Windows 8.1 – Informational Only)
Goal: Document behavior on unsupported OS (e.g., Windows 8.1) without blocking release; ensure any known OS-level issues are well understood.

Steps:

Set up a Windows 8.1 VM with old NSC version.

Run a subset of scenarios:

5.1 Normal auto-upgrade.

5.2 Forced reboot during old uninstall.

5.3 Forced reboot during new install.

Collect:

MSI logs.

NSC logs.

Windows Event Logs (especially Service Control Manager).

Pay special attention to errors such as:

Service creation/start failures due to unsigned drivers or OS-level constraints.

SCM errors like 0x45b or related codes.

Expected Result:

Behavior is documented, with known OS limitations clearly noted.

If previous issues reproduce on Windows 8.1:

They are captured and explicitly flagged as on unsupported OS.

No changes required for supported OS behavior based on these results.

7. Acceptance Criteria
No “Silent Removal” in Any Scenario

Across all scenarios (5.1–5.6, 6.x), the endpoint is never left permanently without:

Either the old stable NSC version, or

The new target NSC version.

All interrupted upgrades converge to a protected state.

Installation Monitor / Watchdog Robustness

stAgentSvcMon:

Always installed with a valid binary path.

Starts successfully after install and after every reboot.

Recovers from being stopped/killed or interrupted by forced reboot.

No recurring errors like:

“The system cannot find the file specified” for stAgentSvcMon.

Upgrade & Rollback Correctness

For all normal upgrades:

New version installs successfully with no terminal 1603 or unrecoverable MSI error.

For all interrupted scenarios:

Either:

Upgrade resumes and completes, or

Rollback completes and old version is fully restored.

MSI logs provide clear evidence of final outcome (install or rollback).

Repeatability & Stability

Stress runs (Section 5.5) show:

100% success in preserving a functional NSC (old or new).

No intermittent, hard-to-reproduce states.

Watchdog FF variants do not introduce new instability.

No New Regressions

Normal-path auto-upgrades behave as well as or better than pre-fix builds.

No additional issues are introduced in:

Service management.

Boot-time behavior.

Performance / resource usage.