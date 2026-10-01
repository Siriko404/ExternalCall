# ExternalCall rewrite review

The active product is a bounded job-to-browser-to-local-results workflow for
ChatGPT Web and Claude Web. ProdLine and FinProdLine are removed from this
release; their source remains in Git history.

## Changes

- Two entry skills: prep and manage. A short brief precedes each package;
  questions address unresolved scope or acceptance decisions.
- One job definition generates the frozen evidence inventory, request, response
  schema, prompt, attempt names and packaging helper.
- One validator serves Done, standalone validation, defect reporting and repair.
  JSON Schema validation and bounded full-archive integrity checks replace
  permissive metadata and basename matching.
- Completion is scoped to an exchange and remains retryable. Expensive checks
  run outside the global state lock, with binding checks before committing.
- Version 2 correction creates a fresh attempt while preserving the job revision.
  Legacy correction preserves superseded files within the original exchange.
- Per-call extension storage rejects callbacks from retired handoff generations.
- Setup uses a local virtual environment and the ExternalCall host/plugin names.

## Boundaries

The operator still clicks Go, website Attach, Send, download and Done. No API
execution, response-page scraping or automatic execution of returned code is
introduced. A valid package does not prove the responder's reasoning is correct:
semantic acceptance remains explicitly pending review.

Existing exchanges use an explicit legacy validation path; original evidence is
not renamed. The GitHub repository URL and Go module name remain unchanged.
Finish or stop active calls before changing the installed launcher.

## Verification

Local Python and extension tests cover generated jobs, task-schema enforcement,
partial results, archive corruption and ambiguous paths, wrong-call retries,
fresh repair identities, concurrent Stop/Done, crash recovery, stale handoffs
and manual repair. Fixture and manual-fallback end-to-end checks pass.

The complete runner also requires Go tests, formatting and a native build.
These require CI because Go is absent in the local execution environment. The
Ubuntu/Windows workflow is included; its actual result must be checked before
merge. Live browser UAT remains unexecuted and is listed separately in
`tests/e2e/UAT_CHECKLIST.md`. This branch has not been installed on the operator's
computer.
