#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Add a git repository to the hunk pane of this Herdr tab, opening the pane if the tab has none.

Usage, from a Herdr terminal (in Claude Code or Codex, with ``!``):

    hunk-pane [<path>]

The repository is the one the path is in, or the current folder's, and becomes the shown one, with
or without uncommitted changes. The pane runs ``hunk patch`` on
``~/.cache/hunk-pane/<tab id>.patch``, which the hunk-pane mod of Herdr writes: this script only
labels the hunk terminal. How the two behave, and the labels they share, are in RECIPE.md:
https://github.com/jobace78/herdr-hunk-pane/blob/main/RECIPE.md. It runs on Python 3.9, the one
macOS ships in ``/usr/bin/python3``.

Exit codes:
    0: added or already in the pane.
    1: run as root, outside Herdr or a repository, in no tab, a terminal unreadable, no hunk on the
       PATH for a new pane, or the pane could not be opened or labelled.
    2: wrong arguments.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from enum import IntEnum
from pathlib import Path
from typing import Final, Optional

ADD_FAILED: Final = "'%s' repository could NOT be added to the hunk pane: %s, exiting."
FROM_PREFIX: Final = "hunk-pane.from."
# hunk sets no terminal title, so the pane header would show the terminal address
HUNK_SCRIPT: Final = 'printf \'\\033]2;%s\\007\' "$1"; shift; exec hunk patch "$@"'
NAME_PREFIX: Final = "hunk-pane.name."
REPOSITORY_PREFIX: Final = "hunk-pane.repo."
SELECTED_LABEL: Final = "hunk-pane.selected"
SUBPROCESS_TIMEOUT_SECONDS: Final = 10.0
# the tab id names the patch file, so anything but a plain word is refused
TAB_ID: Final = re.compile(r"[A-Za-z0-9_-]+")
TAB_LABEL: Final = "hunk-pane.tab"

logger = logging.getLogger(__name__)


class ExitCode(IntEnum):
    """Exit status of the process."""

    OK = 0
    FAILURE = 1
    USAGE = 2  # argparse's own code for invalid arguments


def main(argv: Optional[list[str]] = None) -> int:
    """Add the repository the arguments name, or the current folder's, to the tab's hunk pane.

    Args:
        argv: The arguments: an optional path.

    Returns:
        The exit code of the process.
    """
    args = parse_args(argv)
    logging.basicConfig(format="%(message)s", level=logging.INFO)
    if os.geteuid() == 0:
        logger.error("this script must NOT be run as root, exiting.")
        return ExitCode.FAILURE
    terminal_id = os.environ.get("HC_TERMINAL_ID", "")
    if not terminal_id:
        logger.error("'HC_TERMINAL_ID' is NOT set (this is no Herdr terminal), exiting.")
        return ExitCode.FAILURE
    path = Path(args.path or os.getcwd()).expanduser().absolute()
    folder = str(path if path.is_dir() else path.parent)
    repository = _run(["git", "-C", folder, "rev-parse", "--show-toplevel"])
    if not repository:
        logger.error("'%s' is NOT in a git repository, exiting.", path)
        return ExitCode.FAILURE
    own = _terminal(terminal_id)
    if own is None:
        logger.error("'%s' Herdr terminal could NOT be read, exiting.", terminal_id)
        return ExitCode.FAILURE
    labels = own[0]
    space, tab = labels.get("herdr.space", ""), labels.get("herdr.tab", "")
    if not space or not TAB_ID.fullmatch(tab):
        logger.error("'%s' Herdr terminal is in NO tab, exiting.", terminal_id)
        return ExitCode.FAILURE
    repository_hash = hashlib.md5(repository.encode(), usedforsecurity=False).hexdigest()
    pane = _pane_terminal(tab)
    if pane is None:
        return ExitCode.FAILURE
    pane_terminal, pane_labels = pane
    name = _repository_name(repository, repository_hash, pane_labels)
    repository_labels = [
        f"{FROM_PREFIX}{repository_hash}={terminal_id}",
        f"{NAME_PREFIX}{repository_hash}={name}",
        f"{REPOSITORY_PREFIX}{repository_hash}={repository}",
        f"{SELECTED_LABEL}={repository}",
    ]
    # the new pane runs hunk with this environment: without it, the pane would close at once while
    # this said it is shown
    if not pane_terminal and shutil.which("hunk") is None:
        logger.error("'hunk' is NOT on the PATH, exiting.")
        return ExitCode.FAILURE
    try:
        if pane_terminal:
            _hc(["label", pane_terminal, *repository_labels])
        else:
            _open_pane(space, tab, repository_labels)
    except subprocess.CalledProcessError as error:
        logger.error(ADD_FAILED, repository, _hc_message(error))
        return ExitCode.FAILURE
    except (OSError, subprocess.TimeoutExpired) as error:
        logger.error(ADD_FAILED, repository, error)
        return ExitCode.FAILURE
    logger.info("'%s' repository shown in the hunk pane of this tab.", repository)
    return ExitCode.OK


def parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: The arguments, without the program name; None reads sys.argv.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "path", help="a path in the repository (default: the current folder)", nargs="?"
    )
    return parser.parse_args(argv)


def _run(argv: list[str]) -> Optional[str]:
    """Return the output of a command without its trailing newlines, None when it fails."""
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            check=True,
            encoding="utf-8",
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.rstrip("\n")


def _terminal(terminal_id: str) -> Optional[tuple[dict[str, str], list[str]]]:
    """Return the labels and the argv of a Herdr terminal, empty when it has none.

    None when hc cannot read the terminal: taken for a terminal without labels, the pane's would
    hide the names already taken, and a repository could be renamed or share a name.
    """
    output = _run(["hc", "get", terminal_id, "--json"])
    if output is None:
        return None
    try:
        terminal: object = json.loads(output or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(terminal, dict):
        return {}, []
    labels, argv = terminal.get("labels"), terminal.get("argv")
    return (
        {key: value for key, value in labels.items() if isinstance(value, str)}
        if isinstance(labels, dict)
        else {},
        [argument for argument in argv if isinstance(argument, str)]
        if isinstance(argv, list)
        else [],
    )


def _pane_terminal(tab: str) -> Optional[tuple[str, dict[str, str]]]:
    """Return the address and the labels of the tab's hunk terminal, ("", {}) when it has none.

    A terminal hc brought back after a reboot keeps its labels but runs a login shell, not hunk on
    the tab's patch: it is no pane, and the strip closes it. None, with the reason logged, when hc
    cannot list or read the terminals: taken for none, a second pane would open, or the names the
    pane holds would go unseen.
    """
    listed = _run(["hc", "ls", "--json", "--label", f"{TAB_LABEL}={tab}"])
    try:
        listing: object = None if listed is None else json.loads(listed or "{}")
    except json.JSONDecodeError:
        listing = None
    if listing is None:
        logger.error("'%s' tab's terminals could NOT be listed, exiting.", tab)
        return None
    terminals = listing.get("terminals") if isinstance(listing, dict) else None
    for terminal in terminals if isinstance(terminals, list) else []:
        address = terminal.get("address") if isinstance(terminal, dict) else None
        if not isinstance(address, str):
            continue
        read = _terminal(address)
        if read is None:
            logger.error("'%s' hunk terminal could NOT be read, exiting.", address)
            return None
        labels, argv = read
        if any(argument.endswith(f"/{tab}.patch") for argument in argv):
            return address, labels
    return "", {}


def _repository_name(repository: str, repository_hash: str, pane_labels: dict[str, str]) -> str:
    """Return a repository's name in the pane: its button, and its folder in the patch.

    A repository already in the pane keeps its name, and so does one taken out of it: its name label
    stays. A new one takes its folder's name or, when another repository of the pane has or had it,
    its parent's and its folder's joined by a dash (the folder's alone under /), then with a number.
    hunk returns each note under the name its patch was written with, so a name never changes nor
    passes to another repository while the pane lives; and a name has no slash, so a note's first
    folder names one repository.
    """
    current = pane_labels.get(f"{NAME_PREFIX}{repository_hash}")
    if current:
        return current
    taken = {value for key, value in pane_labels.items() if key.startswith(NAME_PREFIX)}
    path = Path(repository)
    name = path.name
    # a repository right under / has no parent to take: it goes straight to the number
    if name in taken and path.parent.name:
        name = f"{path.parent.name}-{path.name}"
    candidate, number = name, 2
    while candidate in taken:
        candidate, number = f"{name}-{number}", number + 1
    return candidate


def _hc(argv: list[str]) -> None:
    """Run an hc command, raising when it fails."""
    subprocess.run(
        ["hc", *argv], capture_output=True, check=True, timeout=SUBPROCESS_TIMEOUT_SECONDS
    )


def _open_pane(space: str, tab: str, repository_labels: list[str]) -> None:
    """Start hunk on the tab's patch in a terminal the tab adopts as a pane, without the focus.

    The patch is touched first, so hunk has a file to open: the strip writes the shown repository's
    within a moment. The terminal has no parent, so it outlives the program that ran this script and
    is not taken for its sub-agent; HC_SCOPE and HC_TERMINAL_ID are left out of its environment for
    the same reason.
    """
    patch_path = Path.home() / ".cache" / "hunk-pane" / f"{tab}.patch"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.touch()
    environment = {
        key: value for key, value in os.environ.items() if key not in ("HC_SCOPE", "HC_TERMINAL_ID")
    }
    subprocess.run(
        ["hc", "run", "--bg", "--cwd", str(Path.home()), "--json", "--no-parent", "--rm"]
        + ["--label", f"herdr.space={space}", "--label", f"herdr.tab={tab}"]
        + [argument for label in repository_labels for argument in ("--label", label)]
        + ["--label", f"{TAB_LABEL}={tab}"]
        + ["--", "sh", "-c", HUNK_SCRIPT, "sh", "hunk-pane", str(patch_path), "--watch"],
        capture_output=True,
        check=True,
        env=environment,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )


def _hc_message(error: subprocess.CalledProcessError) -> str:
    """Return why an hc command failed, in one line.

    hc writes its errors to stderr as JSON, ``{"error": {"message": ...}}``: the message, else the
    stderr as it is, else the exit code.
    """
    text = (error.stderr or b"").decode("utf-8", "replace").strip()
    try:
        output: object = json.loads(text)
    except json.JSONDecodeError:
        return text or f"hc exited with code {error.returncode}"
    details = output.get("error") if isinstance(output, dict) else None
    message = details.get("message") if isinstance(details, dict) else None
    return message if isinstance(message, str) else text


if __name__ == "__main__":
    raise SystemExit(main())
