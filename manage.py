#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Install the hunk-pane mod of Herdr and its command, update them, or uninstall them.

Usage, from a clone of https://github.com/jobace78/herdr-hunk-pane:

    ./manage.py install
    ./manage.py uninstall

``install`` copies the files git tracks in ``mods/hunk-pane`` into the ``mods`` folder of Herdr's
home (``~/.config/herdr-app``, or ``$HERDR_APP_HOME``), runs ``herdr-app check`` on that copy and
links its ``hunk_pane.py`` as ``~/.local/bin/hunk-pane``, the folder where Herdr links
``herdr-app`` and ``hc``. When the check fails, the copy installed before comes back. To update,
pull and install again. The layout block is yours to add: see "Install both halves" in RECIPE.md.
``uninstall`` removes the copy, the link and the patches in ``~/.cache/hunk-pane``.

Exit codes:
    0: installed, updated or uninstalled.
    1: run as root, not from a clone, the link's place taken by another file, or the check failed.
    2: wrong arguments, or none.
"""

import argparse
import logging
import os
import shutil
import subprocess
import tempfile
from enum import IntEnum
from pathlib import Path
from typing import Final, Optional

LINK: Final = Path("~/.local/bin/hunk-pane")
MOD: Final = "hunk-pane"
SCRIPT: Final = "hunk_pane.py"
SUBPROCESS_TIMEOUT_SECONDS: Final = 60.0

logger = logging.getLogger(__name__)


class ExitCode(IntEnum):
    """Exit status of the process."""

    OK = 0
    FAILURE = 1
    USAGE = 2  # argparse's own code for invalid arguments


def main(argv: Optional[list[str]] = None) -> int:
    """Install, update or uninstall the mod and its command, as the arguments say.

    Args:
        argv: The arguments: the action, ``install`` or ``uninstall``.

    Returns:
        The exit code of the process.
    """
    args = parse_args(argv)
    logging.basicConfig(format="%(message)s", level=logging.INFO)
    if os.geteuid() == 0:
        logger.error("this script must NOT be run as root, exiting.")
        return ExitCode.FAILURE
    app_home = Path(os.environ.get("HERDR_APP_HOME", "~/.config/herdr-app")).expanduser()
    mod = app_home / "mods" / MOD
    link = LINK.expanduser()
    if (link.exists() or link.is_symlink()) and not _ours(link, mod):
        logger.error("'%s' is NOT the hunk-pane link, exiting.", link)
        return ExitCode.FAILURE
    if args.action == "uninstall":
        _uninstall(mod, link)
        return ExitCode.OK
    files = _tracked_files(Path(__file__).resolve().parent / "mods" / MOD)
    if not files:
        logger.error("'%s' is NOT run from a clone of herdr-hunk-pane, exiting.", __file__)
        return ExitCode.FAILURE
    if not _install(files, mod):
        return ExitCode.FAILURE
    if not link.is_symlink():
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(mod / SCRIPT)
    logger.info("'%s' installed, '%s' linked.", mod, link)
    found = shutil.which(link.name)
    if not found or not _ours(Path(found), mod):
        logger.warning("'%s' is NOT on the PATH: add it.", link.parent)
    if not any(f'"{MOD}"' in p.read_text() for p in (app_home / "layouts").glob("*.json")):
        logger.warning("no layout docks the strip yet: see 'Install both halves' in RECIPE.md.")
    return ExitCode.OK


def parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: The arguments, without the program name.

    Returns:
        The parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("install", help="install or update the mod and link its command")
    actions.add_parser("uninstall", help="remove the mod, its link and its patches")
    return parser.parse_args(argv)


def _ours(link: Path, mod: Path) -> bool:
    """Return whether a path leads to the installed command, so it may be replaced or removed."""
    return link.resolve() == (mod / SCRIPT).resolve()


def _tracked_files(source: Path) -> list[Path]:
    """Return the files git tracks in the mod folder of the clone, none outside a clone."""
    try:
        listed = subprocess.run(
            ["git", "-C", str(source), "ls-files", "-z", "."],
            capture_output=True,
            check=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [source / name for name in listed.split("\0") if name]


def _install(files: list[Path], mod: Path) -> bool:
    """Copy the files into the mod folder and check it, bringing the previous copy back on failure.

    Returns:
        Whether the new copy passed ``herdr-app check``.
    """
    with tempfile.TemporaryDirectory(prefix=f"{MOD}-") as backup_parent:
        backup = Path(backup_parent) / MOD
        if mod.exists():
            shutil.move(str(mod), str(backup))
        mod.mkdir(parents=True)
        mod.chmod(0o755)
        for file in files:
            shutil.copyfile(file, mod / file.name)
            (mod / file.name).chmod(0o755 if os.access(file, os.X_OK) else 0o644)
        if _checked(mod):
            return True
        shutil.rmtree(mod)
        if backup.exists():
            shutil.move(str(backup), str(mod))
            logger.error("'%s' failed its check: the previous copy is back, exiting.", mod)
        else:
            logger.error("'%s' failed its check: nothing installed, exiting.", mod)
        return False


def _checked(mod: Path) -> bool:
    """Return whether ``herdr-app check`` passes on the mod folder, logging its report if not."""
    try:
        subprocess.run(
            ["herdr-app", "check", str(mod)],
            capture_output=True,
            check=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as error:
        logger.error("%s%s", error.stdout, error.stderr)
        return False
    except (OSError, subprocess.SubprocessError) as error:
        logger.error("'herdr-app check' could NOT run: %s", error)
        return False
    return True


def _uninstall(mod: Path, link: Path) -> None:
    """Remove the mod folder, the link and the patches, each only when it is there."""
    shutil.rmtree(mod, ignore_errors=True)
    if link.is_symlink():
        link.unlink()
    shutil.rmtree(Path("~/.cache/hunk-pane").expanduser(), ignore_errors=True)
    logger.info("'%s' and '%s' removed: take the strip's block out of your layout.", mod, link)


if __name__ == "__main__":
    raise SystemExit(main())
