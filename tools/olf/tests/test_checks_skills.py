from __future__ import annotations

from pathlib import Path

import pytest

from olf.commands.checks import _skills

REPO_ROOT = Path(__file__).resolve().parents[3]

VALID = """\
---
name: running-things
description: Run things with olf.
license: Apache-2.0
---

# Running things

Check with `olf status --json`, then read [the details](references/details.md#more).

```bash
olf deploy --provider local \\
  --phase artifacts
KUBECTL="$(olf toolchain path kubectl)"
olf status --provider local --json | jq '.urls'
olf diagnostics collect <dir> --namespace olf-dev
```
"""


def _write(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _skill(root: Path, text: str, name: str = "running-things", extra: dict[str, str] | None = None) -> None:
    _write(root, {f"skills/{name}/SKILL.md": text, **(extra or {})})


@pytest.fixture
def details() -> dict[str, str]:
    return {"skills/running-things/references/details.md": "# Details\n\n## More\n"}


def test_accepts_a_skill_whose_commands_options_and_links_exist(tmp_path: Path, details: dict[str, str]) -> None:
    _skill(tmp_path, VALID, extra=details)
    assert _skills.skill_errors(tmp_path) == []


def test_repository_skills_are_valid() -> None:
    assert _skills.skill_errors(REPO_ROOT) == []


def test_rejects_unknown_commands_options_and_bare_groups(tmp_path: Path, details: dict[str, str]) -> None:
    text = VALID + "\nRun `olf deplyo` or `olf check`.\n\n```bash\nolf deploy --provider local \\\n  --phse x\n```\n"
    _skill(tmp_path, text, extra=details)
    errors = _skills.skill_errors(tmp_path)
    assert len(errors) == 3
    assert "SKILL.md:19: `olf` has no command 'deplyo'" in errors[0]
    assert "SKILL.md:19: `olf check` needs a subcommand" in errors[1]
    assert "SKILL.md:22: `olf deploy` has no option --phse" in errors[2]


def test_rejects_frontmatter_outside_the_specification(tmp_path: Path) -> None:
    _skill(tmp_path, "---\nname: Other_Name\nauthor: me\n---\n\n# Skill\n")
    errors = _skills.skill_errors(tmp_path)
    assert "unknown frontmatter field `author`" in errors[0]
    assert "`name` must be lowercase words joined by single hyphens" in errors[1]
    assert "`description` is required" in errors[2]


def test_name_must_match_its_directory(tmp_path: Path) -> None:
    _skill(tmp_path, "---\nname: something-else\ndescription: x\n---\n")
    assert _skills.skill_errors(tmp_path) == [
        "skills/running-things/SKILL.md: `name` 'something-else' must match its directory 'running-things'"
    ]


def test_rejects_a_skill_without_frontmatter_or_without_skill_md(tmp_path: Path) -> None:
    _skill(tmp_path, "# No frontmatter\n")
    _write(tmp_path, {"skills/empty/notes.md": "# Notes\n"})
    assert _skills.skill_errors(tmp_path) == [
        "skills/empty: has no SKILL.md",
        "skills/running-things/SKILL.md: must open with YAML frontmatter between `---` lines",
    ]


def test_links_must_stay_inside_the_skill(tmp_path: Path) -> None:
    text = VALID.replace("references/details.md#more", "../../docs/guide.md")
    _skill(tmp_path, text + "\nSee [missing](references/missing.md).\n", extra={"docs/guide.md": "# Guide\n"})
    errors = _skills.skill_errors(tmp_path)
    assert errors == [
        "skills/running-things/SKILL.md:9: link leaves the skill directory: ../../docs/guide.md",
        "skills/running-things/SKILL.md:19: link target does not exist: references/missing.md",
    ]


def test_requires_the_skills_directory(tmp_path: Path) -> None:
    assert _skills.skill_errors(tmp_path) == ["skills/: directory is missing"]
