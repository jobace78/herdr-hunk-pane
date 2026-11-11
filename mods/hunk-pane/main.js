// The strip under the panes for the hunk pane of the focused tab, the one the hunk-pane command
// (hunk_pane.py, beside this file) opens: a button per repository added to it, whose changes
// patch.py (beside it too) writes to the pane's patch, and the user's hunk notes handed to the
// agents. What it does, and the labels it shares with the command, are in the mod's RECIPE.md,
// which an install leaves out: https://github.com/jobace78/herdr-hunk-pane/blob/main/RECIPE.md
//
// The decisions that need no Herdr (order, the ×, where a note goes) are in helpers.js.

import * as fs from "fs/promises";
import { h_flex } from "gpui-base";
import { View, div, image, svg } from "gpui-kit";
import { agents, appHome, exec, focused, hc, home, panes, run, terminals, theme } from "herdr";
import {
  MOD,
  SELECTED_LABEL,
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
} from "./helpers.js";

const REFRESH_MS = 3000;
// the localStorage key of the notes sent, as noteKey writes them
const SENT_KEY = "sent";

const patchDir = () => `${home()}/.cache/hunk-pane`;

// Runs a program like exec, but a run that could not start or that Herdr stopped (its 30 s limit)
// comes back as a result with no exit code instead of a rejection: one failure then neither stops
// the rest of the refresh nor goes unseen.
async function execResult(command, args) {
  try {
    return await exec(command, args);
  } catch (error) {
    return { code: null, stdout: "", stderr: String(error) };
  }
}

// Every live hunk-pane terminal, with its repositories sorted by name, then path; `lost` when it no
// longer runs hunk (a shell hc brought back after a reboot or a crash).
function hunks() {
  return terminals()
    .filter((t) => t.state === "live" && tabOf(t.labels))
    .map((t) => {
      const { repos, shown } = repositories(t.labels);
      const tab = tabOf(t.labels);
      const patch = `${patchDir()}/${tab}.patch`;
      return { address: t.address, tab, patch, repos, shown, lost: !runsHunk(t, tab) };
    });
}

export default class HunkPane extends View {
  // init and render, the two View methods Herdr calls, open and close the class; the rest are in
  // ASCII order
  init(_props, cx) {
    // per hunk terminal, the shown repository written to its patch last
    this.written = new Map();
    // per hunk terminal, what went wrong when its patch was written last, shown in the strip
    this.errors = new Map();
    // why hunk's notes could not be read at the last look, shown in the strip; null when they could
    this.notesError = null;
    // the files with no pane at the last look: hunk-pane writes a new pane's empty patch just
    // before it starts its hunk, so a file is deleted only when it has no pane twice in a row
    this.orphans = new Set();
    this.sent = parseSent(localStorage.getItem(SENT_KEY));
    // the tab in view at the last refresh: a tab that comes into view gets its pane written at once
    this.tab = null;
    this.busy = false;
    cx.timer.every(REFRESH_MS, (cx) => this.refresh(cx, { full: true }));
  }

  // Closes a hunk pane, and its terminal with it.
  closePane(hunk) {
    const pane = panes().find((p) => p.terminal === hunk.address)?.pane;
    if (pane) {
      run("pane.close", { pane });
    } else {
      // the terminal is started with --rm, so hc kill ends it and then answers not_found
      hc(["kill", hunk.address]);
    }
  }

  // Hands each user note not sent yet to the agent of the terminal that added its repository, when
  // that agent is not working (hc send refuses a blocked one).
  async notes(cx, running) {
    if (running.length === 0) {
      return;
    }
    const list = await execResult("hunk", ["session", "list", "--json"]);
    const { sessions, error } = sessionList(list, running.map((h) => h.patch));
    if (error !== this.notesError) {
      this.notesError = error;
      if (error) {
        console.log(`${MOD}: ${error}: ${list.stderr}`);
      }
      cx.notify();
    }
    const liveAgents = agents();
    for (const h of running) {
      const hunkSession = sessions.find((s) => s.sourceLabel === h.patch);
      if (!hunkSession) {
        continue;
      }
      const sessionId = hunkSession.sessionId;
      // the session list carries each session's notes: no hunk call per pane
      for (const note of userNotes(hunkSession) ?? []) {
        const key = noteKey(h.address, sessionId, note);
        const target = noteTarget(h.repos, note, liveAgents);
        if (this.sent.has(key) || !target) {
          continue;
        }
        const message = noteMessage(note, sessionId, target.file, target.agent.app ?? "agent");
        const result = await hc(["send", target.agent.address, message]);
        if (result.code === 0) {
          this.save(new Set([...this.sent, key]));
        }
      }
    }
  }

  // Forgets what was written and the notes sent for the hunk terminals gone, and deletes the files
  // that had no pane at two full refreshes in a row.
  async prune(running) {
    const liveTabs = new Set(running.map((h) => h.tab));
    const gone = [...this.written.keys()].filter((a) => !running.some((h) => h.address === a));
    for (const address of gone) {
      this.written.delete(address);
      this.errors.delete(address);
    }
    const live = liveSent(this.sent, new Set(running.map((h) => h.address)));
    if (live.size !== this.sent.size) {
      this.save(live);
    }
    const files = await fs.readdir(patchDir()).catch(() => []);
    // a pane's files are its patch and patch.py's private index of each repository it showed (and
    // its lock, when a run was stopped), all named after its tab: <tab>.patch, <tab>.<md5>.index
    const orphans = new Set(
      files.filter((f) => /\.(index|lock|patch)$/.test(f) && !liveTabs.has(f.split(".")[0])),
    );
    for (const file of [...orphans].filter((f) => this.orphans.has(f))) {
      await execResult("rm", ["-f", `${patchDir()}/${file}`]);
      orphans.delete(file);
    }
    this.orphans = orphans;
  }

  // Writes the patch of the focused tab's pane when its shown repository changed since it was
  // written, or the tab just came into view; panes in other tabs, which nobody sees, wait for
  // theirs. A full refresh writes it anyway, closes the panes that lost their hunk, deletes the
  // patches of panes gone and hands the new notes to their agents. render passes the hunk terminals
  // it read.
  refresh(cx, { full = false, hunkTerminals = hunks() } = {}) {
    if (this.busy) {
      return;
    }
    const running = hunkTerminals.filter((h) => !h.lost);
    const tab = focused()?.tab;
    if (tab !== this.tab) {
      this.tab = tab;
      this.written.delete(running.find((h) => h.tab === tab)?.address);
    }
    const toWrite = running.filter(
      (h) => h.tab === tab && (full || this.written.get(h.address) !== (h.shown?.path ?? "")),
    );
    if (toWrite.length === 0 && !full) {
      return;
    }
    this.busy = true;
    cx.spawn(async (cx) => {
      try {
        for (const h of toWrite) {
          const repo = h.shown ? [h.shown.name, h.shown.path] : [];
          const script = `${appHome()}/mods/${MOD}/patch.py`;
          // marked before the run: a failed run must not make the refresh after it retry at once
          this.written.set(h.address, h.shown?.path ?? "");
          const result = await execResult("python3", [script, h.patch, ...repo]);
          this.report(cx, h.address, patchError(result, h.shown), result.stderr);
        }
        if (full) {
          for (const h of hunkTerminals.filter((h) => h.lost)) {
            this.closePane(h);
          }
          await this.prune(running);
          await this.notes(cx, running);
        }
      } catch (error) {
        console.log(`${MOD}: refresh could NOT finish: ${error}`);
      } finally {
        this.busy = false;
      }
      // a click while the patches were written waited for them: write the new ones now
      this.refresh(cx);
    });
  }

  // Takes a repository out; the shown one passes to the next button, or the one before the last.
  remove(hunk, repo) {
    const { close, labels } = removal(hunk, repo);
    if (close) {
      this.closePane(hunk);
    } else {
      hc(["label", hunk.address, ...labels]);
    }
  }

  // Keeps the error of a hunk terminal's last patch, and redraws the strip when it changes; what
  // patch.py wrote to stderr goes to the log.
  report(cx, address, error, stderr) {
    if ((this.errors.get(address) ?? null) === error) {
      return;
    }
    if (error) {
      this.errors.set(address, error);
      console.log(`${MOD}: ${error}: ${stderr}`);
    } else {
      this.errors.delete(address);
    }
    cx.notify();
  }

  // Keeps the notes sent, here and in localStorage, as soon as they change: a failure later in the
  // same pass must not send a note twice.
  save(sent) {
    this.sent = sent;
    localStorage.setItem(SENT_KEY, JSON.stringify([...sent]));
  }

  render(cx) {
    const t = theme();
    const tab = focused()?.tab;
    const hunkTerminals = hunks();
    const hunk = hunkTerminals.find((h) => h.tab === tab && !h.lost);
    this.refresh(cx, { hunkTerminals });
    // image, not svg: svg draws in the text color alone, and the icon keeps its green and red
    const root = h_flex().id("root").size_full().min_w(0).px(8).gap(8).items_center().bg(t.chrome)
      .border_t(1).border_color(t.border).text_size(14)
      .child(div().id("icon").flex_none().tooltip(MOD)
        .child(image(`mods/${MOD}/hunk_pane.svg`).size(14)));
    if (!hunk || hunk.repos.length === 0) {
      return root;
    }
    const error = this.errors.get(hunk.address) ?? this.notesError;
    return root.child(h_flex().id("repos").flex_1().min_w(0).gap(6).items_center().overflow_x_scroll()
      .children(hunk.repos.map((r, i) => {
        const pressed = r.path === hunk.shown?.path;
        return h_flex().id(`repo-${i}`).h(22).pl(12).pr(5).gap(7).flex_none().items_center().rounded(6)
          .border(1).border_color(t.border)
          .bg(pressed ? t.selected : t.chrome).text_color(pressed ? t.foreground : t.dim)
          .hover((s) => s.bg(t.hover).text_color(t.foreground))
          .on_click((_e, cx) => {
            cx.stop_propagation();
            if (!pressed) {
              hc(["label", hunk.address, `${SELECTED_LABEL}=${r.path}`]);
            }
          })
          .child(div().child(r.name))
          .child(div().id(`close-${i}`).p(3).rounded(4).hover((s) => s.bg(t.raised))
            .on_click((_e, cx) => {
              cx.stop_propagation();
              this.remove(hunk, r);
            })
            .child(svg(`mods/${MOD}/close.svg`).size(13)));
      })))
      .when(error, (e) => e.child(div().flex_none().text_size(12).text_color(t.dim).child(error)));
  }
}
