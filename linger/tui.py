"""Curses UI. Every key operates on the view, never on a target process."""

from concurrent.futures import ThreadPoolExecutor
import curses
from pathlib import PurePosixPath
import time

from .models import Snapshot, age, memory


def clean(text: object) -> str:
    # Process paths and diagnostic strings are untrusted terminal content.
    return "".join(c if c.isprintable() else "?" for c in str(text))


def filtered(snapshot: Snapshot, scope: str, query: str, sort: str):
    rows = snapshot.findings
    if scope == "codex":
        rows = [f for f in rows if f.codex]
    elif scope == "review":
        rows = [f for f in rows if not f.protected and f.score >= 35]
    if query:
        query = query.casefold()
        rows = [f for f in rows if query in f"{f.process.pid} {f.process.executable} {f.label} {f.owner} {' '.join(f.owners)} {' '.join(f.reasons)}".casefold()]
    if sort == "memory":
        rows = sorted(rows, key=lambda f: (-f.process.rss_kib, f.process.pid))
    return rows


class UI:
    def __init__(self, source, scope="all", demo=False):
        self.source = source
        self.scope = scope
        self.sort = "stale"
        self.demo = demo
        self.snapshot: Snapshot | None = None
        self.cursor = 0
        self.tab = "processes"
        self.query = ""
        self.editing = False
        self.help = False
        self.detail_offset = 0
        self.message = ""

    def run(self, screen):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.keypad(True)
        screen.timeout(100)
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            for n, color in enumerate([curses.COLOR_CYAN, curses.COLOR_YELLOW, curses.COLOR_GREEN, curses.COLOR_MAGENTA], 1):
                curses.init_pair(n, color, bg)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.source)
            next_refresh = time.monotonic() + 5
            while True:
                if future is not None and future.done():
                    try:
                        selected = self.selected_id()
                        self.snapshot = future.result()
                        self.message = ""
                        self.restore_selection(selected)
                    except Exception as error:
                        self.message = f"Refresh failed ({type(error).__name__}); displayed snapshot is old."
                    future = None
                if future is None and time.monotonic() >= next_refresh:
                    future = executor.submit(self.source)
                    next_refresh = time.monotonic() + 5
                self.draw(screen, future is not None)
                key = screen.getch()
                if self.editing:
                    if key in (10, 13, 27):
                        self.editing = False
                        if key == 27:
                            self.query = ""
                    elif key in (curses.KEY_BACKSPACE, 127, 8):
                        self.query = self.query[:-1]
                    elif 32 <= key <= 126:
                        self.query += chr(key)
                    self.cursor = self.detail_offset = 0
                    continue
                if key in (ord("q"), 27):
                    if self.help:
                        self.help = False
                    else:
                        return
                elif key == ord("?"):
                    self.help = not self.help
                elif key in (9, ord("t")):
                    self.tab = "chats" if self.tab == "processes" else "processes"
                    self.cursor = self.detail_offset = 0
                elif key in (curses.KEY_DOWN, ord("j")):
                    self.cursor += 1
                    self.detail_offset = 0
                elif key in (curses.KEY_UP, ord("k")):
                    self.cursor = max(0, self.cursor - 1)
                    self.detail_offset = 0
                elif key in (curses.KEY_HOME, ord("g")):
                    self.cursor = 0
                elif key in (curses.KEY_END, ord("G")):
                    self.cursor = max(0, len(self.rows()) - 1)
                elif key == curses.KEY_NPAGE:
                    self.cursor += 10
                elif key == curses.KEY_PPAGE:
                    self.cursor = max(0, self.cursor - 10)
                elif key == ord("c"):
                    self.scope = {"all": "codex", "codex": "review", "review": "all"}[self.scope]
                    self.cursor = self.detail_offset = 0
                elif key == ord("s"):
                    self.sort = "memory" if self.sort == "stale" else "stale"
                    self.cursor = self.detail_offset = 0
                elif key == ord("/"):
                    self.editing = True
                elif key == ord("r"):
                    next_refresh = 0
                elif key == ord("o") and self.tab == "chats":
                    rows = self.rows()
                    if rows and self.cursor < len(rows):
                        self.query = rows[self.cursor].id
                        self.tab = "processes"
                        self.scope = "codex"
                        self.cursor = self.detail_offset = 0
                elif key == ord("]"):
                    self.detail_offset += 1
                elif key == ord("["):
                    self.detail_offset = max(0, self.detail_offset - 1)

    def rows(self):
        if self.snapshot is None:
            return []
        if self.tab == "chats":
            rows = self.snapshot.chats
            if self.query:
                q = self.query.casefold()
                rows = [c for c in rows if q in f"{c.id} {c.status} {c.evidence}".casefold()]
            return rows
        return filtered(self.snapshot, self.scope, self.query, self.sort)

    def selected_id(self):
        rows = self.rows()
        if rows and self.cursor < len(rows):
            row = rows[self.cursor]
            return row.id if self.tab == "chats" else row.process.identity
        return None

    def restore_selection(self, selected):
        for i, row in enumerate(self.rows()):
            identity = row.id if self.tab == "chats" else row.process.identity
            if identity == selected:
                self.cursor = i
                break

    def draw(self, screen, loading):
        screen.erase()
        h, w = screen.getmaxyx()

        def put(y, x, text, attr=0):
            if 0 <= y < h and x < w - 1:
                try:
                    screen.addnstr(y, x, clean(text), max(0, w - x - 1), attr)
                except curses.error:
                    pass

        cyan = curses.color_pair(1) if curses.has_colors() else curses.A_BOLD
        amber = curses.color_pair(2) if curses.has_colors() else curses.A_BOLD
        green = curses.color_pair(3) if curses.has_colors() else curses.A_NORMAL
        purple = curses.color_pair(4) if curses.has_colors() else curses.A_NORMAL
        if h < 16 or w < 76:
            put(0, 1, "LINGER / READ ONLY", cyan | curses.A_BOLD)
            put(2, 1, "Resize to at least 76 columns x 16 rows.")
            put(4, 1, "q quit  |  No process actions exist.")
            screen.refresh()
            return
        banner = "DEMO / SYNTHETIC" if self.demo else "READ ONLY / LIVE"
        put(0, 2, f"L I N G E R   {banner}", cyan | curses.A_BOLD)
        put(1, 2, "Find what is lingering. Understand what must stay.", curses.A_DIM)
        if self.help:
            lines = [
                "VIEW CONTROLS", "j/k or arrows: select     PgUp/PgDn: move ten rows",
                "Tab: processes / saved chats     c: all / Codex / review",
                "s: suspicion / memory sort     /: filter (also chat ID)     r: refresh",
                "o in saved chats: show processes with observed chat associations",
                "[ / ]: scroll explanation     g/G: first / last     q: quit",
                "", "INTERPRETING THE EVIDENCE",
                "Score is an explainable suspicion index, not a probability or kill approval.",
                "Low CPU, age, missing TTY, and launchd parentage can all be normal.",
                "Saved transcript != saved in-memory REPL/browser state.",
                "Active = observed start + fresh events; logs can lag live state.",
                "Launch context identifies command origin; it cannot prove exclusive MCP ownership.",
                "Registry associations are remembered by PID + birth. Missing records never mean stale.",
                "Ownership history persists in Linger's private cache; --no-cache disables it.",
                "No signals, termination, renicing, deletion, or settings changes exist.",
            ]
            for i, line in enumerate(lines):
                put(i + 3, 2, line, cyan if line in {"VIEW CONTROLS", "INTERPRETING THE EVIDENCE"} else 0)
            put(h - 1, 2, "? or Esc close help", cyan)
            screen.refresh()
            return
        if self.snapshot is None:
            put(4, 2, self.message or "Collecting a read-only snapshot...")
            put(h - 1, 2, "q quit", cyan)
            screen.refresh()
            return
        snap = self.snapshot
        total = sum(f.process.rss_kib for f in snap.findings)
        protected = sum(f.protected for f in snap.findings)
        put(2, 2, f"{len(snap.findings)} processes  |  {memory(total)} summed RSS*  |  {protected} protected  |  snapshot {age(time.time() - snap.at)} ago", green)
        put(3, 2, f"[{'PROCESSES' if self.tab == 'processes' else 'SAVED CHATS'}]  scope:{self.scope}  sort:{self.sort}  filter:{self.query or '-'}" + ("_" if self.editing else "") + ("  refreshing..." if loading else ""), cyan)
        put(4, 2, "Suspicion is not kill approval. *RSS includes shared pages; not reclaimable RAM.", curses.A_DIM)
        cache_status = next((warning for warning in snap.warnings if warning.startswith("Ownership cache")), "")
        put(5, 2, cache_status, curses.A_DIM)
        rows = self.rows()
        self.cursor = min(self.cursor, max(0, len(rows) - 1))
        detail_h = min(9, max(5, h // 3))
        detail_y = h - detail_h - 2
        capacity = max(1, detail_y - 7)
        start = max(0, self.cursor - capacity + 1)
        if self.tab == "processes":
            put(6, 2, "    PID  SCORE  CLASS          SOURCE     RSS     AGE   PROCESS", curses.A_BOLD)
        else:
            put(6, 2, "CHAT ID                                STATUS    LAST EVENT    TOOL STATE", curses.A_BOLD)
        for y, row in enumerate(rows[start:start + capacity], 7):
            selected = y - 7 + start == self.cursor
            attr = curses.A_REVERSE if selected else 0
            if self.tab == "processes":
                p = row.process
                attr |= purple if row.protected else amber if row.score >= 35 else 0
                name = PurePosixPath(p.executable).name
                source = "active" if row.registry_active else "cached" if row.registry_observed_at is not None else "launch" if row.owners else "-"
                text = f"{p.pid:7}  {row.score:5}  {row.label:14} {source:6} {memory(p.rss_kib):>7} {age(snap.at-p.started):>7}   {name}"
                if w >= 125:
                    text += "  / " + row.reasons[0]
            else:
                attr |= green if row.status == "ACTIVE" else purple
                text = f"{row.id}   {row.status:7}   {age(snap.at-row.updated):>8}    {row.tool_state}"
            put(y, 2, text, attr)
        if not rows:
            put(8, 2, "No matching rows. c changes scope; / changes filter.", curses.A_DIM)
        put(detail_y, 2, "EVIDENCE / " + (f"{self.cursor+1} of {len(rows)}" if rows else "none") + "  ([ ] scroll)", cyan | curses.A_BOLD)
        details = []
        if rows:
            row = rows[self.cursor]
            if self.tab == "processes":
                p = row.process
                details = [f"{p.executable}  |  PID {p.pid} / parent {p.ppid} / state {p.state} / TTY {p.tty}"]
                if row.codex:
                    details.append(f"Chat associations: {', '.join(row.owners) if row.owners else row.owner}.")
                    details.append(f"Source: {row.ownership_kind}.")
                    if row.registry_observed_at is not None:
                        details.append(f"Registry last seen {age(snap.at-row.registry_observed_at)} ago; " +
                                       ("execution observed in latest sample." if row.registry_active else "remembered, not currently observed executing."))
                    details.append("Unresolved and saved tool state stay protected. See saved chats with Tab.")
                details.extend(row.reasons)
            else:
                details = [row.id, row.evidence, "Saved-chat tool state: possible; preserve even when a turn has ended.", "Press o for observed process associations; missing helpers remain unknown."]
        import textwrap
        wrapped = [piece for line in details for piece in textwrap.wrap(clean(line), max(20, w - 5))]
        max_offset = max(0, len(wrapped) - (detail_h - 1))
        self.detail_offset = min(self.detail_offset, max_offset)
        for i, line in enumerate(wrapped[self.detail_offset:self.detail_offset + detail_h - 1], detail_y + 1):
            put(i, 2, line)
        notice = self.message or (snap.warnings[0] if snap.warnings else "No process actions exist in this prototype.")
        put(h - 2, 2, notice, amber)
        put(h - 1, 2, "j/k move  Tab chats  c scope  s sort  / filter  r refresh  ? help  q quit", cyan)
        screen.refresh()


def run(source, scope="all", demo=False):
    curses.wrapper(UI(source, scope, demo).run)
