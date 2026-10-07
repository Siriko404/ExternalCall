# Operating facts

Exactly one file goes up: the generated input ZIP. Exactly one file comes back:
the expected outputs ZIP. The operator controls Go, Attach, Send and download;
a call finishes itself when its outputs ZIP lands in the downloads folder.
No response-page scraping or automatic execution of returned code.

Use `externalcall` (Windows `externalcall.cmd`) from the checkout. New jobs use
`brief --job`, then `prepare --job`; legacy `prepare --spec` is compatibility only.
Read commands: list, active, show --exchange, defects --exchange. Mutations:
done, stop, clone, validate, repair, delete, with explicit exchange IDs.
Name an exchange explicitly whenever several calls or past results exist.

- Finish: automatic once the outputs ZIP lands in the downloads folder; manual
  recovery uses `done --exchange <id> --downloads-dir <dir>` when requested.
- Recheck: `validate --exchange <id>` on an inactive exchange with response files.
- Stop: `stop --exchange <id>` preserves evidence.
- Resend: `clone --exchange <id>` prepares a new call; it never sends it.
- Correct packaging: inspect defects, then `repair --exchange <id> --tab <tab>`
  or the panel's correction control. Preserve valid bytes and rejected originals.
- Delete: `delete --exchange <id>` only when deletion is requested; `--force`
  deliberately stops an active call before removing its evidence.

Use wait --exchange with an optional `--timeout-seconds`; do not write another
shell poller. Report STILL_WAITING honestly.
Events: COMPLETE, INCOMPLETE, STOPPED, DELETED, REPAIR_OPENED,
DOWNLOAD_FILING_FAILED, STILL_WAITING. An event is not permission for action.

Finishing checks bytes and frozen schema. response_status is the responder's claim;
semantic_acceptance requires reading the work against the settled job. Useful
PARTIAL/BLOCKED work is not a corrupt delivery. Defective metadata is reported
explicitly; original files remain available. One validator drives defects/repair.

Read the actual result against JOB.json. Report transport/schema validity,
responder completion and semantic acceptance separately. Do not expose private
response contents through the extension UI.

Setup: `python3 scripts/setup.py`; update: `python3 scripts/update.py` after all
active calls are finished/stopped. Never auto-rename old evidence. Resume asks
for provider/destination again after lost browser state; never use another
call's last thread as a fallback. Use `docs/MANUAL_FALLBACK.md` when extension
attachment or native messaging is unavailable. Returned code remains
inert unless the user authorized a separate implementation/test task.
