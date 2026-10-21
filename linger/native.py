"""Passive macOS process identity and allowlisted launch-context reads.

KERN_PROCARGS2 is NUL-delimited, unlike `ps eww` whose arguments and environment
cannot safely be distinguished. Raw arguments/environment never leave this
module. In particular, credentials must not enter snapshots, caches, or errors.
"""

import ctypes
from dataclasses import dataclass
import os
import platform
import re
import struct

from .models import Process

THREAD = re.compile(rb"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
KEYS = {b"CODEX_THREAD_ID", b"CODEX_SESSION_ID"}
MAX_ARGUMENT_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Context:
    birth_us: int
    threads: tuple[str, ...] = ()
    conflict: bool = False
    readable: bool = True


class BsdInfo(ctypes.Structure):
    _fields_ = (
        [(k, ctypes.c_uint32) for k in (
            "flags", "status", "xstatus", "pid", "ppid", "uid", "gid",
            "ruid", "rgid", "svuid", "svgid", "rfu1")]
        + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
        + [(k, ctypes.c_uint32) for k in ("nfiles", "pgid", "pjobc", "e_tdev", "e_tpgid")]
        + [("nice", ctypes.c_int32), ("startsec", ctypes.c_uint64), ("startusec", ctypes.c_uint64)]
    )


def decode_context(data: bytes) -> tuple[tuple[str, ...], bool]:
    """Extract only two ownership-related keys from the *environment* region.

    A CODEX_THREAD_ID-looking argument must never be interpreted as environment.
    Empty/malformed/duplicate/conflicting bindings fail closed.
    """
    if len(data) < 5 or not data.endswith(b"\0"):
        raise ValueError("Incomplete process arguments")
    argc = struct.unpack_from("=i", data)[0]
    if argc < 1 or argc > 65536:
        raise ValueError("Invalid argument count")
    cursor = data.find(b"\0", 4)
    if cursor < 0:
        raise ValueError("Missing executable boundary")
    cursor += 1
    while cursor < len(data) and data[cursor] == 0:
        cursor += 1
    for _ in range(argc):
        end = data.find(b"\0", cursor)
        if end < 0:
            raise ValueError("Missing argument boundary")
        cursor = end + 1
    values: set[str] = set()
    seen: set[bytes] = set()
    conflict = False
    for entry in data[cursor:].split(b"\0"):
        if not entry:
            break
        key, sep, value = entry.partition(b"=")
        if sep and key in KEYS:
            if key in seen or not THREAD.fullmatch(value):
                conflict = True
            seen.add(key)
            if THREAD.fullmatch(value):
                values.add(value.decode("ascii").lower())
    return tuple(sorted(values)), conflict or len(values) > 1


class NativeReader:
    def __init__(self):
        if platform.system() != "Darwin":
            raise OSError("Native process context is macOS-only")
        self.libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.libproc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        self.libproc.proc_pidinfo.restype = ctypes.c_int
        self.libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        self.libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        self.libc.sysctl.restype = ctypes.c_int

    def identity(self, pid: int) -> tuple[int, int, int] | None:
        info = BsdInfo()
        size = self.libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if size != ctypes.sizeof(info) or info.pid != pid or info.startusec >= 1000000:
            return None
        return info.startsec * 1000000 + info.startusec, info.ppid, info.uid

    def arguments(self, pid: int) -> bytes:
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, pid
        size = ctypes.c_size_t()
        if self.libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
            raise OSError("Process context unavailable")
        if size.value < 5 or size.value > MAX_ARGUMENT_BYTES:
            raise OSError("Process context size unsupported")
        buffer = ctypes.create_string_buffer(size.value)
        if self.libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
            raise OSError("Process context unavailable")
        return buffer.raw[:size.value]

    def read(self, p: Process) -> Context | None:
        before = self.identity(p.pid)
        if before is None:
            return None
        birth, parent, uid = before
        # ps start time is local wall time with one-second resolution.
        if birth // 1000000 != int(p.started) or parent != p.ppid or uid != p.uid:
            return None
        try:
            threads, conflict = decode_context(self.arguments(p.pid))
            readable = True
        except (OSError, ValueError):
            threads, conflict, readable = (), False, False
        if self.identity(p.pid) != before:
            return None
        return Context(birth, threads, conflict, readable)


def contexts(rows: list[Process]) -> tuple[dict[tuple[int, float], Context], list[str]]:
    """Read same-user Codex lineage only; never scan unrelated environments."""
    from .processes import is_codex_executable
    by_pid = {p.pid: p for p in rows}

    def candidate(p):
        seen = set()
        while p.pid not in seen:
            if is_codex_executable(p.executable):
                return True
            seen.add(p.pid)
            if p.ppid not in by_pid:
                break
            p = by_pid[p.ppid]
        return False

    try:
        reader = NativeReader()
    except (OSError, AttributeError):
        return {}, ["Native launch-context reader unavailable; ownership remains unknown."]
    result = {}
    for p in rows:
        if p.uid == os.getuid() and candidate(p):
            context = reader.read(p)
            if context is not None:
                result[p.identity] = context
    return result, []
