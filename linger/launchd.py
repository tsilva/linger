"""Read-only, identity-checked recognition of the current launchd domain.

Only list/list-label are invoked; diagnostic output is never emitted or cached.
The legacy detail format is advisory: unrecognized policy fields stay unknown.
"""

from dataclasses import replace
import os
import re
import subprocess
import time

from .models import Service
from .native import NativeReader

LABEL = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,511}\Z")
MAX_DETAILS = 64


def parse_list(text):
    result = {}
    for line in text.splitlines():
        parts = line.split(None, 2)
        if (len(parts) != 3 or not parts[0].isdigit() or not LABEL.fullmatch(parts[2])
                or not re.fullmatch(r"-?\d+", parts[1])):
            continue
        pid = int(parts[0])
        if 1 < pid < 2**31:
            if pid in result:
                result[pid] = None  # Ambiguous listings never establish ownership.
            else:
                result[pid] = parts[2]
    return {pid: label for pid, label in result.items() if label is not None}


def parse_policy(text, pid):
    """Recognize only the exact loaded-job PID and simple OnDemand boolean."""
    pids = re.findall(r'^\s*"PID"\s*=\s*(\d+);\s*$', text, re.M)
    demand = re.findall(r'^\s*"OnDemand"\s*=\s*(true|false);\s*$', text, re.M)
    if pids != [str(pid)] or len(demand) != 1:
        return "unknown"
    return "on demand or conditional" if demand[0] == "true" else "kept running"


def _read(*args):
    result = subprocess.run(["/bin/launchctl", "list", *args], capture_output=True,
                            text=True, timeout=2, check=True)
    if len(result.stdout) > 2 * 1024 * 1024:
        raise ValueError("Oversized launchd listing")
    return result.stdout


class LaunchdReader:
    def __init__(self, reader=None, read=None):
        self.reader = reader
        self.read = _read if read is None else read

    def services(self, rows):
        try:
            reader = self.reader or NativeReader()
            candidates = {p.pid: p for p in rows if p.uid == os.getuid() and p.ppid == 1}
            before = {pid: reader.identity(pid) for pid in candidates}
            text = self.read()
            listing = parse_list(text)
            if not listing and not re.search(r'^PID\s+Status\s+Label\s*$', text, re.M):
                raise ValueError("Unrecognized launchd listing")
            services = {}
            warnings = []
            details = 0
            deadline = time.monotonic() + 1
            limited = False
            for pid, label in listing.items():
                if pid not in candidates:
                    continue
                p, identity = candidates[pid], before[pid]
                if (identity is None or identity[0] // 1000000 != int(p.started)
                        or identity[1:] != (p.ppid, p.uid)):
                    continue
                policy = "unknown"
                if details < MAX_DETAILS and time.monotonic() < deadline:
                    details += 1
                    try:
                        policy = parse_policy(self.read(label), pid)
                    except (OSError, ValueError, subprocess.SubprocessError):
                        pass
                else:
                    limited = True
                if reader.identity(pid) == identity:
                    services[p.identity] = Service(label, policy)
            if limited:
                warnings.append("Launchd policies partially sampled; unqueried policies are unknown.")
            return services, warnings
        except (OSError, ValueError, subprocess.SubprocessError):
            return {}, ["Launchd service listing unavailable; PID 1 parentage alone remains inconclusive."]


def bind(findings, services):
    by_pid = {f.process.pid: f for f in findings}
    result = []
    for f in findings:
        current, seen = f, set()
        service = None
        while current.process.pid not in seen:
            seen.add(current.process.pid)
            service = services.get(current.process.identity)
            if service is not None or current.process.ppid not in by_pid:
                break
            current = by_pid[current.process.ppid]
        if service is None:
            result.append(f)
            continue
        direct = current.process.identity == f.process.identity
        reasons = (f"Verified launchd service{' descendant' if not direct else ''}: {service.label}.",
                   f"Loaded restart policy: {service.policy}; stopping may interrupt a service or trigger a restart.")
        result.append(replace(f, service=service, protected=True, score=0,
                              label=f.label if f.codex else "LAUNCHD",
                              reasons=(*reasons, *f.reasons) if f.protected else reasons))
    return result
