# ExternalCall skill

One explicit command: `/excall:prep` creates a settled job and frozen package.
Operating and reviewing exchanges afterwards needs no command; the agent follows
`excall/references/OPERATING_CORE.md`. Install with
`python3 scripts/setup.py`; register only the skill with
`python3 scripts/install_skill.py`. Setup/update are deterministic scripts.

The body loads its focused reference as needed. Job and response contracts are
implemented in the companion; prose does not define a second validator.
