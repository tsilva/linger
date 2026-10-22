from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from linger.__main__ import main
from linger.launchd import LaunchdReader, bind as bind_services, parse_list, parse_policy
from linger.models import Finding, Process, Service
from linger.tracking import LaunchTracker, RunStore, bind
from linger.tui import source_label

BOOT = '00000000-0000-0000-0000-000000000001'
NOW = int(time.time()) - 100


def finding(pid=41001, ppid=1):
    p = Process(pid, ppid, os.getuid(), 100, NOW, .2, 'S', '??', '/tmp/worker')
    return Finding(p, 'OBSERVED', 25, ('Generic evidence.',))


class Reader:
    def __init__(self, *findings):
        self.values = {f.process.pid: (NOW * 1000000 + f.process.pid, f.process.ppid, os.getuid()) for f in findings}

    def identity(self, pid):
        return self.values.get(pid)


class LaunchdTests(unittest.TestCase):
    def test_listing_ignores_stopped_malformed_and_ambiguous_jobs(self):
        self.assertEqual(parse_list('PID Status Label\n11 0 com.example.worker\n- 0 com.stopped\n12 0 -evil\n'), {11: 'com.example.worker'})
        self.assertEqual(parse_list('11 0 a\n11 0 b'), {})

    def test_policy_requires_matching_pid_and_unambiguous_supported_fields(self):
        detail = '"PID" = 42;\n"OnDemand" = false;\n'
        self.assertEqual(parse_policy(detail, 42), 'kept running')
        self.assertEqual(parse_policy(detail.replace('false', 'true'), 42), 'on demand or conditional')
        self.assertEqual(parse_policy(detail, 43), 'unknown')
        self.assertEqual(parse_policy(detail + '"OnDemand" = true;\n', 42), 'unknown')
        self.assertEqual(parse_policy('new unknown format', 42), 'unknown')

    def test_native_identity_brackets_listing_and_policy(self):
        f = finding()
        reader = Reader(f)
        def read(*args):
            if not args:
                return f'{f.process.pid} 0 com.example.worker\n'
            return f'"PID" = {f.process.pid};\n"OnDemand" = false;\n'
        services, warnings = LaunchdReader(reader, read).services([f.process])
        self.assertFalse(warnings)
        self.assertEqual(services[f.process.identity].policy, 'kept running')
        def race(*args):
            text = read(*args)
            reader.values[f.process.pid] = (NOW * 1000000 + 999999, 1, os.getuid())
            return text
        self.assertEqual(LaunchdReader(reader, race).services([f.process])[0], {})

    def test_service_protection_propagates_without_overriding_codex(self):
        parent, child = finding(), finding(41002, 41001)
        codex = replace(child, codex=True, protected=True, label='CODEX SAVED', score=0)
        services = {parent.process.identity: Service('com.example.worker', 'unknown')}
        result = bind_services([parent, child, codex], services)
        self.assertEqual([r.label for r in result], ['LAUNCHD', 'LAUNCHD', 'CODEX SAVED'])
        self.assertTrue(all(r.protected and r.score == 0 for r in result))
        self.assertEqual(source_label(result[0]), 'launchd')
        self.assertFalse(bind_services([parent], {})[0].protected)

    def test_unavailable_service_data_is_explicit(self):
        with patch('linger.launchd._read', side_effect=OSError):
            services, warnings = LaunchdReader(Reader()).services([])
        self.assertFalse(services)
        self.assertIn('unavailable', warnings[0])


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'records'
        self.store = RunStore(self.directory, boot_id=BOOT)
        self.root, self.child = finding(), finding(41002, 41001)
        self.reader = Reader(self.root, self.child)
        self.tracker = LaunchTracker((41001, self.reader.identity(41001)[0]), True, self.reader, self.store)

    def record(self):
        self.tracker.observe([self.root.process, self.child.process])
        self.tracker.save()
        records, warnings = self.store.read()
        self.assertFalse(warnings)
        return records[0]

    def test_observed_descendant_survives_reparenting_and_owner_exit(self):
        self.record()
        self.tracker.finish(0)
        record = self.store.read()[0][0]
        child = replace(self.child, process=replace(self.child.process, ppid=1))
        native = self.reader.values[41002]
        self.reader.values[41002] = (native[0], 1, native[2])
        result = bind([child], [record], self.reader)[0]
        self.assertEqual(result.label, 'REVIEW')
        self.assertEqual(result.score, 35)
        self.assertIn('exited successfully', ' '.join(result.reasons))
        self.assertFalse(result.protected)
        self.assertEqual(result.tracking[0].exit_code, 0)

    def test_running_failed_interrupted_persistent_shared_and_incomplete_preserve(self):
        record = self.record()
        cases = [record, dict(record, status='exited', exit_code=2),
                 dict(record, status='interrupted'),
                 dict(record, status='exited', exit_code=0, temporary=False),
                 dict(record, status='exited', exit_code=0, incomplete=True)]
        for document in cases:
            with self.subTest(document=document):
                result = bind([self.child], [document], self.reader)[0]
                self.assertTrue(result.protected)
                self.assertEqual(result.score, 0)
        complete = dict(record, status='exited', exit_code=0)
        self.assertTrue(bind([self.child], [complete, dict(complete, id='a'*32)], self.reader)[0].protected)

    def test_existing_codex_service_and_zombie_protections_win(self):
        record = dict(self.record(), status='exited', exit_code=0)
        for label in ('CODEX SAVED', 'CODEX UNKNOWN', 'LAUNCHD', 'PROTECTED', 'ZOMBIE'):
            f = replace(self.child, label=label, protected=label != 'ZOMBIE', score=90 if label == 'ZOMBIE' else 0)
            self.assertEqual(bind([f], [record], self.reader)[0].label, label)

    def test_pid_reuse_snapshot_race_and_prior_birth_conflict_do_not_bind(self):
        record = self.record()
        old = self.reader.values[41002]
        self.reader.values[41002] = (old[0] + 1, old[1], old[2])
        self.assertFalse(bind([self.child], [record], self.reader)[0].tracking)
        self.reader.values[41002] = old
        conflict = replace(self.child, birth_us=old[0] + 1)
        self.assertFalse(bind([conflict], [record], self.reader)[0].tracking)
        reader = Mock()
        reader.identity.side_effect = [old, None]
        self.assertFalse(bind([self.child], [record], reader)[0].tracking)

    def test_unobserved_parent_and_reused_parent_cannot_adopt_children(self):
        self.reader.values[41001] = (NOW * 1000000 + 9999, 1, os.getuid())
        self.tracker.observe([self.root.process, self.child.process])
        self.assertEqual(len(self.tracker.record['members']), 1)
        self.reader = Reader(self.root, self.child)
        self.tracker.reader = self.reader
        reader = self.reader
        def raced(pid):
            if pid == 41001 and raced.calls:
                return None
            if pid == 41001:
                raced.calls += 1
            return reader.identity(pid)
        raced.calls = 0
        self.tracker.reader = Mock(identity=raced)
        self.tracker.observe([self.root.process, self.child.process])
        self.assertEqual(len(self.tracker.record['members']), 1)

    def test_private_atomic_records_contain_metadata_only_and_keep_runs_separate(self):
        record = self.record()
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        path = self.directory / (record['id'] + '.json')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('worker', path.read_text())
        second = LaunchTracker((41001, self.reader.identity(41001)[0]), False, self.reader, self.store)
        self.tracker.finish(0)
        self.assertEqual(len(self.store.read()[0]), 2)
        self.assertEqual(second.record['status'], 'running')
        self.assertFalse(list(self.directory.glob('*.tmp')))

    def test_corrupt_untrusted_symlink_hardlink_and_wrong_boot_rejected(self):
        record = self.record()
        path = self.directory / (record['id'] + '.json')
        original = path.read_text()
        path.write_text('{bad json')
        self.assertTrue(self.store.read()[1])
        path.write_text(original)
        path.chmod(0o644)
        self.assertFalse(self.store.read()[0])
        path.chmod(0o600)
        link = self.directory / ('a'*32 + '.json')
        link.symlink_to(path)
        self.assertTrue(self.store.read()[1])
        link.unlink()
        os.link(path, link)
        self.assertFalse(self.store.read()[0])
        link.unlink()
        self.assertFalse(RunStore(self.directory, boot_id='f'*8+'-0000-0000-0000-000000000001').read()[0])

    def test_schema_limits_duplicates_and_no_fabricated_exit(self):
        record = self.record()
        for change in [dict(exit_code=0), dict(status='exited', exit_code=None),
                       dict(members=[record['root'], record['root']]), dict(updated=float('nan')),
                       dict(temporary=1), dict(members=[]), dict(boot_id='unknown')]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.decode(json.dumps(dict(record, **change)).encode(), record['id']+'.json')
        data = json.dumps(record).replace('"version": 1', '"version": 1, "version": 1')
        with self.assertRaises(ValueError):
            self.store.decode(data.encode(), record['id']+'.json')

    def test_cli_dispatch_preserves_command_arguments(self):
        with patch('linger.tracking.run_command', return_value=7) as run:
            self.assertEqual(main(['run', '--temporary', '--', 'python3', '-c', 'print(1)', '--flag']), 7)
            run.assert_called_once_with(['python3', '-c', 'print(1)', '--flag'], True)
        with patch('linger.tracking.run_command', return_value=0) as run:
            main(['run', '--', 'echo', '--temporary'])
            run.assert_called_once_with(['echo', '--temporary'], False)
        with self.assertRaises(SystemExit):
            with patch('sys.stderr'):
                main(['run', '--temporary'])

    def test_record_age_and_limits_do_not_synthesize_completion(self):
        record = self.record()
        path = self.directory / (record['id'] + '.json')
        path.write_text(json.dumps(dict(record, updated=time.time() - 8 * 86400)))
        self.assertFalse(self.store.read()[0])
        path.write_text(json.dumps(record))
        with patch('linger.tracking.MAX_RUNS', 0):
            self.assertTrue(self.store.read()[1])
        with patch('linger.tracking.MAX_MEMBERS', 1):
            self.tracker.record['members'] = [self.tracker.record['root']]
            self.tracker.observe([self.root.process, self.child.process])
            self.assertTrue(self.tracker.record['incomplete'])

    def test_store_limit_blocks_new_registration_and_monitors_can_opt_out(self):
        with patch('linger.tracking.MAX_RUNS', 1), self.assertRaises(OSError):
            LaunchTracker((41001, self.reader.identity(41001)[0]), True, self.reader, self.store)
        from linger.scan import Scanner
        with patch('linger.scan.RunStore', side_effect=AssertionError('disk tracking accessed')):
            scanner = Scanner(persist=False)
            self.assertIsNone(scanner.launches)
            scanner.close()

    def test_scanner_combines_launch_records_and_service_protection(self):
        from linger.scan import Scanner
        record = dict(self.record(), status='exited', exit_code=0)
        with patch('linger.scan.collect', return_value=[self.child.process]), \
             patch('linger.scan.contexts', return_value=({}, [])), \
             patch('linger.scan.NativeReader', return_value=self.reader):
            scanner = Scanner(persist=False)
            scanner.registry.poll = Mock()
            scanner.registry.snapshot = Mock(return_value=({}, []))
            scanner.codex.read = Mock(return_value=([], []))
            scanner.launches = Mock(read=Mock(return_value=([record], [])))
            scanner.launchd.services = Mock(return_value=({}, []))
            result = scanner.scan().findings[0]
            self.assertEqual(result.label, 'REVIEW')
            self.assertTrue(result.tracking)
            scanner.launchd.services.return_value = ({self.child.process.identity: Service('com.example.worker')}, [])
            result = scanner.scan().findings[0]
            self.assertEqual(result.label, 'LAUNCHD')
            self.assertTrue(result.protected)
            self.assertTrue(result.tracking)
            scanner.close()


@unittest.skipUnless(__import__('platform').system() == 'Darwin', 'native macOS launch tracking')
class NativeLaunchTests(unittest.TestCase):
    def test_terminal_interrupt_is_recorded_without_synthesizing_success(self):
        import signal
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            code = ('from pathlib import Path; from linger.tracking import RunStore, run_command; '
                    f'raise SystemExit(run_command(["/bin/sleep", "2"], True, RunStore(Path({directory!r})/"records")))')
            wrapper = subprocess.Popen([sys.executable, '-c', code], start_new_session=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertIn('tracking', wrapper.stderr.readline())
                time.sleep(.1)
                self.assertEqual(os.getpgid(wrapper.pid), wrapper.pid)
                os.killpg(wrapper.pid, signal.SIGINT)  # Only this test's isolated group.
                wrapper.communicate(timeout=4)
                self.assertEqual(wrapper.returncode, 130)
                records, warnings = RunStore(Path(directory)/'records').read()
                self.assertFalse(warnings)
                self.assertEqual(records[0]['status'], 'interrupted')
                self.assertIsNone(records[0]['exit_code'])
            finally:
                # The bounded sleep also exits naturally if an assertion fails.
                wrapper.communicate(timeout=4)

    def test_real_launch_preserves_exit_and_missing_command_status(self):
        from linger.tracking import run_command
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory) / 'records')
            self.assertEqual(run_command(['/bin/sh', '-c', 'exit 7'], True, store), 7)
            self.assertEqual(run_command(['/nonexistent/linger-test-command'], False, store), 127)
            self.assertEqual(run_command(['/bin/sh', '-c', 'kill -TERM $$'], True, store), 143)
            records, warnings = store.read()
            self.assertFalse(warnings)
            self.assertEqual(len(records), 3)
            self.assertEqual(sorted(r['status'] for r in records), ['exited', 'exited', 'interrupted'])

    def test_registration_failure_never_executes_the_command(self):
        from linger.tracking import run_command
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'must-not-exist'
            store = RunStore(Path(directory) / 'records')
            with patch.object(store, 'write', side_effect=OSError), self.assertRaises(OSError):
                run_command(['/usr/bin/touch', str(marker)], True, store)
            self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
