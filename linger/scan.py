import time

from .codex import CodexReader
from .models import Snapshot
from .processes import Sampler, collect
from .scoring import classify
from .native import contexts
from .ownership import bind
from .registry import RegistryObserver
from .cache import OwnershipStore


class Scanner:
    def __init__(self, codex_home=None, *, persist=True, cache_dir=None):
        self.sampler = Sampler()
        self.codex = CodexReader(codex_home)
        store = OwnershipStore(self.codex.home, directory=cache_dir) if persist else None
        self.registry = RegistryObserver(self.codex.home, store=store)

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
        return Snapshot(now, findings, chats, ownership_warnings + registry_warnings + native_warnings + warnings)
