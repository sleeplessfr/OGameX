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
from swarm.llm import make_llm
from swarm.personas import PRESETS, ROTATION, make_persona
from swarm.routine import MODES, make_routine


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
    ap.add_argument("--llm", default=None, metavar="URL",
                    help="an OpenAI-compatible server, e.g. http://localhost:1234/v1 (LM Studio), or 'fake' for canned answers without a server. "
                         "Default: off. The agents get a character, rewrite some board texts and keep a diary; play is unchanged")
    ap.add_argument("--llm-model", default="", help="model name to send (LM Studio: the one that is loaded)")
    ap.add_argument("--llm-timeout", type=float, default=60.0, help="seconds to wait for one answer (default 60)")
    ap.add_argument("--llm-max-per-hour", type=int, default=120, help="most requests per hour all agents together (default 120)")
    ap.add_argument("--llm-voices", type=int, default=0, metavar="N",
                    help="only agents number 1..N get the diary and the rewritten board texts; the others use plain template text. "
                         "Diplomacy answers still go through the model for everybody. Default 0 = every agent has a voice")
    ap.add_argument("--llm-per-agent-hour", type=int, default=0, metavar="N",
                    help="most model requests one agent may make per hour, so chatty agents cannot use up the shared budget (default 0 = no limit)")
    ap.add_argument("--routine", choices=MODES, default="off",
                    help="online hours like a real player: off = always playing (default), light = about half the day, real = about 5-15%% of the day. "
                         "An offline agent makes no requests at all")
    ap.add_argument("--time-scale", type=float, default=1.0,
                    help="compress the routine day: 24 = one routine day passes in one real hour (for testing; default 1)")
    ap.add_argument("--procs", type=int, default=1, metavar="N",
                    help="split the agents over N processes (one Python process fills about one processor core). Default 1")
    args = ap.parse_args()
    if args.time_scale <= 0:
        ap.error("--time-scale must be above 0")
    if args.procs < 1:
        ap.error("--procs must be at least 1")
    if args.llm and args.llm != "fake" and not args.llm.startswith(("http://", "https://")):
        ap.error("--llm must be 'fake' or an address like http://localhost:1234/v1")
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

    if args.procs > 1 and args.count > 1:
        from swarm.multiproc import run_children
        return run_children(args, sys.argv[1:])
    if args.count > 200:
        try:
            threading.stack_size(512 * 1024)     # thousands of threads: a small stack each keeps the memory down
        except (ValueError, RuntimeError):
            pass

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
        agents[-1].routine = make_routine(args.routine, agents[-1].name, persona.name, args.time_scale)
        agents[-1].voiced = args.llm_voices <= 0 or number <= args.llm_voices

    if args.board:
        for a in agents:
            a.board = make_board(args.board, a.name)
        up = agents[0].board.read(limit=1)["ok"] if agents else True
        print(f"Galaxy board: {args.board} ({'reachable' if up else 'NOT reachable yet: agents play without it until it is'})")
    if args.llm:
        shared = make_llm(args.llm, args.llm_model, args.llm_timeout, args.llm_max_per_hour, args.log_dir, args.llm_per_agent_hour)
        for a in agents:
            a.llm = shared      # one client for the whole process: one request at a time, one budget
        print(f"Language model: {args.llm} (model {args.llm_model or 'default'}, at most {args.llm_max_per_hour} requests an hour, log {args.log_dir}/llm.jsonl)")
        if args.llm_voices > 0:
            print(f"Voices: agents 1..{args.llm_voices} write in their own words, {sum(1 for a in agents if not a.voiced)} of these {len(agents)} use template text")
        if args.llm_per_agent_hour > 0:
            print(f"Quota: at most {args.llm_per_agent_hour} model requests per agent per hour")
    if args.routine != "off":
        online = sum(1 for a in agents if a.routine and a.routine.online(time.time()))
        print(f"Routine {args.routine} (time scale {args.time_scale:g}): {online} of {len(agents)} agents are online right now; the others wait for their next session")
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
