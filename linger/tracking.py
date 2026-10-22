"""Opt-in command provenance. Records metadata, never arguments or environments."""

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time

from .cache import OwnershipStore, _unique_object, boot_session_id
from .models import TrackedRun
from .native import NativeReader
from .processes import collect

RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
MAX_RUNS = 256
MAX_MEMBERS = 256
MAX_BYTES = 65536
RETENTION = 7 * 86400


def identity(reader, process):
    value = reader.identity(process.pid)
    if (value and value[0] // 1000000 == int(process.started)
            and value[1:] == (process.ppid, process.uid)):
        return value
    return None


class RunStore:
    def __init__(self, directory=None, boot_id=None):
        directory = Path(directory) if directory else Path.home() / "Library/Application Support/Linger/launch-runs"
        self.boot_id = boot_session_id() if boot_id is None else boot_id
        # Reuse the same no-follow, owner-only directory/file checks as ownership history.
        self.guard = OwnershipStore(Path.home() / ".codex", directory=directory, boot_id=self.boot_id)

    def decode(self, data, name):
        record = json.loads(data, object_pairs_hook=_unique_object)
        now = time.time()
        if (not isinstance(record, dict) or type(record.get("version")) is not int or record["version"] != 1
                or not RUN_ID.fullmatch(record.get("id", "")) or name != record["id"] + ".json"
                or type(record.get("uid")) is not int or record["uid"] != os.getuid()
                or record.get("boot_id") != self.boot_id
                or record.get("status") not in {"running", "exited", "interrupted"}
                or type(record.get("temporary")) is not bool
                or type(record.get("updated")) not in (int, float)
                or not 0 < record["updated"] <= now + 1
                or type(record.get("incomplete")) is not bool):
            raise ValueError("Invalid launch record")
        members, root = record.get("members"), record.get("root")
        if not isinstance(members, list) or not 1 <= len(members) <= MAX_MEMBERS or root not in members:
            raise ValueError("Invalid launch identities")
        seen = set()
        for pair in members:
            if (not isinstance(pair, list) or len(pair) != 2
                    or any(type(v) is not int for v in pair)
                    or not 1 < pair[0] < 2**31 or not 0 < pair[1] <= int(now * 1000000)
                    or tuple(pair) in seen):
                raise ValueError("Invalid launch identity")
            seen.add(tuple(pair))
        code = record.get("exit_code")
        if ((record["status"] == "running" and code is not None)
                or (record["status"] == "exited" and (type(code) is not int or not 0 <= code <= 255))
                or (record["status"] == "interrupted" and code is not None)):
            raise ValueError("Invalid launch exit status")
        return record

    def _read(self, directory, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = self.guard._trusted_file(fd)
            if not 0 < info.st_size <= MAX_BYTES:
                raise ValueError("Launch record size limit")
            data = os.read(fd, MAX_BYTES + 1)
            after = os.fstat(fd)
            if (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns):
                raise ValueError("Launch record changed")
            return self.decode(data, name)
        finally:
            os.close(fd)

    def read(self):
        try:
            directory = self.guard._directory()
        except FileNotFoundError:
            return [], []
        except OSError:
            return [], ["Launch tracking unavailable: private storage or boot identity could not be verified."]
        records, warnings = [], []
        try:
            for index, name in enumerate(os.listdir(directory)):
                if index >= MAX_RUNS:
                    warnings.append("Launch tracking record limit reached; coverage incomplete.")
                    break
                if not re.fullmatch(r"[0-9a-f]{32}\.json", name):
                    continue
                try:
                    record = self._read(directory, name)
                    if time.time() - record["updated"] <= RETENTION:
                        records.append(record)
                except (OSError, ValueError, TypeError, RecursionError):
                    warnings.append("Untrusted or incompatible launch record rejected; ownership remains unknown.")
        finally:
            os.close(directory)
        return records, list(dict.fromkeys(warnings))

    def write(self, record):
        self.decode(json.dumps(record).encode(), record["id"] + ".json")
        directory = self.guard._directory(create=True)
        temp = "." + secrets.token_hex(16) + ".tmp"
        try:
            names = os.listdir(directory)
            target = record["id"] + ".json"
            if target not in names and len(names) >= MAX_RUNS:
                raise OSError("Launch record limit reached; remove old Linger launch records before starting another tracked command")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, "w") as stream:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temp, target, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temp, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)


class LaunchTracker:
    def __init__(self, root, temporary, reader, store):
        self.reader, self.store = reader, store
        self.record = dict(version=1, id=secrets.token_hex(16), uid=os.getuid(),
                           boot_id=store.boot_id, root=list(root), members=[list(root)],
                           temporary=temporary, status="running", exit_code=None,
                           incomplete=False, updated=time.time())
        self.save()

    def save(self):
        self.record["updated"] = time.time()
        self.store.write(self.record)

    def observe(self, rows):
        known = {tuple(pair) for pair in self.record["members"]}
        validated = {}
        for p in rows:
            if p.uid == os.getuid():
                native = identity(self.reader, p)
                if native:
                    validated[p.pid] = (p, native)
        # Admit descendants only through a currently identity-validated member.
        live = {pid for pid, (_, native) in validated.items() if (pid, native[0]) in known}
        changed = True
        while changed:
            changed = False
            for pid, (p, native) in validated.items():
                if pid not in live and p.ppid in live:
                    parent_native = validated[p.ppid][1]
                    if (native[0] >= parent_native[0] and self.reader.identity(pid) == native
                            and self.reader.identity(p.ppid) == parent_native):
                        if len(known) >= MAX_MEMBERS:
                            self.record["incomplete"] = True
                            continue
                        live.add(pid)
                        known.add((pid, native[0]))
                        changed = True
        self.record["members"] = [list(pair) for pair in sorted(known)]

    def finish(self, code=None):
        self.record["status"] = "exited" if code is not None else "interrupted"
        self.record["exit_code"] = code
        self.save()


def bind(findings, records, reader):
    indexed = {}
    for record in records:
        for pid, birth in record["members"]:
            indexed.setdefault((pid, birth), []).append(record)
    candidate_pids = {pid for pid, _ in indexed}
    result = []
    for f in findings:
        p = f.process
        if p.pid not in candidate_pids:
            result.append(f)
            continue
        native = identity(reader, p) if p.uid == os.getuid() else None
        if native is None or (f.birth_us is not None and f.birth_us != native[0]):
            result.append(f)
            continue
        matches = indexed.get((p.pid, native[0]), [])
        if not matches or reader.identity(p.pid) != native:
            result.append(f)
            continue
        runs = tuple(TrackedRun(r["id"], r["temporary"], r["status"], r["exit_code"]) for r in matches)
        active = any(r["status"] == "running" for r in matches)
        # A vanished wrapper or stale heartbeat never synthesizes owner completion.
        leftover = (len(matches) == 1 and matches[0]["temporary"] and matches[0]["status"] == "exited"
                    and matches[0]["exit_code"] == 0 and not matches[0]["incomplete"])
        reason = "Tracked launch: " + "; ".join(
            f'{r["id"]} / {"temporary" if r["temporary"] else "persistent"} / {r["status"]}' for r in matches) + "."
        if f.protected or f.label == "ZOMBIE":
            updated = replace(f, reasons=(*f.reasons, reason))
        elif active:
            updated = replace(f, label="TRACKED", score=0, protected=True,
                              reasons=(reason, "Owner command has not reported exit; preserve its observed processes."))
        elif leftover:
            updated = replace(f, label="REVIEW", score=max(35, f.score),
                              reasons=(reason, "Declared temporary at launch; owner command exited successfully while this observed descendant remains.",
                                       "Review priority floor: 35. Retained state and external dependencies are not verified; stopping is not approved.", *f.reasons))
        else:
            updated = replace(f, label="TRACK UNKNOWN", score=0, protected=True,
                              reasons=(reason, "Persistent, interrupted, failed, incomplete, or shared launch history; preserve uncertain state."))
        result.append(replace(updated, tracking=runs, birth_us=native[0]))
    return result


def run_command(command, temporary=False, store=None):
    """Run without a shell, preserving stdio/cwd/environment and the exit status.

    A pipe gate anchors the child identity before exec. No existing processes are
    signaled. Terminal Ctrl-C reaches the child through its inherited foreground
    process group; the wrapper records interruption and waits for its child.
    """
    import platform
    import sys
    if platform.system() != "Darwin":
        raise RuntimeError("Launch tracking currently supports macOS only")
    reader = NativeReader()
    store = store or RunStore()
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(write_fd)
        try:
            allowed = os.read(read_fd, 1) == b"1"
            os.close(read_fd)
            if not allowed:
                os._exit(125)
            os.execvp(command[0], command)
        except OSError as error:
            os.write(2, f"linger: command could not start (errno {error.errno})\n".encode())
            os._exit(127 if error.errno == 2 else 126)
        except KeyboardInterrupt:
            os._exit(130)
        os._exit(125)
    os.close(read_fd)
    tracker, interrupted, status, released = None, False, None, False
    try:
        native = reader.identity(pid)
        if native is None or native[1:] != (os.getpid(), os.getuid()):
            raise RuntimeError("Could not verify launched process identity")
        tracker = LaunchTracker((pid, native[0]), temporary, reader, store)
        os.write(write_fd, b"1")
        released = True
    except KeyboardInterrupt:
        released = False  # Reap even if Ctrl-C arrived just after gate release.
        if tracker is not None:
            try:
                tracker.finish()
            except OSError:
                pass
        return 130
    finally:
        os.close(write_fd)
        if not released:
            while True:
                try:
                    os.waitpid(pid, 0)
                    break
                except KeyboardInterrupt:
                    pass
    try:
        print(f"linger: tracking {tracker.record['id']} ({'temporary' if temporary else 'persistent'}); no automatic termination", file=sys.stderr)
    except OSError:
        pass  # A closed diagnostic stream must not abandon the launched command.
    last_save = time.monotonic()
    try:
        while True:
            try:
                exited, status = os.waitpid(pid, os.WNOHANG)
                if exited:
                    break
                tracker.observe(collect())
                if time.monotonic() - last_save >= 1:
                    tracker.save()
                    last_save = time.monotonic()
                time.sleep(.25)
            except KeyboardInterrupt:
                interrupted = True
                tracker.finish()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"linger: tracking incomplete ({type(error).__name__}); waiting for command", file=sys.stderr)
        tracker.record["incomplete"] = True
        while True:
            try:
                _, status = os.waitpid(pid, 0)
                break
            except KeyboardInterrupt:
                interrupted = True
    code = os.waitstatus_to_exitcode(status)
    try:
        tracker.finish(None if interrupted or code < 0 else code)
    except OSError:
        print("linger: final launch record unavailable; completion remains unknown", file=sys.stderr)
    return code if code >= 0 else 128 - code
