"""Bounded macOS ps reads. No environment or command arguments are collected."""

import datetime as dt
import os
import platform
import re
import subprocess
import time

from .models import Process


def cpu_time(value: str) -> float:
    days = 0
    if "-" in value:
        day, value = value.split("-", 1)
        days = int(day)
    parts = [float(p) for p in value.split(":")]
    if len(parts) not in (2, 3):
        raise ValueError("unknown CPU time format")
    result = 0.0
    for part in parts:
        result = result * 60 + part
    return days * 86400 + result


def parse_ps(output: str) -> list[Process]:
    rows = []
    for line in output.splitlines():
        # lstart is five whitespace-separated fields. comm is an executable
        # path (including spaces), never the command's argument vector.
        parts = line.split(None, 12)
        if len(parts) != 13:
            continue
        try:
            started = dt.datetime.strptime(" ".join(parts[4:9]), "%a %b %d %H:%M:%S %Y").timestamp()
            rows.append(Process(
                pid=int(parts[0]), ppid=int(parts[1]), uid=int(parts[2]),
                rss_kib=int(parts[3]), started=started,
                cpu_seconds=cpu_time(parts[9]), state=parts[10],
                tty=parts[11], executable=parts[12],
            ))
        except (ValueError, OverflowError):
            continue
    return rows


def collect() -> list[Process]:
    if platform.system() != "Darwin":
        raise RuntimeError("Live collection currently supports macOS only; use --demo elsewhere.")
    result = subprocess.run(
        ["/bin/ps", "-axww", "-o", "pid=,ppid=,uid=,rss=,lstart=,time=,state=,tty=,comm="],
        capture_output=True, text=True, timeout=5, check=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    rows = parse_ps(result.stdout)
    if not rows:
        raise RuntimeError("No process rows could be parsed; collection format may have changed.")
    return rows


class Sampler:
    """Measure inactivity since Linger began, never infer it from process age."""

    def __init__(self):
        self.previous: dict[tuple[int, float], tuple[float, float]] = {}

    def observe(self, rows: list[Process], monotonic: float | None = None) -> list[Process]:
        from dataclasses import replace
        now = time.monotonic() if monotonic is None else monotonic
        next_state = {}
        observed = []
        for p in rows:
            previous = self.previous.get(p.identity)
            idle_since = now
            sampled = False
            if previous is not None:
                cpu, last_idle = previous
                sampled = True
                if p.cpu_seconds == cpu:
                    idle_since = last_idle
            next_state[p.identity] = (p.cpu_seconds, idle_since)
            observed.append(replace(p, observed_idle=max(0, now - idle_since), sampled=sampled))
        self.previous = next_state
        return observed


def own_ancestry(rows: list[Process]) -> set[int]:
    parents = {p.pid: p.ppid for p in rows}
    result: set[int] = set()
    pid = os.getpid()
    while pid and pid not in result:
        result.add(pid)
        pid = parents.get(pid, 0)
    return result


def is_codex_executable(path: str) -> bool:
    return bool(re.search(r"/(?:ChatGPT|Codex)\.app/", path)) or path.rsplit("/", 1)[-1] == "codex"
