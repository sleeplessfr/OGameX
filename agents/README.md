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
- `--max-rate 12` most requests per second all agents together send (default 4). Raise it with more agents, together with
  `OGAMEX_API_RATE_LIMIT` in the game's `.env` (default 300 per minute per account).
- `--first 6 --count 5` add five more agents later (agent0006 .. agent0010) without touching the old ones.
- Accounts are remembered in `agents_state.json` (email, password, token). Delete the file to start with new
  accounts. **Keep it private.**
- Logs: one JSON-lines file per agent in `logs/`, plus a short line per event on screen.
- Stop: Ctrl+C, or create a file called `STOP` in this folder (delete it before the next run).
- Quick test: `--ticks 5 --speedup 20` plays 5 rounds with pauses 20x shorter.

## Reading what happened

```
python summarize_logs.py            (writes report.txt and prints it)
python summarize_logs.py --hours 6  (only the last 6 hours)
```

One page: what each agent did, who won or lost its attacks, defence, merchant, lessons and errors. Attach `report.txt` when asking for help.

## Tuning the economy offline

```
python tune_economy.py --persona balanced --generations 25
python tune_economy.py --persona miner --check
```

Plays thousands of planets in minutes without a server (the game's own formulas plus the agent's real building decisions)
and searches for better weights for the mines, solar plant and robot factory. It only prints suggestions; nothing is
changed. Stores, balance, patience and store alarm (a personality's character) are left alone unless you add `--with-stores` or `--with-traits`.

## How an agent decides

Each round it reads `/status`, then, for every planet whose queue is empty, the list of buildings:

1. Low energy -> solar plant first (it saves up for it rather than buying a mine that would starve).
2. A resource store nearly full -> build that store.
3. Otherwise score every available building by the personality's weights, spread over levels (`balance`),
   plus a little noise. If the top choice is too expensive it saves up (`patience`) or buys the best
   affordable thing. Mines are also weighted by how fast they pay back (hours until the extra output has repaid the
   upgrade, including the solar power it needs); patient personalities care more about that.

Research works the same way once a research lab exists. The agent sleeps until its current job finishes (or
until it can afford the next one), with random jitter, so it only asks the server when something can change.

## Spying and attacking

Agents with a military streak also deal with each other:

1. **Build** a few espionage probes and, depending on personality, fighters, cargo ships or defense. Ships are
   bought from spare resources (`army_share`), so the economy still comes first.
2. **Find a neighbour** in the ranking (`/highscore`): not too strong (`max_target_ratio`), not too far
   (`target_range`), not spied on recently.
3. **Spy** with probes, then **read the report** (`/messages`): resources, ships, defense.
4. **Decide**: attack only if the loot is worth it (`min_loot`) and its mood allows (`aggression`). When the spy
   report shows what defends the target, the agent **simulates the fight** (`swarm/combat.py`: rounds, shields,
   rapid fire, technology bonuses; checked against the first real battle on the owner's server) with the fleet it
   would really send, and goes only if the chance of winning is high enough for its personality (`risk_ratio`:
   bold raiders accept about 50%, careful ones want over 80%; after costly defeats everybody wants more) and the
   expected result is profitable. If the report does not show the defenders, the old rule applies: a rough
   count against `risk_ratio`, or a `gamble`. It sends a share of its fighters (`strike_fraction`) plus enough cargo ships for the loot. If the
   fleet could not carry at least an eighth of the loot, it skips the target (`attack_skipped`) and builds cargo
   ships first.
5. **Read the result** (`/messages?category=combat`): each fight is logged (`battle`: what each side lost, loot,
   debris). After a costly defeat (it lost at least twice what it gained plus destroyed) the agent logs a `lesson`,
   becomes more careful for a while (`risk_scale`, shrinks x0.6, grows again after profitable raids) and avoids
   that target for 6 hours. Probes shot down while spying are logged as `probes_lost`. Each agent saves this memory after every round to
   `memory/<name>.json` (change with `--memory-dir`), so a restart no longer makes it forget. Keep that folder when you
   update; delete a file to wipe one agent's memory.

What each personality does with this: the **raider** builds fighters and hunts; the **turtle** builds defense and
never attacks; the **scientist** and **miner** keep a few probes and watch (the miner almost never fights);
**balanced** spies and attacks now and then.

**Safety: agents only spy on and attack other agents.** Your own account and any other human player are left
alone. To allow it anyway, start with `--attack-humans`.

## Defence

`GET /fleet` shows foreign fleets that are flying towards an agent's planets (kind, origin, arrival time, not what is
on board). Every round an agent looks (`threat` in the log). An incoming attack keeps it alert for 15 minutes (it looks
every ~20 s) and it turns a share of its stock (`caution`, 0 to 1 per personality; turtle highest, raider lowest) into
the most cost-effective defence units it can build (`defend` in the log), because an attack steals up to half of the
stock anyway. One order per 10 minutes. A spy probe alone is ignored unless it comes from a planet that has attacked
before (the agent remembers attackers for 12 hours, saved with its memory), so the first raid still surprises it and
the next ones do not. Rules are in `swarm/defense.py`.

## Planet fields

Every building level uses one field of the planet. The agent reads the free fields from the API. With 30 or fewer
left it heads for the terraformer (energy technology 12, a nanite factory, then the terraformer itself, which adds
5 fields per level); with 3 or fewer it stops upgrading minor buildings (stores, shipyard, research lab). On a full
planet it builds nothing, does not count that as "saving", and keeps buying ships and defence.

## Character classes

Each agent takes the free first choice of a character class once, by personality: miner and turtle are Collectors, raider
and balanced are Generals (+2 fleet slots, faster fleets), scientist is a Discoverer (research 25% faster, bigger
colonies). It never pays to change. Needs `GET/POST /character-class` in the game; an older game without it is simply
skipped. Log events: `class`, `class_refused`. Rules are in `_choose_class` (`swarm/agent.py`) and `Persona.character_class`.

## Colonies

When the home planet has 70 or fewer free fields, a personality with an appetite for expansion (`Persona.expansion`; it also
sets how many planets it is content with: 1 + round(4 x expansion)) sets itself a goal: research lab 3, shipyard 4, impulse
drive 3, espionage technology 4 and enough astrophysics, then it builds one colony ship, scans the home system and 6 systems
either side for free positions, picks the one with the best mix of fields and production bonus for that position, and sends
the ship. The new planet is then built up like any other. Colonies are not defended yet. Log events: `expand`,
`colony_sent`, `colony_founded`, `colony_failed`. Rules are in `swarm/colonise.py`; `expand_planner.py` prints the same
tables for a human (terraformer versus colony versus moon).

## Learning

- **Memory per agent** (`memory/<name>.json`): risk scale, avoid list, aggressors, merchant cooldown, colony goal.
- **Opponent memory** (`swarm/opponents.py`): per target, its last strength and the results of our attacks; the raider ranks
  targets by loot times a track-record factor (0.25 to 1.5, losses count double, old news fades after 12 hours).
- **Fight margin bandit** (`swarm/bandit.py`): the attack check can assume the defender is 1.2, 1.5, 2 or 3 times stronger
  than the spy report says. Each attack tries one of these (Thompson sampling), and the battle report tells whether it paid
  off, so each raider slowly learns the margin that suits it. Report section: LEARNED FIGHT MARGIN.

## The merchant

A merchant call costs 3,500 dark matter (accounts start with 8,000), shows random rates and trades once. So an agent
calls one only when it is stuck waiting for something big (a shortfall worth at least 20,000 and a wait of 20+ minutes),
owns a spare resource that can pay for the whole shortfall even at the worst rate (70% of fair value), and is past a
6-hour cooldown. Patient personalities keep one call in reserve. If the rates turn out too poor it sends him away
(`merchant_dismissed`); otherwise it trades (`trade`). Rules are in `swarm/trade.py`.

The attack check assumes the defender has 1.5 times the units the spy report showed, sends the smallest fleet that is sure
enough to win, and never risks fighters worth more than 4 times the loot.

## Observatory

```
python observatory.py            (then open http://localhost:8080)
```

A read-only web page to watch the swarm, in four tabs: the **map** (systems where agents live, coloured by personality,
colonies marked with a hole, battles of the last 6 hours ringed; hover a planet for its owner and the agent's latest
news), **The Daily Ogame** (a newspaper written from the logs with fixed templates, the biggest event as headline: no
language model and no network), a live **feed** (filter by agent, personality, kind or text) and a **leaderboard** (the
game's highscore plus attacks, net loot and colonies counted from the logs).

It never writes to the game or to the agents' files. It reads `logs/*.jsonl` and `agents_state.json` (only for one agent's
token), and asks the game only for `GET /galaxy/..` and `GET /highscore` (at most about 4 requests a second). If the game
cannot be reached the map and ranking say so and the rest keeps working. Options: `--port`, `--host` (default
127.0.0.1: only this computer), `--logs`, `--state`, `--base-url`, `--galaxy`, `--refresh`, `--news-hours`. It can run
next to the swarm and be started or stopped at any time.

## Not built yet

Expeditions, the scrap merchant, alliances, recycling debris, fleet-saving, defending colonies, retaliation, and LLM-driven
decisions. The brain is rule-based; the personas are the knobs; learning so far is the risk lesson, opponent memory and the
fight margin bandit above.

## Tests

```
python -m unittest discover -s . -t . -p "test_*.py"
```

They run against a small simulated game (`tests/fakegame.py`), not your server.
