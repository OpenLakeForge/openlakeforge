from olf.common import config
from olf.common.commands import diagnostics


def test_tree_excludes_generated_directories(tmp_path, monkeypatch, capsys):
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("hello\n")

    cached = tmp_path / ".venv" / "lib" / "package.py"
    cached.parent.mkdir(parents=True)
    cached.write_text("generated\n")

    monkeypatch.setattr(config, "repo_root", lambda: tmp_path)

    terraform_file = tmp_path / ".terraform" / "cache.bin"
    terraform_file.parent.mkdir()
    terraform_file.write_text("cached\n")

    for name in (".git", "__pycache__", ".pytest_cache"):
        generated = tmp_path / name / "cache.txt"
        generated.parent.mkdir()
        generated.write_text("cached\n")

    diagnostics.tree()
    output = capsys.readouterr().out
    assert ".venv" not in output
    assert "app.py" in output
    assert ".terraform" not in output

    for name in (".git", "__pycache__", ".pytest_cache"):
        assert name not in output
