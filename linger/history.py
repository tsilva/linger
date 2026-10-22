"""Bounded display history; never ownership evidence or persistent tool state."""

from collections import deque
from dataclasses import dataclass
import math
import time

from .models import Finding, Snapshot


@dataclass(frozen=True)
class Sample:
    at: float
    cpu_seconds: float
    rss_kib: int
    cpu_percent: float | None


class History:
    """One minute of actual samples, isolated by process and native identity."""

    def __init__(self, seconds=60, limit=120):
        self.seconds = seconds
        self.limit = limit
        self.series = {}

    @staticmethod
    def key(finding: Finding):
        p = finding.process
        return p.pid, p.started, finding.birth_us

    def observe(self, snapshot: Snapshot, now=None):
        now = time.monotonic() if now is None else now
        current = {}
        for finding in snapshot.findings:
            key = self.key(finding)
            p = finding.process
            points = self.series.get(key, deque(maxlen=self.limit))
            previous = points[-1] if points else None
            # Long gaps and backwards counters cannot supply current CPU usage.
            if previous and (now <= previous.at or now - previous.at > self.seconds
                             or p.cpu_seconds < previous.cpu_seconds):
                points = deque(maxlen=self.limit)
                previous = None
            cpu = None
            if previous:
                cpu = 100 * (p.cpu_seconds - previous.cpu_seconds) / (now - previous.at)
                if not math.isfinite(cpu):
                    cpu = None
            while points and points[0].at < now - self.seconds:
                points.popleft()
            points.append(Sample(now, p.cpu_seconds, p.rss_kib, cpu))
            current[key] = points
        # Exited processes and reused identities cannot inherit display history.
        self.series = current

    def points(self, finding):
        return tuple(self.series.get(self.key(finding), ()))

    def cpu(self, finding):
        points = self.points(finding)
        return points[-1].cpu_percent if points else None

    def changing(self, finding):
        points = self.points(finding)
        cpus = [p.cpu_percent for p in points if p.cpu_percent is not None]
        return ((len(cpus) > 1 and max(cpus) - min(cpus) > 0.1)
                or len({p.rss_kib for p in points}) > 1)


def sparkline(values, width=12, unicode=True):
    """Show measured values only; a constant series is deliberately plain."""
    values = list(values)[-width:]
    if len(values) < 2 or max(values) - min(values) <= 0.1:
        return "steady"
    levels = "▁▂▃▄▅▆▇█" if unicode else "._-:=+*#"
    low, high = min(values), max(values)
    return "".join(levels[round((v - low) / (high - low) * (len(levels) - 1))]
                   for v in values)
