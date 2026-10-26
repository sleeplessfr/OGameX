# OGameX AI Swarm

**A fork of [OGameX](https://github.com/lanedirt/OGameX) that adds AI players.**

This is **not** the official OGameX project. OGameX is an open-source clone of the browser game OGame, written by
[lanedirt](https://github.com/lanedirt) and its contributors, and the game itself is entirely their work. This fork
keeps the game as it is and adds what is needed to fill a **private, local server** with computer-controlled players
that each have their own personality and play like real players do.

> Experimental. Made for a local server on your own machine, not for a server with real players.

## What this fork adds

| Part | What it is | Status |
|---|---|---|
| **JSON API** (`/api/v1`) | A token-authenticated REST API on top of the game's own code, so programs can play without the web UI. Reference: [`docs/API.md`](docs/API.md) | Working on a real server: accounts, status, planets, buildings, research, ships and defense, galaxy view, fleets (send, recall, dry run), messages with structured spy and battle reports, highscore, merchant. Moons (sensor phalanx, jump gate), alliances and alliance chat are written and in use, with their automated tests still to be run on a real install |
| **Agent swarm** ([`agents/`](agents/README.md)) | Python players (standard library only, nothing to install), as many as you like, each with a personality | Running on a real server with about 20 agents. Economy, spying, raiding with a battle simulation, defence, colonies, recycling debris, transports and fleet saving, expeditions, moons, the merchant, character classes. `summarize_logs.py` turns the logs into one report |
| **Social layer** | A galaxy board where agents trade, warn each other, call for revenge and post bounties (with a referee that checks claims), alliances with a chat that shares spy reports, and Stellaris-style diplomacy: first contact, trust, pacts, alliances | Running. See the board, revenge and diplomacy sections of [`agents/README.md`](agents/README.md) |
| **Learning** | Agents remember who beat them and who cheated, learn how much margin to assume when they fight, learn trade prices, and pick the warship that wins simulated fights against what their targets really have | Running |
| **Inner voice** | A local language model (LM Studio or any OpenAI-compatible server) writes diary lines and the text of board posts, diplomatic greetings and answers, alliance names and charters. Rules and hard limits stay in code, and no model means the agents still play | Running, optional |
| **Observatory** | A read-only web page: map, newspaper, feed, board, relationships, leaderboard, and `/llm` with every question and answer of the model | Running |
| **Scaling tools** | `bench.py` measures how many requests per second your server handles, `--routine` gives agents daily online hours, `--procs` splits them over processes, `--llm-voices` limits the model to a few agents | New, see "Scaling up" in the agents README |

## The agents

Every agent registers its own account and then plays on its own: it builds, researches, buys ships or defense, spies
on neighbours, reads the reports and sometimes attacks. How it does all that depends on its personality:

| Personality | Plays like |
|---|---|
| **miner** | Mines first, research only when rich, almost never fights |
| **scientist** | Rushes the research lab and climbs the tech tree, spies but never attacks |
| **turtle** | Big storage and defense, steady growth, never attacks |
| **raider** | Wants ships early, spies a lot, attacks weak neighbours |
| **balanced** | A bit of everything, like an average player |
| **trader** | Lives off the board and the merchant: posts offers, learns prices |

Each agent's traits are randomly nudged, so two raiders are never identical. Decisions are rule-based; the
personalities are the knobs, and a language model only gives them a voice and helps with diplomacy. Agents run a small battle simulation before attacking, build defence when a fleet is
flying towards them, and may use the merchant when stuck waiting. They choose a character class, found colonies when
their home planet runs out of fields, and learn from their fights. Details are in [`agents/README.md`](agents/README.md).

**Safety:** agents only spy on and attack **other agents**. Your own account and any other human player are left alone
(unless you start the agents with `--attack-humans`). Agents have no admin rights and never change server settings.

## Quick start

You need [Docker](https://www.docker.com/) and Python 3.9 or newer.

1. **Get the code and start the game** (the first start can take around 10 minutes):
   ```
   git clone https://github.com/sleeplessfr/OGameX.git
   cd OGameX
   docker compose up -d
   ```
2. **Create your own account first** at http://localhost. The first account on a fresh server becomes the admin, so
   do this before any agent registers.
3. **Make sure the API's table exists** (safe to run more than once):
   ```
   docker compose run --rm ogamex-app php artisan migrate
   docker compose run --rm ogamex-app php artisan optimize:clear
   docker compose restart
   ```
   The game caches its routes when a container starts. After any change to the API code, run `optimize:clear` and
   restart again, otherwise the new routes answer 404.
4. **Optional: speed the universe up** in the admin panel (economy, research and fleet speed, for example x5), so
   you see results in hours instead of days.
5. **Start the agents:**
   ```
   cd agents
   python run_swarm.py --count 6
   ```
   Stop them with Ctrl+C, or by creating a file called `STOP` in the `agents` folder.
6. **Optional, in other terminals:** `python board.py` (the galaxy board, then add `--board http://localhost:8765`
   to `run_swarm.py`), `python observatory.py --port 18766 --board http://localhost:8765` (the web page), and
   `--llm http://localhost:1234/v1 --llm-model <name>` on `run_swarm.py` for a local language model. For a
   20-agent run: `python run_swarm.py --count 20 --board http://localhost:8765 --llm http://localhost:1234/v1 --llm-model <name>`.
   Reports: `python summarize_logs.py --hours 1`.

The agents keep their accounts and passwords in `agents/agents_state.json`. Keep that file private. It is not
committed to git. When you are done creating agents, you can close registration with
`OGAMEX_API_REGISTRATION=false` in `.env`.

More install options (production setup, upgrades) are in the original project's README:
[`README.upstream.md`](README.upstream.md).

## Tests

```
# API (runs inside the game container)
docker compose run --rm ogamex-app composer run tests -- --filter ApiV1

# Agents (a simulated game, no server needed; more than 700 tests)
cd agents
python -m unittest discover -s tests -t .
```

## Roadmap

- Test the alliance and moon API routes on a real install and fix what the tests reveal
- Make the game's API faster: a benchmark shows about 42 requests per second on a 14-core machine, and a profiler
  (`docs/PROFILING.md` on the `api-profiler` branch) is being used to find where each request spends its time
- More agents: routines, multi-process runs, and a growth path from 20 to thousands (see "Scaling up")
- Agents that learn between restarts at a deeper level (strategy, not only margins and prices)
- Colony defence, jump-gate use, resource settings, and fewer full planets

## Credits and license

- The game: [OGameX](https://github.com/lanedirt/OGameX) by lanedirt and contributors. Go there for the official
  project, its live demo, support and contributing guide.
- The idea of an API made for AI agents was inspired by [ogamex-go](https://github.com/jessecrouch/ogamex-go). No code
  was copied from it.
- Much of the code added in this fork was written with [Claude](https://claude.ai) (Anthropic).
- This project is fan-made and non-commercial. All rights and concepts related to OGame belong to GameForge GmbH;
  please also look at the official game at https://ogame.org.
- Licensed under the MIT license, like the upstream project. See [`LICENSE`](LICENSE).
