from dataclasses import replace
import json
import struct
import unittest
from unittest.mock import patch

from linger.codex import CodexReader, MAX_CHATS
from linger.models import Chat, Finding, Process
from linger.native import Context, NativeReader, decode_context
from linger.ownership import bind
from linger.tui import filtered
from linger.models import Snapshot

THREAD = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"
P = Process(234567, 100, 501, 1000, 1792596600, .2, "S", "??", "/bundle/node_repl")
F = Finding(P, "CODEX UNKNOWN", 0, ("Unresolved helper",), codex=True, protected=True)


def blob(argv=(b"worker",), env=()):
    return struct.pack("=i", len(argv)) + b"/bin/worker\0\0" + b"\0".join(argv) + b"\0" + b"\0".join(env) + b"\0\0"


class ContextParserTests(unittest.TestCase):
    def test_extract_only_whitelisted_identity_without_credentials(self):
        data = blob(env=(b"CODEX_THREAD_ID=" + THREAD.encode(), b"API_TOKEN=do-not-leak", b"HOME=/private/path"))
        ids, conflict = decode_context(data)
        self.assertEqual(ids, (THREAD,))
        self.assertFalse(conflict)
        self.assertNotIn("do-not-leak", repr((ids, conflict)))

    def test_argument_looking_like_an_environment_variable_is_not_a_binding(self):
        ids, conflict = decode_context(blob(argv=(b"worker", b"CODEX_THREAD_ID=" + THREAD.encode())))
        self.assertEqual(ids, ())
        self.assertFalse(conflict)

    def test_uuid_in_a_secret_or_other_field_is_not_a_binding(self):
        ids, _ = decode_context(blob(env=(b"API_TOKEN=" + THREAD.encode(), b"BROWSER_USE_ACCESS_EXECUTOR_ID=" + THREAD.encode())))
        self.assertEqual(ids, ())

    def test_thread_and_legacy_session_must_agree(self):
        ids, conflict = decode_context(blob(env=(b"CODEX_THREAD_ID=" + THREAD.encode(), b"CODEX_SESSION_ID=" + OTHER.encode())))
        self.assertTrue(conflict)
        self.assertEqual(ids, tuple(sorted((THREAD, OTHER))))

    def test_duplicate_or_invalid_bindings_are_conflicts(self):
        for env in [(b"CODEX_THREAD_ID=bad",), (b"CODEX_THREAD_ID=" + THREAD.encode(),) * 2]:
            self.assertTrue(decode_context(blob(env=env))[1])

    def test_truncated_or_impossible_arguments_are_rejected(self):
        for data in (b"", struct.pack("=i", -1) + b"x\0", blob(env=(b"CODEX_THREAD_ID=" + THREAD.encode(),))[:-2], struct.pack("=i", 50000) + b"x\0"):
            with self.subTest(data=data[:10]):
                with self.assertRaises(ValueError):
                    decode_context(data)

    def test_values_can_contain_spaces_and_newlines_without_ps_ambiguity(self):
        data = blob(env=(b"UNRELATED=hello\n CODEX_THREAD_ID=" + OTHER.encode(), b"CODEX_THREAD_ID=" + THREAD.encode()))
        self.assertEqual(decode_context(data), ((THREAD,), False))


class NativeIdentityTests(unittest.TestCase):
    def reader(self, identities, data=None, error=None):
        reader = object.__new__(NativeReader)
        reader.identity = unittest.mock.Mock(side_effect=identities)
        reader.arguments = unittest.mock.Mock(return_value=data or blob(env=(b"CODEX_THREAD_ID=" + THREAD.encode(),)), side_effect=error)
        return reader

    def test_identity_is_checked_before_and_after(self):
        identity = (int(P.started) * 1000000 + 123, P.ppid, P.uid)
        result = self.reader([identity, identity]).read(P)
        self.assertEqual(result.threads, (THREAD,))
        self.assertEqual(result.birth_us, identity[0])

    def test_pid_reuse_inside_same_second_discards_binding(self):
        identity = (int(P.started) * 1000000 + 123, P.ppid, P.uid)
        changed = (identity[0] + 1, P.ppid, P.uid)
        self.assertIsNone(self.reader([identity, changed]).read(P))

    def test_parent_or_uid_change_discards_binding_before_read(self):
        identity = (int(P.started) * 1000000 + 123, P.ppid + 1, P.uid)
        reader = self.reader([identity])
        self.assertIsNone(reader.read(P))
        reader.arguments.assert_not_called()

    def test_permission_denied_becomes_unreadable_not_an_inferred_binding(self):
        identity = (int(P.started) * 1000000 + 123, P.ppid, P.uid)
        result = self.reader([identity, identity], error=OSError("access denied")).read(P)
        self.assertFalse(result.readable)
        self.assertEqual(result.threads, ())


class OwnershipTests(unittest.TestCase):
    def evaluate(self, context=None, chats=(), saved=None, finding=F):
        contexts = {} if context is None else {P.identity: context}
        rows, _ = bind([finding], contexts, list(chats), saved)
        return rows[0]

    def context(self, thread=THREAD, **fields):
        return Context(int(P.started) * 1000000 + 123, (thread,), **fields)

    def test_saved_chat_outside_recent_window_is_still_protected(self):
        f = self.evaluate(self.context(), saved={THREAD})
        self.assertEqual(f.label, "CODEX SAVED")
        self.assertEqual(f.owner, THREAD)
        self.assertTrue(f.protected)
        self.assertIn("outside", " ".join(f.reasons))

    def test_active_chat_keeps_state_protection(self):
        chat = Chat(THREAD, "ACTIVE", "Observed activity", P.started)
        f = self.evaluate(self.context(), chats=[chat], saved={THREAD})
        self.assertEqual(f.label, "CODEX ACTIVE")
        self.assertTrue(f.protected)

    def test_ended_saved_chat_is_never_stale(self):
        chat = Chat(THREAD, "SAVED", "Turn ended", P.started)
        f = self.evaluate(self.context(), chats=[chat], saved={THREAD})
        self.assertEqual(f.label, "CODEX SAVED")
        self.assertEqual(f.score, 0)
        self.assertTrue(f.protected)

    def test_database_absence_does_not_prove_ephemeral_completion(self):
        f = self.evaluate(self.context(), saved=set())
        self.assertEqual(f.label, "CODEX UNKNOWN")
        self.assertTrue(f.protected)
        self.assertIn("does not prove", " ".join(f.reasons))

    def test_unreadable_membership_fails_closed(self):
        f = self.evaluate(self.context(), saved=None)
        self.assertEqual(f.label, "CODEX UNKNOWN")
        self.assertTrue(f.protected)

    def test_shared_infrastructure_cannot_inherit_exclusive_chat_ownership(self):
        f = self.evaluate(self.context(), saved={THREAD}, finding=replace(F, label="CODEX CORE"))
        self.assertEqual(f.owner, "unresolved")
        self.assertEqual(f.label, "CODEX CORE")

    def test_conflict_or_missing_identity_never_flags_stale(self):
        for context in [None, self.context(conflict=True), Context(1, (), readable=False), Context(1, ())]:
            f = self.evaluate(context, saved=set())
            self.assertTrue(f.protected)
            self.assertEqual(f.owner, "unresolved")

    def test_timing_and_old_cpu_cannot_create_ownership(self):
        old = replace(F, process=replace(P, observed_idle=999999, sampled=True))
        f = self.evaluate(saved=set(), finding=old)
        self.assertEqual(f.label, "CODEX UNKNOWN")
        self.assertEqual(f.score, 0)

    def test_filter_matches_exact_chat_context(self):
        f = self.evaluate(self.context(), saved={THREAD})
        self.assertEqual(filtered(Snapshot(P.started, [f]), "codex", THREAD, "stale"), [f])

    def test_snapshot_contains_no_raw_environment(self):
        from dataclasses import asdict
        f = self.evaluate(self.context(), saved={THREAD})
        data = json.dumps(asdict(f))
        self.assertNotIn("API_TOKEN", data)
        self.assertEqual(f.ownership_kind, "launch context")


class FullMembershipTests(unittest.TestCase):
    def test_saved_ids_include_more_than_the_recent_lifecycle_limit(self):
        import tempfile
        import sqlite3
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            c = sqlite3.connect(home / "state_5.sqlite")
            c.execute("CREATE TABLE threads (id TEXT,rollout_path TEXT,updated_at INTEGER)")
            ids = {f"00000000-0000-0000-0000-{i:012x}" for i in range(MAX_CHATS + 5)}
            c.executemany("INSERT INTO threads VALUES (?,?,?)", [(tid, "missing", n) for n, tid in enumerate(sorted(ids))])
            c.commit()
            c.close()
            reader = CodexReader(home)
            chats, _ = reader.read(P.started)
            self.assertEqual(len(chats), MAX_CHATS)
            self.assertEqual(reader.saved_ids, ids)


if __name__ == "__main__":
    unittest.main()
