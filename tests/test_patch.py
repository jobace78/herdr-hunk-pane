import importlib.util
import os
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Callable

import pytest

ROOT = Path(__file__).resolve().parent.parent
TRACKED_CHANGE = (
    b"diff --git a/repo/a.txt b/repo/a.txt\n"
    b"index 5626abf..f719efd 100644\n"
    b"--- a/repo/a.txt\n"
    b"+++ b/repo/a.txt\n"
    b"@@ -1 +1 @@\n"
    b"-one\n"
    b"+two\n"
)
UNTRACKED_FILE = (
    b"diff --git a/repo/new.txt b/repo/new.txt\n"
    b"new file mode 100644\n"
    b"index 0000000..587be6b\n"
    b"--- /dev/null\n"
    b"+++ b/repo/new.txt\n"
    b"@@ -0,0 +1 @@\n"
    b"+x\n"
)


def git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args], capture_output=True, check=True, text=True
    ).stdout


def load_script(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


patch = load_script("patch", ROOT / "mods" / "hunk-pane" / "patch.py")


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param([], id="no-arguments"),
        pytest.param(["out.patch", "repo"], id="name-without-repository"),
        pytest.param(["out.patch", "a", "/r/a", "b", "/r/b"], id="two-repositories"),
    ],
)
def test_main_rejects_wrong_arguments(argv: list[str], caplog: pytest.LogCaptureFixture) -> None:
    exit_code = patch.main(argv)

    assert exit_code == 2
    assert caplog.messages == ["usage: patch.py <patch file> [<name> <repository>], exiting."]


def test_main_writes_an_empty_patch_without_repositories(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    patch_path = tmp_path / "cache" / "tab.patch"

    exit_code = patch.main([str(patch_path)])

    assert exit_code == 0
    assert patch_path.read_bytes() == b""
    assert capsys.readouterr().out == ""


# hunk reloads when the file is replaced, so an unchanged patch must keep its file
def test_main_keeps_the_file_when_the_patch_is_unchanged(tmp_path: Path) -> None:
    patch_path = tmp_path / "tab.patch"
    patch.main([str(patch_path)])
    inode = patch_path.stat().st_ino

    exit_code = patch.main([str(patch_path)])

    assert exit_code == 0
    assert patch_path.stat().st_ino == inode


def test_main_fails_when_the_patch_cannot_be_written(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    not_a_folder = tmp_path / "cache"
    not_a_folder.write_text("", encoding="utf-8")
    patch_path = not_a_folder / "tab.patch"

    exit_code = patch.main([str(patch_path)])

    assert exit_code == 1
    assert caplog.messages == [f"'{patch_path}' patch file could NOT be written, exiting."]


def test_main_leaves_no_temporary_file_when_the_patch_cannot_be_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_source: str, _destination: Path) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(patch.os, "replace", fail)
    patch_path = tmp_path / "cache" / "tab.patch"

    exit_code = patch.main([str(patch_path)])

    assert exit_code == 1
    assert list(patch_path.parent.iterdir()) == []


def test_main_writes_tracked_changes_under_the_repository_name(
    tmp_path: Path, make_repository: Callable[..., Path]
) -> None:
    repository = make_repository()
    (repository / "a.txt").write_text("two\n", encoding="utf-8")
    patch_path = tmp_path / "tab.patch"

    patch.main([str(patch_path), "repo", str(repository)])

    assert patch_path.read_bytes() == TRACKED_CHANGE


# a note's first folder must be the repository's name, which a "rename to" line lacks
@pytest.mark.parametrize(
    "renames", [pytest.param("true", id="default"), pytest.param("copies", id="copies")]
)
def test_main_writes_a_renamed_file_as_a_deletion_and_a_new_file(
    tmp_path: Path, make_repository: Callable[..., Path], renames: str
) -> None:
    repository = make_repository()
    git(repository, "config", "diff.renames", renames)
    (repository / "a.txt").rename(repository / "b.txt")
    patch_path = tmp_path / "tab.patch"

    patch.main([str(patch_path), "repo", str(repository)])

    written = patch_path.read_bytes()
    assert b"rename" not in written
    assert b"diff --git a/repo/a.txt b/repo/a.txt\ndeleted file mode 100644" in written
    assert b"diff --git a/repo/b.txt b/repo/b.txt\nnew file mode 100644" in written


@pytest.mark.parametrize(
    ("attributes", "key", "value"),
    [
        pytest.param("", "color.diff", "always", id="color"),
        pytest.param("", "diff.external", "{script}", id="external-diff"),
        pytest.param("a.txt diff=named\n", "diff.named.textconv", "{script}", id="textconv"),
    ],
)
def test_main_writes_a_plain_diff_whatever_the_config_says(
    tmp_path: Path, make_repository: Callable[..., Path], attributes: str, key: str, value: str
) -> None:
    files = {"a.txt": "one\n", ".gitattributes": attributes} if attributes else None
    repository = make_repository(files=files)
    script = tmp_path / "convert.sh"
    script.write_text("#!/bin/sh\necho converted\n", encoding="utf-8")
    script.chmod(0o755)
    git(repository, "config", key, value.format(script=script))
    (repository / "a.txt").write_text("two\n", encoding="utf-8")
    patch_path = tmp_path / "tab.patch"

    patch.main([str(patch_path), "repo", str(repository)])

    assert patch_path.read_bytes() == TRACKED_CHANGE


def test_main_adds_untracked_files_without_touching_the_index(
    tmp_path: Path, make_repository: Callable[..., Path]
) -> None:
    repository = make_repository()
    (repository / "new.txt").write_text("x\n", encoding="utf-8")
    index = (repository / ".git" / "index").read_bytes()
    patch_path = tmp_path / "tab.patch"

    patch.main([str(patch_path), "repo", str(repository)])

    assert patch_path.read_bytes() == UNTRACKED_FILE
    assert (repository / ".git" / "index").read_bytes() == index
    assert git(repository, "status", "--porcelain") == "?? new.txt\n"


# a file whose stat data no longer matches the index, as after a checkout or a sync from another
# machine
def test_main_diffs_through_a_refreshed_private_copy_of_a_stale_index(
    tmp_path: Path, make_repository: Callable[..., Path]
) -> None:
    repository = make_repository(files={"a.txt": "one\n", "b.txt": "same\n"})
    os.utime(repository / "b.txt", (1, 1))
    index = (repository / ".git" / "index").read_bytes()
    patch_path = tmp_path / "tab.patch"

    patch.main([str(patch_path), "repo", str(repository)])

    private_index = next(tmp_path.glob("tab.*.index"))
    stale = git(repository, "diff-files", "--name-only")
    fresh = subprocess.run(
        ["git", "-C", str(repository), "diff-files", "--name-only"],
        capture_output=True,
        check=True,
        env={**os.environ, "GIT_INDEX_FILE": str(private_index)},
        text=True,
    ).stdout
    assert (stale, fresh) == ("b.txt\n", "")
    assert (repository / ".git" / "index").read_bytes() == index


def test_main_keeps_the_private_index_while_the_index_is_unchanged(
    tmp_path: Path, make_repository: Callable[..., Path]
) -> None:
    repository = make_repository()
    patch_path = tmp_path / "tab.patch"
    patch.main([str(patch_path), "repo", str(repository)])
    private_index = next(tmp_path.glob("tab.*.index"))
    copied = private_index.stat().st_ctime_ns

    patch.main([str(patch_path), "repo", str(repository)])

    assert private_index.stat().st_ctime_ns == copied


def test_main_copies_the_index_again_once_it_changes(
    tmp_path: Path, make_repository: Callable[..., Path]
) -> None:
    repository = make_repository()
    patch_path = tmp_path / "tab.patch"
    patch.main([str(patch_path), "repo", str(repository)])
    (repository / "a.txt").write_text("two\n", encoding="utf-8")
    git(repository, "add", "a.txt")

    patch.main([str(patch_path), "repo", str(repository)])

    private_index = next(tmp_path.glob("tab.*.index"))
    index = repository / ".git" / "index"
    assert private_index.stat().st_mtime_ns == index.stat().st_mtime_ns
    assert private_index.read_bytes() == index.read_bytes()


def test_main_refreshes_the_private_index_despite_a_lock_left_behind(
    tmp_path: Path, make_repository: Callable[..., Path]
) -> None:
    repository = make_repository()
    patch_path = tmp_path / "tab.patch"
    patch.main([str(patch_path), "repo", str(repository)])
    private_index = next(tmp_path.glob("tab.*.index"))
    Path(f"{private_index}.lock").write_bytes(b"")
    (repository / "a.txt").write_text("two\n", encoding="utf-8")
    git(repository, "add", "a.txt")

    exit_code = patch.main([str(patch_path), "repo", str(repository)])

    assert exit_code == 0
    assert not Path(f"{private_index}.lock").exists()


# the empty blob already in the repository, as in one that ever committed an empty file
def test_main_adds_untracked_files_without_touching_the_objects(
    tmp_path: Path, make_repository: Callable[..., Path]
) -> None:
    repository = make_repository(files={"a.txt": "one\n", "empty.txt": ""})
    (repository / "new.txt").write_text("x\n", encoding="utf-8")
    objects = [path for path in (repository / ".git" / "objects").rglob("*") if path.is_file()]
    for path in objects:
        os.utime(path, (0, 0))
    patch_path = tmp_path / "tab.patch"

    patch.main([str(patch_path), "repo", str(repository)])

    assert [path.stat().st_mtime for path in objects] == [0] * len(objects)


def test_main_leaves_out_a_nested_repository_without_commits(
    tmp_path: Path, make_repository: Callable[..., Path]
) -> None:
    repository = make_repository()
    (repository / "new.txt").write_text("x\n", encoding="utf-8")
    (repository / "nested").mkdir()
    git(repository / "nested", "init", "--quiet")
    (repository / "nested" / "b.txt").write_text("y\n", encoding="utf-8")
    patch_path = tmp_path / "tab.patch"

    patch.main([str(patch_path), "repo", str(repository)])

    assert patch_path.read_bytes() == UNTRACKED_FILE


def test_main_diffs_a_linked_worktree_without_touching_its_index(
    tmp_path: Path, make_repository: Callable[..., Path]
) -> None:
    repository = make_repository()
    worktree = tmp_path / "worktree"
    git(repository, "worktree", "add", "--quiet", "-b", "feature", str(worktree))
    (worktree / "new.txt").write_text("x\n", encoding="utf-8")
    index_path = Path(
        git(worktree, "rev-parse", "--path-format=absolute", "--git-path", "index").strip()
    )
    index = index_path.read_bytes()
    patch_path = tmp_path / "tab.patch"

    patch.main([str(patch_path), "repo", str(worktree)])

    assert patch_path.read_bytes() == UNTRACKED_FILE
    assert index_path.read_bytes() == index


def test_main_diffs_a_repository_without_commits_against_the_empty_tree(
    tmp_path: Path, make_repository: Callable[..., Path]
) -> None:
    repository = make_repository(files={"new.txt": "x\n"}, commit=False)
    patch_path = tmp_path / "tab.patch"

    patch.main([str(patch_path), "repo", str(repository)])

    assert patch_path.read_bytes() == UNTRACKED_FILE


def test_main_writes_an_empty_patch_for_a_folder_git_cannot_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    not_a_repository = tmp_path / "plain"
    not_a_repository.mkdir()
    patch_path = tmp_path / "tab.patch"
    patch_path.write_bytes(TRACKED_CHANGE)

    exit_code = patch.main([str(patch_path), "plain", str(not_a_repository)])

    assert exit_code == 3
    assert patch_path.read_bytes() == b""
    assert caplog.messages == [
        f"'{not_a_repository}' repository could NOT be diffed, writing an empty patch."
    ]


def test_main_writes_an_empty_patch_over_the_size_limit(
    tmp_path: Path,
    make_repository: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(patch, "MAX_PATCH_BYTES", len(TRACKED_CHANGE) - 1)
    repository = make_repository()
    (repository / "a.txt").write_text("two\n", encoding="utf-8")
    patch_path = tmp_path / "tab.patch"

    exit_code = patch.main([str(patch_path), "repo", str(repository)])

    assert exit_code == 4
    assert patch_path.read_bytes() == b""
    assert caplog.messages == [
        f"'{repository}' repository patch is over 0 MiB, writing an empty patch."
    ]


def test_main_writes_an_empty_patch_when_git_takes_too_long(
    tmp_path: Path, make_repository: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(patch, "GIT_TIMEOUT_SECONDS", 0.000001)
    repository = make_repository()
    (repository / "a.txt").write_text("two\n", encoding="utf-8")
    patch_path = tmp_path / "tab.patch"

    exit_code = patch.main([str(patch_path), "repo", str(repository)])

    assert exit_code == 3
    assert patch_path.read_bytes() == b""
