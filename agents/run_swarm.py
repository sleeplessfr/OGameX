#!/usr/bin/env python3
"""Start a swarm of AI players against a local OGameX server.

Example:  python run_swarm.py --count 5
"""
import argparse
import random
import sys
import threading
import time

from swarm.agent import Agent, TokenStore
from swarm.boardclient import is_local_url, make_board
from swarm.client import ApiClient, ApiError
from swarm.personas import PRESETS, ROTATION, make_persona


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://localhost/api/v1")
    ap.add_argument("--count", type=int, default=3, help="number of agents (any number)")
    ap.add_argument("--prefix", default="agent", help="agent names are <prefix>0001, <prefix>0002, ...")
    ap.add_argument("--first", type=int, default=1, help="number of the first agent (to add more agents later)")
    ap.add_argument("--personas", default="", help=f"comma list to rotate through; default: {','.join(ROTATION)} (also available: {','.join(p for p in PRESETS if p not in ROTATION)})")
    ap.add_argument("--seed", type=int, default=1, help="same seed = same personalities")
    ap.add_argument("--state", default="agents_state.json", help="where accounts/tokens are remembered")
    ap.add_argument("--log-dir", default="logs")
    ap.add_argument("--memory-dir", default="memory", help="where each agent saves what it has learned (survives restarts)")
    ap.add_argument("--stagger", type=float, default=2.0, help="seconds between agent starts (registration is rate limited)")
    ap.add_argument("--max-rate", type=float, default=4.0, help="most requests per second all agents together may send (4 = 240 a minute; raise it together with OGAMEX_API_RATE_LIMIT in the game's .env; 0 = no limit)")
    ap.add_argument("--ticks", type=int, default=0, help="stop each agent after N rounds (0 = run until Ctrl+C)")
    ap.add_argument("--speedup", type=float, default=1.0, help="divide every pause by this (for quick tests)")
    ap.add_argument("--stop-file", default="STOP", help="create this file to stop all agents")
    ap.add_argument("--email-domain", default="example.com")
    ap.add_argument("--attack-humans", action="store_true",
                    help="allow agents to spy on / attack players that are not agents (default: agents only)")
    ap.add_argument("--board", default=None, metavar="URL",
                    help="the galaxy board (start board.py first), e.g. http://localhost:8765. Default: none = agents do not use a board. "
                         "If the board is not running the agents just play without it")
    args = ap.parse_args()
    if args.board and not is_local_url(args.board):
        ap.error("--board must be an http://localhost:PORT or http://127.0.0.1:PORT address: the board is for this computer only")

    names = [n.strip() for n in args.personas.split(",") if n.strip()] or None
    for n in names or []:
        if n not in PRESETS:
            ap.error(f"unknown persona {n!r}; choose from {', '.join(PRESETS)}")

    try:  # fail early with a clear message if the server is not there
        ApiClient(args.base_url).get("/universe")
    except Exception as e:
        print(f"Cannot reach the API at {args.base_url}: {e}", file=sys.stderr)
        return 1

    ApiClient.shared_interval = 1.0 / args.max_rate if args.max_rate > 0 else 0.0
    store = TokenStore(args.state)
    stop = threading.Event()
    agents: list[Agent] = []
    for i in range(args.count):
        number = args.first + i
        rng = random.Random(args.seed * 100003 + number)
        persona = make_persona(number - 1, rng, names)
        agents.append(Agent(f"{args.prefix}{number:04d}", persona, args.base_url, store, rng,
                            log_dir=args.log_dir, email_domain=args.email_domain,
                            attack_humans=args.attack_humans, memory_dir=args.memory_dir))

    if args.board:
        for a in agents:
            a.board = make_board(args.board, a.name)
        up = agents[0].board.read(limit=1)["ok"] if agents else True
        print(f"Galaxy board: {args.board} ({'reachable' if up else 'NOT reachable yet: agents play without it until it is'})")
    threads = []
    print(f"Starting {len(agents)} agents on {args.base_url}. Ctrl+C or create a file named {args.stop_file} to stop.")
    for agent in agents:
        t = threading.Thread(target=agent.run, name=agent.name, daemon=True, kwargs=dict(
            stop=stop, max_ticks=args.ticks, speedup=args.speedup, stop_file=args.stop_file))
        t.start()
        threads.append(t)
        stop.wait(args.stagger / max(args.speedup, 0.01))

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        stop.set()
        for t in threads:
            t.join(timeout=10)

    print("\nSummary:")
    for a in agents:
        print(f"  {a.name:<12} {a.persona.name:<10} {a.stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
