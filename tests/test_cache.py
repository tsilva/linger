from contextlib import redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from linger.__main__ import main
from linger.cache import CacheState, OwnershipStore, SavedAssociation
from linger.models import Finding, Process, Snapshot
from linger.ownership import bind
from linger.registry import RegistryObserver
from linger.scan import Scanner

CHAT = str(uuid.UUID(int=1))
OTHER = str(uuid.UUID(int=2))
BOOT = str(uuid.UUID(int=10))


class FakeNative:
    def __init__(self, identities):
        self.identities = identities

    def identity(self, pid):
        return self.identities.get(pid)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "codex"
        self.registry = self.home / "node_repl/active_execs"
        self.registry.mkdir(parents=True)
        self.directory = self.root / "linger"
        self.now = time.time()
        self.birth = int(self.now - 100) * 1000000 + 123
        self.reader = FakeNative({201: (self.birth, 100, os.getuid()),
                                  202: (self.birth + 1000000, 201, os.getuid())})
        self.path = self.registry / "execution.json"

    def store(self, **kw):
        return OwnershipStore(self.home, directory=self.directory, boot_id=BOOT, **kw)

    def observer(self, store=None):
        result = RegistryObserver(self.home, reader=self.reader, store=store or self.store())
        self.addCleanup(result.close)
        return result

    def publish(self, owner=CHAT, execution="execution-one"):
        self.path.write_text(json.dumps(dict(version=1, execId=execution, sessionId=owner,
            turnId="turn-one", nodeReplPid=201, kernelPid=202, startedAtMs=int((self.now-1)*1000))))

    def rows(self):
        return [Process(pid, value[1], value[2], 1000, value[0] // 1000000, .1,
                        "S", "??", "/Applications/Codex.app/runtime/node_repl")
                for pid, value in self.reader.identities.items()]

    def seed(self):
        self.publish()
        observer = self.observer()
        observer.poll()
        observer.close()
        self.path.unlink()
        return self.directory / observer.store.filename

    def bindings(self, observer):
        return observer.snapshot(self.rows())[0]

    def test_restart_recovers_saved_associations_without_activity(self):
        self.seed()
        observer = self.observer()
        observer.poll()
        bindings = self.bindings(observer)
        self.assertEqual(len(bindings), 2)
        fs = [Finding(p, "CODEX UNKNOWN", 0, (), codex=True, protected=True) for p in self.rows()]
        findings, _ = bind(fs, {}, [], {CHAT}, bindings)
        for f in findings:
            self.assertEqual(f.label, "CODEX SAVED")
            self.assertEqual(f.owners, (CHAT,))
            self.assertFalse(f.registry_active)
            self.assertTrue(f.protected)

    def test_poll_checkpoints_before_exit_and_clears_only_memory_on_exit(self):
        self.publish()
        observer = self.observer()
        observer.poll()
        cache = self.directory / observer.store.filename
        self.assertTrue(cache.exists())
        self.assertEqual(len(json.loads(cache.read_text())["entries"]), 2)
        observer.close()
        self.assertFalse(observer.cache)
        self.assertTrue(cache.exists())

    def test_every_restored_entry_rechecks_microsecond_birth(self):
        self.seed()
        birth, parent, uid = self.reader.identities[201]
        self.reader.identities[201] = birth + 1, parent, uid
        observer = self.observer()
        observer.poll()
        self.assertEqual({key[0] for key in self.bindings(observer)}, {202})

    def test_exited_unreadable_or_other_user_entries_are_discarded(self):
        self.seed()
        del self.reader.identities[201]
        birth, parent, uid = self.reader.identities[202]
        self.reader.identities[202] = birth, parent, uid + 1
        observer = self.observer()
        observer.poll()
        self.assertEqual(self.bindings(observer), {})

    def test_runtime_identity_change_invalidates_restored_ownership(self):
        self.seed()
        observer = self.observer()
        self.assertEqual(len(self.bindings(observer)), 2)
        self.reader.identities.clear()
        self.assertEqual(self.bindings(observer), {})
        observer.close()
        document = json.loads((self.directory / observer.store.filename).read_text())
        self.assertEqual(document["entries"], [])

    def test_identity_race_during_restore_does_not_bind_reused_process(self):
        self.seed()
        original = self.reader.identity
        calls = 0

        def race(pid):
            nonlocal calls
            if pid == 201:
                calls += 1
                if calls >= 2:
                    birth, parent, uid = original(pid)
                    return birth + 1, parent, uid
            return original(pid)

        self.reader.identity = race
        observer = self.observer()
        self.assertNotIn(self.rows()[0].identity, self.bindings(observer))

    def test_record_anchors_survive_restart_and_reject_reused_pid(self):
        self.publish()
        first = self.observer()
        first.poll()
        first.close()
        birth, parent, uid = self.reader.identities[201]
        self.reader.identities[201] = birth + 1, parent, uid
        second = self.observer()
        second.poll()
        self.assertNotIn(self.rows()[0].identity, self.bindings(second))

    def test_reboot_and_different_codex_home_cannot_restore_history(self):
        cache = self.seed()
        other_boot = OwnershipStore(self.home, directory=self.directory, boot_id=OTHER)
        self.assertEqual(other_boot.load(lambda key: True).entries, {})
        other_home = OwnershipStore(self.root / "other-codex", directory=self.directory, boot_id=BOOT)
        self.assertNotEqual(other_home.filename, cache.name)
        (self.directory / other_home.filename).write_bytes(cache.read_bytes())
        (self.directory / other_home.filename).chmod(0o600)
        self.assertEqual(other_home.load(lambda key: True).entries, {})

    def test_incompatible_or_malformed_history_supplies_no_association(self):
        cache = self.seed()
        original = json.loads(cache.read_text())
        for change in [dict(version=2), dict(version=True), dict(uid=os.getuid()+1),
                       dict(entries=[dict(pid=True)]), dict(entries=[dict(pid=201)]),
                       dict(anchors={"bad": []})]:
            cache.write_text(json.dumps({**original, **change}))
            self.assertEqual(self.store().load(lambda key: True).entries, {})
        cache.write_text('{"version":1,"version":1,"secret":"DO_NOT_EMIT"}')
        store = self.store()
        self.assertEqual(store.load(lambda key: True).entries, {})
        self.assertNotIn("DO_NOT_EMIT", store.status)

    def test_insecure_file_directory_symlink_fifo_and_hardlink_fail_closed(self):
        cache = self.seed()
        raw = cache.read_bytes()
        cache.chmod(0o644)
        self.assertFalse(self.store().load(lambda key: True).entries)
        cache.chmod(0o600)
        self.directory.chmod(0o755)
        self.assertFalse(self.store().load(lambda key: True).entries)
        self.directory.chmod(0o700)
        cache.unlink()
        target = self.root / "unrelated"
        target.write_bytes(raw)
        target.chmod(0o600)
        for mode in ("symlink", "fifo", "hardlink"):
            if mode == "symlink":
                cache.symlink_to(target)
            elif mode == "fifo":
                os.mkfifo(cache, 0o600)
            else:
                os.link(target, cache)
            store = self.store()
            self.assertFalse(store.load(lambda key: True).entries)
            self.assertIsNone(store.sync(CacheState(), lambda key: True))
            self.assertEqual(target.read_bytes(), raw)
            cache.unlink()

    def test_symlink_directory_is_not_followed(self):
        cache = self.seed()
        other = self.root / "other"
        self.directory.rename(other)
        self.directory.symlink_to(other, target_is_directory=True)
        store = self.store()
        self.assertFalse(store.load(lambda key: True).entries)
        self.assertIsNone(store.sync(CacheState(), lambda key: True))
        self.assertTrue((other / cache.name).exists())

    def test_missing_boot_identity_disables_disk_io(self):
        with patch("linger.cache.boot_session_id", return_value=None):
            store = OwnershipStore(self.home, directory=self.directory)
        self.assertFalse(store.load(lambda key: True).entries)
        self.assertIsNone(store.sync(CacheState(), lambda key: True))
        self.assertFalse(self.directory.exists())

    def test_oversized_cache_and_invalid_timestamps_are_rejected(self):
        cache = self.seed()
        with patch("linger.cache.MAX_BYTES", 16):
            self.assertFalse(self.store().load(lambda key: True).entries)
        document = json.loads(cache.read_text())
        for invalid in [float("nan"), float("inf"), self.now + 3600, -1, True]:
            document["entries"][0]["last_seen"] = invalid
            cache.write_text(json.dumps(document))
            self.assertFalse(self.store().load(lambda key: True).entries)

    def test_cache_cannot_be_placed_inside_codex_home(self):
        store = OwnershipStore(self.home, directory=self.home / "linger", boot_id=BOOT)
        self.assertIsNone(store.sync(CacheState(), lambda key: True))
        self.assertFalse((self.home / "linger").exists())

    def test_concurrent_observers_merge_all_chats_without_restoring_activity(self):
        first, second = self.observer(), self.observer()
        first._last_checkpoint = second._last_checkpoint = time.monotonic()
        self.publish()
        first.poll()
        self.publish(OTHER, "execution-two")
        second.poll()
        self.path.unlink()
        first.close()
        second.close()
        third = self.observer()
        third.poll()
        for binding in self.bindings(third).values():
            self.assertEqual(binding.owners, (CHAT, OTHER))
            self.assertFalse(binding.active_owners)

    def test_busy_lock_does_not_block_or_discard_memory(self):
        self.seed()
        fd = os.open(self.directory / "ownership.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            observer = self.observer()
            observer.poll()
            self.assertEqual(len(self.bindings(observer)), 2)
            self.assertIn("busy", observer.store.status)
        finally:
            os.close(fd)

    def test_atomic_replace_failure_preserves_previous_cache_and_cleans_temp(self):
        cache = self.seed()
        before = cache.read_bytes()
        observer = self.observer()
        with patch("linger.cache.os.replace", side_effect=OSError("DO_NOT_EMIT")):
            observer.poll()
        self.assertEqual(cache.read_bytes(), before)
        self.assertFalse(list(self.directory.glob("*.tmp")))
        self.assertNotIn("DO_NOT_EMIT", observer.store.status)
        self.assertEqual(len(self.bindings(observer)), 2)

    def test_cache_is_private_minimal_and_codex_files_unchanged(self):
        self.publish()
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        observer = self.observer()
        observer.poll()
        observer.close()
        cache = self.directory / observer.store.filename
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(before, (self.path.read_bytes(), self.path.stat().st_mtime_ns))
        document = json.loads(cache.read_text())
        self.assertNotIn("active", cache.read_text())
        self.assertNotIn(str(self.home), cache.read_text())
        self.assertEqual(set(document["entries"][0]), {"pid", "birth_us", "owners", "last_seen", "incomplete"})

    def test_owner_cap_marks_merged_history_incomplete(self):
        store = self.store()
        key = 201, self.birth
        owners = tuple(str(uuid.UUID(int=n)) for n in range(1, 65))
        store.sync(CacheState({key: SavedAssociation(*key, owners, self.now)}), lambda key: True)
        state = store.sync(CacheState({key: SavedAssociation(*key, (str(uuid.UUID(int=65)),), self.now)}), lambda key: True)
        self.assertEqual(len(state.entries[key].owners), 64)
        self.assertTrue(state.entries[key].incomplete)

    def test_restored_unsaved_owner_remains_unknown_and_protected(self):
        self.seed()
        observer = self.observer()
        observer.poll()
        findings = [Finding(p, "CODEX UNKNOWN", 0, (), codex=True, protected=True) for p in self.rows()]
        findings, _ = bind(findings, {}, [], set(), self.bindings(observer))
        self.assertTrue(all(f.label == "CODEX UNKNOWN" and f.protected for f in findings))

    def test_no_cache_flag_and_demo_do_not_create_a_store(self):
        with patch("linger.scan.OwnershipStore") as constructor:
            scanner = Scanner(self.home, persist=False)
            scanner.close()
            self.assertIsNone(scanner.registry.store)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--demo", "--once"]), 0)
            constructor.assert_not_called()
        with patch("linger.__main__.Scanner") as constructor, redirect_stdout(io.StringIO()):
            constructor.return_value.scan.return_value = Snapshot(self.now, [])
            self.assertEqual(main(["--once", "--no-cache"]), 0)
            constructor.assert_called_once_with(None, persist=False)


if __name__ == "__main__":
    unittest.main()
