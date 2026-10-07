# ExternalCall live browser UAT

Record browser/version, provider, commit and actual observation for each case.
These cases have not been executed by the automated runner.

- [ ] ChatGPT new conversation: Go, site Attach, exact input ZIP, generated launch
  instruction, operator Send, one outputs ZIP. The call finishes itself: its row
  turns green in Running with nothing pressed.
- [ ] Claude new conversation: repeat the same flow using that provider explicitly.
- [ ] Grok new conversation: repeat the same flow; record whether the file chooser
  needed the generic input fallback.
- [ ] Toggles: the agent toggle retints the whole panel within a moment; Go sends
  to the agent and destination the toggles show when it is pressed.
- [ ] Current conversation: explicit provider/current-thread selection, no unexpected
  new tab or automatic message insertion before user attachment.
- [ ] Two simultaneous calls: reverse download order; each return belongs to its
  generated attempt and finishes only its own call.
- [ ] Names: the exchange folder, the input ZIP and the outputs ZIP all start with
  the job's project. A call prepared before projects still sends and finishes.
- [ ] Faulty ZIP: the row turns red in Running; Correct opens a fresh attempt.
- [ ] Finished calls stay in Running until moved; Move all to history moves every
  finished one and leaves running calls alone. History is folded on open.
- [ ] Folder watch: with the panel closed, or after a browser restart, an outputs
  ZIP already in the downloads folder finishes its call without Resume.
- [ ] A call whose ZIP has not landed is never finished automatically.
- [ ] Attach/typing failure: error or copy-prompt fallback becomes visible after the
  asynchronous operation ends; Send remains an operator action.
- [ ] Honest PARTIAL/BLOCKED: valid delivery, incomplete work, semantic review pending.
- [ ] Invalid schema/hash/CRC and duplicate paths: explicit defects, bytes preserved.
- [ ] Correction: fresh attempt/package, explicit Attach and Send in the chosen
  original-provider conversation; late old ZIP cannot satisfy the correction.
- [ ] Worker restart: durable call remains discoverable; stale callbacks cannot
  recreate dropped handoffs.
- [ ] Browser restart: a lost call offers Resume in a new tab and Resume in this
  chat; no guessed previous tab.
- [ ] Wrong-provider or navigated-away repair tab: refusal with Resume instruction.
- [ ] Manual fallback: download one ZIP and validate through the explicit CLI route.

For a local fixture use tests/mock_chatgpt/index.html after running its
build_fixtures.py and serving the directory. This page tests manual file input
and ZIP downloads; it is not evidence of ChatGPT/Claude integration success.
