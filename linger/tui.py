"""Read-only, character-grid process inspection with bounded display history."""

from concurrent.futures import ThreadPoolExecutor
import curses
from pathlib import PurePosixPath
import sys
import time

from .history import History, sparkline
from .models import Snapshot, age, memory
from .scoring import score_factors
from .terminal import Canvas, cells, clean, clip, fit, palette, wrap


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
        return sorted(rows, key=lambda f: (-f.process.rss_kib, f.process.pid))
    # Bindings can add protection after generic classification has sorted rows.
    return sorted(rows, key=lambda f: (f.protected, -f.score, -f.process.rss_kib, f.process.pid))


def source_label(finding):
    if finding.registry_active:
        return "active"
    if finding.registry_observed_at is not None:
        return "cached"
    if finding.tracking:
        return "tracked"
    if finding.service:
        return "launchd"
    return "launch" if finding.owners else "-"


def row_tone(finding):
    if finding.label in {"CODEX UNKNOWN", "TRACK UNKNOWN"}:
        return "unknown"
    if finding.registry_active and finding.label == "CODEX ACTIVE":
        return "mint"
    if finding.protected:
        return "purple"
    return "amber" if finding.score >= 35 else "normal"


def process_columns(width, pid_width=6):
    columns = [("pid", "PID", pid_width, True), ("name", "PROCESS", 0, False),
               ("label", "CLASS", 14, False), ("score", "SCORE", 5, True)]
    if width >= 90:
        columns.append(("cpu", "CPU%", 6, True))
    columns.extend([("rss", "RSS", 7, True), ("age", "AGE", 6, True)])
    if width >= 84:
        columns.append(("source", "SOURCE", 7, False))
    if width >= 110:
        columns.append(("activity", "ACTIVITY", 14, False))
    available = width - 6
    name_width = available - sum(c[2] for c in columns) - 2 * (len(columns) - 1)
    return [(key, title, name_width if key == "name" else size, right)
            for key, title, size, right in columns]


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
        self.help_offset = 0
        self.detail_offset = 0
        self.message = ""
        self.inspector = True
        self.show_history = False
        self.history = History()
        self.unicode = True
        self.styles = dict.fromkeys(("normal", "muted", "unknown", "cyan", "amber", "mint", "purple"), 0)
        self.styles["selected"] = curses.A_REVERSE
        self.page_size = 10
        self._layout = None

    def accept_snapshot(self, snapshot, now=None):
        first = self.snapshot is None
        selected = self.selected_id()
        self.snapshot = snapshot
        self.history.observe(snapshot, now)
        self.message = ""
        self.restore_selection(selected)
        # Start the synthetic tour on a currently implemented review case.
        if first and snapshot.demo and self.tab == "processes":
            self.cursor = next((i for i, row in enumerate(self.rows()) if row.label == "REVIEW"), 0)

    def run(self, screen):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.keypad(True)
        screen.timeout(100)
        self.styles = palette(screen)
        self.unicode = "UTF" in str(getattr(screen, "encoding", None) or sys.stdout.encoding).upper()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.source)
            next_refresh = time.monotonic() + 5
            while True:
                if future is not None and future.done():
                    try:
                        self.accept_snapshot(future.result())
                    except Exception as error:
                        self.message = f"Refresh failed ({type(error).__name__}); displayed snapshot is old."
                    future = None
                if future is None and time.monotonic() >= next_refresh:
                    future = executor.submit(self.source)
                    next_refresh = time.monotonic() + 5
                self.draw(screen, future is not None)
                action = self.handle_key(screen.getch())
                if action == "quit":
                    return
                if action == "refresh":
                    next_refresh = 0

    def handle_key(self, key):
        if self.editing:
            if key in (10, 13, 27):
                self.editing = False
                if key == 27:
                    self.query = ""
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self.query = self.query[:-1]
            elif 32 <= key <= 126 and len(self.query) < 1000:
                self.query += chr(key)
            self.cursor = self.detail_offset = 0
            return
        if self.help:
            if key in (ord("?"), ord("q"), 27):
                self.help = False
            elif key in (ord("j"), curses.KEY_DOWN, ord("]"), curses.KEY_NPAGE):
                self.help_offset += self.page_size if key == curses.KEY_NPAGE else 1
            elif key in (ord("k"), curses.KEY_UP, ord("["), curses.KEY_PPAGE):
                self.help_offset = max(0, self.help_offset - (self.page_size if key == curses.KEY_PPAGE else 1))
            return
        if key in (ord("q"), 27):
            return "quit"
        if key == ord("?"):
            self.help = True
        elif key in (9, ord("t")):
            self.tab = "chats" if self.tab == "processes" else "processes"
            self.cursor = self.detail_offset = 0
        elif key in (curses.KEY_DOWN, ord("j")):
            self.cursor = min(self.cursor + 1, max(0, len(self.rows()) - 1))
            self.detail_offset = 0
        elif key in (curses.KEY_UP, ord("k")):
            self.cursor = max(0, self.cursor - 1)
            self.detail_offset = 0
        elif key in (curses.KEY_HOME, ord("g")):
            self.cursor = self.detail_offset = 0
        elif key in (curses.KEY_END, ord("G")):
            self.cursor = max(0, len(self.rows()) - 1)
            self.detail_offset = 0
        elif key == curses.KEY_NPAGE:
            self.cursor = min(self.cursor + self.page_size, max(0, len(self.rows()) - 1))
            self.detail_offset = 0
        elif key == curses.KEY_PPAGE:
            self.cursor = max(0, self.cursor - self.page_size)
            self.detail_offset = 0
        elif key == ord("c"):
            self.scope = {"all": "codex", "codex": "review", "review": "all"}[self.scope]
            self.cursor = self.detail_offset = 0
        elif key == ord("s"):
            selected = self.selected_id()
            self.sort = "memory" if self.sort == "stale" else "stale"
            self.restore_selection(selected)
        elif key == ord("/"):
            self.editing = True
        elif key == ord("r"):
            return "refresh"
        elif key == ord("i"):
            self.inspector = not self.inspector
        elif key == ord("h"):
            self.show_history = not self.show_history
        elif key == ord("o") and self.tab == "chats":
            rows = self.rows()
            if rows:
                self.query = rows[self.cursor].id
                self.tab, self.scope = "processes", "codex"
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
        self.cursor = 0
        for i, row in enumerate(self.rows()):
            identity = row.id if self.tab == "chats" else row.process.identity
            if identity == selected:
                self.cursor = i
                break

    def activity(self, finding):
        cpu = self.history.cpu(finding)
        if cpu is None:
            return "sampling"
        points = self.history.points(finding)
        cpus = [p.cpu_percent for p in points if p.cpu_percent is not None]
        if len(cpus) > 1 and max(cpus) - min(cpus) > 0.1:
            return sparkline(cpus, 12, self.unicode)
        if cpu == 0 and finding.process.sampled:
            return "idle " + age(finding.process.observed_idle)
        return "steady"

    def draw(self, screen, loading=False):
        c = Canvas(screen, self.styles, self.unicode)
        h, w = c.height, c.width
        layout = (h, w, self.inspector, self.show_history, self.tab, self.help)
        # Repaint changed panel boundaries rather than relying on terminal
        # insert/delete-line optimizations to move the previous layout.
        if layout != self._layout and hasattr(screen, "clear"):
            screen.clear()
        else:
            screen.erase()
        self._layout = layout
        if h < 16 or w < 76:
            c.put(0, 1, "LINGER / READ ONLY", "cyan", bold=True)
            c.put(2, 1, "Resize to at least 76 columns x 16 rows.")
            c.put(4, 1, "q quit")
            screen.refresh()
            return
        snap = self.snapshot
        demo = snap.demo if snap else self.demo
        banner = "DEMO / SYNTHETIC" if demo else "READ ONLY / LIVE"
        brand = f"L I N G E R / {banner}" if w >= 100 else f"LINGER / {banner}"
        c.put(0, 2, brand, "cyan", bold=True)
        if snap:
            review = sum(f.label == "REVIEW" for f in snap.findings)
            protected = sum(f.protected for f in snap.findings)
            summary = f"{len(snap.findings)} processes | {review} review | {protected} protected"
            if cells(brand) + cells(summary) + 6 < w:
                x = w - cells(summary) - 2
                c.put(0, x, summary, "cyan")
                prefix = f"{len(snap.findings)} processes | "
                c.put(0, x + cells(prefix), f"{review} review", "amber")
        c.rule(1, 1, w - 2)
        if self.help:
            self._draw_help(c)
            screen.refresh()
            return
        nav = "[ Processes ]  Chats" if self.tab == "processes" else "Processes  [ Chats ]"
        if snap:
            nav += f" ({len(snap.chats)})"
        c.put(2, 2, nav, "cyan")
        sort = "score" if self.sort == "stale" else "RSS"
        query = self.query + ("_" if self.editing else "") if self.query or self.editing else "-"
        controls = f"scope: {self.scope}  sort: {sort}  filter: {query}"
        available = max(0, w - cells(nav) - 8)
        controls = clip(controls, available)
        c.put(2, max(cells(nav) + 5, w - cells(controls) - 2), controls, "cyan")
        if snap is None:
            c.frame(3, h - 7, "processes")
            c.put(5, 3, self.message or "Collecting a read-only snapshot...", "muted")
            c.put(h - 1, 2, "q quit", "cyan")
            screen.refresh()
            return
        rows = self.rows()
        self.cursor = min(self.cursor, max(0, len(rows) - 1))
        detail_h = 7 if self.inspector and h >= 22 else 0
        history_h = 7 if self.show_history and self.tab == "processes" and h >= 28 else 0
        table_h = h - 6 - detail_h - history_h - bool(detail_h) - bool(history_h)
        self.page_size = max(1, table_h - 4)
        self._draw_table(c, rows, 3, table_h)
        y = 3 + table_h
        if detail_h:
            self._draw_evidence(c, rows, y + 1, detail_h)
            y += detail_h + 1
        if history_h:
            self._draw_history(c, rows, y + 1, history_h)
        self._draw_status(c, rows, loading, bool(history_h))
        screen.refresh()

    def _draw_table(self, c, rows, top, height):
        capacity = max(1, height - 4)
        start = min(max(0, self.cursor - capacity + 1), max(0, len(rows) - capacity))
        end = min(start + capacity, len(rows))
        title = "processes" if self.tab == "processes" else "saved chat associations"
        caption = f"{start + 1 if rows else 0}-{end} / {len(rows)}"
        c.frame(top, height, title, "cyan" if self.tab == "processes" else "purple", caption)
        if self.tab == "processes":
            pid_width = max([6] + [len(str(f.process.pid)) for f in self.snapshot.findings])
            columns = process_columns(c.width, pid_width)
        else:
            columns = [("id", "CHAT ID", 36, False), ("status", "STATUS", 8, False),
                       ("count", "PROCS", 5, True), ("updated", "LAST EVENT", 10, True)]
            if c.width >= 100:
                columns.append(("state", "TOOL STATE", c.width - 73, False))
        header = "  ".join(fit(title, width, right) for _, title, width, right in columns)
        c.put(top + 1, 3, header, bold=True)
        c.rule(top + 2, 2, c.width - 4, "muted")
        for i, row in enumerate(rows[start:end], start):
            yy = top + 3 + i - start
            if self.tab == "processes":
                p = row.process
                cpu = self.history.cpu(row)
                cpu_text = "--" if cpu is None else f"{cpu:.1f}" if cpu < 1000 else f"{cpu:.0f}"
                values = dict(pid=p.pid, name=PurePosixPath(p.executable).name, label=row.label,
                              score=row.score, cpu=cpu_text, rss=memory(p.rss_kib),
                              age=age(self.snapshot.at - p.started), source=source_label(row),
                              activity=self.activity(row))
                tone = row_tone(row)
            else:
                count = sum(row.id.lower() in f.owners for f in self.snapshot.findings)
                values = dict(id=row.id, status=row.status, count=count,
                              updated=age(self.snapshot.at - row.updated), state=row.tool_state)
                tone = "mint" if row.status == "ACTIVE" else "unknown" if row.status == "UNKNOWN" else "purple"
            if i == self.cursor:
                tone = "selected"
                c.put(yy, 2, " " * (c.width - 4), tone)
            text = "  ".join(fit(values[key], width, right) for key, _, width, right in columns)
            c.put(yy, 3, text, tone)
        if not rows:
            c.put(top + 3, 3, "No matching rows. c changes scope; / changes filter.", "muted")

    def _evidence(self, row):
        """Return factual lines with optional weights; full reasons stay scrollable."""
        p = row.process
        lines = []
        if row.tracking and not row.protected and row.label == "REVIEW":
            lines.append(("Temporary owner exited successfully; observed helper remains.", None))
            lines.append(("Retained state and dependencies unknown; review before stopping.", None))
        elif not row.protected and row.label in {"REVIEW", "OBSERVED"}:
            factors = score_factors(p, self.snapshot.at, {f.process.pid for f in self.snapshot.findings})
            if sum(f.weight for f in factors) == row.score:
                lines = [(f.label, f.weight) for f in factors]
                if not lines:
                    lines.append(("No review factors observed.", None))
                lines.append(("Abandonment unproven; may be waiting for work.", None))
        elif row.label == "CODEX UNKNOWN":
            lines.append(("Ownership unresolved; possible tool state is still protected.", None))
        elif row.protected:
            if row.registry_active:
                lines.append(("Execution observed in latest registry sample.", None))
            elif row.label == "CODEX SAVED":
                lines.append(("Saved chat association; preserve possible live tool state.", None))
            elif row.label == "CODEX MULTI":
                lines.append(("Multiple chat associations; preserve shared tool state.", None))
            else:
                lines.append((row.reasons[0] if row.reasons else "Protected infrastructure.", None))
        if row.label == "ZOMBIE":
            lines.append(("Already exited; the parent must reap this process.", None))
        lines.extend((reason, None) for reason in row.reasons)
        context = [("Parent", self._parent_name(p.ppid)),
                   ("Chat association", ", ".join(row.owners) if row.owners else "not observed"),
                   ("Source", row.ownership_kind if row.owners else "process snapshot"),
                   ("Executable", p.executable)]
        if row.birth_us is not None:
            context.append(("Native birth (us)", str(row.birth_us)))
        if row.registry_observed_at is not None:
            context.append(("Registry last seen", age(self.snapshot.at - row.registry_observed_at) + " ago"))
        if row.service:
            context.extend([("Launchd service", row.service.label), ("Restart policy", row.service.policy)])
        for tracked in row.tracking:
            context.append(("Tracked run", f"{tracked.id} / {'temporary' if tracked.temporary else 'persistent'} / {tracked.status} / exit {tracked.exit_code}"))
        return lines, context

    def _parent_name(self, pid):
        if pid == 1:
            return "launchd (1)"
        parent = next((f.process for f in self.snapshot.findings if f.process.pid == pid), None)
        return f"{PurePosixPath(parent.executable).name} ({pid})" if parent else f"{pid} (not in snapshot)"

    def _draw_evidence(self, c, rows, top, height):
        if not rows:
            c.frame(top, height, "evidence", "amber")
            c.put(top + 1, 3, "Select a row to inspect its evidence.", "muted")
            return
        row = rows[self.cursor]
        if self.tab == "chats":
            self._draw_chat_evidence(c, row, top, height)
            return
        p = row.process
        name = PurePosixPath(p.executable).name
        title = f"evidence / PID {p.pid} / {name} / {row.score} {row.label}"
        c.frame(top, height, title, "amber", "weights, not probabilities | [ ] details")
        split = int(c.width * .60) if c.width >= 100 else None
        left_width = split - 5 if split else c.width - 6
        lines, context = self._evidence(row)
        wrapped = []
        summary_count = max(0, len(lines) - len(row.reasons))
        summary_height = 0
        for index, (text, weight) in enumerate(lines):
            if weight is not None:
                wrapped.append((text, weight))
            else:
                wrapped.extend((piece, None) for piece in wrap(text, left_width))
            if index < summary_count:
                summary_height = len(wrapped)
        if not split:
            for key, value in context:
                wrapped.extend((piece, None) for piece in wrap(f"{key}: {value}", left_width))
        metadata = []
        if split:
            right_width = c.width - split - 5
            for key, value in context:
                metadata.extend(wrap(f"{key}: {value}", right_width))
        inner = height - 2
        self.detail_offset = min(self.detail_offset, max(0, max(len(wrapped), len(metadata)) - inner))
        visible = wrapped[self.detail_offset:self.detail_offset + inner]
        if self.detail_offset == 0 and summary_height:
            visible = visible[:summary_height]
        for index, (text, weight) in enumerate(visible):
            yy = top + 1 + index
            if weight is None:
                c.put(yy, 3, text, "amber" if text.startswith("Abandonment") else "normal", left_width)
            else:
                bar_width = min(24, max(0, left_width - 30))
                filled = round(min(weight, 40) / 40 * bar_width)
                bar = ("█" if self.unicode else "#") * filled
                bar += ("░" if self.unicode else ".") * (bar_width - filled)
                c.put(yy, 3, f"{fit(text, 22)} {weight:+4d}  {bar}", "amber", left_width)
        if split:
            for yy in range(top + 1, top + height - 1):
                c.put(yy, split, "│" if self.unicode else "|", "muted")
            offset = min(self.detail_offset, max(0, len(metadata) - inner))
            for index, text in enumerate(metadata[offset:offset + inner]):
                c.put(top + 1 + index, split + 3, text, "normal", right_width)

    def _draw_chat_evidence(self, c, chat, top, height):
        c.frame(top, height, f"chat evidence / {chat.status}", "purple", "[ ] details / o processes")
        associations = [f for f in self.snapshot.findings if chat.id.lower() in f.owners]
        lines = [chat.id, chat.evidence, "Saved tool state may still be live; transcript is not a state backup."]
        if associations:
            for f in associations:
                lines.append(f"{f.process.pid} {PurePosixPath(f.process.executable).name} / {source_label(f)} / {f.label}")
        else:
            lines.append("No process associations observed for this chat; ownership remains unknown.")
        wrapped = [piece for line in lines for piece in wrap(line, c.width - 6)]
        self.detail_offset = min(self.detail_offset, max(0, len(wrapped) - height + 2))
        for i, text in enumerate(wrapped[self.detail_offset:self.detail_offset + height - 2]):
            c.put(top + 1 + i, 3, text, "normal")

    def _draw_history(self, c, rows, top, height):
        c.frame(top, height, "selected process history / last 60s", "cyan")
        if not rows:
            c.put(top + 1, 3, "Select a process to inspect history.", "muted")
            return
        row = rows[self.cursor]
        points = self.history.points(row)
        middle = c.width // 2
        plots = [(3, middle - 6, "CPU usage (%)", "cpu_percent", "amber"),
                 (middle + 2, c.width - middle - 6, "RSS", "rss_kib", "mint")]
        for x, width, label, field, tone in plots:
            valid = [(point.at, getattr(point, field)) for point in points if getattr(point, field) is not None]
            latest = (f"{valid[-1][1]:.1f}%" if field == "cpu_percent" else memory(valid[-1][1])) if valid else "sampling"
            c.put(top + 1, x, f"{label}: {latest}", tone, width)
            if len(valid) < 2:
                c.put(top + 3, x, "Waiting for measured samples...", "muted", width)
                continue
            now = points[-1].at
            maximum = max(100 if field == "cpu_percent" else 1, max(value for _, value in valid))
            for at, value in valid:
                xx = x + min(width - 1, max(0, round((at - (now - 60)) / 60 * (width - 1))))
                yy = top + 4 - min(2, round(value / maximum * 2))
                c.put(yy, xx, "▄" if self.unicode else "*", tone, 1)
            c.put(top + 5, x, "60s ago" + " " * max(1, width - 10) + "now", "muted", width)

    def _draw_status(self, c, rows, loading, expanded):
        h, w = c.height, c.width
        state = "expanded" if expanded else "collapsed"
        history = f"[h] History: {state}"
        if self.tab == "processes" and rows:
            points = self.history.points(rows[self.cursor])
            if len(points) < 2:
                history += " / collecting samples"
            elif not self.history.changing(rows[self.cursor]):
                history += " / no recent change"
        if self.show_history and not expanded:
            history += " / needs Processes and 28 rows"
        cached = sum(f.registry_observed_at is not None for f in self.snapshot.findings)
        observer = "Synthetic data | Refresh 5s" if self.snapshot.demo else f"Registry 100ms | Cache {cached} | Refresh 5s"
        c.put(h - 3, 2, history, "muted", max(0, w - cells(observer) - 7))
        c.put(h - 3, w - cells(observer) - 2, observer, "muted")
        problem = next((warning for warning in self.snapshot.warnings
                        if any(word in warning.lower() for word in ("unavailable", "failed", "rejected", "untrusted", "incomplete", "limit", "evicted"))), "")
        note = self.message or problem
        if not note:
            note = ("refreshing..." if loading else f"snapshot {age(time.time() - self.snapshot.at)} ago")
            cache = next((warning for warning in self.snapshot.warnings if warning.startswith("Ownership cache")), "")
            if cache:
                note += " | " + cache
            if not self.inspector or h < 22:
                note += " | i inspector"
        c.put(h - 2, 2, note, "amber" if self.message or problem else "muted")
        footer = "j/k select  Tab chats  c scope  s sort  / filter  h history  i inspector  ? help  q quit"
        if self.tab == "chats":
            footer = "j/k select  Tab processes  o associations  / filter  i inspector  ? help  q quit"
        if w < 100:
            footer = "j/k move  Tab view  / filter  h history  i info  ? help  q quit"
        c.put(h - 1, 2, footer, "cyan")

    def _draw_help(self, c):
        lines = [
            "VIEW CONTROLS", "j/k or arrows: select     PgUp/PgDn: move one visible page",
            "Tab: processes / saved chats     c: all / Codex / review",
            "s: score / RSS sort     /: filter, Enter accepts, Esc clears",
            "h: expand/collapse CPU and RSS history     i: toggle evidence inspector",
            "o in Chats: show processes with observed associations to that chat",
            "[ / ]: scroll full evidence     g/G: first / last     r: refresh",
            "? or Esc: close help     q in main view: quit",
            "", "INTERPRETING THE EVIDENCE",
            "Scores are rule weights, not probabilities or permission to terminate.",
            "Mint: execution observed in latest registry sample. Lavender: protected state.",
            "Muted CODEX UNKNOWN: unresolved ownership; still protected.",
            "CPU% needs two successful samples; 100% is one core, and values can exceed 100%.",
            "History is local to this run: up to 60s, at most 120 points per live identity.",
            "Flat histories show idle/steady text. Missing history is never drawn as zero.",
            "Low CPU, age, missing TTY, and launchd parentage can all be normal.",
            "Summed RSS includes shared pages and is not reclaimable memory.",
            "Saved transcript != saved REPL variables or browser state.",
            "Launch context identifies command origin, not exclusive helper ownership.",
            "Registry history uses PID + precise native birth; missing records never mean stale.",
            "Ownership associations persist in Linger's own cache; --no-cache disables it.",
            "The monitor never stops processes or changes Codex settings.",
            "LAUNCHD: verified managed service or descendant; preserve.",
            "TRACKED: command has not reported exit. TRACK UNKNOWN: preserve uncertain history.",
            "Temporary leftovers with observed successful owner exit receive Review, not stop approval.",
            "Optional: linger run --temporary -- command (metadata stored privately).",
        ]
        if self.snapshot and self.snapshot.warnings:
            lines += ["", "CURRENT DIAGNOSTICS", *self.snapshot.warnings]
        wrapped = [piece for line in lines for piece in (wrap(line, c.width - 6) or [""])]
        height = c.height - 5
        self.help_offset = min(self.help_offset, max(0, len(wrapped) - height + 2))
        c.frame(3, height, "help / evidence limitations", "cyan", "j/k scroll")
        for i, text in enumerate(wrapped[self.help_offset:self.help_offset + height - 2]):
            c.put(4 + i, 3, text, "cyan" if text.isupper() else "normal")
        c.put(c.height - 1, 2, "j/k scroll  ? / Esc return", "cyan")


def run(source, scope="all", demo=False):
    curses.wrapper(UI(source, scope, demo).run)
