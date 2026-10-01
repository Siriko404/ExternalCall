# ExternalCall skills

Two explicit flows: `/externalcall:prep` creates a settled job and frozen package;
`/externalcall:manage` operates and reviews exchanges. Install with
`python3 scripts/setup.py`; register only the skills with
`python3 scripts/install_skill.py`. Setup/update are deterministic scripts.

The bodies load focused references as needed. Job and response contracts are
implemented in the companion; prose does not define a second validator.
