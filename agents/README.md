# OGameX agent swarm

Lives in the `agents/` folder of the OGameX repository. Run every command from inside this folder.

AI players for a local OGameX server. They talk to the `/api/v1` JSON API (see `docs/API.md` in the OGameX
repo) like a real player would use the web UI: build, research, wait, repeat. Python 3.9+, **no packages to install**.

## Run

```
python run_swarm.py --count 5
```

- `--count N` any number of agents. Each gets its own account, planet and personality.
- `--personas miner,raider` limit / reorder the personalities (default: all, in rotation).
  Available: `miner`, `scientist`, `turtle`, `raider`, `balanced`. Every agent's weights are randomly nudged,
  so two "miners" are never identical. Same `--seed` = same personalities.
- `--first 6 --count 5` add five more agents later (agent0006 .. agent0010) without touching the old ones.
- Accounts are remembered in `agents_state.json` (email, password, token). Delete the file to start with new
  accounts. **Keep it private.**
- Logs: one JSON-lines file per agent in `logs/`, plus a short line per event on screen.
- Stop: Ctrl+C, or create a file called `STOP` in this folder (delete it before the next run).
- Quick test: `--ticks 5 --speedup 20` plays 5 rounds with pauses 20x shorter.

## How an agent decides

Each round it reads `/status`, then, for every planet whose queue is empty, the list of buildings:

1. Low energy -> solar plant first (it saves up for it rather than buying a mine that would starve).
2. A resource store nearly full -> build that store.
3. Otherwise score every available building by the personality's weights, spread over levels (`balance`),
   plus a little noise. If the top choice is too expensive it saves up (`patience`) or buys the best
   affordable thing.

Research works the same way once a research lab exists. The agent sleeps until its current job finishes (or
until it can afford the next one), with random jitter, so it only asks the server when something can change.

## Not built yet

Fleets, expeditions, attacking/spying, messages, trading, and any learning from experience. The brain is
rule-based for now; the personas are the knobs. Learning and LLM-driven decisions come on top of this.

## Tests

```
python -m unittest discover -s . -t . -p "test_*.py"
```

They run against a small simulated game (`tests/fakegame.py`), not your server.
