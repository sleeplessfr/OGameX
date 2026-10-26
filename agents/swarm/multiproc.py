"""Several processes for a big swarm (`run_swarm.py --procs N`).

One Python process runs every agent as a thread, and Python lets only one thread compute at a time, so a few hundred agents
fill one processor core. `--procs N` splits the agent numbers into N slices and starts one run_swarm.py child per slice:

* each child gets `--first` / `--count` for its slice and everything else exactly as the parent got it;
* each child keeps its accounts in its own file `<state>.p<k>` (two processes never write one file); before the start the
  parent copies the accounts it already knows into the child files and, when all children are done, merges them back;
* the shared request pace is divided: every child gets `--max-rate / N` (and `--llm-max-per-hour / N`);
* the board and language-model addresses are passed on unchanged, so all children use the same ones;
* Ctrl+C (and the stop file, which every child watches itself) stops the children; the parent waits for them.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# options the parent rewrites for each child (given as "--x value" or "--x=value")
_REWRITTEN = ("--procs", "--first", "--count", "--state", "--max-rate", "--llm-max-per-hour")


def slices(first: int, count: int, procs: int) -> list[tuple[int, int]]:
    """Split agent numbers first..first+count-1 into `procs` consecutive slices (first_k, count_k); sizes differ by at most one."""
    procs = max(1, min(int(procs), int(count))) if count > 0 else 1
    base, extra = divmod(max(count, 0), procs)
    out, at = [], first
    for k in range(procs):
        n = base + (1 if k < extra else 0)
        out.append((at, n))
        at += n
    return out


def strip_options(argv: list[str], names=_REWRITTEN) -> list[str]:
    """argv without the given options (and their values)."""
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in names:
            skip = True
            continue
        if any(a.startswith(n + "=") for n in names):
            continue
        out.append(a)
    return out


def state_path(state: str, k: int) -> str:
    return f"{state}.p{k}"


def child_command(script: str, argv: list[str], k: int, first: int, count: int, state: str, max_rate: float, llm_per_hour: int,
                  procs: int, python: str | None = None) -> list[str]:
    """The command line of child number k (1-based)."""
    cmd = [python or sys.executable, script, *strip_options(argv),
           "--first", str(first), "--count", str(count), "--state", state_path(state, k), "--procs", "1"]
    if max_rate > 0:
        cmd += ["--max-rate", f"{max_rate / procs:.4f}"]
    else:
        cmd += ["--max-rate", "0"]
    cmd += ["--llm-max-per-hour", str(max(1, llm_per_hour // procs))]
    return cmd


def _read(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: str, data: dict) -> None:
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def seed_child_state(main_path: str, child_path: str, names: list[str]) -> int:
    """Give a child the accounts of its agents that the main state file already knows (only if the child has no file yet)."""
    if Path(child_path).exists():
        return 0
    known = _read(main_path)
    mine = {n: known[n] for n in names if n in known}
    if mine:
        _write(child_path, mine)
    return len(mine)


def merge_child_states(main_path: str, child_paths: list[str]) -> int:
    """Copy every child's accounts into the main state file (children win: they hold the newest tokens). Best effort."""
    merged = _read(main_path)
    n = 0
    for path in child_paths:
        for name, entry in _read(path).items():
            merged[name] = entry
            n += 1
    if n:
        try:
            _write(main_path, merged)
        except OSError:
            return 0
    return n


def run_children(args, argv: list[str], script: str | None = None, popen=subprocess.Popen, sleep=time.sleep) -> int:
    """Start the children, wait for them, forward Ctrl+C. Returns 0 if all exited cleanly, else 1."""
    script = script or str(Path(__file__).resolve().parent.parent / "run_swarm.py")
    parts = slices(args.first, args.count, args.procs)
    print(f"Starting {len(parts)} processes for agents {args.first}..{args.first + args.count - 1} "
          f"(each gets 1/{len(parts)} of --max-rate {args.max_rate:g}); accounts in {args.state}.p1 .. .p{len(parts)}")
    children, paths = [], []
    try:
        for k, (first, count) in enumerate(parts, start=1):
            path = state_path(args.state, k)
            paths.append(path)
            names = [f"{args.prefix}{n:04d}" for n in range(first, first + count)]
            seed_child_state(args.state, path, names)
            cmd = child_command(script, argv, k, first, count, args.state, args.max_rate, args.llm_max_per_hour, len(parts))
            children.append(popen(cmd))
        stop_seen = None
        while any(c.poll() is None for c in children):
            sleep(1)
            if args.stop_file and Path(args.stop_file).exists():
                stop_seen = stop_seen or time.monotonic()
                if time.monotonic() - stop_seen > 90:       # the children watch the file themselves; this is the safety net
                    raise KeyboardInterrupt
    except KeyboardInterrupt:
        print("\nStopping the processes...")
        for c in children:
            try:
                c.send_signal(signal.SIGINT)
            except Exception:
                pass
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and any(c.poll() is None for c in children):
            sleep(0.5)
        for c in children:
            if c.poll() is None:
                c.terminate()
    merged = merge_child_states(args.state, paths)
    print(f"All processes ended; {merged} accounts merged into {args.state}.")
    return 0 if all((c.poll() or 0) == 0 for c in children) else 1
