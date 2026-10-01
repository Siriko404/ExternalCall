# ExternalCall live browser UAT

Record browser/version, provider, commit and actual observation for each case.
These cases have not been executed by the automated runner.

- [ ] ChatGPT new conversation: Go, site Attach, exact input ZIP, generated launch
  instruction, operator Send, one outputs ZIP, Done.
- [ ] Claude new conversation: repeat the same flow using that provider explicitly.
- [ ] Current conversation: explicit provider/current-thread selection, no unexpected
  new tab or automatic message insertion before user attachment.
- [ ] Two simultaneous calls: reverse download order; each return belongs to its
  generated attempt. Repeated Done retrieves that call's report.
- [ ] Attach/typing failure: error or copy-prompt fallback becomes visible after the
  asynchronous operation ends; Send remains an operator action.
- [ ] Honest PARTIAL/BLOCKED: valid delivery, incomplete work, semantic review pending.
- [ ] Invalid schema/hash/CRC and duplicate paths: explicit defects, bytes preserved.
- [ ] Correction: fresh attempt/package, explicit Attach and Send in the chosen
  original-provider conversation; late old ZIP cannot satisfy the correction.
- [ ] Worker restart: durable call remains discoverable; stale callbacks cannot
  recreate dropped handoffs.
- [ ] Browser restart: Resume asks provider/destination; no guessed previous tab.
- [ ] Wrong-provider or navigated-away repair tab: refusal with Resume instruction.
- [ ] Manual fallback: download one ZIP and validate through the explicit CLI route.

For a local fixture use tests/mock_chatgpt/index.html after running its
build_fixtures.py and serving the directory. This page tests manual file input
and ZIP downloads; it is not evidence of ChatGPT/Claude integration success.
