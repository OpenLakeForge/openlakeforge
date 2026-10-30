# CLAUDE.md

Read [AGENTS.md](AGENTS.md). It is the single source for how to work in this
repository: project context, orientation reading order, repository map,
architectural rules, coding standards, the gates that must pass, and guardrails.

This file exists only so Claude Code finds the guide by its conventional name.
The rules are deliberately not duplicated here — two copies drift apart, and a
stale copy is worse than none.

## Quick reference

```bash
uv run --project tools/olf --locked olf infra deploy --provider local   # bring up the local kind stack
uv run --project tools/olf --locked olf infra destroy --provider local  # bring down the local stack
uv run --project tools/olf --locked olf infra e2e run --env local        # full runtime verification
```

Before writing code: read `AGENTS.md`, then the issue you are working on in
full (`gh issue view <n>`) — issue bodies carry verified file and line
references that a summary will not.
