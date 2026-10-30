# Contributing

This page is the shortest path from a fresh clone to a pull request.
[AGENTS.md](/AGENTS.md) holds the architecture rules, test and docs
standards, and CI gates; this page links to it instead of repeating it.

## Prerequisites

- Git and a GitHub account.
- [uv](https://docs.astral.sh/uv/), which creates the virtual environment
  for `tools/olf`. The tooling needs Python 3.12 or newer.
- Linux or macOS. Windows is not supported.
- Docker, to deploy the platform locally with `olf deploy`. The commands
  under "Check your change" do not need it.

## Pick an issue

1. Open the issues labelled
   [good first issue](https://github.com/OpenLakeForge/openlakeforge/issues?q=is%3Aissue%20state%3Aopen%20label%3A%22good%20first%20issue%22).
2. Choose one with no assignee and no linked pull request.
3. Comment that you want to take it, with a one-line plan, and wait for a
   maintainer to reply before you start.

## Set up

1. Fork the repository and clone your fork.
2. Add the original repository as `upstream`:

```bash
   git remote add upstream https://github.com/OpenLakeForge/openlakeforge.git
```

3. Install the `olf` development environment from the repository root:

```bash
   uv sync --project tools/olf
```

## Check your change

Run these from the repository root.

```bash
uv run --project tools/olf ruff check tools/olf
uv run --project tools/olf pytest tools/olf/tests/test_checks_docs.py
```

Swap in the test file that covers the code you changed. Documentation
changes also need:

```bash
uv run --project tools/olf olf check docs
```

Before you open a pull request, run all four gates listed under
[Gates](/AGENTS.md#gates--all-must-pass-before-opening-a-pull-request).

## Open a pull request

1. Branch from `main`. Never commit to `main`.
2. Write commit messages as `type: subject`, with `type` one of `feat`,
   `fix`, `docs`, `refactor` or `chore`.
3. Keep the pull request to one issue. Over roughly 15 files or 600
   changed lines, split it.
4. Put `Closes #<issue>` in the body and say which checks you ran.
5. Resolve every review thread; `main` will not merge with open threads.

Read [AGENTS.md](/AGENTS.md) before your first code change.