import time

from .codex import CodexReader
from .models import Snapshot
from .processes import Sampler, collect
from .scoring import classify
from .native import contexts
from .ownership import bind
from .registry import RegistryObserver
from .cache import OwnershipStore
from .launchd import LaunchdReader, bind as bind_services
from .tracking import RunStore, bind as bind_tracking
from .native import NativeReader


class Scanner:
    def __init__(self, codex_home=None, *, persist=True, cache_dir=None):
        self.sampler = Sampler()
        self.codex = CodexReader(codex_home)
        store = OwnershipStore(self.codex.home, directory=cache_dir) if persist else None
        self.registry = RegistryObserver(self.codex.home, store=store)
        self.launchd = LaunchdReader()
        self.launches = RunStore() if persist else None

    def start(self):
        self.registry.start()

    def close(self):
        self.registry.close()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.close()

    def scan(self) -> Snapshot:
        now = time.time()
        rows = self.sampler.observe(collect())
        if not self.registry.running:
            self.registry.poll()
        context, native_warnings = contexts(rows)
        registry, registry_warnings = self.registry.snapshot(rows)
        chats, warnings = self.codex.read(now)
        findings, ownership_warnings = bind(classify(rows, now), context, chats, self.codex.saved_ids, registry)
        services, service_warnings = self.launchd.services(rows)
        findings = bind_services(findings, services)
        tracking_warnings = []
        if self.launches is not None:
            records, tracking_warnings = self.launches.read()
            if records:
                try:
                    # Partial record coverage must not produce leftover recommendations.
                    if tracking_warnings:
                        records = [dict(record, incomplete=True) for record in records]
                    findings = bind_tracking(findings, records, NativeReader())
                except (OSError, ValueError):
                    tracking_warnings.append("Launch tracking identity verification unavailable; no lifecycle recommendations applied.")
        return Snapshot(now, findings, chats, ownership_warnings + registry_warnings + native_warnings + warnings + service_warnings + tracking_warnings)
