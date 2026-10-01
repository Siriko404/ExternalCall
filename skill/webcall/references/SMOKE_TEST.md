# Improvised end-to-end smoke test

Use only when `init` reaches its final step, or when `menu health smoke` is
explicitly asked for.

**Build it fresh, every time.** No repository fixture, no checked-in smoke
request, no prebuilt package. A canned probe passes on a machine where the live
path is broken, because the canned parts are the parts that were already known to
work. That is an owner requirement, not a preference.

## 1. Build a fresh probe

1. Generate a token: a GUID plus the current UTC timestamp. Work in a temporary
   directory outside `calls/` and `state/`.
2. Write `smoke_source.txt` containing the token and two randomly generated short
   key/value facts. Compute its exact byte size and SHA-256 locally, now.
3. Derive a request ID and a pass prefix from the token, e.g.
   `webcall-smoke-<short-token>-v1`. One file comes back, `<pass>_outputs.zip`,
   with `<pass>_response.json` inside it. Unique names are mandatory: filename
   is the only routing key.
4. Write a fresh `WEB_REVIEW_REQUEST.json` asking the responder to read the
   supplied source, report the token and both facts, reproduce the source's
   SHA-256, and create `smoke_receipt.txt` holding those values. Forbid outside
   research. Require honest `PARTIAL` or `BLOCKED` if it cannot comply.
5. Write a fresh `WEB_RESPONSE_SCHEMA.json` requiring `request_id`, `status`,
   the smoke results, `artifacts_manifest`, `limitations`, and `delivery`.
   Require a manifest entry for `smoke_receipt.txt` — never for the main JSON
   itself.

   **Pin every enum, especially the per-artifact `status`.** Left as
   `{"type": "string"}` it has twice come back as `"COMPLETE"`, borrowed from
   the top-level field, where the validator requires `CREATED`. Byte-perfect
   work then validates with `manifest_verified: false`, which reads as the
   validator failing when the schema was loose.
6. Write a fresh preparation spec: `input_files` = the request JSON, the schema
   JSON, and `smoke_source.txt`; `expected_main_json` = the unique response name;
   `expected_artifacts` = the unique outputs ZIP.
7. `prompt_text` must say the reply is exactly one downloadable `.zip`, name it,
   name the main JSON that goes inside it, put every other created file in there
   too, require sizes and digests computed from the bytes actually written, and
   forbid conversational text outside the download.

## 2. Prepare and inspect

```sh
<root>/gptwebcall prepare --spec <fresh-spec>   # Windows: & '<root>\gptwebcall.cmd' prepare --spec <fresh-spec>
<root>/gptwebcall show --exchange <id>
```

Before any browser action confirm: state `PREPARED`, request ID matches, the
expected filenames are the fresh ones, and `attach_files` is exactly one file —
the inputs ZIP, with the prompt inside it as `000_READ_ME_FIRST.md`. `show` is
the pre-send check — not `validate`.

## 3. Operator round trip

Ask the operator: side panel → click the call's row in the top list →
destination set to **In a new tab**, agent set to **GPT** (or **Claude** when
smoking the Claude path) → **Go** → the conversation's **Attach files** →
**Send** → download the one named archive → **Done and validate**. The extension
attaches and types the launch line; it never sends.

Watch the composer at Go. One line naming the archive and `000_READ_ME_FIRST.md`
should appear in it by itself. If the panel shows that line with a copy button
instead, typing it failed — paste it, and record that as a defect of this run
even if the rest passes. In the current-conversation pass below, nothing is
typed and nothing should appear: that thread already has the context, and the
line would be words put into the operator's own conversation.

Naming the destination matters even in the default case: the control persists
across browser restarts, so a panel left on *Send in the conversation I am in*
from earlier work will silently deliver the probe into whatever thread happens
to be focused.

### The current-conversation path

Run it a second time, with a fresh probe, when the smoke test is checking the
destination control rather than the pipeline — after any change to the
extension, and whenever the operator asks for it.

Build a new probe with a new token, open a conversation with the chosen agent
that already
has messages in it, focus that tab, set the destination to **Send in the
conversation I am in**, then Go. Pass conditions are the ones in §4, unchanged.
Two extra things are being watched: the call must land in that existing thread
rather than a new one, and the attachment must still reach the composer — a
rendered thread is where `Page.fileChooserOpened` has been seen arriving with
no node id, which is the path the fallback exists for.

The refusals are part of the test. With the destination on *current* and a
tab that is not a conversation with the chosen agent focused, Go must fail with a message naming the cause and must
not open a tab instead.

## 4. Pass or fail

Pass only if all of these hold:

- delivery `status` is `COMPLETE`;
- `response_status` is `COMPLETE` — for a probe this small, anything else is a
  failure to investigate, even though `PARTIAL` is a valid outcome in real work;
- `manifest_verified` is `true`;
- the response and `smoke_receipt.txt` carry the fresh token, both fresh facts,
  and the SHA-256 you computed locally;
- the outputs ZIP exists under its expected name and its declared member sizes
  and digests verify;
- the main JSON is in `response\` beside the archive, lifted out of it by the
  companion rather than downloaded.

If the archive reached the Downloads folder but never reached the exchange, the
smoke test has failed even if a later manual `done` or `validate` rescues it.
Record that, and say so plainly. That exact failure — files written by Chrome,
nothing filed, and `validate` clicked by hand to rescue it — happened three
times before the download handling was rebuilt around a single event, so it is
the thing this step is watching for.

## 5. What this test gives up

It exercises the live web conversation, Chrome attachment, native messaging, filename
routing, download collection, archive-member accounting, and validation in a
single path. In exchange it is slow, needs operator clicks, spends a live model
interaction, and can fail because the network or the model is unavailable while
the local code is sound.

Run the unit suites first so this is an integration test rather than the first
diagnostic. `README.md` records that `tests/e2e/` still encodes an older request
contract and is not a release gate.
