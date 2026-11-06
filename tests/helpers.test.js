import assert from "node:assert/strict";
import { describe, test } from "node:test";
import {
  liveSent,
  noteKey,
  noteMessage,
  noteTarget,
  parseSent,
  patchError,
  removal,
  repositories,
  runsHunk,
  sessionList,
  tabOf,
  userNotes,
} from "../mods/hunk-pane/helpers.js";

const API = "/home/me/work/api";
const WEB = "/home/me/work/web";

const repo = (path, name, from = null) => ({ from, key: `hunk-pane.repo.${name}`, name, path });

describe("liveSent", () => {
  test("keeps the notes of the open hunk terminals and forgets the rest", () => {
    const kept = "t1/s1/user:1@2026-11-05T22:37:41.253Z";
    const sent = new Set([kept, "t2/s2/user:1@2026-11-05T22:38:02.104Z"]);

    const live = liveSent(sent, new Set(["t1"]));

    assert.deepEqual(live, new Set([kept]));
  });
});

describe("noteKey", () => {
  test("keys a note by its terminal, session, id and creation", () => {
    const note = { noteId: "user:1", createdAt: "2026-11-05T22:37:41.253Z" };

    assert.equal(noteKey("t1", "s1", note), "t1/s1/user:1@2026-11-05T22:37:41.253Z");
  });

  test("gives an edited note a new key", () => {
    const note = {
      noteId: "user:1",
      createdAt: "2026-11-05T22:37:41.253Z",
      updatedAt: "2026-11-05T22:39:45.734Z",
    };

    assert.equal(noteKey("t1", "s1", note), "t1/s1/user:1@2026-11-05T22:39:45.734Z");
  });
});

describe("noteMessage", () => {
  test("names the note, the session, the real file and the reply command", () => {
    const note = { noteId: "user:1", newRange: [16, 16], body: "why?" };

    const message = noteMessage(note, "s1", `${API}/a.py`, "claude-code");

    assert.equal(
      message,
      `[hunk-pane] The user left note user:1 in the hunk pane of this tab (hunk session s1), on ${API}/a.py, new line 16: "why?". If the note asks for a change, make it. Then answer in the note's thread, in the user's language, not only here: hunk session comment add s1 --author claude-code --reply-to user:1 --summary "<your answer>"`,
    );
  });

  test("says the user edited a note that has been edited", () => {
    const note = { noteId: "user:1", newRange: [1, 1], body: "now", updatedAt: "2026-11-05" };

    const message = noteMessage(note, "s1", "f", "agent");

    assert.match(message, /^\[hunk-pane] The user edited note user:1 /);
  });

  test("keeps a note with line breaks on one line", () => {
    const note = { noteId: "user:1", newRange: [1, 1], body: "  first\n\nsecond\t " };

    const message = noteMessage(note, "s1", "f", "agent");

    assert.match(message, /: "first second"\. /);
    assert.doesNotMatch(message, /\n/);
  });

  test("leaves no double quote, backquote or $( for a shell to run", () => {
    const note = {
      noteId: "user:1",
      newRange: [1, 1],
      body: 'rename "total" & use `git stash`, not $(make clean)',
    };

    const message = noteMessage(note, "s1", "f", "agent");

    assert.match(message, /: "rename 'total' & use 'git stash', not \$ \(make clean\)"\. /);
  });

  test("names only the file for a note with no line range", () => {
    const note = { noteId: "user:1", body: "rename it" };

    const message = noteMessage(note, "s1", "/r/a/f.py", "agent");

    assert.match(message, / on \/r\/a\/f\.py: "rename it"\. /);
  });

  test("says old line for a note on a removed line", () => {
    const note = { noteId: "user:1", oldRange: [7, 7], body: "gone" };

    const message = noteMessage(note, "s1", "f", "agent");

    assert.match(message, / old line 7: /);
  });
});

describe("noteTarget", () => {
  const agents = [
    { address: "t1", status: "idle", app: "claude-code" },
    { address: "t2", status: "working", app: "claude-code" },
  ];

  test("sends a note to the agent that added its repository, with the real path", () => {
    const repos = [repo(API, "api", "t1")];

    const target = noteTarget(repos, { filePath: "api/src/a.py" }, agents);

    assert.deepEqual(target, { agent: agents[0], file: `${API}/src/a.py` });
  });

  test("does not take a repository whose name only starts the same", () => {
    const repos = [repo(API, "api", "t1"), repo("/home/me/api-v2", "api-v2", "t1")];

    const target = noteTarget(repos, { filePath: "api-v2/a.py" }, agents);

    assert.equal(target.file, "/home/me/api-v2/a.py");
  });

  test("takes the repository its first folder names, not one named like a folder inside it", () => {
    const repos = [repo(API, "api", "t1"), repo(WEB, "web-api", "t1")];

    const target = noteTarget(repos, { filePath: "api/web-api/a.py" }, agents);

    assert.equal(target.file, `${API}/web-api/a.py`);
  });

  test("holds a note whose repository was taken out", () => {
    const target = noteTarget([repo(WEB, "web", "t1")], { filePath: "api/a.py" }, agents);

    assert.equal(target, null);
  });

  test("holds a note whose repository was added from a terminal with no agent", () => {
    const target = noteTarget([repo(API, "api", "t9")], { filePath: "api/a.py" }, agents);

    assert.equal(target, null);
  });

  test("holds a note while its agent is working", () => {
    const target = noteTarget([repo(API, "api", "t2")], { filePath: "api/a.py" }, agents);

    assert.equal(target, null);
  });
});

describe("parseSent", () => {
  const first = "t1/s1/user:1@2026-11-05T22:37:41.253Z";
  const second = "t1/s1/user:2@2026-11-05T22:38:02.104Z";

  test("reads the stored list", () => {
    assert.deepEqual(parseSent(JSON.stringify([first, second])), new Set([first, second]));
  });

  for (const [name, text] of [
    ["nothing stored", null],
    ["not JSON", "{"],
    ["not a list", JSON.stringify({ [first]: true })],
  ]) {
    test(`starts empty with ${name}`, () => {
      assert.deepEqual(parseSent(text), new Set());
    });
  }

  test("drops entries that are not strings", () => {
    assert.deepEqual(parseSent(JSON.stringify([first, 3, null])), new Set([first]));
  });
});

describe("patchError", () => {
  test("names the shown repository and the exit code", () => {
    const result = { code: 3, stdout: "", stderr: "'/r/a' repository could NOT be diffed" };

    const error = patchError(result, repo("/r/a", "a"));

    assert.equal(error, "'a' repository could NOT be shown (exit 3)");
  });

  test("says patch.py did not run when it has no exit code", () => {
    const result = { code: null, stdout: "", stderr: "Error: took longer than 30 s" };

    const error = patchError(result, repo("/r/a", "a"));

    assert.equal(error, "'a' repository could NOT be shown (did not run)");
  });

  test("names the empty patch with no repository shown", () => {
    const error = patchError({ code: 1, stdout: "", stderr: "" }, null);

    assert.equal(error, "empty patch could NOT be written (exit 1)");
  });

  test("finds nothing wrong when patch.py succeeded", () => {
    const error = patchError({ code: 0, stdout: "", stderr: "" }, repo("/r/a", "a"));

    assert.equal(error, null);
  });
});

describe("removal", () => {
  const repos = [repo("/r/a", "a"), repo("/r/b", "b"), repo("/r/c", "c")];
  const hunk = (shown) => ({ repos, shown });

  test("closes the pane when the last repository goes", () => {
    const only = repo("/r/a", "a");

    assert.deepEqual(removal({ repos: [only], shown: only }, only), { close: true, labels: [] });
  });

  test("takes out a repository that is not shown, leaving the shown one", () => {
    assert.deepEqual(removal(hunk(repos[0]), repos[1]), {
      close: false,
      labels: ["hunk-pane.from.b=", "hunk-pane.repo.b="],
    });
  });

  test("shows the next repository when the shown one goes", () => {
    assert.deepEqual(removal(hunk(repos[1]), repos[1]).labels, [
      "hunk-pane.from.b=",
      "hunk-pane.repo.b=",
      "hunk-pane.selected=/r/c",
    ]);
  });

  test("shows the one before when the shown one was the last button", () => {
    assert.deepEqual(removal(hunk(repos[2]), repos[2]).labels, [
      "hunk-pane.from.c=",
      "hunk-pane.repo.c=",
      "hunk-pane.selected=/r/b",
    ]);
  });
});

describe("repositories", () => {
  test("sorts the repositories by name and keeps who added each one", () => {
    const labels = {
      "herdr.tab": "tb1",
      "hunk-pane.repo.h2": WEB,
      "hunk-pane.name.h2": "web",
      "hunk-pane.repo.h1": API,
      "hunk-pane.name.h1": "api",
      "hunk-pane.from.h1": "t1",
      "hunk-pane.selected": WEB,
    };

    const result = repositories(labels);

    assert.deepEqual(result, {
      repos: [
        { from: "t1", key: "hunk-pane.repo.h1", name: "api", path: API },
        { from: null, key: "hunk-pane.repo.h2", name: "web", path: WEB },
      ],
      shown: { from: null, key: "hunk-pane.repo.h2", name: "web", path: WEB },
    });
  });

  test("shows the first repository when the selected one is not among them", () => {
    const labels = {
      "hunk-pane.repo.h1": API,
      "hunk-pane.name.h1": "api",
      "hunk-pane.selected": "/gone",
    };

    assert.equal(repositories(labels).shown.path, API);
  });

  test("ignores the name a repository taken out leaves behind", () => {
    const labels = {
      "hunk-pane.repo.h1": API,
      "hunk-pane.name.h1": "api",
      "hunk-pane.name.h2": "web",
    };

    assert.deepEqual(repositories(labels).repos.map((r) => r.name), ["api"]);
  });

  test("leaves out a repository without a name", () => {
    const labels = {
      "hunk-pane.repo.h1": API,
      "hunk-pane.repo.h2": WEB,
      "hunk-pane.name.h2": "web",
    };

    assert.deepEqual(repositories(labels).repos.map((r) => r.path), [WEB]);
  });

  test("finds none in a terminal without repositories", () => {
    assert.deepEqual(repositories({ "hunk-pane.tab": "tb1" }), { repos: [], shown: null });
  });
});

describe("runsHunk", () => {
  const started = [
    "sh",
    "-c",
    "printf '\\033]2;%s\\007' \"$1\"; shift; exec hunk patch \"$@\"",
    "sh",
    "hunk-pane",
    "/home/me/.cache/hunk-pane/tb1.patch",
    "--watch",
  ];

  test("sees the hunk hunk-pane started on the tab's patch", () => {
    assert.equal(runsHunk({ argv: started }, "tb1"), true);
  });

  test("sees no hunk in the shell hc brings back after a reboot", () => {
    assert.equal(runsHunk({ argv: ["/bin/zsh", "-l"] }, "tb1"), false);
  });

  test("sees no hunk on another tab's patch", () => {
    assert.equal(runsHunk({ argv: started }, "tb12"), false);
  });

  test("sees no hunk in a terminal without argv", () => {
    assert.equal(runsHunk({}, "tb1"), false);
  });
});

describe("sessionList", () => {
  const readable = {
    sessionId: "s1",
    sourceLabel: "/c/tb1.patch",
    snapshot: { state: { reviewNotes: [] } },
  };
  const listed = (sessions) => ({ code: 0, stdout: JSON.stringify({ sessions }), stderr: "" });

  test("reads the sessions, with no error", () => {
    const result = sessionList(listed([readable]), ["/c/tb1.patch"]);

    assert.deepEqual(result, { sessions: [readable], error: null });
  });

  test("takes a hunk daemon that is down for no sessions, not an error", () => {
    assert.deepEqual(sessionList(listed([]), ["/c/tb1.patch"]), { sessions: [], error: null });
  });

  test("says hunk did not run when it has no exit code", () => {
    const result = sessionList({ code: null, stdout: "", stderr: "Error: no such file" }, []);

    assert.deepEqual(result, { sessions: [], error: "hunk notes could NOT be read (did not run)" });
  });

  test("names the exit code when hunk fails", () => {
    const failed = { code: 2, stdout: "", stderr: "unknown command" };

    const result = sessionList(failed, ["/c/tb1.patch"]);

    assert.deepEqual(result, { sessions: [], error: "hunk notes could NOT be read (exit 2)" });
  });

  for (const [name, stdout] of [
    ["not JSON", "{"],
    ["no session list", '{"items": []}'],
    ["null", "null"],
  ]) {
    test(`cannot read ${name}`, () => {
      const result = sessionList({ code: 0, stdout, stderr: "" }, ["/c/tb1.patch"]);

      const error = "hunk notes could NOT be read (unknown format)";
      assert.deepEqual(result, { sessions: [], error });
    });
  }

  test("cannot read the notes of a pane's session without the notes list", () => {
    const changed = { sessionId: "s1", sourceLabel: "/c/tb1.patch", snapshot: {} };

    const result = sessionList(listed([changed]), ["/c/tb1.patch"]);

    assert.equal(result.error, "hunk notes could NOT be read (unknown format)");
  });

  test("ignores a session of no pane", () => {
    const other = { sessionId: "s2", sourceLabel: "/elsewhere.patch", snapshot: {} };

    assert.equal(sessionList(listed([other]), ["/c/tb1.patch"]).error, null);
  });
});

describe("tabOf", () => {
  test("reads the tab of a hunk-pane terminal", () => {
    const tab = "tb75edd68f90a211c59de4ed7266627481";

    assert.equal(tabOf({ "hunk-pane.tab": tab }), tab);
  });

  for (const [name, labels] of [
    ["a tab id that names a path", { "hunk-pane.tab": "../x" }],
    ["an empty tab id", { "hunk-pane.tab": "" }],
    ["a terminal of no hunk pane", { "herdr.tab": "tb1" }],
    ["a terminal without labels", undefined],
  ]) {
    test(`finds none in ${name}`, () => {
      assert.equal(tabOf(labels), null);
    });
  }
});

describe("userNotes", () => {
  test("takes the user's notes from the session's snapshot, not the agents' replies", () => {
    const note = { noteId: "user:1", source: "user", body: "why?" };
    const reply = { noteId: "mcp:1", source: "agent", body: "because" };
    const session = { sessionId: "s1", snapshot: { state: { reviewNotes: [note, reply] } } };

    assert.deepEqual(userNotes(session), [note]);
  });

  test("finds none in a snapshot without notes", () => {
    assert.deepEqual(userNotes({ snapshot: { state: { reviewNotes: [] } } }), []);
  });

  test("cannot read a session without the notes list", () => {
    assert.equal(userNotes({ sessionId: "s1", snapshot: { state: {} } }), null);
  });
});
