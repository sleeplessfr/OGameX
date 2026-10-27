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
  Available: `miner`, `scientist`, `turtle`, `raider`, `balanced` (handed out in this order by default) and `trader`
  (only with `--personas`, see the galaxy board below). Every agent's weights are randomly nudged,
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

## Economy simulator check (step 5, milestone 1)

```
python validate_econsim.py --hours 12
```

Every 30 minutes each agent logs an `econ_snapshot` (levels, output, energy and storage of its planets, research levels).
`validate_econsim.py` compares those numbers and the real construction/research times with `swarm/econsim.py` (the game's
formulas in plain Python, prices generated from the game's PHP by `gen_econdata.py`) and writes `econsim_report.txt`.
Send that file back: it decides whether the simulator can be trusted for the next steps.

## What-if games (economy simulator)

```
python simulate_player.py                                   (every personality, 72 game hours)
python simulate_player.py --styles --personas miner         (the building styles the agents can learn)
python simulate_player.py --personas miner,scientist --hours 168 --csv growth.csv
```

Plays whole players (home planet, colonies, research, the colony-ship chain) with the agents' real building and research
decisions on the game's formulas, about 1 second per player per 3 game days. Economy only: no fleets, fights or trade.

## Trained genomes and live evolution

A genome is about 14 bounded dials around a personality (mines, power, factories, lab, a few technologies, patience, balance, store
alarm); the military, social and diplomatic axes are never touched, so a miner stays a miner.

```
python train_genome.py --persona all --procs 12              (offline, in the economy simulator; about a minute per personality)
python run_swarm.py ... --genomes genomes                    (every non-control agent starts from the trained genome of its personality)
python run_swarm.py ... --evolve on --evolve-hours 12        (live: the weakest quarter copies a top-quarter genome each window)
```

Both are OFF unless you add the flags. One agent in four (the same controls as the learning) never changes: the report section
EVOLUTION compares evolving agents with them. `--genomes` without `--evolve on` just starts everyone from the trained genome.

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

**Full planets (`swarm/fullplanet.py`).** The terraformer needs a free field itself (so do the robot factory levels and the
nanite factory before it), so a planet that is allowed to fill up completely can never build it: an agent used to sit on
"no free building fields" for hours with huge stores. Now: (1) with 30 or fewer fields left and the chain still fitting, the
fields it needs (robot factory 10, nanite factory, one terraformer level, plus the shipyard 4 / research lab 3 of a colony
ship) are reserved, ordinary upgrades may not touch them, and the robot factory and nanite factory are built first; research
is no longer skipped by a low `research_share`. (2) With no field left, or when the chain no longer fits, the planet is
"full": the agent founds colonies whatever its `expansion` and planet limit (up to 8 planets; a failed search widens the
radius by 6 systems, astrophysics is researched as needed), researches every useful technology and turns 40% of the spare stock
into ships and defence per order (at most 250 units, a colony ship's price is kept back, the colony ship is ordered first).
(3) Once an hour and planet it logs `full_planet_plan` (decision: terraformer path / expanding / spending / nothing, and why);
`summarize_logs.py` shows them in FULL PLANETS. Other planets behave as before.

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

## Logistics (agents with 2+ planets)

Rules are in `swarm/logistics.py` (pure) and `swarm/logistics_agent.py` (the calls, mixed into `Agent`).

- **Supply** (`_plan_supply`): the richest planet sends half the gap per resource to a colony whose stock is worth under 40% of it
  (mission `transport`, the cargo ships fly home alone). The source keeps at least 30%, the colony's storage is respected, the
  cargo is capped by the real capacity from `/ships`, and the trip is skipped when the deuterium (from `/planets/{id}/fleet/check`)
  costs over 5% of the cargo. It only uses cargo ships the agent already has; only a miner or turtle with none may order up to 5
  small cargos (at most 10% of its stock). One trip per 15 minutes. Events: `supply_sent`, `supply_skipped`.
- **Fleet saving** (`_save_fleet`): when `GET /fleet` shows an attack on a planet that holds ships worth 8,000+ and the simulated
  fight cannot be held (attacker strength comes from opponent memory x1.5; unknown attacker = run), the ships and as many
  resources as fit leave on a `deployment` to the safest other own planet (farthest from the attacker, not itself under attack).
  A deployment has no return trip, so the fleet is remembered (`Memory.away`, saved to disk) and sent back once the raid
  has landed (+90 s). Events: `fleet_saved`, `fleet_stays`, `fleet_save_failed`, `fleet_returned`. Not done: a single-planet agent
  has nowhere to send its fleet. Report section: LOGISTICS.

## Moons: sensor phalanx (agents that own a moon)

Rules are in `swarm/moons.py` (pure) and `swarm/moons_agent.py` (the calls, mixed into `Agent`). An agent without a moon does
nothing new. Needs the moon endpoints of the game (`POST /planets/{moon}/phalanx`, see `docs/API.md`, "Moons"); a game without
them answers 404 once and the agent never asks again.

- **Building** (`_moon_build`): fighters (aggression 0.25+ or the General class: raider, balanced) want a sensor phalanx level 3
  (range 8 systems), the others only when some planet holds 400,000+ (metal-equivalents), then level 2. The lunar base comes first
  when the game says the phalanx's requirements are not met. A moon has no mines, so what it lacks is carried from the planet at
  the same coordinates in cargo ships the agent already owns (`moon_fed`, mission `transport`, `"type": "moon"`); no cargo ships
  = one quiet `phalanx_skipped` line. Orders are logged as ordinary `build` events, a finished upgrade as `phalanx_built`.
- **Scanning** (`_moon_scan`): every 3 minutes (90 s while alert), at most 6 scans an hour for fighters and 3 for the others
  (5,000 deuterium each, from the moon's own stock; the agent brings deuterium for two scans only if it is a fighter or alert).
  Who: known aggressors first, then a raider's targets (spy reports, opponent memory), then the other players' main planets
  from `/highscore`, only in range, never the same planet within 10-60 minutes. Refused planets (`out_of_range`, moon, ...) are left alone 6 h.
- **Early warning:** a scanned rival whose fleet is out attacking one of our planets shows a return flight whose origin is our
  planet; its `departure_time` is when the attack lands. Such an attack becomes a `Threat` (defence orders, alert, aggressor memory:
  `_phalanx_threats`) and an `Attack` for fleet saving (`_phalanx_attacks`, used by `_save_fleet`), unless `GET /fleet` already shows it.
  Event `phalanx_warning`. Note the phalanx cannot scan our own planets, so it finds a raid by scanning the raider's home.
- **Raiding:** a scanned rival whose ships are out (`away` in `Memory.moons`) raises the worth of a raid on it by up to 30%
  (`away_factor`, used by `pick_attack`), more the larger the share of its known strength that is away.
- Events: `moon_seen`, `phalanx_built`, `phalanx_scan`, `phalanx_warning`, `phalanx_skipped`, `moon_fed`. Report section: MOONS.
- Not done: the jump gate (the API has it, agents do not use it), scanning to find new raid targets, building the moon's own defence.

## Alliances (agents with sociability)

Rules are in `swarm/alliance.py` (pure) and `swarm/alliance_agent.py` (the calls, mixed into `Agent`). Needs the alliance and chat
endpoints of the game (`/alliance`, `/alliances`, `/chat`, see `docs/API.md`, "Alliances and chat"). A game without them answers 404 to
`GET /alliance`: the agent writes `alliance_skipped`, never asks again, and plays as before. Persona field `sociability` (0 = no alliances):
miner, scientist and turtle 0.8-0.9, trader 0.7, balanced 0.5, raider 0.15 (drawn last in `make_persona`, from its own stream, so nothing else changes).

- **Founding:** sociability 0.6+, 300+ points, playing 30+ minutes, at most one alliance per 5 agents (at least one), 35% chance per look.
  Name, tag and charter: see "Alliance names" below.
- **Applying:** after 15 minutes, to the open, not full alliance whose members are the best fit: neighbours in the galaxy and similar
  personalities (`compat`), never one with a member who attacked this agent or beat it in a fight. A short text says who and where.
- **Accepting:** the founder answers within one look: max 8 members, rejects a grudge (attacked it, or beat it in a fight in the
  opponents memory) and anyone who attacked another member (members tell the chat when they are attacked).
- **Allies are never targets:** their ids are taken out of `_allowed_target_ids`, and spy reports on their planets are forgotten.
- **Sharing:** members use the alliance chat in a fixed format: `INTEL T=1:2:3 O=45 L=12000 D=3 A=120` (target, owner, loot, defenders,
  age) at most once in 4 minutes, `WARN AT=1:3:8 FROM=1:5:5 ETA=300` when an attack is on its way (once per mission, 1 a minute).
  Attacking agents turn fresh `INTEL` of allies into targets (`intel_used`); a `WARN` makes everybody remember the raider.
  The circular message `/alliance/broadcast` is not used: it needs a rank with a permission that new members do not have.
- **Leaving:** a member leaves after an ally attacked it, or when nobody has said anything for 12 hours (no way back to the same alliance
  for 4 hours; the game also has its own cooldown). A founder cannot leave, so it kicks a member that attacked it.
- **Board:** a founder with room left posts an `ALLIANCE` note to the galaxy board once an hour (nothing without `--board`).
- **Alliance names, charters and choices:** the name says what the alliance wants (its *purpose*, from the founder's persona: miner = mining
  cartel, turtle = defence pact, raider = raiding fleet, scientist = research circle, trader = trade guild, balanced = mixed), in a *tone*
  drawn from 12 (funny, edgy, menacing, solemn, bureaucratic, poetic, mysterious, absurd, pompous, laconic, heroic, folksy) by crc32 of agent
  name and attempt. Never the founder's name or story. Without a model (`alliance.fallback_identity`) names come from tone patterns times
  purpose words ("Smelters Anonymous", "Bureau of Rock", "Lanterns of Tender Deuterium"), cut to 30 characters, and the tag from the name's
  initials (3-8 of A-Z0-9, made unique). With `--llm` the founder asks the model once (purpose `alliance_name`, normal priority, answer
  `NAME | TAG | CHARTER`, no founder data in the prompt), founds on a later look (the round never waits; 90 s without an answer, or an
  invalid one, or a taken name: the rules name it) and cannot found twice. The *charter* (max 150 characters: what it wants and who it
  welcomes) goes into the board `ALLIANCE` advert (`data.charter`, `purpose`, `tone`) and into the leader's welcome line in the alliance
  chat (the game API has no way to set an alliance text, only to read `external_text`, which is used as a fallback charter).
  Before applying, an agent with a model reads the charter (advert) and the fit, and the model answers ACCEPT / REFUSE / WAIT
  (purpose `alliance_choice`); the rules (open, room, no member who attacked us, 4 h between tries) have already filtered and picked, so
  the model can only confirm or veto; WAIT twice = REFUSE; no model or no answer = the rules apply as before. Events `alliance_named`
  (name, tag, tone, purpose, source llm|rule, charter) and `alliance_choice`. `--llm fake` gives canned valid names and decisions.
- Events: `alliance_founded`, `alliance_applied`, `alliance_accepted` (written by the leader), `alliance_rejected`, `alliance_joined`,
  `alliance_left`, `alliance_kicked`, `intel_shared`, `intel_used`, `ally_warned`, `alliance_skipped`, `alliance_named`, `alliance_choice`. Report section: ALLIANCES.
  The observatory leaderboard shows each player's alliance tag from `/highscore`.
- Not done: ranks, alliance texts, voting, a leader handing over, joint attacks, resource help between allies, members beyond the top 100 of the highscore.

## Learning

- **Memory per agent** (`memory/<name>.json`): risk scale, avoid list, aggressors, merchant cooldown, colony goal.
- **Opponent memory** (`swarm/opponents.py`): per target, its last strength and the results of our attacks; the raider ranks
  targets by loot times a track-record factor (0.25 to 1.5, losses count double, old news fades after 12 hours).
- **Fight margin bandit** (`swarm/bandit.py`): the attack check can assume the defender is 1.2, 1.5, 2 or 3 times stronger
  than the spy report says. Each attack tries one of these (Thompson sampling), and the battle report tells whether it paid
  off, so each raider slowly learns the margin that suits it. Report section: LEARNED FIGHT MARGIN.

## Expeditions

An agent sends a small cargo fleet to position 16 of its home system (`swarm/expedition.py`). Discoverers (the scientist) go whenever
a slot is free; the other personalities have an `expedition_interest` (0.05 to 0.25) and only go once astrophysics is 3 and then rarely
(6 h pause). Rules taken from the game's `ExpeditionMission.php`: it needs astrophysics 1, a free expedition slot
(`/status` `fleet_slots.expedition_used/max`: floor(sqrt(astrophysics)), +2 for a Discoverer) and a normal fleet slot; there is no
fleet-size limit and the holding time does not change the odds, so agents hold 1 hour. Finds are capped by cargo capacity, so the fleet is
mostly cargo ships (pathfinders first for a Discoverer) plus a few fighters as escort, never more than 40-60% of the ships at home. About
1 in 3 expeditions finds resources, 1 in 6 ships, 1 in 13 dark matter, 1 in 500 loses the fleet. It stays home while it is on guard or an
attack is on its way. Reports (`/messages?category=expeditions`) are parsed into counts saved in the agent's memory (`expedition`: outcomes,
resources, dark matter, ships found and lost); a lost fleet shrinks the next fleet and pauses for 6 hours, good trips grow it again.
Log events: `expedition_sent`, `expedition_result`, `expedition_refused`. Report section: EXPEDITIONS.

## The merchant

A merchant call costs 3,500 dark matter (accounts start with 8,000), shows random rates and trades once. So an agent
calls one only when it is stuck waiting for something big (a shortfall worth at least 20,000 and a wait of 20+ minutes),
owns a spare resource that can pay for the whole shortfall even at the worst rate (70% of fair value), and is past a
6-hour cooldown. Patient personalities keep one call in reserve. If the rates turn out too poor it sends him away
(`merchant_dismissed`); otherwise it trades (`trade`). Rules are in `swarm/trade.py`.

The attack check assumes the defender has 1.5 times the units the spy report showed, sends the smallest fleet that is sure
enough to win, and never risks fighters worth more than 4 times the loot.

## Recycling debris

Fights leave metal and crystal floating at the target's coordinates. The API does not show debris fields in the galaxy view,
so agents learn about them from reports they read anyway: `debris` in their own battle reports (as attacker or defender) and in
spy reports. Fields are remembered in `memory/<name>.json` for 24 hours. Fighting personalities (raider, balanced, general class)
buy recyclers once 15,000 debris is remembered; peaceful ones only for 60,000 or more (needs shipyard 4, combustion drive 6,
shielding 2; the agent just waits until the game allows it). It dry-runs the trip (`/fleet/check`), goes only if the haul is
worth at least twice the fuel, sends `recycle` to the `debris_field`, and the ships unload the haul when they come home.
Log events: `recycle_sent`, `recycle_skipped`, `units` (what=recycler); see RECYCLING in the report. Rules: `swarm/recycle.py`.

## Military doctrine (raider, balanced, General class)

Rules are in `swarm/doctrine.py` (pure, with a plain-language description at the top); the calls are in `Agent._plan_military`.
Raiders that only bought light fighters found out the hard way that mass fighters cannot beat hundreds of rocket launchers
(a whole night with zero attacks, "simulated fight: win chance 0%"). Fighting personas now work out what to buy:

- **Which ship.** The battle simulator (`swarm/combat.py`, with rapid fire and technology) lets a batch of each warship type
  (light and heavy fighter, cruiser, battle ship, bomber, destroyer, battlecruiser, and the reaper for the General class), worth the
  same money, fight a typical defender: the average of the recent spy reports that showed a defence (rocket launchers if none did).
  Score = value destroyed / (own losses + a quarter of the batch price), at 3:2:1 prices. The ranking replaces the fixed `unit_weights`
  for warships in `choose_units` (cargo ships, probes and defence keep theirs). It is used once a report showed a defence or targets
  have been out of reach; before that the old weights apply. Other personas never use it. Event `doctrine` (best ship that can be built now,
  best ship to aim for, score table, why), logged when the answer changes.
- **More money for the army.** Every skip with a simulated win chance of 10% or less is counted (`Memory.doctrine`, saved with the memory).
  With 3 or more in the last 4 hours (attacks that went out count against them twice) the agent raises the share of spare resources
  it spends on ships from `army_share` towards 0.8, and lets it fall back as the skips age or attacks resume. Event `army_push`.
- **Research.** In the same situation the fighter works through goals (like colony or recycler goals): the shipyard, lab, drives and
  technologies the aimed ship needs (the whole chain), and weapons 6, armour 5, shielding 4.
- **Surplus spending.** When the economy waits for a must-do item (a solar plant, a goal), a fighting persona spends only what is
  *above* that item's price (`Choice.goal_cost`), on every resource, so the reserve is never touched. When it merely saves up by choice
  the old coin flip still applies, and when it fails the surplus is spent anyway. A planet whose whole stock is worth under 20,000
  (3:2:1) is still starting up and keeps the old coin flip. Peaceful personas keep the old coin flip.

Report section: DOCTRINE.

## Observatory

```
python observatory.py            (then open http://localhost:8080)
```

A read-only web page to watch the swarm, light or dark following the system, in four tabs (five with `--board`): the **map** (systems where agents live, coloured by personality,
colonies marked with a hole, battles of the last 6 hours ringed; hover a planet for its owner and the agent's latest
news), **The Daily Ogame** (a front page written from the logs with fixed templates: a big headline, the biggest story of the
period among bounties, robberies, colonies, completed trades and saved fleets, then up to five other stories, then the
rest; no language model and no network), a live **feed** (filter by agent, personality, kind or text) and a **leaderboard** (the
game's highscore plus attacks, net loot and colonies counted from the logs).

It never writes to the game or to the agents' files. It reads `logs/*.jsonl` and `agents_state.json` (only for one agent's
token), and asks the game only for `GET /galaxy/..` and `GET /highscore` (at most about 4 requests a second). If the game
cannot be reached the map and ranking say so and the rest keeps working. Options: `--port`, `--host` (default
127.0.0.1: only this computer), `--logs`, `--state`, `--base-url`, `--galaxy`, `--refresh`, `--news-hours`. It can run
next to the swarm and be started or stopped at any time.

## Galaxy board and trading (optional)

A small notice board where agents leave short messages for each other, so rule-based agents can react to each other
without any language model. It is a **separate program** and **off by default**: nothing changes unless you start it and pass
`--board`.

```
python board.py                                   (terminal 1: http://127.0.0.1:8765, posts saved in board.jsonl)
python run_swarm.py --count 6 --personas miner,turtle,raider,trader --board http://localhost:8765
python observatory.py --board http://localhost:8765   (optional: adds a Board tab and a newspaper section)
```

Posts have a kind: `OFFER`, `WANT`, `BOUNTY`, `WARNING`, `REVENGE`, `TAUNT`, `ALLIANCE`, `CLAIM`, `VERDICT`, `HELLO`, `REPLY` (the last two are diplomacy, below), a text of at most 200 characters and a small
`data` object. `POST /post`, `GET /posts?since=<id>&kind=OFFER,WANT&limit=50`, `GET /health` (JSON). Safety: it listens on 127.0.0.1 only
and refuses other peers, posts expire (30 min to 12 h by kind, at most a day), only the newest 2,000 are kept, every agent may post
20 times an hour (agents limit themselves to 6), request bodies are capped. The client (`swarm/boardclient.py`) has a 2 second timeout
and, after a failure, does not try again for a minute: **if the board is down the agents behave exactly as they did without it.**
If it was not reachable when the swarm started, the agents simply start using it once it is up.

What agents do with it (rules in `swarm/social.py`, calls in `swarm/social_agent.py`):

- **Robbed -> WARNING and REVENGE.** An agent that reads a battle report in which it was robbed (loot or losses of 1,000 or more)
  posts the raider's player id and the coordinates the fleet came from (seen in `/fleet`, kept in `Memory.aggressors`), plus the
  strength opponent memory has for that planet. Once per 30 minutes. Every personality of the default rotation reads battle reports; the trader keeps one probe for that.
- **Warnings raise the guard.** A careful, peaceful agent (caution 0.5+, aggression 0.2 or less: miner, scientist, turtle, trader) that reads
  a fresh WARNING/REVENGE about a raider or victim within 25 systems puts the raider on its aggressor list as if it had hit 90
  minutes ago: it looks every minute and keeps rearming for the next 90 minutes, and a probe from that planet counts as a threat.
- **The trader** (persona `trader`: steady economy, `collector` class, builds up to 5 small cargos if it has none) swaps a surplus for what it lacks
  with **other agents only**. Its target mix is 50% metal, 30% crystal, 20% deuterium (by value). When one resource is far above and another far
  below, it posts an OFFER/WANT (a tenth of the surplus at the 3:2:1 reference prices of the merchant, asking 3% more than fair) and answers
  anybody else's fair offer. Goods go by `transport` in four instalments, **tit-for-tat**: whoever answers sends first, one small instalment
  on credit; each side sends the next only after the previous one has really arrived (the game's `transport_received` message names what
  came). Whoever has not paid back after the flight time twice plus 30 minutes is a cheat: `trade_cheated`, shunned for 24 hours (doubling
  for repeat offenders, up to 8 days), and its offers are ignored. Partners who paid before get bigger instalments (three instead of four).
  Reputation is saved in the agent's memory (`memory/<name>.json`, key `social`).
- **Caps so nobody can be drained:** a deal is at most 10% of the stock of the resource given; goods out and not yet paid for, over all deals,
  are at most 10% of the stock's value; two deals at a time; deals under 3,000 metal-equivalents are not made; no new flight while under
  attack; the recipient must be one of your agents (it is in `agents_state.json`, its user id matches and the coordinates are its main
  planet in the ranking's first 100), never a human player or your own account.

- **Prices are learned** (`swarm/pricing.py`, saved in `Memory.social["prices"]`). For every pair "give X, want Y" an agent keeps a markup
  r = value asked / value given (1.0 = fair 3:2:1; starts at 1.03). An offer nobody answered in its 30 minutes lowers r by 8% (floor 0.7, or straight back
  to the best known price if r was above it); an answered or completed one raises it by 3% (cap 1.3); one post in four is nudged by up to 3% either way
  (exploration, from a private random stream, so the agent's other dice are untouched). The answering side takes a post whose markup is at most its own
  limit for that pair: 1/0.95 at first, 2% looser per hour in which it needed something and found nothing acceptable (cap 1.25), 1% tighter after a deal.
- **No promises it cannot keep:** an agent posts or answers an offer only if cargo ships at home carry at least half of the amount, or it is a
  persona that may build cargos (`logistics.BUILDERS`: miner, turtle, trader) and can order enough small cargos now. Otherwise `trade_offer` with
  action `skipped` (looks again after ten minutes).

Log events: `board_post`, `board_read`, `trade_offer` (action posted / answered / accepted / done / skipped / refused; `posted` carries the `ratio`),
`trade_sent`, `trade_received`, `trade_cheated`, `price_learned` (pair, kind ask/accept, old, new, why: unanswered / answered / completed / need),
`board_error`. BOARD AND TRADE in the report ends with a PRICES LEARNED list (per agent and pair: start, now, answered / unanswered / completed).
The default rotation of personalities is unchanged: the trader is **not** in it (adding it would change which personality the 6th agent gets),
so ask for it with `--personas miner,scientist,turtle,raider,balanced,trader` (or any list); adding it never changed anybody else's random draws.

## Revenge, bounties and the referee (optional, needs the board)

```
python board.py                                                    (terminal 1)
python referee.py --board http://localhost:8765 --logs logs        (terminal 2: the judge of bounty claims)
python run_swarm.py --count 8 --board http://localhost:8765        (terminal 3)
python observatory.py --board http://localhost:8765                (optional: wanted notices, Board tab)
```

`referee.py` flags: `--board URL` (required, this computer only), `--logs DIR` (the swarm's `--log-dir`, default `logs`), `--interval 10`
(seconds between passes), `--once` (one pass, then stop). Start it from this folder, any time before or after the swarm; it keeps nothing
but what is on the board (a restarted referee reads its own old VERDICT posts, so it never judges a claim twice). Without a referee bounties
are still posted and claimed, but nobody is ever paid: the claims just wait.

- **Revenge join** (rules in `swarm/bounty.py`, calls in `swarm/bounty_agent.py`). A hunter (aggression above 0.2, or general class with some
  aggression: raider and balanced; never miner, scientist, turtle or trader) that reads a fresh REVENGE call joins the biggest one: it
  adds the named raider to its target list (x2 on the worth of that target when it chooses whom to attack, and it is the first one it
  spies on), logs `revenge_join`, and answers on the board with a REVENGE post that has `data.reply_to` set. Conditions: the raider is an agent of
  ours (or `--attack-humans`), not a friend (agents it joined for, trade partners who paid, open trade partners), not itself; its
  strength (opponent memory first, else the poster's figure) is at most 1.5x the hunter's fleet, and a fresh spy report that shows the garrison must
  pass the usual fight simulation; one joined revenge at a time, over after 2 hours (server time) or after the hunter has raided the raider
  (`revenge_done`). Joining only changes whom the hunter prefers; spy report, fight simulation and risk rules still decide whether it attacks.
- **Bounty.** A victim robbed for at least 5,000, which is 15% or more of what it had, and with a stock worth 30,000 or more, posts a BOUNTY (once at a time,
  6 hours): data `target_player_id`, `target_at`, `reward` ({resource: amount}), `expires` (wall clock), `poster_id`, `poster_at`. The reward is about
  half the loot and **at most 10% of its stock** (of that resource and of the stock's value). There is **no escrow**: the poster pays only after the referee has confirmed a claim.
  Hunters add the reward's value (the 3:2:1 reference prices) to the target's score when choosing whom to attack, and when they win loot from the
  target they post a CLAIM (`bounty_id`, battle report id, coordinates, time, `claimant_id`, `claimant_at`).
- **Referee.** For every CLAIM it posts a VERDICT (`bounty_id`, `claim_id`, `ok`, `why`, `claimant`, `claimant_id`, `claimant_at`). It says OK when the
  claimant's own log shows an `attack` and a winning `battle` (as attacker, with loot) on the bounty's target, both after the bounty was posted and the
  battle before it expired; when the claimant is not the poster; and when it is the first valid claim on that bounty. **A verdict is only as honest as the
  logs**: the referee believes the `attack` / `battle` lines the agents write (`their_id` and `report` were added to the `battle` line for this). The
  referee is ours: a separate program that only reads files and the board, so an agent cannot lie to it by posting something, and agents believe
  only VERDICT posts from the board name `referee`; but anybody who can edit a log file or post as `referee` on the board (it is local and has no passwords)
  can cheat. Fine for your own computer.
- **Payment.** On an OK verdict the poster sends the reward by `transport` (the logistics code) to the claimant's main planet, after checking in the
  highscore that the claimant is one of our agents at those coordinates. One payment, at most the promised reward, at most 20% of its current stock of
  that resource; it waits while under attack and for a free fleet slot, may order a few small cargo ships if it has none, and gives up after 2 hours
  (`bounty_unpaid`, side poster). A claimant that is not paid within 3 hours of the OK verdict logs `bounty_unpaid` (side claimant) and puts the poster in its
  reputation as a cheat: shunned for 24 hours (doubling for repeat offenders), its bounties and trade offers ignored.

Log events: `revenge_join`, `revenge_done`, `bounty_post`, `bounty_claim`, `bounty_verdict`, `bounty_paid` (side poster / claimant), `bounty_unpaid`. See BOUNTIES AND
REVENGE in the report. The Board tab shows the same cards as the board's own page (avatars, deal boxes, nested replies, bounty status, kind chips with counts). The Board tab and the newspaper of the observatory show BOUNTY, CLAIM, VERDICT and joined revenges in their own colours, and the
newspaper has a "Wanted" section with every bounty and what became of it (open, claimed, confirmed, paid, unpaid, expired). Without `--board` nothing here
does anything.

## The inner voice: a local language model (optional, text only)

Off by default. With it on, each agent gets a short **character sheet** (a name, temperament words taken from its personality
knobs, a one-line backstory; always the same for the same agent) and a small **voice**. The language model NEVER touches the
game: it cannot build, attack or trade, and the rules decide everything exactly as before. It only writes text (the one exception, answering a
diplomatic proposal, is fenced in by hard rules: see Diplomacy):

1. **Board posts**: when the rules have decided a WARNING, REVENGE, BOUNTY, OFFER, TAUNT or CLAIM post, the model may put
   its `text` in the agent's own words (25 words at most). The structured `data` is never changed. An answer is thrown away if it is
   empty, too long, contains markup, or has a number or coordinate that is not in the original post.
2. **Diary**: after a notable event (robbed, a big raid won, colony founded, expedition result, fleet saved, trade completed) the
   agent writes one line. The last 20 are kept in its memory file, logged as `diary` events and shown in the observatory Feed
   as a quote. The header of the observatory shows the number of LLM calls and the average latency.

Each agent asks for at most one voice request per 5 minutes. A post never waits for the model for more than about a second:
if the rewrite is not ready it goes out in the rules' words (a late rewrite is only used if the same post is tried again, for
example after the board was down), so with a slow model many posts stay original. The diary is not time critical and works fine slowly.

**Start LM Studio's server**: load one model (for instance gpt-oss 20b), open the Developer tab, start the local server
(default `http://localhost:1234`), then:

```
python run_swarm.py --count 20 --board http://localhost:8765 --llm http://localhost:1234/v1 --llm-model <the model name LM Studio shows>
```

- `--llm fake` answers from templates without any server: use it first to see the plumbing (posts get a short opener, the diary fills, `logs/llm.jsonl` is written).
- `--llm-model NAME` the model to ask for. `--llm-timeout 60` seconds to wait for one answer.
- `--llm-max-per-hour 120` budget for ALL agents together; when it is used up the agents simply keep their own words.
- One request is in flight at a time (all agents share the process and the GPU). Urgent talk (warnings, revenge, bounties) goes before
  routine chatter; a request that waits more than 10 minutes in the queue is dropped. Any failure (server off, timeout, bad
  answer) just means "no rewrite / no diary line"; the agents never stop or wait for it.
- `logs/llm.jsonl` has every call (agent, purpose, prompt, answer, latency, tokens when given). `python summarize_logs.py` adds an LLM
  section: calls, failures, average latency, posts rewritten, diary lines.

Honest notes: this was tested only against a fake server, not on a real GPU. How fast gpt-oss 20b or qwen2.5-coder 14b answers on a Radeon VII
(Vulkan), and whether 20 agents fit in the budget, is unknown until you try; watch the latency in the observatory header and in the report.
Small models sometimes ignore the word limit or change a number; those answers are rejected and cost a call.
The server address may be any `http://` address (it is not limited to this computer like the board).

## Not built yet

The scrap merchant, jump gate use by agents, defending colonies, and other retaliation than the revenge join. The only decision the
language model makes is the answer to a diplomatic proposal (see Diplomacy); everything else is rule-based. The brain is rule-based; the personas are the knobs; learning so far is the risk lesson, opponent memory and the
fight margin bandit above.

## Diplomacy: first contact, trust, pacts (optional, needs the board; a language model makes it livelier)

Rules are in `swarm/diplomacy.py` (pure) and `swarm/diplomacy_agent.py` (the calls, mixed into `Agent`).

**What it is.** In the Stellaris style, an agent does not know another player until they meet. A meeting ("contact") is: a spy report on
them, a battle with them, a post of theirs on the galaxy board, a spy probe or fleet seen coming from them, or living within 2 systems of one
of its planets. Only other agents count; humans are never part of it. From then on each pair has a **relationship** that climbs a ladder, one
step at a time, and can fall back down it:

```
unknown -> contact -> neutral -> friendly -> pact -> alliance
```

- `contact -> neutral`: one agent posts a HELLO on the board, the other answers (below).
- `neutral -> friendly`: automatic once trust is 20 or more and the opinion is not negative.
- `friendly -> pact`: a proposal, only with trust 40 or more. A **pact** is a non-aggression pact: neither side raids the other (the raid target
  picker skips the partner, and a last check in `_try_attack` stops even a gamble).
- `pact -> alliance`: a proposal, only with trust 65 or more, a shared threat (someone attacked us lately, or the partner warned us) and a sociable
  persona. The agent then prefers the in-game alliance of its partner when it looks for one; the existing alliance code does the rest.

Every pair has **trust** (0 to 100) and **opinion** (-100 to 100). They only move by events: a completed trade (+8), an accepted proposal (+6), a warning
shared (+3) and peaceful time (+1 an hour; peace and warnings together at most +6 a day) go up; a raid on us (-40), a raid by us (-25) and a cheat in a trade (-30)
go down. If a partner attacks us under a pact it is a **betrayal**: back to `contact`, trust 0, and for 48 hours nothing is proposed or accepted. The first
opinion comes from the highscore (a much stronger neighbour is respected, a much weaker one is a target or nuisance, more so for aggressive agents) and the
personality (aggression, caution, trade interest, sociability). Category scores are used when the highscore has them, and everything works without them.

**Hard rules the language model cannot override** (checked in code, on both sides): no step with anyone who attacked us in the last 12 hours or who robbed
us (48 hours), nor against whom we hold an open revenge call or bounty; no skipping a step; after a refusal 6 hours of silence for that pair; a pact needs
trust 40 and an alliance 65. A proposal that breaks one is refused before the model is even asked.

**How a proposal goes.** An agent in `contact` (or ready for the next step) posts a HELLO (`to_id`, `step`) at most once every ~3 hours, one open at a time; the
text is written by the language model if there is one (else a fixed sentence). The addressed agent reads it, checks the hard rules, then asks the model
with the facts (trust, opinion, how often they spied on or attacked us, score gap and rank, honest trades, threats). The model must start its answer with
one word: `ACCEPT`, `REFUSE` or `WAIT` (the first word decides, capitals do not matter, anything else counts as WAIT), then optionally a short reason. The
reply is posted as a REPLY. WAIT is silent: the agent asks again after 30 minutes, at most twice more, and then treats it as a refusal; a proposal nobody
answers in 2 hours counts as refused. **Without a model** (no `--llm`) a deterministic rule answers: accept if trust is high enough, the opinion not bad
and no grudge. If the model fails (down, budget used up) the rule answers too. On a pact or an alliance the accepting agent posts a one-line announcement
(an `ALLIANCE` post with `announce` set; the model may put it in its own words). Nothing waits for the model: the question is queued (answers get priority
over greetings) and the result is used on a later round.

**Why peace cannot take over (pact limits).** Left alone, every agent would end up with pacts and nobody would raid any more. Four rules prevent that
(constants at the top of `swarm/diplomacy.py`):

- **Slots.** Each personality can hold only so many pact and alliance partners at once (`pact_slots`): a raider or other ruthless agent (aggression 0.6 or
  more) 1, or 2 if it is sociable; a middling one 2 or 3 (balanced: 3); the peaceful ones 4 to 6 (scientist and trader 5, miner and turtle 6). With all slots
  taken an agent neither proposes nor accepts another pact (reason `pact slots full`). An alliance keeps its slot.
- **Raiders only pact with someone they could not raid.** An agent with aggression 0.4 or more accepts or proposes a pact only with a partner that is much
  stronger than itself (score ratio 2 or more) or whose last spy report (at most 24 hours old) shows a defence worth at least twice the loot, or a loot under the
  agent's own minimum. A weak or rich-looking neighbour is a target (`looks like a target`), and with no information at all a raider refuses.
- **Pacts expire.** A pact lasts 3 days (`pact_since`, `pact_until`). In its last 12 hours, peaceful time renews it if trust is still 40 or more, there is no grudge
  and (for a raider) the partner still could not be raided: log `diplo_renewed`. Otherwise it falls back to `friendly` (log `diplo_expired`), the partner is no
  longer protected and nobody proposes a new pact to that pair for 6 hours. Alliances do not expire (they still fall by the old trust rule) but use a slot. Saved
  pacts from before this update get their 3 days starting at the first scan.
- **Betrayal by ambition.** A ruthless agent (aggression 0.6 or more) with a pact (never an alliance step, never a member of its own in-game alliance) has a
  15% chance on a given day (its own dice from its name and the day, not the personality's random numbers) to consider breaking it. Only then is the partner
  spied on and open to the raid picker, and only if the loot is at least 8 times its usual minimum and 5000, and the battle simulation says it wins with 90%
  or more. The raid is logged as `diplo_betrayal`; the partner is back to `contact`, trust 0, for 48 hours nothing is proposed or accepted, and the partner
  sees a normal betrayal. At most one betrayal per agent per 24 hours.

The report (`summarize_logs.py`, DIPLOMACY) now shows the share of agents holding a pact, pacts formed, renewed, expired and broken, slots used against
available, and pact refusals by reason. The observatory Diplomacy tab adds a table of slots used and the next pact expiry per agent. New log events:
`diplo_renewed`, `diplo_expired`, `diplo_betrayal`, `diplo_declined` (a pact not proposed, once a day per reason and partner) and `diplo_slots`.

**How to run it.**

```
python board.py
python run_swarm.py --count 20 --board http://localhost:8765 --llm fake                    (canned, repeatable answers: try this first)
python run_swarm.py --count 20 --board http://localhost:8765 --llm http://localhost:1234/v1 --llm-model <name>   (a real model)
python run_swarm.py --count 20 --board http://localhost:8765                                (no model: the rule answers)
```

Without `--board` the agents still meet and keep their grudges, but never get beyond `contact` (no way to talk). HELLO and REPLY posts count towards the
agent's 6 board posts an hour.

**How to see it.** The observatory has a **Diplomacy** tab (agent, other player, step, trust; read only, from the logs) and the Board tab shows HELLO and REPLY
cards next to each other; the LLM page names the calls `hello`, `answer` and `announce`. `python summarize_logs.py` has a DIPLOMACY section: contacts, proposals,
accepted / refused / waited, steps reached, pacts, alliances, betrayals and the 5 strongest relationships. State is saved in the agent's memory file
(`diplomacy`); delete the key (or the file) to start over.

Log events: `contact` (who, how), `diplo_hello` (who, step, text), `diplo_answer` (side answerer / proposer, decision, reason, source llm / rule / hard rule,
a digest of the facts, `raw` = what the model said), `diplo_step` (from, to, trust, why; `betrayal` set when a pact was broken), `diplo_trust` (delta, why, trust;
only changes of 5 or more, or a small one every 6 hours per pair), `diplo_announce`, `diplo_blocked` (a raid on a partner was stopped), `diplo_error`.

## Scaling up: from 20 agents to thousands

A real universe has thousands of players, but each one is online only a small part of the day (typically 5-10 %). The
tools below let you copy that: a big population of agents, of which only a few are playing at any moment. Everything is
off by default; a plain `python run_swarm.py --count 20` behaves as before.

### The order to try things

1. **Measure the game first** (`bench.py`, below). It tells you how many requests a second your server can take.
2. **Turn on the online routine** (`--routine real`): the agents then make requests only while "online". This is the big win:
   with 7 % online, 1000 agents cost about as much as 70 playing all day.
3. **Add processes** (`--procs N`) when one run of `run_swarm.py` is no longer enough (the computer shows one core at 100 %,
   or agents react late).
4. Only then raise the number of agents, one step at a time, and look at `python summarize_logs.py` after each step.

### 1. The benchmark: how much can my server take?

```
python bench.py --base-url http://localhost/api/v1 --agents 50 --seconds 60 --max-rate 50
python bench.py --dry-run                      (try the tool itself on a pretend server; no game needed)
```

It makes throwaway accounts named `bench0001`, `bench0002`... (remembered in `bench_state.json`, so the second run makes no
new accounts; delete the file to start over) and then only **reads**, the way agents do: status, buildings, galaxy view,
fleet, highscore, messages. It builds, researches, attacks and posts nothing. Do not run it while a real swarm is playing:
the two would add up.

How to read the result: a table per call with how many requests, the typical answer time (p50), the slow ones (p95 = 19 of
20 answers were faster than this, p99 = 99 of 100), and the counts of `429` (the game refused: its own rate limit),
`502` (the web server lost its PHP workers: overloaded) and other errors. The last line is the verdict in plain words, e.g.
"the server handled 120 req/s with p95 80 ms; ... supports about 1200 agents online at the same time". Rules of thumb:

- **Healthy**: no 429/502, p95 under 1 second. If the verdict says "the test itself was the limit", raise `--max-rate`
  (and `--agents`) and run again until it stops saying that: that is the real limit.
- **Struggling**: errors above a few percent or p95 over 1 second. The number printed is then your ceiling; stay clearly below it
  (half is comfortable).
- 429 for every call right from the start means the game's per-minute limit: raise `OGAMEX_API_RATE_LIMIT` in the game's `.env`
  (it counts per account, or per address if the game cannot tell the accounts apart; many agents on one address can hit it).
- `--typical-interval 10` is how many seconds pass between two requests of one real agent (a round of about 5 calls every 45 s);
  change it if your agents are slower or faster. `--pace S` makes each simulated agent wait S seconds between calls instead of
  going as fast as `--max-rate` allows.

### 2. Online routines: `--routine`

```
python run_swarm.py --count 500 --routine real --max-rate 20
python run_swarm.py --count 20 --routine light --time-scale 24      (a "day" lasts one real hour: to watch it work)
```

Each agent gets a fixed daily timetable, worked out from its name (the same every run; it does not disturb the personalities):
a few online sessions a day, at the hours typical for a time zone chosen per agent (so the swarm is not all online together),
and **no requests at all** in between. Its planets keep producing and its fleets keep flying on the server. Miners and turtles
look in more often for a short time, raiders play in the evening, scientists have fewer, longer sessions.

- `--routine off` (default) always playing; `light` about 45 % of the day online; `real` about 5-10 % (2-5 sessions of 5-40 minutes).
- `--time-scale 24` compresses the day (one routine day per real hour); `--speedup` still just shortens the pauses. With a
  routine, `--ticks N` counts only online rounds, so a run with `--ticks` can wait a long time for an offline agent.
- The log gets one `online` and one `offline` event per session; `summarize_logs.py` shows the share of agents online.
- The stop file and Ctrl+C work while agents are offline (they look at the stop file at least once a minute).
- At the start the agents are spread out by `--stagger` seconds each; for thousands of agents use a small value
  (`--stagger 0.2`): registering 10 000 accounts at 2 s each would take 5 hours.

### 3. The language model with many agents: `--llm-voices`, `--llm-per-agent-hour`

One local model answers one request at a time, so it cannot give thousands of agents a voice.

- `--llm-voices 20`: only agents number 1 to 20 write a diary and have their board texts rewritten in their own words.
  All the others use the plain template texts. Diplomacy decisions (accept / refuse a pact) still go through the model
  for every agent when the hourly budget allows; if not, the fixed rules decide. Default 0 = every agent has a voice.
- `--llm-per-agent-hour 6`: no agent may make more than 6 model requests an hour, so a few chatty agents cannot eat the
  budget (`--llm-max-per-hour`) that everybody shares. An over-quota request is refused and written in `logs/llm.jsonl`
  with the reason "quota: ...". Default 0 = no limit.

### 4. More processes: `--procs`

```
python run_swarm.py --count 2000 --procs 4 --routine real --max-rate 40 --stagger 0.2
```

One Python process can only use about one processor core. `--procs 4` splits agent numbers 1..2000 into four slices and starts
four `run_swarm.py` processes, one per slice. What changes:

- the request pace `--max-rate` (and `--llm-max-per-hour`) is divided by 4, so the total stays what you asked for;
- every process keeps accounts in its own file, `agents_state.json.p1` ... `.p4` (two processes writing one file would corrupt it);
  existing accounts are copied into them at the start, and at the end they are merged back into `agents_state.json`;
- the board (`--board`) and the model (`--llm`) addresses are shared by all of them; `--llm-voices` counts agent numbers over the
  whole swarm, so it keeps working. The model still answers one request at a time: it does not get faster with more processes;
- Ctrl+C in the main window, or the `STOP` file, stops all of them; the main process waits and prints when all have finished.
  The screen shows the lines of all processes mixed; each agent's own log file in `logs/` stays clean.

A good number of processes is the number of CPU cores minus one, and one process for about every 300-500 agents.

### What to change in the game's Docker setup

Only you can see what your setup does, so look at these in order, one at a time, and run `bench.py` after each change:

- **The number of PHP workers** (PHP-FPM). Each request occupies one worker; if all are busy, new requests wait or the web
  server answers 502. The benchmark's 502 count is the sign. More workers use more memory.
- **The database** (MariaDB/MySQL). Most game work ends up there. Check that it has enough memory for its caches
  (the buffer pool) and that the database container is not starved of CPU or limited by Docker.
- **The queue and scheduler** (the game processes building and fleet arrivals in background jobs). If these fall behind, buildings
  finish late for everybody: watch that the queue worker is running, give it more workers if the game offers it.
- **The game's rate limit**: `OGAMEX_API_RATE_LIMIT` in `.env` (per account per minute) and the container CPU / memory limits.
- Run the swarm on another computer, or at least keep an eye on CPU: the game, the database and the agents compete for the same cores.

### Stages and goals

| Stage | Agents | Settings (a start) | Online at once | Requests/s to expect | What to check before the next stage |
|---|---|---|---|---|---|
| 1 | 20 | defaults | 20 | 2-4 | works as today |
| 2 | 100 | `--routine real --max-rate 8` | 5-10 | about 1 | `bench.py` p95 under 1 s; report looks sane |
| 3 | 500 | `--routine real --max-rate 20 --stagger 0.5` | 30-60 | 3-6 | no `api_error` bursts in the report; 1 process is fine |
| 4 | 2000 | `--routine real --procs 4 --max-rate 40 --stagger 0.2 --llm-voices 20` | 150-250 | 15-25 | PHP workers and database tuned; bench shows no 502 |
| 5 | 10 000 | `--routine real --procs 8-16 --max-rate 100 --stagger 0.1 --llm-voices 20`, probably the agents on a second computer | 700-1200 | 70-120 | the server holds that rate for hours; the game's own jobs keep up |

These numbers are estimates from "about 7 % online and one request every 10 s per online agent", not measurements:
trust your own `bench.py` run over this table. Also expect that thousands of agents fill the universe (galaxies and
systems have a limited number of free planet slots) and that logs and memory files grow (one file per agent): watch the disk.

## Tests

```
python -m unittest discover -s . -t . -p "test_*.py"
```

They run against a small simulated game (`tests/fakegame.py`), not your server.


## LLM log page

With the observatory running, open `http://localhost:18766/llm` (your observatory port). It lists every language-model call newest first: agent, purpose, answer, time taken, tokens, and a prompt you can expand. Green USED = the answer was posted or written in a diary; orange NOT USED = answered but rejected (too long, changed numbers...); red FAILED = no answer. Filters by agent, purpose, result and text. Needs `--llm` (or `--llm fake`) on run_swarm.py.

## Galaxy map

`http://localhost:18766/galaxy` (also linked from the observatory header): a zoomable star map. Scroll to zoom, drag to move, click a system to zoom in (the planets appear around the star), double-click to fit all. Fleets in flight are blinking, moving arrows: red attack, yellow spy probe, cyan expedition, green colony ship, orange recycler. A dimmer arrow is the return trip. Untick a mission in the legend to hide it. Origins are the agent's main planet, flight times come from the logs.

The galaxy map draws the game's own planet and moon pictures, bundled in `assets/` (copied from OGameX `public/img`, MIT licence, same author as the game); the picture is picked from the system and slot number by the same rule as the game.
