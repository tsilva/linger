from dataclasses import replace
import curses
import unittest

from linger.demo import snapshot as demo_snapshot
from linger.history import History
from linger.models import Chat, Finding, Process, Snapshot
from linger.scoring import classify, score_factors
from linger.terminal import cells, clip, fit, wrap
from linger.tui import UI, filtered, process_columns, row_tone


CHAT = "00000000-0000-0000-0000-000000000001"
NOW = 1792596600.0


def finding(pid=4812, **kwargs):
    p = Process(pid, 1, 501, 120000, NOW - 7200, .2, "S", "??", "/demo/worker", 120, True)
    return Finding(p, "REVIEW", 50, ("Observed only; abandonment is unproven.",), **kwargs)


class Screen:
    """Reject out-of-bounds writes instead of silently hiding layout mistakes."""

    encoding = "utf-8"

    def __init__(self, width=120, height=36):
        self.width, self.height = width, height
        self.buffer = [[" "] * width for _ in range(height)]
        self.clears = 0

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.buffer = [[" "] * self.width for _ in range(self.height)]

    def clear(self):
        self.clears += 1
        self.erase()

    def addstr(self, y, x, text, attr=0):
        if not (0 <= y < self.height and 0 <= x < self.width and x + cells(text) < self.width):
            raise AssertionError((self.width, self.height, y, x, text))
        xx = x
        for char in text:
            size = cells(char)
            if size:
                self.buffer[y][xx] = char
                if size == 2:
                    self.buffer[y][xx + 1] = " "
                xx += size

    def refresh(self):
        pass

    def text(self):
        return "\n".join("".join(row) for row in self.buffer)


class HistoryTests(unittest.TestCase):
    def test_cpu_needs_two_samples_and_uses_elapsed_time_not_lifetime(self):
        history = History()
        f = finding()
        history.observe(Snapshot(NOW, [f]), 10)
        self.assertIsNone(history.cpu(f))
        next_f = replace(f, process=replace(f.process, cpu_seconds=1.2))
        history.observe(Snapshot(NOW + 5, [next_f]), 15)
        self.assertEqual(history.cpu(next_f), 20)

    def test_counter_reset_gap_and_reused_native_identity_never_inherit_usage(self):
        for change, elapsed in [(dict(cpu_seconds=.1), 5), (dict(cpu_seconds=5), 61),
                                (dict(started=NOW - 1, cpu_seconds=9), 5)]:
            with self.subTest(change=change):
                history = History()
                f = finding()
                history.observe(Snapshot(NOW, [f]), 10)
                changed = replace(f, process=replace(f.process, **change))
                history.observe(Snapshot(NOW + elapsed, [changed]), 10 + elapsed)
                self.assertIsNone(history.cpu(changed))
        history = History()
        f = replace(finding(), birth_us=100)
        history.observe(Snapshot(NOW, [f]), 10)
        changed = replace(f, birth_us=101)
        history.observe(Snapshot(NOW + 5, [changed]), 15)
        self.assertIsNone(history.cpu(changed))

    def test_real_zero_is_distinct_from_missing_and_multicore_is_not_capped(self):
        history = History()
        f = finding()
        history.observe(Snapshot(NOW, [f]), 10)
        history.observe(Snapshot(NOW + 5, [f]), 15)
        self.assertEqual(history.cpu(f), 0)
        self.assertFalse(history.changing(f))
        f = replace(f, process=replace(f.process, cpu_seconds=10.2))
        history.observe(Snapshot(NOW + 10, [f]), 20)
        self.assertEqual(history.cpu(f), 200)

    def test_window_point_cap_and_exited_processes_are_bounded(self):
        history = History(limit=4)
        f = finding()
        for now in range(20):
            history.observe(Snapshot(NOW + now, [f]), now)
        self.assertEqual(len(history.points(f)), 4)
        history.observe(Snapshot(NOW + 100, []), 100)
        self.assertEqual(history.series, {})
        history = History()
        for now in range(0, 101, 5):
            history.observe(Snapshot(NOW + now, [f]), now)
        self.assertTrue(all(p.at >= 40 for p in history.points(f)))


class LayoutTests(unittest.TestCase):
    def ui(self):
        ui = UI(lambda: None)
        fs = [finding(pid=4800 + i) for i in range(25)]
        fs[1] = replace(fs[1], process=replace(fs[1].process, executable="/demo/程序🙂\x1b[2Jworker"))
        ui.accept_snapshot(Snapshot(NOW, fs, [Chat(CHAT, "SAVED", "Turn ended; tool state may still be live.", NOW)]), 10)
        return ui

    def test_all_layouts_fit_real_cell_widths_and_toggles(self):
        for width, height in [(76, 16), (76, 22), (80, 24), (100, 28), (120, 36), (160, 48)]:
            for inspector in (False, True):
                for history in (False, True):
                    for tab in ("processes", "chats"):
                        with self.subTest(width=width, height=height, inspector=inspector, history=history, tab=tab):
                            ui = self.ui()
                            ui.inspector, ui.show_history, ui.tab = inspector, history, tab
                            screen = Screen(width, height)
                            ui.draw(screen)
                            self.assertIn("READ ONLY", screen.text())
                            self.assertIn("q quit", screen.text())
                            self.assertNotIn("\x1b", screen.text())
                            self.assertGreaterEqual(ui.page_size, 1)
                            ui.help = True
                            ui.draw(screen)

    def test_120_column_layout_keeps_columns_evidence_and_factual_context(self):
        ui = self.ui()
        screen = Screen()
        ui.draw(screen)
        text = screen.text()
        for expected in ("PROCESS", "CLASS", "SCORE", "CPU%", "RSS", "SOURCE", "ACTIVITY", "launchd parent", "+20", "Chat association: not observed", "History: collapsed", "scope: all"):
            self.assertIn(expected, text)
        self.assertIn("not probabilities", text)
        self.assertNotIn("may reference", text)
        self.assertNotIn("CPU time (%)", text)
        columns = process_columns(120)
        self.assertGreaterEqual(next(c[2] for c in columns if c[0] == "name"), 24)

    def test_unknown_protection_and_execution_colors_stay_distinct(self):
        f = replace(finding(), protected=True, codex=True, score=0)
        unknown = replace(f, label="CODEX UNKNOWN", registry_active=True)
        saved = replace(f, label="CODEX SAVED")
        recent = replace(f, label="CODEX ACTIVE")
        active = replace(recent, registry_active=True)
        self.assertEqual(row_tone(unknown), "unknown")
        self.assertEqual(row_tone(saved), "purple")
        self.assertEqual(row_tone(recent), "purple")
        self.assertEqual(row_tone(active), "mint")
        ui = self.ui()
        ui.accept_snapshot(Snapshot(NOW, [unknown]), 15)
        lines, _ = ui._evidence(unknown)
        self.assertIn("still protected", lines[0][0])
        self.assertTrue(all(weight is None for _, weight in lines))

    def test_score_breakdown_uses_the_actual_classification_rules(self):
        f = finding()
        factors = score_factors(f.process, NOW, {f.process.pid})
        self.assertEqual(sum(factor.weight for factor in factors), 50)
        classified = classify([f.process], NOW, uid=501)[0]
        self.assertEqual(classified.score, sum(factor.weight for factor in factors))
        self.assertEqual(tuple(f.reason for f in factors), classified.reasons[:-1])
        ui = self.ui()
        lines, _ = ui._evidence(classified)
        self.assertEqual(sum(weight or 0 for _, weight in lines), classified.score)

    def test_key_navigation_association_filter_and_selection_survive_toggles(self):
        ui = self.ui()
        ui.handle_key(ord("j"))
        selected = ui.selected_id()
        ui.handle_key(ord("h")); ui.handle_key(ord("i"))
        self.assertEqual(ui.selected_id(), selected)
        self.assertTrue(ui.show_history)
        self.assertFalse(ui.inspector)
        ui.handle_key(ord("s"))
        self.assertEqual(ui.selected_id(), selected)
        ui.handle_key(9)
        ui.handle_key(ord("o"))
        self.assertEqual((ui.tab, ui.scope, ui.query), ("processes", "codex", CHAT))
        self.assertEqual(ui.rows(), [])
        ui.handle_key(ord("/")); ui.handle_key(27)
        self.assertEqual(ui.query, "")
        self.assertFalse(ui.editing)
        self.assertEqual(ui.handle_key(ord("q")), "quit")

    def test_scroll_keeps_complete_explanations_available_and_resize_is_safe(self):
        ui = self.ui()
        f = replace(finding(), reasons=("First. " + "More detailed evidence. " * 80,))
        ui.accept_snapshot(Snapshot(NOW, [f]), 15)
        ui.handle_key(ord("]"))
        for width in (120, 76):
            screen = Screen(width, 24)
            ui.draw(screen)
            self.assertIn("More detailed", screen.text())
        ui.help = True
        ui.handle_key(curses.KEY_NPAGE)
        ui.draw(Screen(76, 24))
        ui.handle_key(27)
        self.assertFalse(ui.help)

    def test_protection_added_after_classification_reorders_rows(self):
        protected = replace(finding(pid=1), protected=True, score=0, label="CODEX SAVED")
        review = finding(pid=2)
        self.assertEqual(filtered(Snapshot(NOW, [protected, review]), "all", "", "stale"), [review, protected])

    def test_panel_switches_repaint_and_history_plots_measured_values(self):
        ui = UI(lambda: None)
        f = finding()
        ui.accept_snapshot(Snapshot(NOW, [f]), 15)
        for i in (1, 2):
            changed = replace(f, process=replace(f.process, cpu_seconds=.2 + i, rss_kib=120000 + i * 1024))
            ui.accept_snapshot(Snapshot(NOW + 5 * i, [changed]), 15 + 5 * i)
        screen = Screen()
        ui.draw(screen)
        clears = screen.clears
        ui.handle_key(ord("h"))
        ui.draw(screen)
        self.assertGreater(screen.clears, clears)
        self.assertIn("CPU usage (%): 20.0%", screen.text())
        self.assertIn("RSS: 119.2M", screen.text())
        self.assertNotIn("Waiting for measured", screen.text())
        self.assertEqual(len(ui.history.points(changed)), 3)

    def test_all_observed_chat_ids_remain_reachable_in_compact_inspector(self):
        ui = UI(lambda: None)
        owners = tuple(f"00000000-0000-0000-0000-{i:012x}" for i in range(1, 65))
        f = replace(finding(protected=True, codex=True), label="CODEX MULTI", score=0,
                    owners=owners, ownership_kind="registry remembered", registry_observed_at=NOW - 1)
        ui.accept_snapshot(Snapshot(NOW, [f]), 10)
        for _ in range(100):
            ui.handle_key(ord("]"))
        screen = Screen()
        ui.draw(screen)
        self.assertIn(owners[-1], screen.text())

    def test_unicode_and_controls_do_not_escape_a_column(self):
        self.assertEqual(cells("程序"), 4)
        self.assertEqual(cells("e\u0301"), 1)
        for width in range(20):
            text = fit("程序🙂e\u0301long\x1b[2J", width)
            self.assertEqual(cells(text), width)
            self.assertNotIn("\x1b", text)
            self.assertLessEqual(cells(clip(text, width)), width)
        text = "程序" * 20
        wrapped = wrap(text, 7)
        self.assertEqual("".join(wrapped), text)
        self.assertTrue(all(cells(line) <= 7 for line in wrapped))

    def test_demo_identities_survive_refresh_for_history_and_selection(self):
        first, second = demo_snapshot(), demo_snapshot()
        self.assertEqual([f.process.identity for f in first.findings], [f.process.identity for f in second.findings])


if __name__ == "__main__":
    unittest.main()
