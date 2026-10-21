import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from . import __version__
from .models import age, memory
from .scan import Scanner
from .tui import clean, filtered, run


def main(argv=None):
    parser = argparse.ArgumentParser(description="Linger: read-only macOS process triage. No process actions exist.")
    parser.add_argument("--version", action="version", version=f"linger {__version__}")
    parser.add_argument("--once", action="store_true", help="Print one read-only snapshot instead of opening the TUI.")
    parser.add_argument("--json", action="store_true", help="Print one structured snapshot; contains local paths and chat IDs.")
    parser.add_argument("--demo", action="store_true", help="Use synthetic examples; do not inspect local processes or chats.")
    parser.add_argument("--scope", choices=("all", "codex", "review"), default="all")
    parser.add_argument("--codex-home", type=Path, help="Read a different Codex state directory (default: ~/.codex).")
    parser.add_argument("--no-cache", action="store_true", help="Do not read or write Linger's persistent ownership cache.")
    args = parser.parse_args(argv)
    scanner = None
    if args.demo:
        from .demo import snapshot
        source = snapshot
    else:
        scanner = Scanner(args.codex_home, persist=not args.no_cache)
        source = scanner.scan
    try:
        if args.once or args.json:
            snap = source()
            snap.findings = filtered(snap, args.scope, "", "stale")
            if args.json:
                print(json.dumps(asdict(snap), indent=2))
            else:
                print("LINGER | " + ("DEMO / SYNTHETIC" if snap.demo else "READ ONLY"))
                print("Score = suspicion, not probability or permission to kill. Memory = summed RSS.")
                print(f"{'PID':>7} {'SCORE':>5} {'CLASS':14} {'RSS':>7} {'AGE':>7} PROCESS")
                for f in snap.findings:
                    p = f.process
                    print(clean(f"{p.pid:7} {f.score:5} {f.label:14} {memory(p.rss_kib):>7} {age(snap.at-p.started):>7} {p.executable}"))
                    for reason in f.reasons:
                        print("        " + clean(reason))
                    if f.owners:
                        print("        Chat associations: " + ", ".join(f.owners))
                        print("        Source: " + f.ownership_kind)
                print(f"\nSAVED CHATS ({len(snap.chats)} recent; possible tool state always preserved)")
                for c in snap.chats:
                    print(clean(f"{c.status:7} {c.id}  {c.evidence}"))
                for warning in snap.warnings:
                    print("NOTE: " + clean(warning))
        else:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                parser.error("The TUI needs a terminal. Use --once, --json, or --demo --once here.")
            if scanner is not None:
                scanner.start()
            run(source, args.scope, args.demo)
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, OSError) as error:
        print(f"linger: {clean(error)}", file=sys.stderr)
        return 1
    finally:
        if scanner is not None:
            scanner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
