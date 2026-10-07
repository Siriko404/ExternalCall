---
name: manage
description: Inspect, resume, stop, validate, repair or review an ExternalCall web exchange. Use when the user names an existing external call or asks for its status or results.
disable-model-invocation: true
---

# Manage ExternalCall

Read [operating reference](../../references/OPERATING_CORE.md) for command and
recovery details. Work in the checkout root; Windows uses `externalcall.cmd`.
Name an exchange explicitly whenever several calls or past results exist.

- Read: `list`, `active`, `show --exchange <id>`, `defects --exchange <id>`.
- Wait: `wait --exchange <id>`; use `--timeout-seconds` for a bounded wait.
  Report STILL_WAITING honestly; an event is not permission for another action.
- Finish: automatic once the outputs ZIP lands in the downloads folder; manual
  recovery uses `done --exchange <id> --downloads-dir <dir>` when requested.
- Recheck: `validate --exchange <id>` on an inactive exchange with response files.
- Stop: `stop --exchange <id>` preserves evidence.
- Resend: `clone --exchange <id>` prepares a new call; it never sends it.
- Correct packaging: inspect defects, then `repair --exchange <id> --tab <tab>`
  or the panel's correction control. Preserve valid bytes and rejected originals.
- Delete: `delete --exchange <id>` only when deletion is requested; `--force`
  deliberately stops an active call before removing its evidence.

Read the actual result against JOB.json. Report transport/schema validity,
responder completion and semantic acceptance separately. PARTIAL/BLOCKED work
can arrive intact. A defective delivery retains useful bytes for inspection.
Do not execute returned code automatically or expose private response contents
through the extension UI. A user-authorized implementation/test task is separate.

After lost browser state, ask for the intended provider and new/current thread;
Resume does not know the old tab after a browser restart. Never use another
call's last thread as a fallback. Use [manual recovery](../../../../docs/MANUAL_FALLBACK.md)
when extension attachment or native messaging is unavailable.
