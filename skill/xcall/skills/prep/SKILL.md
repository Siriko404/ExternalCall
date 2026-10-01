---
name: prep
description: Keyword xcall. Clarify and prepare one bounded external AI job for ChatGPT Web or Claude Web, with explicit scope, deliverables and acceptance criteria. Use when the user asks to send work to an external web model.
disable-model-invocation: true
---

# Prepare an ExternalCall

Work in the checkout containing `externalcall` / `externalcall.cmd`. Read the
focused operating reference only for lifecycle commands or recovery.

1. Inspect relevant supplied files and repository facts. Carry forward settled
   user decisions. Do not ask for facts you can determine yourself.
2. Reflect purpose and intended use, task, scope in/out and constraints briefly.
   Preserve facts, hypotheses and assumptions separately. A research question
   can remain unanswered; a material scope choice cannot be silently assumed.
3. Ask only consequential missing/conflicting decisions, prerequisites first.
   Recommend an answer and explain its tradeoff. Prefer one question per turn.
   Do not re-ask settled choices or add mandatory approval ceremony.
4. Define logical deliverables and requirement IDs. For each requirement define
   an observable acceptance condition, verification method, evidence and reviewer.
   A machine can check shape/bytes; semantic quality needs terminal/operator review.
5. Author one job JSON following `examples/repository_audit.job.json` and
   `companion.jobs.JOB_SCHEMA`. Do not separately author prompts, response schemas,
   routing IDs or output ZIP names. Select sources deliberately by evidence role;
   include context needed to answer the job, without dumping unrelated files.
6. Run `./externalcall brief --job <job.json>` and show its short job_brief.
   Do this for every package. If already authorized and clear, continue without
   another approval. Fix reported contract errors before preparation.
7. Run `./externalcall prepare --job <job.json>` (Windows: `externalcall.cmd`).
   Report the exchange ID and input archive path. One file comes back: the
   generated outputs ZIP containing main JSON and agreed artifacts.
8. Explain the next operator action: choose ChatGPT/Claude and **In a new tab** or
   **In the conversation I am in** per call; Go, site Attach, inspect, Send,
   download the outputs ZIP, Done. Never send, download or press Done for them.

For status/results use `/xcall:manage`. For setup use `scripts/setup.py`;
for update use `scripts/update.py`. Never upload internal calls/state storage or
recognized credentials. Export an explicit sanitized source for approved reuse.
Do not claim tests, complete work or semantic acceptance from schema/hash success.
