# Agent Lessons Learned

## 2026-04-22 — `nsdiag -f` fallback and protection mode

- If logs show `nsconfig.json ... falling back to nsdiag -f`, treat the run as **protection-mode context**.
- Do **not** emit or rely on `non-protection mode` classification for that same condition.
- In one pass, fix both:
  - the **symptom** (do not hard-fail this simulate pre-write path), and
  - the **root cause** (mode detection/classification).
- Before closing a fix, run an intent-completeness check:
  - Does behavior match logs?
  - Does messaging/classification match behavior?
  - Did we address all user requirements, not only unblock execution?
