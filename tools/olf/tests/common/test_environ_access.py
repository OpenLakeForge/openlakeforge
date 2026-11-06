"""`olf` does not write the process environment, and reads it only by the names listed here (#559).

Contract values travel as a mapping (`environ=`) and reach a child process as `env=`; a write to `os.environ`
would make them ambient again and let one deployment's values leak into the next block.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import olf

OLF = Path(olf.__file__).parent

# (module, function) -> why that function still writes `os.environ`.
WRITE_ALLOWLIST = {
    ("common/project_code_check.py", "_environment"): (
        "dlt resolves `dlt.config.value`/`dlt.secrets.value` from the process environment itself, so the check's "
        "source bindings can reach it no other way; restored on exit"
    ),
    ("infra/e2e/_shell.py", "user_trust_environment"): (
        "drives the CLI's documented NO_PROXY/REQUESTS_CA_BUNDLE contract (docs/reference/cli.md) the way a user "
        "sets it; restored on exit"
    ),
}

# (module, key expression) -> why the value is the operator's, not the contract's. A ratchet: a new read fails
# until it is listed here, and an entry nothing reads any more fails until it is removed.
# ponytail: only keyed reads are listed. Whole-mapping fallbacks (`os.environ if environ is None else environ`,
# `command_env(base=os.environ)`) at CLI roots are not; listing them needs a per-call-site table.
READ_ALLOWLIST = {
    ("common/commands/checks/_dbt.py", "PATH"): "check subprocess: extends the operator's search path",
    ("common/commands/checks/_dbt.py", "PYTHONPATH"): "check subprocess: extends the operator's import path",
    ("common/commands/checks/_project_code.py", "PATH"): "check subprocess: extends the operator's search path",
    ("common/contract_state.py", "PROVIDER_CONTRACTS_FILE_ENV"): "CI selector a scoped provider env drops",
    ("common/deployment/context.py", "DOCKER_CONFIG"): "the developer's own Docker config, linked for cli-plugins",
    ("common/deployment/context.py", "FOUNDATION_STATE_PATH"): "operator override of the foundation state file",
    ("common/project_code_check.py", "name"): "saved by `_environment` to restore on exit",
    ("common/toolchain/manager.py", "OLF_HOME"): "operator's OLF home",
    ("common/tooling/docker.py", "DOCKER_CONFIG"): "the developer's own Docker config, for registries OLF does not own",
    ("infra/auth/azure.py", "PATH"): "auth bridge subprocess: extends the operator's search path",
    ("infra/commands/auth.py", "AWS_PROFILE"): "CLI root: the operator's AWS profile to log in with",
    ("infra/commands/diagnostics.py", "KUBE_CONTEXT"): "CLI root: the operator's kube context",
    ("infra/commands/runtime.py", "OPENLAKEFORGE_CONTRACT_TERRAFORM_DIR"): "CLI root: operator override",
    ("infra/commands/source.py", "var"): "CLI root: the source secrets the operator exports",
    ("infra/deployment/local/platform.py", "CI"): "CI flag: never prompt",
    ("infra/e2e/_shell.py", "name"): "saved by `user_trust_environment` to restore on exit",
    ("infra/e2e/github_stub.py", "STUB_USERS"): "runs in the stub pod, which sets it",
    ("platform/session.py", "NO_PROXY"): "the CLI's documented proxy contract",
    ("platform/session.py", "REQUESTS_CA_BUNDLE"): "the CLI's documented trust contract",
    ("platform/session.py", "SSL_CERT_FILE"): "the CLI's documented trust contract",
}

_MUTATORS = frozenset({"update", "pop", "popitem", "setdefault", "clear", "__setitem__", "__delitem__"})


def _is_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return "<module>"


def _is_write(node: ast.AST, parent: ast.AST | None, grandparent: ast.AST | None) -> bool:
    if isinstance(node, ast.Call):
        name = ast.unparse(node.func)
        # `patch.dict(os.environ, ...)`, under any import spelling of `patch`.
        patches = name.endswith("patch.dict") and bool(node.args) and _is_environ(node.args[0])
        return name in {"os.putenv", "os.unsetenv"} or patches
    if not _is_environ(node):
        return False
    if isinstance(parent, ast.Subscript):
        return isinstance(parent.ctx, (ast.Store, ast.Del))
    return isinstance(parent, ast.Attribute) and parent.attr in _MUTATORS and isinstance(grandparent, ast.Call)


def environ_writes(source: str) -> list[tuple[int, str]]:
    """(line, enclosing function) of every write to `os.environ` in `source`."""
    tree = ast.parse(source)
    parents = _parents(tree)
    return [
        (node.lineno, _function(node, parents))
        for node in ast.walk(tree)
        if _is_write(node, parents.get(node), parents.get(parents.get(node, node)))
    ]


def environ_reads(source: str) -> set[str]:
    """The key expressions `source` reads from `os.environ` by name: `os.environ[k]`, `.get(k)`, `os.getenv(k)`."""
    tree = ast.parse(source)
    parents = _parents(tree)
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "os.getenv" and node.args:
            keys.add(_key(node.args[0]))
        parent = parents.get(node)
        if not _is_environ(node):
            continue
        if isinstance(parent, ast.Subscript) and isinstance(parent.ctx, ast.Load):
            keys.add(_key(parent.slice))
        elif isinstance(parent, ast.Attribute) and parent.attr == "get":
            call = parents.get(parent)
            if isinstance(call, ast.Call) and call.args:
                keys.add(_key(call.args[0]))
    return keys


def _key(node: ast.expr) -> str:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ast.unparse(node)


def _modules() -> list[tuple[str, str]]:
    return [(path.relative_to(OLF).as_posix(), path.read_text(encoding="utf-8")) for path in sorted(OLF.rglob("*.py"))]


def test_only_allowlisted_functions_write_the_process_environment() -> None:
    found: dict[tuple[str, str], int] = {}
    for module, source in _modules():
        for line, function in environ_writes(source):
            found.setdefault((module, function), line)
    unlisted = [
        f"{module}:{line} {function}()"
        for (module, function), line in found.items()
        if (module, function) not in WRITE_ALLOWLIST
    ]
    assert not unlisted, "pass a mapping (`environ=`/`env=`) instead of writing os.environ:\n" + "\n".join(unlisted)
    assert set(WRITE_ALLOWLIST) <= set(found), f"stale entries: {sorted(set(WRITE_ALLOWLIST) - set(found))}"


def test_keyed_reads_of_the_process_environment_are_allowlisted() -> None:
    found = {(module, key) for module, source in _modules() for key in environ_reads(source)}
    unlisted = sorted(found - set(READ_ALLOWLIST))
    assert not unlisted, f"read it from the `environ` mapping the caller passes, or list it with a reason: {unlisted}"
    assert not set(READ_ALLOWLIST) - found, f"stale entries: {sorted(set(READ_ALLOWLIST) - found)}"


@pytest.mark.parametrize(
    "source",
    [
        'import os\ndef f():\n    os.environ["X"] = "1"',
        'import os\ndef f():\n    del os.environ["X"]',
        'import os\ndef f():\n    os.environ["X"] += "1"',
        'import os\ndef f():\n    os.environ.update({"X": "1"})',
        'import os\ndef f():\n    os.environ.pop("X", None)',
        'import os\ndef f():\n    os.environ.setdefault("X", "1")',
        "import os\ndef f():\n    os.environ.clear()",
        'import os\ndef f():\n    os.putenv("X", "1")',
        "import os\nfrom unittest import mock\ndef f():\n    with mock.patch.dict(os.environ, {}):\n        ...",
        "import os\nfrom unittest.mock import patch\ndef f():\n    with patch.dict(os.environ, {}):\n        ...",
    ],
)
def test_a_write_is_detected_with_its_function(source: str) -> None:
    assert [function for _, function in environ_writes(source)] == ["f"]


def test_reads_and_mappings_are_not_writes() -> None:
    source = (
        "import os\n"
        "def f(environ=None):\n"
        '    env = dict(os.environ); env["X"] = "1"\n'
        '    environ = {}; environ["X"] = "1"\n'
        '    return os.environ.get("X"), os.environ["Y"], os.getenv("Z")\n'
    )

    assert environ_writes(source) == []
    assert environ_reads(source) == {"X", "Y", "Z"}
