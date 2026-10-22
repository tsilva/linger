from dataclasses import dataclass, field


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    uid: int
    rss_kib: int
    started: float
    cpu_seconds: float
    state: str
    tty: str
    executable: str
    observed_idle: float = 0
    sampled: bool = False

    @property
    def identity(self) -> tuple[int, float]:
        return self.pid, self.started


@dataclass(frozen=True)
class Chat:
    id: str
    status: str
    evidence: str
    updated: float
    # Every saved chat may own persistent in-memory tool state. The transcript
    # cannot establish whether that state exists, or which process owns it.
    tool_state: str = "possible; preserve"


@dataclass(frozen=True)
class Service:
    label: str
    policy: str = "unknown"


@dataclass(frozen=True)
class TrackedRun:
    id: str
    temporary: bool
    status: str
    exit_code: int | None = None


@dataclass(frozen=True)
class Finding:
    process: Process
    label: str
    score: int
    reasons: tuple[str, ...]
    codex: bool = False
    protected: bool = False
    # Associations do not imply exclusive ownership or safe disposal.
    owner: str = "unresolved"
    ownership_kind: str = "unknown"
    birth_us: int | None = None
    owners: tuple[str, ...] = ()
    registry_active: bool = False
    registry_observed_at: float | None = None
    service: Service | None = None
    tracking: tuple[TrackedRun, ...] = ()


@dataclass
class Snapshot:
    at: float
    findings: list[Finding]
    chats: list[Chat] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    demo: bool = False


def memory(kib: int) -> str:
    return f"{kib / 1048576:.2f}G" if kib >= 1048576 else f"{kib / 1024:.1f}M"


def age(seconds: float) -> str:
    seconds = max(0, seconds)
    if seconds >= 86400:
        return f"{seconds / 86400:.1f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.0f}m"
