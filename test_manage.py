import importlib.util
import logging
import os
from pathlib import Path
from types import ModuleType

import pytest

# a stand-in for herdr-app: records each call, and fails its check when FAKE_HERDR_APP_FAIL is set
FAKE_HERDR_APP = """#!/bin/sh
echo "$@" >> "$FAKE_HERDR_APP_CALLS"
if [ -n "$FAKE_HERDR_APP_FAIL" ]; then
  echo "main.js: broken" >&2
  exit 1
fi
"""
ROOT = Path(__file__).resolve().parent


def load_script(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


manage = load_script("manage", ROOT / "manage.py")


# the conftest.py of the mod's tests, which isolates HOME, does not reach this file
@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture
def app_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "herdr-app"
    (home / "layouts").mkdir(parents=True)
    monkeypatch.setenv("HERDR_APP_HOME", str(home))
    return home


@pytest.fixture
def herdr_app_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    herdr_app = bin_dir / "herdr-app"
    herdr_app.write_text(FAKE_HERDR_APP, encoding="utf-8")
    herdr_app.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_HERDR_APP_CALLS", str(tmp_path / "herdr-app.calls"))
    monkeypatch.delenv("FAKE_HERDR_APP_FAIL", raising=False)
    return tmp_path / "herdr-app.calls"


@pytest.fixture
def link() -> Path:
    return Path(os.environ["HOME"]) / ".local" / "bin" / "hunk-pane"


def contents(folder: Path) -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(folder.iterdir())}


def modes(folder: Path) -> dict[str, int]:
    return {path.name: path.stat().st_mode & 0o777 for path in sorted(folder.iterdir())}


def test_parse_args_requires_an_action(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        manage.parse_args([])

    assert excinfo.value.code == 2
    assert "the following arguments are required: action" in capsys.readouterr().err


def test_main_refuses_to_run_as_root(app_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manage.os, "geteuid", lambda: 0)

    exit_code = manage.main(["install"])

    assert exit_code == 1
    assert not (app_home / "mods").exists()


@pytest.mark.usefixtures("herdr_app_calls")
def test_main_installs_the_tracked_files_with_their_modes(app_home: Path, link: Path) -> None:
    mod = app_home / "mods" / "hunk-pane"

    exit_code = manage.main(["install"])

    assert exit_code == 0
    assert modes(mod) == {
        "close.svg": 0o644,
        "helpers.js": 0o644,
        "hunk_pane.py": 0o755,
        "hunk_pane.svg": 0o644,
        "main.js": 0o644,
        "mod.json": 0o644,
        "patch.py": 0o644,
    }
    assert mod.stat().st_mode & 0o777 == 0o755
    assert link.resolve() == (mod / "hunk_pane.py").resolve()


def test_main_checks_the_installed_copy(app_home: Path, herdr_app_calls: Path) -> None:
    manage.main(["install"])

    assert herdr_app_calls.read_text(encoding="utf-8") == f"check {app_home}/mods/hunk-pane\n"


@pytest.mark.usefixtures("herdr_app_calls")
def test_main_replaces_the_previous_copy(app_home: Path) -> None:
    mod = app_home / "mods" / "hunk-pane"
    mod.mkdir(parents=True)
    (mod / "main.js").write_text("old\n", encoding="utf-8")
    (mod / "stale.js").write_text("gone upstream\n", encoding="utf-8")

    exit_code = manage.main(["install"])

    assert exit_code == 0
    assert not (mod / "stale.js").exists()
    assert (mod / "main.js").read_bytes() == (ROOT / "mods" / "hunk-pane" / "main.js").read_bytes()


@pytest.mark.usefixtures("herdr_app_calls")
def test_main_brings_the_previous_copy_back_when_the_check_fails(
    app_home: Path, link: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    mod = app_home / "mods" / "hunk-pane"
    mod.mkdir(parents=True)
    (mod / "main.js").write_text("old\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_HERDR_APP_FAIL", "1")

    exit_code = manage.main(["install"])

    assert exit_code == 1
    assert contents(mod) == {"main.js": "old\n"}
    assert not link.is_symlink()
    assert caplog.messages == [
        "main.js: broken\n",
        f"'{mod}' failed its check: the previous copy is back, exiting.",
    ]


@pytest.mark.usefixtures("herdr_app_calls")
def test_main_installs_nothing_when_the_first_check_fails(
    app_home: Path, link: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    mod = app_home / "mods" / "hunk-pane"
    monkeypatch.setenv("FAKE_HERDR_APP_FAIL", "1")

    exit_code = manage.main(["install"])

    assert exit_code == 1
    assert not mod.exists()
    assert not link.is_symlink()
    assert caplog.messages[-1] == f"'{mod}' failed its check: nothing installed, exiting."


@pytest.mark.parametrize("action", ["install", "uninstall"])
def test_main_leaves_a_hunk_pane_that_is_not_its_link(
    action: str,
    app_home: Path,
    herdr_app_calls: Path,
    link: Path,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    other = tmp_path / "other-hunk-pane"
    other.write_text("#!/bin/sh\n", encoding="utf-8")
    link.parent.mkdir(parents=True)
    link.symlink_to(other)

    exit_code = manage.main([action])

    assert exit_code == 1
    assert link.resolve() == other.resolve()
    assert not (app_home / "mods").exists()
    assert not herdr_app_calls.exists()
    assert caplog.messages == [f"'{link}' is NOT the hunk-pane link, exiting."]


@pytest.mark.usefixtures("herdr_app_calls")
def test_main_keeps_its_link_on_an_update(app_home: Path, link: Path) -> None:
    mod = app_home / "mods" / "hunk-pane"
    manage.main(["install"])

    exit_code = manage.main(["install"])

    assert exit_code == 0
    assert link.resolve() == (mod / "hunk_pane.py").resolve()


@pytest.mark.usefixtures("herdr_app_calls")
def test_main_warns_when_the_link_is_off_the_path_and_no_layout_docks_the_strip(
    app_home: Path, link: Path, caplog: pytest.LogCaptureFixture
) -> None:
    mod = app_home / "mods" / "hunk-pane"
    caplog.set_level(logging.INFO)

    manage.main(["install"])

    assert caplog.messages == [
        f"'{mod}' installed, '{link}' linked.",
        f"'{link.parent}' is NOT on the PATH: add it.",
        "no layout docks the strip yet: see 'Install both halves' in RECIPE.md.",
    ]


@pytest.mark.usefixtures("herdr_app_calls")
def test_main_warns_of_nothing_when_the_link_is_on_the_path_and_a_layout_docks_the_strip(
    app_home: Path, link: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    mod = app_home / "mods" / "hunk-pane"
    (app_home / "layouts" / "mine.json").write_text('{"mod": "hunk-pane"}\n', encoding="utf-8")
    monkeypatch.setenv("PATH", f"{link.parent}{os.pathsep}{os.environ['PATH']}")
    caplog.set_level(logging.INFO)

    manage.main(["install"])

    assert caplog.messages == [f"'{mod}' installed, '{link}' linked."]


def test_main_fails_outside_a_clone(
    app_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manage, "__file__", str(tmp_path / "manage.py"))

    exit_code = manage.main(["install"])

    assert exit_code == 1
    assert not (app_home / "mods").exists()


def test_checked_fails_when_herdr_app_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    checked = manage._checked(tmp_path)

    assert not checked
    assert caplog.messages[0].startswith("'herdr-app check' could NOT run: ")


@pytest.mark.usefixtures("herdr_app_calls")
def test_main_uninstalls_the_mod_its_link_and_its_patches(app_home: Path, link: Path) -> None:
    manage.main(["install"])
    patches = Path(os.environ["HOME"]) / ".cache" / "hunk-pane"
    patches.mkdir(parents=True)
    (patches / "tb1.patch").write_text("", encoding="utf-8")

    exit_code = manage.main(["uninstall"])

    assert exit_code == 0
    assert not (app_home / "mods" / "hunk-pane").exists()
    assert not link.is_symlink()
    assert not patches.exists()


def test_main_uninstalls_when_nothing_is_installed(app_home: Path) -> None:
    exit_code = manage.main(["uninstall"])

    assert exit_code == 0
    assert not (app_home / "mods").exists()
