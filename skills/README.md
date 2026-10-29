# Agent skills

Skills that teach a coding agent to operate OpenLakeForge through `olf`. Each
directory is one skill in the open [Agent Skills](https://agentskills.io)
format: a `SKILL.md` with `name` and `description` frontmatter, plus optional
`references/` the agent reads only when the skill points to them. Any agent
that loads that format can use them; nothing here is specific to one vendor.

| Skill | Use it to |
| --- | --- |
| [`deploying-openlakeforge`](deploying-openlakeforge/SKILL.md) | Install `olf`, create a project, deploy, inspect, troubleshoot and tear down a stack |

## Install

With the [`skills`](https://github.com/vercel-labs/skills) installer, which
finds this directory and asks which agents to install for:

```bash
npx skills add OpenLakeForge/openlakeforge
```

Or copy a skill directory, whole, to where your agent loads skills, for
example `.claude/skills/` in a project for Claude Code. A skill is
self-contained: its links stay inside its own directory.

The skills describe the `olf` of the commit they come from. Install them from
the release tag that matches your installed `olf version`.

## Writing a skill

The audience is an agent working in a user's lakehouse project, not a
contributor to this repository; contributor rules stay in
[AGENTS.md](../AGENTS.md).

- Keep `SKILL.md` short: the decisions an agent cannot make from `--help`, the
  order commands run in, and what needs a person's approval. Move long tables
  into `references/`.
- Write the `description` as when to use the skill, naming the words a user
  would type: agents choose a skill from that field alone.
- Show only `olf` commands that exist. `olf check skills` resolves every `olf`
  line in a fenced block, and every inline code span starting with `olf `,
  against the real command tree and its options, checks the frontmatter
  against the Agent Skills specification, and rejects a link that leaves the
  skill directory. A shell operator or an unquoted `<placeholder>` ends the
  checked part of a command.
