# ExternalCall contract

System root: the directory containing this file. The skills are the operating
surface; this file defines the contract and recovery facts.

## Job preparation

`/xcall:prep` establishes purpose, task, scope in/out, constraints,
requirements, acceptance evidence/reviewer, deliverables, approved sources and
stopping conditions. Inspect discoverable facts before asking questions. Ask
only consequential missing decisions in dependency order. Preserve hypotheses,
assumptions and verified facts separately. Research unknowns can be the assignment;
unresolved scope decisions prevent preparation.

Show the brief for every package. An already clear authorized instruction does
not need another approval. Semantic judgment remains with the user and agent.

The agent authors one job JSON. `./externalcall brief --job job.json` checks it;
`./externalcall prepare --job job.json` compiles the request. Windows uses
`externalcall.cmd` with the same arguments. `examples/repository_audit.job.json`
shows the supported structure. Source paths are relative to the working directory
or absolute. The wrapper runs from the checkout root.

The compiler assigns attempt IDs and names, snapshots approved evidence and
creates JOB.json, WEB_REVIEW_REQUEST.json, WEB_RESPONSE_SCHEMA.json,
package_response.py and 000_READ_ME_FIRST.md. These are generated projections.
The job revision binds evidence hashes and task schema. Private source paths stay
local. Internal calls/state/Git and recognized credential paths cannot be inputs;
export a sanitized approved artifact when an earlier response is needed.

## One zip up, one zip down

Exactly one file goes up: the frozen `_inputs.zip`. Exactly one file comes back:
the attempt's generated outputs ZIP containing the generated main JSON at its
root and agreed artifact paths. `expected_artifacts` is exactly one `.zip`.

The response has request_id, job_revision, status, results, criteria_report,
artifacts_manifest, limitations and delivery. Use the generated schema verbatim.
`delivery` contains only the outputs ZIP. Artifact entries cover each agreed
artifact once, with filename, CREATED/MISSING/NOT_CREATED, media_type, byte size
and SHA-256. An absent artifact has size 0 and null hash. Never put the main JSON
or containing ZIP in that manifest: their self-hashes would be circular.

Write artifacts first, then measure their actual bytes. The bundled trusted
`package_response.py --payload payload.json --artifacts outputs --status COMPLETE`
helper creates the envelope and archive without external dependencies. Use
PARTIAL/BLOCKED honestly for unavailable work, with limitations and criterion
reports. The local companion never executes returned code automatically.

Paths are canonical portable relative paths, preserving directories. New jobs
inspect nested ZIPs as artifact bytes; they do not search recursively by basename.
Legacy exchanges retain bounded recursive inspection with unambiguous aliases.

## Validation and acceptance

One validator supplies Done, validate, defects and repair. It checks all ZIP
member CRCs, archive budgets, normalized-path uniqueness, frozen task schema,
identity, artifact sizes/hashes and output inventory. All schema references must
be document-local; no network retrieval. Draft 2020-12 and standard formats are
validated with the pinned jsonschema dependency.

Limits: 512 MiB compressed archive/per member; 1 GiB total expanded bytes;
4096 members; 30 seconds streaming inspection; 2 MiB main JSON. Legacy nested
inspection additionally limits each nested archive to 64 MiB and depth to three.
Violations retain bytes and report defects. No malformed-metadata fallback is
silently promoted to a verified delivery.

Delivery COMPLETE means mechanical checks passed. response_status and
work_complete describe the responder's completion claim. A valid PARTIAL or
BLOCKED can be delivered correctly. semantic_acceptance remains NOT_REVIEWED
until the terminal/operator reviews the actual result against the job. Passing
hashes and schemas does not prove the answer is correct.

## Parallel calls and destinations

Several calls may be active at once. Generated output names are the routing key;
Chrome does not report an originating tab for every download. The destination is
per call: choose ChatGPT/Claude and either **In a new tab** or **In the conversation
I am in**. Existing-thread context can affect the answer; choose intentionally.

Go verifies frozen inputs before activating. The extension never presses Send,
automatically downloads, or reads ChatGPT's response page. Done and Stop name an
exchange explicitly. Repeated Done retrieves that exchange's own report.

Resume requires an explicit destination choice after lost browser state. A browser
restart destroys tab identity; recovery must not guess or borrow another call's
tab. Repair retains provider identity and a call-specific conversation binding.

## Commands and recovery

| Command | Purpose |
| --- | --- |
| brief --job | Check and display the settled job |
| prepare --job | Generate the frozen package |
| list / active / show --exchange | Inspect prepared/running calls |
| done --exchange | Ingest expected downloads and validate |
| stop --exchange | Stop monitoring without deleting evidence |
| clone --exchange | Prepare another attempt from frozen evidence |
| validate --exchange | Recheck an inactive exchange |
| defects --exchange | Show the same validator's repair findings |
| repair --exchange --tab | Open a mechanical correction |
| wait --exchange | Wait for a lifecycle event |
| delete --exchange | Explicit deletion; retain evidence unless requested |

`prepare --spec` is an explicit legacy compatibility path, not the authoring path
for new jobs. Legacy exchange identifiers and filenames remain readable.

Wait events are COMPLETE, INCOMPLETE, STOPPED, DELETED, REPAIR_OPENED,
DOWNLOAD_FILING_FAILED and STILL_WAITING. An event is not permission to send,
repair or execute anything. Timeouts are not completion.

If delivery is defective, read the defects before repair. Preserve valid artifact
bytes, regenerate metadata/archive as needed, and retain rejected originals.
Semantic dissatisfaction requires reviewing scope/evidence or a new job revision.
The manual fallback remains available when the extension is unavailable; see
`docs/MANUAL_FALLBACK.md`. No shared pending-download pool is used.
