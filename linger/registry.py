"""Read-only observer for Codex's transient active REPL execution registry.

Associations are observations, never exclusive ownership or disposal approval.
Codex files are opened read-only. Optional persistence writes only Linger's own
cache, keyed by PID and native microsecond process birth time.
"""

from collections import OrderedDict
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import stat
import threading
import time

from .models import Process
from .native import NativeReader
from .cache import CacheState, SavedAssociation

UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
MAX_RECORD_BYTES = 8192
MAX_RECORDS = 1024
MAX_OWNERS = 64
POLL_SECONDS = 0.1


@dataclass(frozen=True)
class RegistryBinding:
    birth_us: int
    owners: tuple[str, ...]
    active_owners: tuple[str, ...]
    last_seen: float
    incomplete: bool = False


@dataclass
class _Entry:
    owners: set[str] = field(default_factory=set)
    active: set[str] = field(default_factory=set)
    last_seen: float = 0
    incomplete: bool = False


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("Duplicate registry field")
        obj[key] = value
    return obj


def parse_record(data: bytes, now: float) -> dict:
    if not data or len(data) > MAX_RECORD_BYTES:
        raise ValueError("Invalid registry record size")
    record = json.loads(data, object_pairs_hook=_unique_object)
    if not isinstance(record, dict) or type(record.get("version")) is not int or record["version"] != 1:
        raise ValueError("Unknown registry schema")
    for key in ("execId", "turnId"):
        if not isinstance(record.get(key), str) or not IDENTIFIER.fullmatch(record[key]):
            raise ValueError("Invalid execution identity")
    if not isinstance(record.get("sessionId"), str) or not UUID.fullmatch(record["sessionId"]):
        raise ValueError("Invalid chat identity")
    for key in ("nodeReplPid", "kernelPid"):
        if type(record.get(key)) is not int or not 1 < record[key] < 2**31:
            raise ValueError("Invalid process identity")
    if record["nodeReplPid"] == record["kernelPid"]:
        raise ValueError("Invalid process relationship")
    started = record.get("startedAtMs")
    if type(started) is not int or started < 0 or started > int(now * 1000):
        raise ValueError("Invalid execution timestamp")
    # Unknown fields may contain arbitrary data. Do not cache or emit them.
    return {key: record[key] for key in (
        "version", "execId", "sessionId", "turnId", "nodeReplPid", "kernelPid", "startedAtMs")}


def _fingerprint(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class RegistryObserver:
    def __init__(self, home: Path | None = None, *, reader=None, uid=None, cache_limit=2048, store=None):
        self.home = Path(home or Path.home() / ".codex")
        self.uid = os.getuid() if uid is None else uid
        self.cache_limit = max(1, cache_limit)
        self.cache: OrderedDict[tuple[int, int], _Entry] = OrderedDict()
        # Keep a record's first validated process identities while its filename
        # remains present, even if a referenced process exits. An unchanged old
        # record must not rebind itself to a reused PID in a later poll.
        self._anchors: dict[str, tuple] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_poll = 0.0
        self._last_prune = 0.0
        self._status = "Registry has not been sampled."
        self._ignored = 0
        self._evictions = 0
        self._truncated = False
        self.store = store
        self._loaded = False
        self._last_checkpoint = 0.0
        self._closed = False
        try:
            self.reader = reader if reader is not None else NativeReader()
        except (OSError, AttributeError):
            self.reader = None

    @staticmethod
    def _flags():
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def _trusted_directory(self):
        """Open each registry directory without following directory symlinks."""
        fd = os.open(self.home, self._flags())
        try:
            for name in (None, "node_repl", "active_execs"):
                if name is not None:
                    child = os.open(name, self._flags(), dir_fd=fd)
                    os.close(fd)
                    fd = child
                info = os.fstat(fd)
                if info.st_uid != self.uid or info.st_mode & 0o022:
                    raise OSError("Untrusted registry directory")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _record(self, directory, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != self.uid
                    or before.st_mode & 0o022 or not 0 < before.st_size <= MAX_RECORD_BYTES):
                return None
            data = os.read(fd, MAX_RECORD_BYTES + 1)
            if _fingerprint(before) != _fingerprint(os.fstat(fd)):
                return None
            record = parse_record(data, time.time())
            pids = (record["nodeReplPid"], record["kernelPid"])
            identities = tuple(self._identity(pid) for pid in pids)
            if any(info is None or info[2] != self.uid for info in identities):
                return None
            repl, kernel = identities
            if kernel[1] != pids[0]:
                return None
            # startedAtMs is millisecond-rounded; native birth is microseconds.
            if any(info[0] // 1000 > record["startedAtMs"] for info in identities):
                return None
            if identities != tuple(self._identity(pid) for pid in pids):
                return None
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if _fingerprint(before) != _fingerprint(current):
                return None
            return record["execId"], record["sessionId"].lower(), tuple((pid, info[0]) for pid, info in zip(pids, identities))
        finally:
            os.close(fd)

    def _identity(self, pid):
        try:
            return self.reader.identity(pid)
        except (OSError, ValueError, OverflowError):
            return None

    def _prune(self):
        for key in list(self.cache):
            if not self._valid_key(key):
                del self.cache[key]

    def _valid_key(self, key):
        if self.reader is None:
            return False
        pid, birth = key
        identity = self._identity(pid)
        return identity is not None and identity[0] == birth and identity[2] == self.uid

    def _merge_history(self, state):
        for key, saved in state.entries.items():
            if not self._valid_key(key):
                continue
            entry = self.cache.setdefault(key, _Entry())
            owners = sorted(entry.owners | set(saved.owners))
            entry.owners = set(owners[:MAX_OWNERS])
            entry.incomplete |= saved.incomplete or len(owners) > MAX_OWNERS
            entry.last_seen = max(entry.last_seen, saved.last_seen)
        # Current in-memory observations take precedence over imported anchors.
        self._anchors = {**state.anchors, **self._anchors}
        while len(self.cache) > self.cache_limit:
            self.cache.popitem(last=False)
            self._evictions += 1

    def _load_history(self):
        if not self._loaded:
            self._loaded = True
            if self.store is not None:
                self._merge_history(self.store.load(self._valid_key))

    def _checkpoint(self, force=False):
        if self.store is None or self.reader is None:
            return
        now = time.monotonic()
        if not force and now - self._last_checkpoint < 5:
            return
        self._last_checkpoint = now
        self._prune()
        state = CacheState(
            {key: SavedAssociation(*key, tuple(sorted(entry.owners)), entry.last_seen, entry.incomplete)
             for key, entry in self.cache.items()}, dict(self._anchors))
        merged = self.store.sync(state, self._valid_key)
        if merged is not None:
            self._merge_history(merged)

    def poll(self):
        """One bounded sample. No creation, cleanup, or writes in Codex home."""
        with self._lock:
            self._load_history()
            for entry in self.cache.values():
                entry.active.clear()
            self._last_poll = time.monotonic()
            self._ignored = 0
            self._truncated = False
            if self.reader is None:
                self._status = "Native identity reader unavailable; registry associations unknown."
                return
            if self._last_poll - self._last_prune >= 1:
                self._prune()
                self._last_prune = self._last_poll
            directory = None
            try:
                directory = self._trusted_directory()
                # scandir stops after the record limit; do not read an unbounded
                # directory listing or unrelated files into memory.
                names = []
                with os.scandir(directory) as items:
                    for count, item in enumerate(items):
                        if count >= MAX_RECORDS * 4:
                            self._truncated = True
                            break
                        if not item.name.endswith(".json"):
                            continue
                        if len(names) >= MAX_RECORDS:
                            self._truncated = True
                            break
                        names.append(item.name)
                self._anchors = {name: anchor for name, anchor in self._anchors.items() if name in names}
                for name in names:
                    if self._stop.is_set():
                        break
                    try:
                        observed = self._record(directory, name)
                    except (OSError, ValueError, TypeError, RecursionError):
                        observed = None
                    if observed is None:
                        self._ignored += 1
                        continue
                    exec_id, owner, keys = observed
                    anchor = self._anchors.get(name)
                    if anchor is not None and anchor[0] == exec_id and anchor != observed:
                        self._ignored += 1
                        continue
                    self._anchors[name] = observed
                    for key in keys:
                        # A new birth for the same PID cannot inherit history.
                        for old in [k for k in self.cache if k[0] == key[0] and k != key]:
                            del self.cache[old]
                        entry = self.cache.setdefault(key, _Entry())
                        if owner in entry.owners or len(entry.owners) < MAX_OWNERS:
                            entry.owners.add(owner)
                            entry.active.add(owner)
                        else:
                            entry.incomplete = True
                        entry.last_seen = time.time()
                        self.cache.move_to_end(key)
                        while len(self.cache) > self.cache_limit:
                            self.cache.popitem(last=False)
                            self._evictions += 1
                self._status = "Registry sampled read-only."
            except OSError:
                self._status = "Registry unavailable or untrusted; missing records never imply stale helpers."
            finally:
                if directory is not None:
                    os.close(directory)
                self._checkpoint()

    def snapshot(self, rows: list[Process]) -> tuple[dict, list[str]]:
        with self._lock:
            self._load_history()
            if self.reader is None:
                return {}, [self._status]
            self._prune()
            by_pid = {p.pid: p for p in rows}
            result = {}
            fresh = time.monotonic() - self._last_poll <= 1
            for (pid, birth), entry in self.cache.items():
                p = by_pid.get(pid)
                if p is None:
                    continue
                identity = self._identity(pid)
                if (identity is None or identity[0] != birth or identity[2] != p.uid
                        or identity[1] != p.ppid or birth // 1000000 != int(p.started)):
                    continue
                result[p.identity] = RegistryBinding(
                    birth, tuple(sorted(entry.owners)),
                    tuple(sorted(entry.active)) if fresh else (),
                    entry.last_seen, entry.incomplete,
                )
            suffix = f" Ignored {self._ignored} invalid/racing records." if self._ignored else ""
            if self._truncated:
                suffix += " Record limit reached; coverage incomplete."
            if self._evictions:
                suffix += f" {self._evictions} cache entries evicted; lost coverage stays unknown."
            warnings = [self._status + suffix]
            if self.store is not None:
                warnings.append(self.store.status)
            return result, warnings

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        if self._closed:
            raise RuntimeError("Registry observer is closed; create a new observer.")
        self._stop.clear()

        def observe():
            while not self._stop.is_set():
                try:
                    self.poll()
                except Exception:
                    # Preserve history but never keep an "active" flag after a
                    # failed observation. Do not expose file contents in errors.
                    with self._lock:
                        for entry in self.cache.values():
                            entry.active.clear()
                        self._status = "Registry observation failed; activity unknown, helpers protected."
                self._stop.wait(POLL_SECONDS)

        self._thread = threading.Thread(target=observe, name="linger-registry", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        with self._lock:
            if self._closed:
                return
            self._closed = True
            # Do not create a disk cache for a scanner that was never used.
            if self._loaded:
                self._checkpoint(force=True)
            self.cache.clear()
            self._anchors.clear()
