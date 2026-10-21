from dataclasses import replace
import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from linger.codex import CodexReader, lifecycle
from linger.models import Process
from linger.processes import Sampler, parse_ps
from linger.scoring import classify
from linger.tui import clean, filtered
from linger.demo import snapshot

NOW = 1792596600.0
THREAD = "00000000-0000-0000-0000-000000000001"


def event(kind, when=NOW - 1, **fields):
    return json.dumps({"timestamp": dt.datetime.fromtimestamp(when, dt.timezone.utc).isoformat(),
                       "type": "event_msg", "payload": {"type": kind, **fields}}).encode() + b"\n"


def process(pid=1234567, ppid=1, **fields):
    return replace(Process(pid, ppid, 501, 50000, NOW - 7200, .3, "S", "??", "/usr/local/bin/worker"), **fields)


class LifecycleTests(unittest.TestCase):
    def test_active_turn_is_evidence_not_idle_inference(self):
        status, _, _ = lifecycle(event("task_started", NOW - 20) + event("item_completed"), NOW)
        self.assertEqual(status, "ACTIVE")

    def test_ended_turn_preserves_possible_tool_state(self):
        status, evidence, _ = lifecycle(event("task_started", NOW - 100) + event("task_complete"), NOW)
        self.assertEqual(status, "SAVED")
        self.assertIn("tool state", evidence)

    def test_stale_start_is_not_authoritatively_active(self):
        status, _, _ = lifecycle(event("task_started", NOW - 3600), NOW)
        self.assertEqual(status, "UNKNOWN")

    def test_truncated_tail_is_recent_not_falsely_active(self):
        status, _, _ = lifecycle(b"a truncated JSON prefix\n" + event("item_completed"), NOW)
        self.assertEqual(status, "RECENT")

    def test_future_timestamp_does_not_create_active_chat(self):
        self.assertEqual(lifecycle(event("task_started", NOW + 900), NOW)[0], "UNKNOWN")

    def test_chat_content_is_not_in_evidence(self):
        self.assertNotIn("SECRET", repr(lifecycle(event("task_complete", text="SECRET"), NOW)))


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        self.addCleanup(self.directory.cleanup)

    def database(self, path):
        c = sqlite3.connect(self.home / "state_5.sqlite")
        c.execute("CREATE TABLE threads (id TEXT,rollout_path TEXT,updated_at INTEGER)")
        c.execute("INSERT INTO threads VALUES (?,?,?)", (THREAD, str(path), NOW))
        c.commit()
        c.close()

    def test_missing_database_keeps_explicit_uncertainty(self):
        chats, warnings = CodexReader(self.home).read(NOW)
        self.assertEqual(chats, [])
        self.assertIn("unknown", warnings[0])

    def test_unknown_schema_fails_closed(self):
        sqlite3.connect(self.home / "state_5.sqlite").close()
        self.assertIn("protected", CodexReader(self.home).read(NOW)[1][0])

    def test_reads_lifecycle_without_modifying_database_or_rollout(self):
        path = self.home / "sessions/chat.jsonl"
        path.parent.mkdir()
        path.write_bytes(event("task_started"))
        self.database(path)
        db = self.home / "state_5.sqlite"
        before = (db.read_bytes(), path.read_bytes())
        chats, _ = CodexReader(self.home).read(NOW)
        self.assertEqual(chats[0].status, "ACTIVE")
        self.assertEqual(chats[0].tool_state, "possible; preserve")
        self.assertEqual(before, (db.read_bytes(), path.read_bytes()))

    def test_external_rollout_path_is_not_followed(self):
        self.database(self.home.parent / "secret.jsonl")
        reader = CodexReader(self.home)
        chats, _ = reader.read(NOW)
        self.assertEqual(chats[0].status, "UNKNOWN")
        self.assertFalse(reader.cache)

    def test_symlink_escape_is_not_followed(self):
        outside = self.home / "outside.jsonl"
        outside.write_bytes(event("task_started"))
        sessions = self.home / "sessions"
        sessions.mkdir()
        link = sessions / "chat.jsonl"
        link.symlink_to(outside)
        self.database(link)
        self.assertEqual(CodexReader(self.home).read(NOW)[0][0].status, "UNKNOWN")

    def test_cache_does_not_keep_old_active_status_forever(self):
        path = self.home / "sessions/chat.jsonl"
        path.parent.mkdir()
        path.write_bytes(event("task_started"))
        self.database(path)
        reader = CodexReader(self.home)
        self.assertEqual(reader.read(NOW)[0][0].status, "ACTIVE")
        self.assertEqual(reader.read(NOW + 200)[0][0].status, "UNKNOWN")


class ProcessTests(unittest.TestCase):
    def test_macos_paths_with_spaces_and_cpu_time(self):
        line = "1234 1 501 65536 Wed Oct 21 15:00:00 2026 1:02.34 S ?? /Applications/My App.app/Contents/MacOS/Test"
        p = parse_ps(line)[0]
        self.assertEqual(p.cpu_seconds, 62.34)
        self.assertEqual(p.executable, "/Applications/My App.app/Contents/MacOS/Test")
        self.assertEqual(parse_ps("broken data"), [])

    def test_pid_reuse_resets_inactivity(self):
        sampler = Sampler()
        p = process()
        sampler.observe([p], 10)
        self.assertEqual(sampler.observe([p], 80)[0].observed_idle, 70)
        reused = replace(p, started=p.started + 100)
        observed = sampler.observe([reused], 90)[0]
        self.assertFalse(observed.sampled)
        self.assertEqual(observed.observed_idle, 0)

    def test_cpu_progress_resets_idle_clock(self):
        sampler = Sampler()
        p = process()
        sampler.observe([p], 10)
        progressed = sampler.observe([replace(p, cpu_seconds=1)], 100)[0]
        self.assertEqual(progressed.observed_idle, 0)

    def test_codex_helper_and_descendant_never_stale_by_age(self):
        helper = process(executable="/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node_repl", observed_idle=9000, sampled=True)
        child = process(pid=1234568, ppid=helper.pid, executable="/usr/local/bin/python")
        findings = classify([helper, child], NOW, uid=501)
        for finding in findings:
            self.assertTrue(finding.protected)
            self.assertTrue(finding.codex)
            self.assertEqual(finding.score, 0)
            self.assertEqual(finding.owner, "unresolved")

    def test_missing_parent_is_only_review_evidence(self):
        f = classify([process(ppid=99999999)], NOW, uid=501)[0]
        self.assertEqual(f.label, "REVIEW")
        self.assertIn("collection race", " ".join(f.reasons))

    def test_launchd_parentage_does_not_prove_abandonment(self):
        f = classify([process()], NOW, uid=501)[0]
        self.assertEqual(f.label, "REVIEW")
        self.assertIn("legitimate", " ".join(f.reasons))

    def test_launchd_ancestry_does_not_protect_every_user_process(self):
        launchd = process(pid=1, ppid=0, uid=0, executable="/sbin/launchd")
        worker = process()
        findings = {f.process.pid: f for f in classify([launchd, worker], NOW, uid=501)}
        self.assertFalse(findings[worker.pid].protected)
        self.assertEqual(findings[worker.pid].label, "REVIEW")
        self.assertTrue(findings[1].protected)

    def test_zombie_is_already_exited_not_memory_cleanup(self):
        f = classify([process(state="Z")], NOW, uid=501)[0]
        self.assertEqual(f.label, "ZOMBIE")
        self.assertIn("will not reclaim", " ".join(f.reasons))

    def test_services_and_their_children_are_protected(self):
        service = process(executable="/usr/local/bin/postgres")
        child = process(pid=1234568, ppid=service.pid)
        self.assertTrue(all(f.protected for f in classify([service, child], NOW, uid=501)))

    def test_ancestry_cycle_does_not_hang(self):
        a = process(ppid=1234568)
        b = process(pid=1234568, ppid=a.pid)
        self.assertEqual(len(classify([a, b], NOW, uid=501)), 2)


class ViewTests(unittest.TestCase):
    def test_codex_scope_retains_protection(self):
        rows = filtered(snapshot(), "codex", "", "stale")
        self.assertTrue(rows)
        self.assertTrue(all(f.protected for f in rows))

    def test_terminal_escape_sequences_cannot_execute(self):
        self.assertEqual(clean("name\x1b[2J\n"), "name?[2J?")

    def test_demo_does_not_read_host(self):
        with patch("subprocess.run", side_effect=AssertionError("host access")), patch("sqlite3.connect", side_effect=AssertionError("database access")):
            self.assertTrue(snapshot().demo)


if __name__ == "__main__":
    unittest.main()
