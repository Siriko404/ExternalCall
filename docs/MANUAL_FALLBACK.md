# Manual web exchange and recovery

Use the same generated job and archive when the extension is unavailable.
Prepare with `./externalcall brief --job job.json`, show the brief, then
`./externalcall prepare --job job.json`. Windows uses `externalcall.cmd`.

Open the chosen ChatGPT/Claude/Grok conversation yourself. Attach the recorded input
ZIP, paste the supplied launch instruction if needed, inspect and send. Download
one expected outputs archive. Do not rename another call's ZIP to force routing.

For an inactive prepared exchange:

```sh
./externalcall validate --exchange <id> --downloads-dir <downloads-directory>
```

For an active exchange, use `done --exchange <id> --downloads-dir <directory>`.
Use `show` and `defects` to inspect the same validator's report. Main JSON is lifted
from the ZIP without extracting or executing other members. Preserve original
bytes, report unverified checks and review semantics separately.
