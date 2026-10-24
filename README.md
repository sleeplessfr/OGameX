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
| **JSON API** (`/api/v1`) | A token-authenticated REST API on top of the game's own code, so programs can play without the web UI. Reference: [`docs/API.md`](docs/API.md) | Working and tested on a real server: accounts, status, planets, buildings, research, ships and defense, galaxy view, fleets (send, recall, dry run), messages with structured spy and battle reports, highscore |
| **Agent swarm** ([`agents/`](agents/README.md)) | Python players (standard library only, no packages to install), as many as you like, each with a personality | Working. Economy behaviour has run on a real server; spying and attacking have so far only been tested against a simulated game |
| **Learning layer** | Agents that remember who beat them, adapt, and use a language model for the big decisions | Planned |

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

Each agent's traits are randomly nudged, so two raiders are never identical. Decisions are rule-based for now; the
personalities are the knobs. Details are in [`agents/README.md`](agents/README.md).

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

The agents keep their accounts and passwords in `agents/agents_state.json`. Keep that file private. It is not
committed to git. When you are done creating agents, you can close registration with
`OGAMEX_API_REGISTRATION=false` in `.env`.

More install options (production setup, upgrades) are in the original project's README:
[`README.upstream.md`](README.upstream.md).

## Tests

```
# API (runs inside the game container)
docker compose run --rm ogamex-app composer run tests -- --filter ApiV1

# Agents (a simulated game, no server needed)
cd agents
python -m unittest discover -s . -t . -p "test_*.py"
```

## Roadmap

- Test the spying and attacking on a real server and fix what real reports reveal
- Read battle results, remember who is dangerous, adapt (first simple learning)
- Colonising and expeditions as agent behaviour, more personalities
- A language model for the big decisions, with a budget cap and per-agent memory
- A battle simulator endpoint, alliances, trading

## Credits and license

- The game: [OGameX](https://github.com/lanedirt/OGameX) by lanedirt and contributors. Go there for the official
  project, its live demo, support and contributing guide.
- The idea of an API made for AI agents was inspired by [ogamex-go](https://github.com/jessecrouch/ogamex-go). No code
  was copied from it.
- Much of the code added in this fork was written with [Claude](https://claude.ai) (Anthropic).
- This project is fan-made and non-commercial. All rights and concepts related to OGame belong to GameForge GmbH;
  please also look at the official game at https://ogame.org.
- Licensed under the MIT license, like the upstream project. See [`LICENSE`](LICENSE).
