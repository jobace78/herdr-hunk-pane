# The scripts are not beside their tests, as a script's tests usually are: patch.py sits in the mod
# folder, which is copied whole into ~/.config/herdr-app/mods/, and a test beside it would be
# installed with it. Each test module loads its script by path. pytest 8.4 is the last release that
# runs on Python 3.9, the one the scripts target.

import subprocess
from pathlib import Path
from typing import Callable, Optional

import pytest


def git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args], capture_output=True, check=True, text=True
    ).stdout


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    global_config = tmp_path / "gitconfig"
    global_config.write_text(
        "[user]\n\tname = Test\n\temail = test@example.invalid\n", encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for variable in ("HC_SCOPE", "HC_TERMINAL_ID", "HC_TERMINAL_SOCKET"):
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def make_repository(tmp_path: Path) -> Callable[..., Path]:
    def make(
        name: str = "repo", files: Optional[dict[str, str]] = None, *, commit: bool = True
    ) -> Path:
        repository = tmp_path / "repos" / name
        repository.mkdir(parents=True)
        # sha1 pinned: the expected patches carry its object ids
        git(repository, "init", "--quiet", "--initial-branch=main", "--object-format=sha1")
        for path, content in (files or {"a.txt": "one\n"}).items():
            (repository / path).parent.mkdir(parents=True, exist_ok=True)
            (repository / path).write_text(content, encoding="utf-8")
        if commit:
            git(repository, "add", "--all")
            git(repository, "commit", "--quiet", "--message=first")
        return repository.resolve()

    return make
