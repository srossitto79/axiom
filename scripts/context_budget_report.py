"""Summarize context_budget.jsonl — the Task 0 peak-window measurements.

Run after letting the system process tasks for a few hours. By default it reads
the file straight from the running container (the data lives in the axiom_data
Docker volume, not on the host):

    python scripts/context_budget_report.py                 # read from container
    python scripts/context_budget_report.py path/to.jsonl   # read a local file
    python scripts/context_budget_report.py --container axiom-backend
    python scripts/context_budget_report.py --remote-path /data/workspace/context_budget.jsonl
    python scripts/context_budget_report.py --local         # only look on local FS

Prints, per agent, the distribution of PEAK single-round input tokens — the
number a local context window must actually hold — next to the accumulated sum
(the misleading figure in the old tables).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

FILENAME = "context_budget.jsonl"
DEFAULT_CONTAINER = "axiom-backend"
DEFAULT_REMOTE_PATH = "/data/workspace/context_budget.jsonl"


def _local_candidate_paths(explicit: str | None) -> list[Path]:
    paths: list[Path] = []
    if explicit:
        paths.append(Path(explicit))
    try:
        from axiom.config import WORKSPACE_DIR, LEGACY_WORKSPACE_DIR

        for root in (WORKSPACE_DIR, LEGACY_WORKSPACE_DIR):
            if root:
                paths.append(Path(root) / FILENAME)
    except Exception:
        pass
    paths.append(Path(FILENAME))
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        try:
            key = str(p.resolve())
        except Exception:
            key = str(p)
        if key and key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _read_local(explicit: str | None) -> list[str] | None:
    for path in _local_candidate_paths(explicit):
        if path.exists():
            print(f"Reading local file {path}\n")
            return path.read_text(encoding="utf-8").splitlines()
    return None


def _read_from_container(container: str, remote_path: str) -> list[str] | None:
    """Cat the JSONL out of the running container via `docker exec`.

    Uses an arg list (no shell) so the absolute /data path is not mangled by
    Git Bash path conversion.
    """
    try:
        proc = subprocess.run(
            ["docker", "exec", container, "cat", remote_path],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        print("docker not found on PATH — pass a local file path instead.", file=sys.stderr)
        return None
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        print(f"Could not read {remote_path} from container '{container}': {err}", file=sys.stderr)
        return None
    print(f"Reading {container}:{remote_path}\n")
    return proc.stdout.splitlines()


def _parse(lines: list[str]) -> list[dict]:
    rows: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _load(args: argparse.Namespace) -> list[dict]:
    lines: list[str] | None = None
    # An explicit local path always wins. Otherwise: container first (that's
    # where the data is), then local FS — unless --local forces local-only.
    if args.path:
        lines = _read_local(args.path)
    elif args.local:
        lines = _read_local(None)
    else:
        lines = _read_from_container(args.container, args.remote_path)
        if lines is None:
            lines = _read_local(None)
    if lines is None:
        print(f"No {FILENAME} found (container or local).", file=sys.stderr)
        sys.exit(1)
    return _parse(lines)


def _pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    idx = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return s[idx]


def _fmt(n: int) -> str:
    return f"{n:,}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", help="local JSONL file to read (skips the container)")
    parser.add_argument("--container", default=DEFAULT_CONTAINER, help=f"docker container name (default: {DEFAULT_CONTAINER})")
    parser.add_argument("--remote-path", default=DEFAULT_REMOTE_PATH, help=f"path inside the container (default: {DEFAULT_REMOTE_PATH})")
    parser.add_argument("--local", action="store_true", help="only read from the local filesystem, never the container")
    args = parser.parse_args()

    rows = _load(args)
    if not rows:
        print("File is empty — no tasks measured yet.")
        return

    groups: dict[str, list[dict]] = {}
    for r in rows:
        key = str(r.get("agent_name") or r.get("role") or r.get("agent_id") or "unknown")
        groups.setdefault(key, []).append(r)

    print(f"Total tasks measured: {len(rows)}\n")
    header = (
        f"{'Agent':<24} {'N':>4} "
        f"{'peak P50':>10} {'peak P90':>10} {'peak P99':>10} {'peak max':>10} "
        f"{'rounds P50':>11} {'accum P50':>11}"
    )
    print(header)
    print("-" * len(header))

    for key in sorted(groups, key=lambda k: -_pct([d.get("peak_input_tokens") or 0 for d in groups[k]], 0.5)):
        items = groups[key]
        peaks = [int(d.get("peak_input_tokens") or 0) for d in items]
        accum = [int(d.get("accumulated_input_tokens") or 0) for d in items]
        rounds = [int(d.get("rounds") or 0) for d in items]
        print(
            f"{key[:24]:<24} {len(items):>4} "
            f"{_fmt(_pct(peaks, 0.5)):>10} {_fmt(_pct(peaks, 0.9)):>10} "
            f"{_fmt(_pct(peaks, 0.99)):>10} {_fmt(max(peaks)):>10} "
            f"{_pct(rounds, 0.5):>11} {_fmt(_pct(accum, 0.5)):>11}"
        )

    print("\nFit check - share of tasks whose PEAK fits a given window:")
    for window in (32_000, 64_000, 128_000, 256_000):
        usable = int(window * 0.90)
        fit = sum(1 for r in rows if int(r.get("peak_input_tokens") or 0) <= usable)
        print(f"  {window // 1000:>4}K window (~{usable // 1000}K usable): {fit}/{len(rows)} ({100 * fit / len(rows):.0f}%)")


if __name__ == "__main__":
    main()
