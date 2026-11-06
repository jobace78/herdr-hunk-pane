#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Write the uncommitted changes of a git repository to one patch, for hunk patch --watch.

Usage:

    python3 patch.py <patch file> [<name> <repository>]

The repository is diffed against HEAD (against the empty tree before its first commit), untracked
files included but not nested repositories, a submodule as the commit it points to, under its name:
``a/<name>/<path>``; with no repository the patch is empty. The file is replaced only when the patch
changes, so hunk reloads only then. A repository git cannot read leaves the patch empty, with a
message to stderr and exit code 3; so does a patch over 8 MiB, with exit code 4.

git runs as it does in the user's shell, the repository's config included, but neither the
repository's index nor its objects are changed, so the agents' own git never finds the index locked:
diff reads a private copy of the index, kept beside the patch with fresh stat data, and untracked
files enter it, in a temporary copy, listed with the empty blob's id, which writes no object. It
needs only the standard library of Python 3.9, the one macOS ships in ``/usr/bin/python3``.

Exit codes:
    0: written, or already up to date.
    1: the patch file could not be written.
    2: wrong arguments.
    3: written empty: git could not read the repository.
    4: written empty: the patch is over 8 MiB.
"""

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from enum import IntEnum
from pathlib import Path
from typing import Final, Optional

# a run makes at most seven git calls, which must end within the 30 s Herdr gives the mod's exec
GIT_TIMEOUT_SECONDS: Final = 4.0
# hunk loads the whole patch at each change; a bigger one is most likely a log nobody ignored
MAX_PATCH_BYTES: Final = 8 * 2**20
# no optional lock and no index refresh: git leaves the index as it is
SAFE_GIT: Final = ("--no-optional-locks", "-c", "diff.autoRefreshIndex=false")

logger = logging.getLogger(__name__)


class ExitCode(IntEnum):
    """Exit status of the process."""

    OK = 0
    FAILURE = 1
    USAGE = 2
    REPOSITORY_UNREADABLE = 3
    PATCH_TOO_LARGE = 4


def main(argv: Optional[list[str]] = None) -> int:
    """Write the patch of the repository the arguments name, or an empty one.

    Args:
        argv: The arguments: the patch file, then optionally the name and the repository.

    Returns:
        The exit code of the process.
    """
    logging.basicConfig(format="%(message)s", level=logging.INFO)
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (1, 3):
        logger.error("usage: patch.py <patch file> [<name> <repository>], exiting.")
        return ExitCode.USAGE
    patch = _repository_patch(args[1], Path(args[2]), Path(args[0])) if len(args) == 3 else b""
    too_large = patch is not None and len(patch) > MAX_PATCH_BYTES
    if too_large:
        logger.warning(
            "'%s' repository patch is over %d MiB, writing an empty patch.",
            args[2],
            MAX_PATCH_BYTES // 2**20,
        )
    try:
        _replace_if_different(Path(args[0]), b"" if patch is None or too_large else patch)
    except OSError:
        logger.exception("'%s' patch file could NOT be written, exiting.", args[0])
        return ExitCode.FAILURE
    if patch is None:
        return ExitCode.REPOSITORY_UNREADABLE
    return ExitCode.PATCH_TOO_LARGE if too_large else ExitCode.OK


def _repository_patch(name: str, repository: Path, patch_path: Path) -> Optional[bytes]:
    """Return the changes of a repository against HEAD, untracked files included, under its name.

    None, with a message to stderr, when git cannot read the repository.
    """
    try:
        index_path, base = _index_and_base(repository)
        private_index = _private_index(repository, index_path, patch_path)
        listed = _git(repository, ["ls-files", "-z", "--exclude-standard", "--others"]).split(b"\0")
        # a nested repository is listed as "<folder>/" and stays out
        untracked = [path for path in listed if path and not path.endswith(b"/")]
        # the diff hunk reads, whatever the config says: no color, external diff, textconv or
        # submodule log
        diff = [
            "diff",
            "--no-color",
            "--no-ext-diff",
            # a renamed file is a deletion and a new file: hunk takes a pure rename's path from its
            # "rename to" line, which carries no <name>/ prefix, and diff.renames could change it
            "--no-renames",
            "--no-textconv",
            "--submodule=short",
            f"--src-prefix=a/{name}/",
            f"--dst-prefix=b/{name}/",
            base,
        ]
        if not untracked:
            return _git(
                repository, diff, environment={**os.environ, "GIT_INDEX_FILE": str(private_index)}
            )
        with tempfile.TemporaryDirectory() as temporary_dir:
            temporary_index = Path(temporary_dir) / "index"
            if private_index.is_file():
                # copy2 keeps the index's mtime, which git needs to compare the content of a file
                # changed in the same second as the index (racy git): a fresh mtime would hide it
                shutil.copy2(private_index, temporary_index)
            environment = {**os.environ, "GIT_INDEX_FILE": str(temporary_index)}
            # each untracked file as an entry with the empty blob's id (diff reads its mode and
            # content from the file itself): git add --intent-to-add would write that blob again
            # on every run, bumping the mtime of the object, or of the whole pack holding it
            algorithm = hashlib.sha1 if len(base) == 40 else hashlib.sha256
            empty_blob = algorithm(b"blob 0\0").hexdigest().encode()
            entries = b"".join(b"100644 " + empty_blob + b"\t" + path + b"\0" for path in untracked)
            _git(
                repository,
                ["update-index", "-z", "--index-info"],
                stdin=entries,
                environment=environment,
            )
            return _git(repository, diff, environment=environment)
    except (OSError, subprocess.SubprocessError):
        logger.exception("'%s' repository could NOT be diffed, writing an empty patch.", repository)
        return None


def _index_and_base(repository: Path) -> tuple[Path, str]:
    """Return the index file of a repository and what to diff against: HEAD, or the empty tree.

    One rev-parse gives both while the repository has a commit; before its first one, HEAD does
    not resolve, and the empty tree of the repository's hash algorithm takes its place.
    """
    resolved = subprocess.run(
        _git_command(repository, ["rev-parse", "--path-format=absolute", "--git-path", "index"])
        + ["--verify", "--quiet", "HEAD^{commit}"],
        capture_output=True,
        check=False,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    lines = resolved.stdout.decode("utf-8", "surrogateescape").splitlines()
    if resolved.returncode == 0 and len(lines) == 2:
        return Path(lines[0]), lines[1]
    index_path = _git(repository, ["rev-parse", "--path-format=absolute", "--git-path", "index"])
    empty_tree = _git(repository, ["hash-object", "-t", "tree", "--stdin"], stdin=b"")
    return Path(os.fsdecode(index_path.strip())), empty_tree.decode("ascii").strip()


def _private_index(repository: Path, index_path: Path, patch_path: Path) -> Path:
    """Return a copy of a repository's index with fresh stat data, kept beside the patch.

    The repository's own index is never refreshed, so its stat data can be stale: after a checkout,
    or for good in a folder another machine syncs (Dropbox), where the other machine's git writes
    it; diff would then hash every such file again on each refresh. The copy is refreshed once each
    time the repository's index changes, and takes its mtime: that tells when it changed, and git
    still compares the content of a file changed after it (racy git). One per repository, so two
    repositories shown in turn never share one. Before the repository has an index, the path names
    no file, which git reads as an empty index.
    """
    digest = hashlib.md5(bytes(repository), usedforsecurity=False).hexdigest()
    private_index = patch_path.with_name(f"{patch_path.stem}.{digest}.index")
    if not index_path.is_file():
        private_index.unlink(missing_ok=True)
        return private_index
    mtime = index_path.stat().st_mtime_ns
    if private_index.is_file() and private_index.stat().st_mtime_ns == mtime:
        return private_index
    shutil.copyfile(index_path, private_index)
    # left by a run Herdr or the timeout stopped, it would make every later refresh fail; runs on
    # the same patch never overlap
    Path(f"{private_index}.lock").unlink(missing_ok=True)
    _git(
        repository,
        # -q and --unmerged: a changed or conflicted file stays as it is instead of stopping it
        ["update-index", "-q", "--unmerged", "--refresh"],
        environment={**os.environ, "GIT_INDEX_FILE": str(private_index)},
    )
    os.utime(private_index, ns=(mtime, mtime))
    return private_index


def _git_command(repository: Path, args: list[str]) -> list[str]:
    """Return the argv that runs a git command on a repository, with SAFE_GIT."""
    return ["git", "-C", str(repository), *SAFE_GIT, *args]


def _git(
    repository: Path,
    args: list[str],
    *,
    stdin: Optional[bytes] = None,
    environment: Optional[dict[str, str]] = None,
) -> bytes:
    """Return what a git command writes on a repository, raising when it fails."""
    return subprocess.run(
        _git_command(repository, args),
        capture_output=True,
        check=True,
        env=environment,
        input=stdin,
        timeout=GIT_TIMEOUT_SECONDS,
    ).stdout


def _replace_if_different(patch_path: Path, patch: bytes) -> None:
    """Replace the patch file when its content differs, through a rename.

    The new content is written next to the file and renamed over it, so hunk never reads it half
    written.
    """
    if patch_path.is_file() and patch_path.read_bytes() == patch:
        return
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=patch_path.parent, delete=False) as temporary:
        try:
            temporary.write(patch)
            temporary.close()
            os.replace(temporary.name, patch_path)
        except OSError:
            # left behind, it would stay for good: the strip deletes only patches, indexes and locks
            Path(temporary.name).unlink(missing_ok=True)
            raise


if __name__ == "__main__":
    raise SystemExit(main())
