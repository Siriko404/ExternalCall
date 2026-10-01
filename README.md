# ExternalCall

Prepare a clear, bounded job for ChatGPT Web or Claude Web, hand over one frozen
ZIP, and collect one returned ZIP. The terminal agent discusses the job with you;
software generates names, prompt, schemas and package inventory.

The operator chooses the website and conversation, clicks Go, Attach, Send,
download and Done. The extension never sends a message or reads the response page.
API execution and production-line orchestration are outside this release.

## Install

Windows and Linux; Python 3.10+, Go 1.24+ for the launcher build, Node for tests,
and Chromium-family browser 125+. Runtime is local and uses no paid APIs.

```sh
python3 scripts/setup.py
```

Use `--dry-run` to inspect the installation plan or `--no-browser` for manual
extension loading. Setup installs the pinned Python validator, registers the
native host and Claude plugin, and guides unpacked-extension loading. Restart
Claude Code after registration. Other skill hosts need their own explicit
invocation configuration; Claude's invocation flag is not a portable guarantee.

## Use

- `/externalcall:prep`: clarify purpose, scope and acceptance; show a short brief;
  prepare a frozen package from one job definition.
- `/externalcall:manage`: status, results, resume, stop, correction and recovery.

A precise instruction needs no repeated interview or ceremonial approval. Missing
material decisions must be settled. Every package gets a short job brief.

The side panel chooses ChatGPT/Claude and new/current conversation per call.
It attaches only when you click the site's Attach control. Review before Send.
After downloading the expected outputs ZIP, click Done. Integrity, schema,
responder completion and semantic acceptance are distinct facts.

See [contract](WEB_CALL_PROTOCOL.md), [manual recovery](docs/MANUAL_FALLBACK.md)
and [example job](examples/repository_audit.job.json). The legacy `gptwebcall`
launchers and `prepare --spec` remain compatibility entry points for old exchanges.

## Upgrade

Stop or finish active calls before changing the native-host installation. Keep the
old call/state folders as evidence. The new host is `com.sina.externalcall` and
plugin is `externalcall@externalcall-local`; setup disables the old plugin entry.
Historical requests and responses are not renamed. The GitHub repository URL
remains unchanged until a separate repository rename is requested.

## Verify

```sh
python3 tests/e2e/run_all.py
```

The runner requires Python, Go and Node/npm and fails visibly if a required tool
is missing. CI runs the automated matrix. Live website UAT is documented in
`tests/e2e/UAT_CHECKLIST.md`; automated tests do not establish that UAT passed.
