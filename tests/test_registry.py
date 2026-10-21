from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from linger.models import Chat, Finding, Process, Snapshot
from linger.native import Context
from linger.ownership import bind
from linger.registry import MAX_RECORD_BYTES, RegistryObserver, parse_record
from linger.tui import filtered

CHAT = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"


class FakeNative:
    def __init__(self, identities):
        self.identities = identities

    def identity(self, pid):
        return self.identities.get(pid)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.directory = self.home / "node_repl/active_execs"
        self.directory.mkdir(parents=True)
        self.uid = os.getuid()
        self.now = int(time.time())
        self.birth = (self.now - 100) * 1000000 + 123
        self.reader = FakeNative({
            201: (self.birth, 100, self.uid),
            202: (self.birth + 1000000, 201, self.uid),
        })
        self.observer = RegistryObserver(self.home, reader=self.reader)
        self.addCleanup(self.observer.close)
        self.path = self.directory / "execution.json"
        self.record = dict(version=1, execId="execution-one", sessionId=CHAT,
                           turnId="turn-one", nodeReplPid=201, kernelPid=202,
                           startedAtMs=(self.now - 1) * 1000)

    def publish(self, **changes):
        self.path.write_text(json.dumps({**self.record, **changes}))

    def rows(self):
        return [Process(pid, ident[1], ident[2], 1000, ident[0] // 1000000, .1,
                        "S", "??", "/Applications/Codex.app/runtime/node_repl")
                for pid, ident in self.reader.identities.items()]

    def bindings(self):
        return self.observer.snapshot(self.rows())[0]

    def findings(self, contexts=None, saved=None):
        rows = self.rows()
        fs = [Finding(p, "CODEX UNKNOWN", 0, (), codex=True, protected=True) for p in rows]
        return bind(fs, contexts or {}, [], saved, self.bindings())[0]

    def test_both_native_processes_get_direct_association(self):
        self.publish()
        self.observer.poll()
        bindings = self.bindings()
        self.assertEqual(len(bindings), 2)
        for p in self.rows():
            binding = bindings[p.identity]
            self.assertEqual(binding.owners, (CHAT,))
            self.assertEqual(binding.active_owners, (CHAT,))
            self.assertEqual(binding.birth_us, self.reader.identities[p.pid][0])

    def test_disappeared_record_retains_history_and_saved_state(self):
        self.publish()
        self.observer.poll()
        self.path.unlink()
        self.observer.poll()
        for f in self.findings(saved={CHAT}):
            self.assertEqual(f.owners, (CHAT,))
            self.assertEqual(f.label, "CODEX SAVED")
            self.assertEqual(f.ownership_kind, "registry remembered")
            self.assertFalse(f.registry_active)
            self.assertTrue(f.protected)

    def test_empty_registry_does_not_create_stale_or_owned_helpers(self):
        self.observer.poll()
        for f in self.findings(saved=set()):
            self.assertEqual(f.owner, "unresolved")
            self.assertEqual(f.label, "CODEX UNKNOWN")
            self.assertTrue(f.protected)

    def test_live_registry_protects_even_without_saved_membership(self):
        self.publish()
        self.observer.poll()
        for f in self.findings(saved=None):
            self.assertEqual(f.label, "CODEX ACTIVE")
            self.assertTrue(f.registry_active)
            self.assertTrue(f.protected)

    def test_remembered_unsaved_association_never_proves_ephemeral_completion(self):
        self.publish()
        self.observer.poll()
        self.path.unlink()
        self.observer.poll()
        for f in self.findings(saved=set()):
            self.assertEqual(f.label, "CODEX UNKNOWN")
            self.assertTrue(f.protected)
            self.assertIn("does not prove", " ".join(f.reasons))

    def test_pid_reuse_within_same_second_clears_history(self):
        self.publish()
        self.observer.poll()
        self.path.unlink()
        birth, parent, uid = self.reader.identities[201]
        self.reader.identities[201] = birth + 1, parent, uid
        self.assertNotIn(self.rows()[0].identity, self.bindings())

    def test_unchanged_old_record_cannot_rebind_reused_pid(self):
        self.publish()
        self.observer.poll()
        birth, parent, uid = self.reader.identities[201]
        self.reader.identities[201] = birth + 1, parent, uid
        self.observer.poll()
        self.assertNotIn(self.rows()[0].identity, self.bindings())

    def test_reuse_after_execution_timestamp_cannot_bind_old_record(self):
        self.publish()
        self.reader.identities[201] = (self.now * 1000000, 100, self.uid)
        self.observer.poll()
        self.assertEqual(self.bindings(), {})

    def test_exit_or_lost_identity_invalidates_only_that_process(self):
        self.publish()
        self.observer.poll()
        del self.reader.identities[202]
        self.assertEqual(len(self.bindings()), 1)
        self.assertEqual(list(self.observer.cache), [(201, self.birth)])

    def test_native_identity_race_rejects_record(self):
        self.publish()
        original = self.reader.identity
        calls = 0

        def race(pid):
            nonlocal calls
            calls += 1
            value = original(pid)
            return (value[0] + 1, *value[1:]) if calls == 3 else value

        self.reader.identity = race
        self.observer.poll()
        self.assertEqual(self.bindings(), {})

    def test_record_modified_during_native_validation_is_not_accepted(self):
        self.publish()
        original = self.reader.identity
        changed = False

        def modify(pid):
            nonlocal changed
            if not changed:
                self.publish(sessionId=OTHER)
                changed = True
            return original(pid)

        self.reader.identity = modify
        self.observer.poll()
        self.assertEqual(self.bindings(), {})

    def test_known_kernel_stays_protected_after_parent_exits_and_lineage_is_lost(self):
        self.publish()
        self.observer.poll()
        self.path.unlink()
        del self.reader.identities[201]
        birth, _, uid = self.reader.identities[202]
        self.reader.identities[202] = birth, 1, uid
        self.observer.poll()
        p = replace(self.rows()[0], executable="/usr/local/bin/node")
        generic = Finding(p, "REVIEW", 50, ("Orphaned worker",))
        fs, _ = bind([generic], {}, [], {CHAT}, self.observer.snapshot([p])[0])
        self.assertTrue(fs[0].codex)
        self.assertTrue(fs[0].protected)
        self.assertEqual(fs[0].owners, (CHAT,))
        self.assertEqual(fs[0].label, "CODEX SAVED")

    def test_wrong_parent_or_other_user_rejects_record(self):
        self.publish()
        for identity in [(self.birth, 999, self.uid), (self.birth, 201, self.uid + 1)]:
            self.reader.identities[202] = identity
            self.observer.poll()
            self.assertEqual(self.bindings(), {})

    def test_symlink_and_fifo_records_are_not_followed_or_blocked(self):
        outside = self.home / "external.json"
        outside.write_text(json.dumps(self.record))
        self.path.symlink_to(outside)
        self.observer.poll()
        self.assertEqual(self.bindings(), {})
        self.path.unlink()
        os.mkfifo(self.path)
        self.observer.poll()
        self.assertEqual(self.bindings(), {})

    def test_symlink_registry_directory_is_not_followed(self):
        self.directory.rmdir()
        outside = self.home / "outside"
        outside.mkdir()
        self.directory.symlink_to(outside, target_is_directory=True)
        self.publish()
        self.observer.poll()
        self.assertEqual(self.bindings(), {})

    def test_insecure_permissions_reject_record(self):
        self.publish()
        self.path.chmod(0o666)
        self.observer.poll()
        self.assertEqual(self.bindings(), {})

    def test_missing_directory_retains_only_remembered_associations(self):
        self.publish()
        self.observer.poll()
        self.path.unlink()
        self.directory.rmdir()
        self.observer.poll()
        for b in self.bindings().values():
            self.assertEqual(b.owners, (CHAT,))
            self.assertEqual(b.active_owners, ())

    def test_multiple_chats_accumulate_without_exclusive_ownership(self):
        self.publish()
        self.observer.poll()
        self.publish(execId="execution-two", sessionId=OTHER)
        self.observer.poll()
        for f in self.findings(saved={CHAT, OTHER}):
            self.assertEqual(f.owners, (CHAT, OTHER))
            self.assertEqual(f.label, "CODEX MULTI")
            self.assertTrue(f.protected)
            self.assertEqual(filtered(Snapshot(self.now, [f]), "codex", OTHER, "stale"), [f])

    def test_launch_context_conflict_keeps_observed_chats_but_protects(self):
        self.publish()
        self.observer.poll()
        p = self.rows()[0]
        context = Context(self.birth, (OTHER,), conflict=True)
        fs = self.findings(contexts={p.identity: context}, saved={CHAT, OTHER})
        f = next(f for f in fs if f.process.pid == 201)
        self.assertEqual(f.label, "CODEX UNKNOWN")
        self.assertEqual(f.owners, (CHAT, OTHER))
        self.assertTrue(f.protected)

    def test_evidence_from_two_different_births_fails_closed(self):
        self.publish()
        self.observer.poll()
        p = self.rows()[0]
        fs = self.findings(contexts={p.identity: Context(self.birth + 1, (CHAT,))}, saved={CHAT})
        f = next(f for f in fs if f.process.pid == 201)
        self.assertEqual(f.owners, ())
        self.assertEqual(f.label, "CODEX UNKNOWN")
        self.assertTrue(f.protected)

    def test_cache_is_bounded_and_evicted_coverage_remains_unknown(self):
        self.observer.cache_limit = 1
        self.publish()
        self.observer.poll()
        self.assertEqual(len(self.observer.cache), 1)
        fs = self.findings(saved={CHAT})
        self.assertEqual(sum(bool(f.owners) for f in fs), 1)
        self.assertTrue(all(f.protected for f in fs))

    def test_registry_files_and_metadata_are_never_modified(self):
        self.publish()
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        self.observer.poll()
        self.observer.snapshot(self.rows())
        self.assertEqual(before, (self.path.read_bytes(), self.path.stat().st_mtime_ns))
        self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_background_observer_remembers_then_stops_cleanly(self):
        self.publish()
        self.observer.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not self.bindings():
            time.sleep(.01)
        self.assertTrue(self.bindings())
        self.path.unlink()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and any(b.active_owners for b in self.bindings().values()):
            time.sleep(.01)
        self.assertTrue(self.bindings())
        self.assertFalse(any(b.active_owners for b in self.bindings().values()))
        self.observer.close()
        self.assertFalse(self.observer.running)
        self.assertFalse(self.observer.cache)

    def test_poll_failure_does_not_leave_false_active_flags(self):
        self.publish()
        self.observer.poll()
        self.assertTrue(any(b.active_owners for b in self.bindings().values()))
        self.path.chmod(0o666)
        self.observer.poll()
        self.assertTrue(self.bindings())
        self.assertFalse(any(b.active_owners for b in self.bindings().values()))

    def test_delayed_observation_cannot_keep_claiming_current_execution(self):
        self.publish()
        self.observer.poll()
        with patch("linger.registry.time.monotonic", return_value=self.observer._last_poll + 2):
            bindings = self.bindings()
        self.assertTrue(bindings)
        self.assertFalse(any(b.active_owners for b in bindings.values()))

    def test_owner_cap_is_incomplete_and_protected(self):
        with patch("linger.registry.MAX_OWNERS", 1):
            self.publish()
            self.observer.poll()
            self.publish(execId="another-execution", sessionId=OTHER)
            self.observer.poll()
        for f in self.findings(saved={CHAT}):
            self.assertEqual(f.label, "CODEX UNKNOWN")
            self.assertTrue(f.protected)

    def test_malformed_future_and_unknown_schema_records_fail_closed(self):
        for changes in [dict(version=2), dict(version=True), dict(nodeReplPid=True),
                        dict(sessionId="not-a-chat"), dict(startedAtMs=(self.now+3600)*1000)]:
            self.publish(**changes)
            self.observer.poll()
            self.assertEqual(self.bindings(), {})

    def test_oversized_and_duplicate_records_are_rejected_without_data_leaks(self):
        cases = [b"x" * (MAX_RECORD_BYTES + 1), b'{"version":1,"version":1,"secret":"DO_NOT_EMIT"}']
        for data in cases:
            self.path.write_bytes(data)
            self.observer.poll()
            bindings, warnings = self.observer.snapshot(self.rows())
            self.assertEqual(bindings, {})
            self.assertNotIn("DO_NOT_EMIT", repr(warnings))


if __name__ == "__main__":
    unittest.main()
