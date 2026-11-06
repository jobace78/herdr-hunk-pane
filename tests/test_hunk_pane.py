import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Callable, Optional

import pytest

# a stand-in for Herdr's hc: answers get (per terminal) and ls from a state file and records
# every call
FAKE_HC = """
import json, os, sys
state_path = os.environ["FAKE_HC_STATE"]
with open(state_path, encoding="utf-8") as state_file:
    state = json.load(state_file)
with open(state_path + ".calls", "a", encoding="utf-8") as calls_file:
    hc_environment = sorted(key for key in os.environ if key.startswith("HC_"))
    calls_file.write(json.dumps({"argv": sys.argv[1:], "hc_environment": hc_environment}) + "\\n")
command = sys.argv[1]
failing_get = command == "get" and sys.argv[2] in state.get("fail_get", [])
if command in state.get("fail", []) or failing_get:
    sys.stderr.write(state.get("error", ""))
    sys.exit(1)
if command == "get":
    print(state["get"].get(sys.argv[2], ""))
if command == "ls":
    print(state["ls"])
"""
# the tab's hunk terminal as hc ls lists it and as hc get reads it: running hunk on the tab's patch
LISTING = json.dumps({"terminals": [{"address": "t9"}]})
PANE = json.dumps({"argv": ["sh", "hunk-pane", "/home/me/.cache/hunk-pane/tb1.patch", "--watch"]})
ROOT = Path(__file__).resolve().parent.parent
TAB_LABELS = json.dumps({"labels": {"herdr.space": "sp1", "herdr.tab": "tb1"}})


class FakeHc:
    """The state the fake hc answers from, and the calls it received."""

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self.set()

    def argvs(self) -> list[list[str]]:
        return [call["argv"] for call in self.calls()]

    def calls(self) -> list[dict[str, list[str]]]:
        calls_path = Path(f"{self.state_path}.calls")
        if not calls_path.exists():
            return []
        return [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]

    def set(
        self,
        *,
        error: str = "",
        fail: tuple[str, ...] = (),
        fail_get: tuple[str, ...] = (),
        get: Optional[dict[str, str]] = None,
        ls: str = "",
    ) -> None:
        state = {
            "error": error,
            "fail": list(fail),
            "fail_get": list(fail_get),
            "get": get or {},
            "ls": ls,
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")


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


def repository_labels(repository: Path, name: str = "repo") -> list[str]:
    repository_hash = hashlib.md5(str(repository).encode(), usedforsecurity=False).hexdigest()
    return [
        f"hunk-pane.from.{repository_hash}=t1",
        f"hunk-pane.name.{repository_hash}={name}",
        f"hunk-pane.repo.{repository_hash}={repository}",
        f"hunk-pane.selected={repository}",
    ]


hunk_pane = load_script("hunk_pane", ROOT / "bin" / "hunk_pane.py")


@pytest.fixture
def changed_repository(make_repository: Callable[..., Path]) -> Path:
    repository = make_repository()
    (repository / "a.txt").write_text("two\n", encoding="utf-8")
    return repository


@pytest.fixture
def fake_hc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeHc:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    hc = bin_dir / "hc"
    hc.write_text(f"#!{sys.executable}\n{FAKE_HC}", encoding="utf-8")
    hc.chmod(0o755)
    # never run, since hc is fake: a new pane only needs it on the PATH
    hunk = bin_dir / "hunk"
    hunk.write_text("#!/bin/sh\n", encoding="utf-8")
    hunk.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_HC_STATE", str(tmp_path / "hc-state.json"))
    return FakeHc(tmp_path / "hc-state.json")


@pytest.fixture
def in_herdr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HC_SCOPE", "scope")
    monkeypatch.setenv("HC_TERMINAL_ID", "t1")
    monkeypatch.setenv("HC_TERMINAL_SOCKET", "socket")


def test_main_refuses_to_run_as_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hunk_pane.os, "geteuid", lambda: 0)

    exit_code = hunk_pane.main([])

    assert exit_code == 1


def test_main_fails_outside_herdr(caplog: pytest.LogCaptureFixture) -> None:
    exit_code = hunk_pane.main([])

    assert exit_code == 1
    assert caplog.messages == ["'HC_TERMINAL_ID' is NOT set (this is no Herdr terminal), exiting."]


@pytest.mark.usefixtures("in_herdr")
def test_main_fails_outside_a_repository(tmp_path: Path, fake_hc: FakeHc) -> None:
    exit_code = hunk_pane.main([str(tmp_path)])

    assert exit_code == 1
    assert fake_hc.argvs() == []


@pytest.mark.usefixtures("in_herdr")
def test_main_leaves_out_a_repository_without_changes(
    make_repository: Callable[..., Path], fake_hc: FakeHc, caplog: pytest.LogCaptureFixture
) -> None:
    repository = make_repository()

    exit_code = hunk_pane.main([str(repository)])

    assert exit_code == 0
    assert fake_hc.argvs() == []
    assert caplog.messages == [f"'{repository}' repository has NO uncommitted changes, exiting."]


@pytest.mark.usefixtures("in_herdr")
def test_main_fails_when_the_repository_status_cannot_be_read(
    changed_repository: Path, fake_hc: FakeHc, caplog: pytest.LogCaptureFixture
) -> None:
    (changed_repository / ".git" / "index").write_bytes(b"not an index")

    exit_code = hunk_pane.main([str(changed_repository)])

    assert exit_code == 1
    assert fake_hc.argvs() == []
    assert caplog.messages == [
        f"'{changed_repository}' repository status could NOT be read, exiting."
    ]


@pytest.mark.usefixtures("in_herdr")
def test_main_opens_no_second_pane_when_the_terminals_cannot_be_listed(
    changed_repository: Path, fake_hc: FakeHc, caplog: pytest.LogCaptureFixture
) -> None:
    fake_hc.set(fail=("ls",), get={"t1": TAB_LABELS})

    exit_code = hunk_pane.main([str(changed_repository)])

    assert exit_code == 1
    assert [argv[0] for argv in fake_hc.argvs()] == ["get", "ls"]
    assert caplog.messages == ["'tb1' tab's terminals could NOT be listed, exiting."]


@pytest.mark.usefixtures("in_herdr")
def test_main_counts_untracked_files_the_repository_hides_from_status(
    make_repository: Callable[..., Path], fake_hc: FakeHc
) -> None:
    repository = make_repository()
    git(repository, "config", "status.showUntrackedFiles", "no")
    (repository / "new.txt").write_text("x\n", encoding="utf-8")
    fake_hc.set(get={"t1": TAB_LABELS, "t9": PANE}, ls=LISTING)

    exit_code = hunk_pane.main([str(repository)])

    assert exit_code == 0
    assert fake_hc.argvs()[-1] == ["label", "t9", *repository_labels(repository)]


@pytest.mark.usefixtures("in_herdr")
@pytest.mark.parametrize(
    ("failing", "message"),
    [
        pytest.param("t1", "'t1' Herdr terminal could NOT be read, exiting.", id="own-terminal"),
        pytest.param("t9", "'t9' hunk terminal could NOT be read, exiting.", id="pane-terminal"),
    ],
)
def test_main_fails_when_a_terminal_cannot_be_read(
    changed_repository: Path,
    fake_hc: FakeHc,
    caplog: pytest.LogCaptureFixture,
    failing: str,
    message: str,
) -> None:
    fake_hc.set(
        fail_get=(failing,),
        get={"t1": TAB_LABELS, "t9": PANE},
        ls=LISTING,
    )

    exit_code = hunk_pane.main([str(changed_repository)])

    assert exit_code == 1
    assert caplog.messages == [message]
    assert "label" not in [argv[0] for argv in fake_hc.argvs()]


@pytest.mark.usefixtures("in_herdr")
def test_main_labels_the_pane_the_tab_has(changed_repository: Path, fake_hc: FakeHc) -> None:
    fake_hc.set(get={"t1": TAB_LABELS, "t9": PANE}, ls=LISTING)

    exit_code = hunk_pane.main([str(changed_repository / "a.txt")])

    assert exit_code == 0
    assert fake_hc.argvs() == [
        ["get", "t1", "--json"],
        ["ls", "--json", "--label", "hunk-pane.tab=tb1"],
        ["get", "t9", "--json"],
        ["label", "t9", *repository_labels(changed_repository)],
    ]


@pytest.mark.usefixtures("in_herdr")
def test_main_names_a_repository_after_its_parent_when_the_pane_has_its_folder_name(
    changed_repository: Path, fake_hc: FakeHc
) -> None:
    pane = json.dumps({**json.loads(PANE), "labels": {"hunk-pane.name.h9": "repo"}})
    fake_hc.set(get={"t1": TAB_LABELS, "t9": pane}, ls=LISTING)

    hunk_pane.main([str(changed_repository)])

    assert fake_hc.argvs()[-1] == [
        "label",
        "t9",
        *repository_labels(changed_repository, "repos-repo"),
    ]


# after a reboot hc brings the hunk terminal back as a login shell, with its labels
@pytest.mark.usefixtures("in_herdr")
def test_main_opens_a_pane_when_the_tab_has_only_a_shell_hc_brought_back(
    changed_repository: Path, fake_hc: FakeHc
) -> None:
    shell = json.dumps({"argv": ["/bin/zsh", "-l"], "labels": {"hunk-pane.tab": "tb1"}})
    fake_hc.set(get={"t1": TAB_LABELS, "t9": shell}, ls=LISTING)

    exit_code = hunk_pane.main([str(changed_repository)])

    assert exit_code == 0
    assert [argv[0] for argv in fake_hc.argvs()] == ["get", "ls", "get", "run"]


@pytest.mark.usefixtures("in_herdr")
def test_main_takes_the_current_folder_by_default(
    changed_repository: Path, fake_hc: FakeHc, monkeypatch: pytest.MonkeyPatch
) -> None:
    (changed_repository / "src").mkdir()
    monkeypatch.chdir(changed_repository / "src")
    fake_hc.set(get={"t1": TAB_LABELS, "t9": PANE}, ls=LISTING)

    hunk_pane.main([])

    assert fake_hc.argvs()[-1] == ["label", "t9", *repository_labels(changed_repository)]


@pytest.mark.usefixtures("in_herdr")
def test_main_opens_no_pane_without_hunk_on_the_path(
    tmp_path: Path,
    changed_repository: Path,
    fake_hc: FakeHc,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bin_dir = tmp_path / "bin"
    (bin_dir / "hunk").unlink()
    git_path = shutil.which("git")
    assert git_path is not None
    (bin_dir / "git").symlink_to(git_path)
    monkeypatch.setenv("PATH", str(bin_dir))
    fake_hc.set(get={"t1": TAB_LABELS}, ls=json.dumps({"terminals": []}))

    exit_code = hunk_pane.main([str(changed_repository)])

    assert exit_code == 1
    assert [argv[0] for argv in fake_hc.argvs()] == ["get", "ls"]
    assert caplog.messages == ["'hunk' is NOT on the PATH, exiting."]


@pytest.mark.usefixtures("in_herdr")
def test_main_opens_a_pane_when_the_tab_has_none(
    tmp_path: Path, changed_repository: Path, fake_hc: FakeHc
) -> None:
    fake_hc.set(get={"t1": TAB_LABELS}, ls=json.dumps({"terminals": []}))
    home = tmp_path / "home"
    patch_path = home / ".cache" / "hunk-pane" / "tb1.patch"

    exit_code = hunk_pane.main([str(changed_repository)])

    assert exit_code == 0
    labels = repository_labels(changed_repository)
    assert fake_hc.calls()[-1] == {
        "argv": [
            "run", "--bg", "--cwd", str(home), "--json", "--no-parent", "--rm",
            "--label", "herdr.space=sp1", "--label", "herdr.tab=tb1",
            "--label", labels[0], "--label", labels[1], "--label", labels[2],
            "--label", labels[3],
            "--label", "hunk-pane.tab=tb1",
            "--", "sh", "-c", "printf '\\033]2;%s\\007' \"$1\"; shift; exec hunk patch \"$@\"",
            "sh", "hunk-pane", str(patch_path), "--watch",
        ],
        "hc_environment": ["HC_TERMINAL_SOCKET"],
    }  # fmt: skip
    assert patch_path.read_bytes() == b""


@pytest.mark.usefixtures("in_herdr")
@pytest.mark.parametrize(
    "tab_labels",
    [
        pytest.param({"herdr.space": "sp1"}, id="no-tab"),
        pytest.param({"herdr.tab": "tb1"}, id="no-space"),
        pytest.param({"herdr.space": "sp1", "herdr.tab": "../tb1"}, id="tab-that-names-a-path"),
    ],
)
def test_main_fails_when_the_terminal_is_in_no_valid_tab(
    changed_repository: Path, fake_hc: FakeHc, tab_labels: dict[str, str]
) -> None:
    fake_hc.set(get={"t1": json.dumps({"labels": tab_labels})})

    exit_code = hunk_pane.main([str(changed_repository)])

    assert exit_code == 1
    assert fake_hc.argvs() == [["get", "t1", "--json"]]


@pytest.mark.usefixtures("in_herdr")
@pytest.mark.parametrize(
    ("terminals", "failing"),
    [
        pytest.param([{"address": "t9"}], "label", id="label"),
        pytest.param([], "run", id="run"),
    ],
)
def test_main_fails_when_hc_fails(
    changed_repository: Path,
    fake_hc: FakeHc,
    caplog: pytest.LogCaptureFixture,
    terminals: list[dict[str, str]],
    failing: str,
) -> None:
    fake_hc.set(
        error=json.dumps({"error": {"kind": "not_found", "message": "no terminal"}}),
        fail=(failing,),
        get={"t1": TAB_LABELS, "t9": PANE},
        ls=json.dumps({"terminals": terminals}),
    )

    exit_code = hunk_pane.main([str(changed_repository)])

    assert exit_code == 1
    assert caplog.messages == [
        f"'{changed_repository}' repository could NOT be added to the hunk pane: "
        "no terminal, exiting."
    ]


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        pytest.param(b'{"error": {"message": "no terminal"}}', "no terminal", id="hc-error"),
        pytest.param(b"core not running\n", "core not running", id="plain-text"),
        pytest.param(b"", "hc exited with code 3", id="nothing"),
        pytest.param(b'{"error": {"kind": "x"}}', '{"error": {"kind": "x"}}', id="no-message"),
    ],
)
def test_hc_message_says_why_hc_failed(stderr: bytes, expected: str) -> None:
    error = subprocess.CalledProcessError(3, ["hc", "label"], stderr=stderr)

    message = hunk_pane._hc_message(error)

    assert message == expected


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        pytest.param("not json", None, id="not-json"),
        pytest.param(json.dumps({"labels": None, "argv": None}), ({}, []), id="not-lists"),
        pytest.param(json.dumps(["t1"]), ({}, []), id="not-an-object"),
        pytest.param(
            json.dumps({"labels": {"herdr.tab": "tb1", "count": 3}, "argv": ["sh", 3]}),
            ({"herdr.tab": "tb1"}, ["sh"]),
            id="non-string-values",
        ),
    ],
)
def test_terminal_keeps_only_string_labels_and_arguments(
    fake_hc: FakeHc, output: str, expected: Optional[tuple[dict[str, str], list[str]]]
) -> None:
    fake_hc.set(get={"t1": output})

    terminal = hunk_pane._terminal("t1")

    assert terminal == expected


@pytest.mark.parametrize(
    "output",
    [
        pytest.param(json.dumps({"terminals": []}), id="no-terminal"),
        pytest.param(json.dumps({"terminals": ["t9"]}), id="terminal-not-an-object"),
        pytest.param(json.dumps({"terminals": [{"address": 9}]}), id="address-not-a-string"),
    ],
)
def test_pane_terminal_finds_none_in_a_bad_listing(fake_hc: FakeHc, output: str) -> None:
    fake_hc.set(ls=output)

    pane = hunk_pane._pane_terminal("tb1")

    assert pane == ("", {})


def test_pane_terminal_fails_on_a_listing_that_is_not_json(
    fake_hc: FakeHc, caplog: pytest.LogCaptureFixture
) -> None:
    fake_hc.set(ls="not json")

    pane = hunk_pane._pane_terminal("tb1")

    assert pane is None
    assert caplog.messages == ["'tb1' tab's terminals could NOT be listed, exiting."]


@pytest.mark.parametrize(
    ("pane_names", "expected"),
    [
        pytest.param({}, "api", id="first-in-the-pane"),
        pytest.param({"h9": "web"}, "api", id="folder-name-free"),
        pytest.param({"h9": "api"}, "work-api", id="folder-name-taken"),
        pytest.param({"h8": "api", "h9": "work-api"}, "work-api-2", id="parent-name-taken"),
        pytest.param({"h1": "api-old", "h9": "api"}, "api-old", id="already-in-the-pane"),
    ],
)
def test_repository_name_is_unique_in_the_pane_and_kept(
    pane_names: dict[str, str], expected: str
) -> None:
    pane_labels = {f"hunk-pane.name.{key}": name for key, name in pane_names.items()}

    name = hunk_pane._repository_name("/home/me/work/api", "h1", pane_labels)

    assert name == expected


def test_repository_name_numbers_a_repository_right_under_the_root() -> None:
    pane_labels = {"hunk-pane.name.h9": "api"}

    name = hunk_pane._repository_name("/api", "h1", pane_labels)

    assert name == "api-2"
