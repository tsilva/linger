"""Linger-owned, bounded ownership history. Never writes inside Codex home."""

from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import time

UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
MAX_ENTRIES = 2048
MAX_OWNERS = 64
MAX_ANCHORS = 1024
MAX_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class SavedAssociation:
    pid: int
    birth_us: int
    owners: tuple[str, ...]
    last_seen: float
    incomplete: bool = False


@dataclass
class CacheState:
    entries: dict[tuple[int, int], SavedAssociation] = field(default_factory=dict)
    anchors: dict[str, tuple] = field(default_factory=dict)


def boot_session_id():
    try:
        value = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
            capture_output=True, text=True, timeout=2, check=True,
        ).stdout.strip().lower()
        return value if UUID.fullmatch(value) else None
    except (OSError, subprocess.SubprocessError):
        return None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate cache field")
        result[key] = value
    return result


def _positive_int(value, maximum):
    return type(value) is int and 0 < value < maximum


class OwnershipStore:
    def __init__(self, home: Path, *, directory=None, boot_id=None, uid=None):
        self.home = Path(home).resolve()
        self.uid = os.getuid() if uid is None else uid
        self.directory = Path(directory) if directory is not None else Path.home() / "Library/Application Support/Linger"
        self.boot_id = boot_session_id() if boot_id is None else boot_id
        self.scope = hashlib.sha256(os.fsencode(str(self.home))).hexdigest()
        self.filename = f"ownership-v1-{self.scope[:24]}.json"
        self.status = "Ownership persistence not yet sampled."

    def _directory(self, create=False):
        # An alternate Codex home never becomes an output directory.
        if self.directory.resolve().is_relative_to(self.home):
            raise OSError("Cache overlaps Codex home")
        if not self.boot_id or not UUID.fullmatch(self.boot_id):
            raise OSError("Boot identity unavailable")
        if create:
            self.directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if info.st_uid != self.uid or info.st_mode & 0o077:
                raise OSError("Cache directory must be private")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _trusted_file(self, fd):
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.uid
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise OSError("Untrusted cache file")
        return info

    def _decode(self, data):
        document = json.loads(data, object_pairs_hook=_unique_object)
        if (not isinstance(document, dict) or type(document.get("version")) is not int
                or document["version"] != 1 or type(document.get("uid")) is not int
                or document["uid"] != self.uid or document.get("boot_id") != self.boot_id
                or document.get("scope") != self.scope):
            raise ValueError("Cache scope or schema mismatch")
        entries, anchors = document.get("entries"), document.get("anchors")
        if (not isinstance(entries, list) or len(entries) > MAX_ENTRIES
                or not isinstance(anchors, dict) or len(anchors) > MAX_ANCHORS):
            raise ValueError("Cache limits exceeded")
        now = time.time()
        state = CacheState()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Invalid cache entry")
            pid, birth = entry.get("pid"), entry.get("birth_us")
            owners, seen, incomplete = entry.get("owners"), entry.get("last_seen"), entry.get("incomplete")
            if (not _positive_int(pid, 2**31) or pid <= 1
                    or not _positive_int(birth, int(now * 1000000) + 1)
                    or not isinstance(owners, list) or not 0 < len(owners) <= MAX_OWNERS
                    or any(not isinstance(owner, str) or not UUID.fullmatch(owner) for owner in owners)
                    or type(seen) not in (int, float) or not math.isfinite(seen)
                    or not birth / 1000000 <= seen <= now or type(incomplete) is not bool):
                raise ValueError("Invalid cache association")
            key = pid, birth
            if key in state.entries:
                raise ValueError("Duplicate process identity")
            state.entries[key] = SavedAssociation(pid, birth, tuple(sorted({s.lower() for s in owners})), seen, incomplete)
        for name, anchor in anchors.items():
            if (not name.endswith(".json") or len(name) > 255 or "/" in name
                    or not isinstance(anchor, list) or len(anchor) != 3):
                raise ValueError("Invalid cache anchor")
            execution, owner, keys = anchor
            if (not isinstance(execution, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", execution)
                    or not isinstance(owner, str) or not UUID.fullmatch(owner)
                    or not isinstance(keys, list) or len(keys) != 2):
                raise ValueError("Invalid cache anchor identity")
            for key in keys:
                if (not isinstance(key, list) or len(key) != 2
                        or not _positive_int(key[0], 2**31) or key[0] <= 1
                        or not _positive_int(key[1], int(now * 1000000) + 1)):
                    raise ValueError("Invalid cache anchor birth")
            state.anchors[name] = execution, owner.lower(), tuple(tuple(key) for key in keys)
        return state

    def _read(self, directory):
        try:
            fd = os.open(self.filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return CacheState()
        try:
            info = self._trusted_file(fd)
            if not 0 < info.st_size <= MAX_BYTES:
                raise ValueError("Invalid cache size")
            chunks, size = [], 0
            while size <= MAX_BYTES:
                chunk = os.read(fd, min(65536, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            after = os.fstat(fd)
            if (size > MAX_BYTES or (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise ValueError("Changed cache file")
            return self._decode(b"".join(chunks))
        finally:
            os.close(fd)

    def load(self, valid):
        directory = None
        try:
            directory = self._directory()
            state = self._read(directory)
            state.entries = {key: entry for key, entry in state.entries.items() if valid(key)}
            self.status = f"Ownership cache: restored {len(state.entries)} revalidated associations."
            return state
        except FileNotFoundError:
            self.status = "Ownership cache: no saved history."
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            self.status = "Ownership cache unavailable or rejected; missing associations remain protected."
        finally:
            if directory is not None:
                os.close(directory)
        return CacheState()

    def sync(self, current: CacheState, valid):
        """Merge under a nonblocking interprocess lock; atomically replace our file."""
        directory = lock = None
        temporary = None
        try:
            directory = self._directory(create=True)
            lock = os.open("ownership.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                           0o600, dir_fd=directory)
            self._trusted_file(lock)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                merged = self._read(directory)
            except (ValueError, TypeError, RecursionError, OverflowError):
                # Invalid history supplies no evidence; only fresh validated
                # observations can rebuild an owned cache with a bad schema.
                merged = CacheState()
            for key, entry in current.entries.items():
                previous = merged.entries.get(key)
                if previous is None:
                    merged.entries[key] = entry
                else:
                    owners = tuple(sorted(set(previous.owners) | set(entry.owners)))
                    merged.entries[key] = SavedAssociation(
                        *key, owners[:MAX_OWNERS], max(previous.last_seen, entry.last_seen),
                        previous.incomplete or entry.incomplete or len(owners) > MAX_OWNERS)
            merged.entries = {key: entry for key, entry in merged.entries.items() if valid(key)}
            merged.entries = dict(sorted(merged.entries.items(), key=lambda item: item[1].last_seen)[-MAX_ENTRIES:])
            merged.anchors.update(current.anchors)
            merged.anchors = dict(list(merged.anchors.items())[-MAX_ANCHORS:])
            payload = json.dumps(dict(version=1, uid=self.uid, boot_id=self.boot_id, scope=self.scope,
                                      entries=[dict(pid=e.pid, birth_us=e.birth_us, owners=e.owners,
                                                    last_seen=e.last_seen, incomplete=e.incomplete)
                                               for e in merged.entries.values()],
                                      anchors=merged.anchors), separators=(",", ":")).encode()
            if len(payload) > MAX_BYTES:
                raise ValueError("Cache size limit reached")
            temporary = f".ownership-{secrets.token_hex(12)}.tmp"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.filename, src_dir_fd=directory, dst_dir_fd=directory)
            temporary = None
            self.status = f"Ownership cache: {len(merged.entries)} revalidated associations persisted."
            return merged
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            self.status = "Ownership cache save unavailable or busy; observing in memory, helpers protected."
            return None
        finally:
            if temporary is not None and directory is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError:
                    pass
            if lock is not None:
                os.close(lock)
            if directory is not None:
                os.close(directory)
