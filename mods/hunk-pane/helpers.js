// The decisions of the hunk-pane strip that need no Herdr: what its labels, a note and the agents
// say. Kept apart from main.js, whose Herdr modules only load inside Herdr, so node --test can load
// this one (tests/helpers.test.js).

const FROM_PREFIX = "hunk-pane.from.";
export const MOD = "hunk-pane";
const NAME_PREFIX = "hunk-pane.name.";
const REPOSITORY_PREFIX = "hunk-pane.repo.";
export const SELECTED_LABEL = "hunk-pane.selected";
// any process can set labels and the tab id names a file, so anything but a plain word is ignored
const TAB_ID = /^[A-Za-z0-9_-]+$/;
const TAB_LABEL = "hunk-pane.tab";

// A result's exit code as the strip says it: "exit <code>", or "did not run" without one.
function exitOf(result) {
  return result.code === null ? "did not run" : `exit ${result.code}`;
}

// The notes sent whose hunk terminal is still open: a note's key starts with that terminal, so it
// is forgotten when its pane closes, never while the hunk daemon is down or restarting.
export function liveSent(sent, terminals) {
  return new Set([...sent].filter((key) => terminals.has(key.split("/")[0])));
}

// The key a sent note is kept under: its hunk terminal, session and id, and its version (hunk keeps
// a note's id when the user edits it and stamps updatedAt), so an edited note is sent again.
export function noteKey(terminal, session, note) {
  return `${terminal}/${session}/${note.noteId}@${note.updatedAt ?? note.createdAt}`;
}

// The message that hands a user's note to an agent, or the edit of one: one line, since hc send
// submits it with Enter.
export function noteMessage(note, session, file, author) {
  const range = note.newRange ?? note.oldRange;
  // a note with no line range names only the file
  const where = range ? `${file}, ${note.newRange ? "new" : "old"} line ${range[0]}` : file;
  // hc send waits for the user to pause, so a note can land in the shell under an agent that quit
  // meanwhile: with no double quote (which would end the quoted body), no backquote and no "$(",
  // that shell runs nothing from it
  const body = note.body.replace(/\s+/g, " ").trim().replace(/["`]/g, "'").replace(/\$\(/g, "$ (");
  const did = note.updatedAt ? "edited" : "left";
  return `[${MOD}] The user ${did} note ${note.noteId} in the hunk pane of this tab (hunk session `
    + `${session}), on ${where}: "${body}". If the note asks for a change, `
    + "make it. Then answer in the note's thread, in the user's language, not only here: "
    + `hunk session comment add ${session} --author ${author} --reply-to ${note.noteId} --summary "<your answer>"`;
}

// The agent a user's note goes to and the note's real path; null while it cannot go: its
// repository was taken out, was added from a terminal with no agent, or that agent is working.
// The note's first folder is its repository's name, which hunk-pane keeps unique and without slash.
export function noteTarget(repos, note, agents) {
  const repo = repos.find((r) => note.filePath.split("/")[0] === r.name);
  const agent = agents.find((a) => a.address === repo?.from);
  if (!agent || agent.status === "working") {
    return null;
  }
  return { agent, file: `${repo.path}${note.filePath.slice(repo.name.length)}` };
}

// The notes sent before, from what localStorage holds; none when that is not a JSON list.
export function parseSent(text) {
  try {
    const sent = JSON.parse(text ?? "[]");
    return new Set(Array.isArray(sent) ? sent.filter((key) => typeof key === "string") : []);
  } catch {
    return new Set();
  }
}

// What the strip says when patch.py could not write the shown repository's patch, with its exit
// code (1: the patch file, 3: the repository, 4: over 8 MiB; see patch.py), or "did not run" when
// it has none (it could not start, or Herdr stopped it); null when it could.
export function patchError(result, repo) {
  if (result.code === 0) {
    return null;
  }
  // a pane with no repository shows no strip line, but the error still goes to the log
  const what = repo
    ? `'${repo.name}' repository could NOT be shown`
    : "empty patch could NOT be written";
  return `${what} (${exitOf(result)})`;
}

// What a repository's × does: closes the pane when it is the last one; else the labels that take it
// out and, when it was the shown one, show the next button (the one before, when it was the last).
// Its name label stays: hunk-pane never gives that name to another repository of the pane, so a
// note still waiting on it never reaches another one.
export function removal(hunk, repo) {
  const rest = hunk.repos.filter((r) => r.key !== repo.key);
  if (rest.length === 0) {
    return { close: true, labels: [] };
  }
  const hash = repo.key.slice(REPOSITORY_PREFIX.length);
  const labels = [`${FROM_PREFIX}${hash}=`, `${repo.key}=`];
  if (repo.path === hunk.shown?.path) {
    const index = hunk.repos.findIndex((r) => r.key === repo.key);
    labels.push(`${SELECTED_LABEL}=${rest[Math.min(index, rest.length - 1)].path}`);
  }
  return { close: false, labels };
}

// A hunk terminal's repositories, from its labels, sorted by name and then path, and the shown one:
// the selected repository, else the first. One without a name is left out: hunk-pane sets both
// labels at once.
export function repositories(labels) {
  const repos = Object.keys(labels)
    .filter((k) => k.startsWith(REPOSITORY_PREFIX))
    .map((key) => {
      const hash = key.slice(REPOSITORY_PREFIX.length);
      const from = labels[`${FROM_PREFIX}${hash}`] ?? null;
      return { from, key, name: labels[`${NAME_PREFIX}${hash}`], path: labels[key] };
    })
    .filter((r) => r.name)
    .sort((a, b) => a.name.localeCompare(b.name) || a.path.localeCompare(b.path));
  const shown = repos.find((r) => r.path === labels[SELECTED_LABEL]) ?? repos[0] ?? null;
  return { repos, shown };
}

// Whether a hunk-pane terminal still runs the hunk hunk-pane started on its tab's patch. Herdr
// keeps a pane's terminal, so after a reboot or a crash hc brings it back as a login shell, with
// the same labels and another argv.
export function runsHunk(terminal, tab) {
  return Array.isArray(terminal.argv) && terminal.argv.some((a) => a.endsWith(`/${tab}.patch`));
}

// The hunk sessions from what `hunk session list --json` returned, and why the panes' notes cannot
// be read, or null: its exit code, output that is not its JSON, or a pane's session whose notes
// userNotes cannot read. A hunk daemon that is down is no error: it lists no sessions, with code 0.
export function sessionList(list, patches) {
  const error = (reason) => `hunk notes could NOT be read (${reason})`;
  if (list.code !== 0) {
    return { sessions: [], error: error(exitOf(list)) };
  }
  let sessions;
  try {
    sessions = JSON.parse(list.stdout).sessions;
  } catch {
    sessions = null;
  }
  if (!Array.isArray(sessions)) {
    return { sessions: [], error: error("unknown format") };
  }
  const unreadable = sessions.some((s) => patches.includes(s.sourceLabel) && userNotes(s) === null);
  return { sessions, error: unreadable ? error("unknown format") : null };
}

// The tab a hunk-pane terminal belongs to, from its labels: a plain word, since it names the
// patch file; null for any other terminal, or a label any process could have set to a path.
export function tabOf(labels) {
  const tab = labels?.[TAB_LABEL];
  return typeof tab === "string" && TAB_ID.test(tab) ? tab : null;
}

// The notes the user left in a hunk session, from its entry in `hunk session list --json`: its
// snapshot's reviewNotes (hunk 0.24.1), which also hold the agents' replies. null when the session
// has no such list: a hunk release that changed it.
export function userNotes(session) {
  const notes = session.snapshot?.state?.reviewNotes;
  return Array.isArray(notes) ? notes.filter((note) => note.source === "user") : null;
}
