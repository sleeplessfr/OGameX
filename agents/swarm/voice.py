"""The inner voice of an agent: a character sheet, prompts, and the checks on what the language model answers. Pure functions.

The voice is TEXT ONLY and has no effect on play. It does two things (see llm_agent.py):
  * rewrites the `text` of a board post the rules already decided on (the structured `data` is never touched), and
  * writes a one-line diary entry after a notable event.
Whatever the model says is checked here before it is used: too long, markup, an empty answer, or a number / coordinate that is
not in the original, and the answer is thrown away.
"""
from __future__ import annotations

import random
import re
import zlib

MAX_WORDS = 25
MAX_CHARS = 220
DIARY_KEEP = 20
VOICE_KINDS = ("WARNING", "REVENGE", "BOUNTY", "OFFER", "TAUNT", "CLAIM")

FIRST = ("Ada", "Brik", "Cato", "Dara", "Edda", "Fenn", "Gorm", "Hale", "Ines", "Jory", "Kade", "Lena", "Mira", "Nox", "Orla",
         "Pike", "Quin", "Rhea", "Sorn", "Tavi", "Ulla", "Vex", "Wren", "Yara", "Zed")
LAST = ("Vance", "Okoro", "Larsen", "Quill", "Mbeki", "Roth", "Castell", "Ibarra", "Nakamura", "Dray", "Holt", "Sato", "Varga", "Kemp")
ORIGIN = {
    "miner": ("grew up on a dusty ore moon", "was a foreman in the deep shafts", "inherited a failing mine and made it sing"),
    "scientist": ("left the academy before the funding ran out", "kept notes on everything since childhood", "once lost a lab to a bad experiment"),
    "turtle": ("lost a first colony to raiders and never forgot", "was a quartermaster who counted every crate", "learned that walls are cheaper than funerals"),
    "raider": ("flew cargo runs until the pay insulted them", "was born in a scrapyard fleet", "learned early that stores are for taking"),
    "balanced": ("did a bit of everything and finished little", "drifted between jobs until space called", "likes to keep options open"),
    "trader": ("ran a market stall on a freight hub", "never met a price they did not haggle", "keeps a ledger and a long memory"),
}
GOAL = ("wants a name the whole galaxy knows", "is saving for one big colony", "wants to be left alone and well stocked",
        "is out to prove an old rival wrong", "dreams of a fleet that darkens the sky", "just wants a quiet, rich life")


def temperament(persona) -> list[str]:
    """Words for the personality knobs, strongest first. Deterministic."""
    w: list[str] = []
    a = getattr(persona, "aggression", 0.0)
    w.append("ruthless" if a >= 0.6 else "opportunistic" if a >= 0.25 else "peaceful")
    p = getattr(persona, "patience", 0.5)
    w.append("patient" if p >= 0.75 else "impatient" if p <= 0.5 else "steady")
    if getattr(persona, "caution", 0.0) >= 0.6:
        w.append("cautious")
    if getattr(persona, "trade_interest", 0.0) >= 0.5 or getattr(persona, "scout_interest", 0.0) >= 0.7:
        w.append("sociable")
    if getattr(persona, "min_loot", 500) <= 500 and a >= 0.25:
        w.append("greedy")
    if getattr(persona, "research_share", 0.5) >= 0.9:
        w.append("curious")
    if getattr(persona, "expansion", 0.0) >= 0.6:
        w.append("restless")
    return w


def make_sheet(name: str, persona) -> dict:
    """A short character sheet from the agent's name and persona: always the same for the same inputs."""
    rng = random.Random(zlib.crc32(f"{name}|{persona.name}".encode()))
    full = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
    origin = rng.choice(ORIGIN.get(persona.name, ORIGIN["balanced"]))
    goal = rng.choice(GOAL)
    traits = temperament(persona)
    return {"name": full, "handle": name, "persona": persona.name, "traits": traits,
            "backstory": f"{full} {origin} and {goal}."}


def system_prompt(sheet: dict) -> str:
    """The system message: who the agent is and the rules of the voice."""
    return (f"You are {sheet['name']}, a player in a space strategy game (callsign {sheet['handle']}) in the style of OGame: "
            f"you mine metal, crystal and deuterium, research, build fleets, raid, trade and send expeditions. "
            f"Speak as the player, not as a narrator; never mention being an AI or a diary task.\n"
            f"BACKSTORY: {sheet['backstory']}\n"
            f"TONE: {', '.join(sheet['traits'])}\n"
            f"Stay in character. Answer with ONE line of plain text, at most {MAX_WORDS} words. No markup, no quotes, no emojis, "
            f"no explanations. Never change a number or a coordinate.")


def rewrite_prompt(kind: str, text: str) -> str:
    return (f"TASK: rewrite\nA {kind} post for the galaxy board is already decided; say the same thing in your own voice. "
            f"Keep every number and coordinate exactly as written.\nTEXT: {text}")


def diary_prompt(event_text: str) -> str:
    return (f"TASK: diary\nWrite one line for your diary about what just happened, in the first person. "
            f"Do not invent numbers.\nEVENT: {event_text}")


# ---- checking what comes back --------------------------------------------------------------------------------------
_MARKUP = re.compile(r"[<>`*#{}\[\]|\\]|https?://|\bwww\.|^\s*[-=_]{2,}|\n", re.I)
_COORD = re.compile(r"\b\d+:\d+:\d+\b")
_NUMBER = re.compile(r"\d[\d,.]*")


def numbers_in(text: str) -> set[str]:
    """Every number in a text, normalised (thousands commas and a trailing full stop removed); coordinates stay whole."""
    found = set(_COORD.findall(text))
    rest = _COORD.sub(" ", text)
    for n in _NUMBER.findall(rest):
        n = n.rstrip(".,").replace(",", "")
        if n:
            found.add(n)
    return found


def _data_numbers(data) -> set[str]:
    out: set[str] = set()
    if isinstance(data, dict):
        for v in data.values():
            out |= _data_numbers(v)
    elif isinstance(data, (list, tuple)):
        for v in data:
            out |= _data_numbers(v)
    elif isinstance(data, bool) or data is None:
        pass
    elif isinstance(data, (int, float)):
        out.add(str(int(data)) if float(data).is_integer() else str(data))
    elif isinstance(data, str):
        out |= numbers_in(data)
    return out


def clean(answer) -> str:
    """The first line of an answer, without wrapping quotes."""
    if not isinstance(answer, str):
        return ""
    text = answer.strip().strip("\"'“”").strip()
    return re.sub(r"\s+", " ", text) if "\n" not in text else text   # a multi-line answer is rejected by check()


def check(answer, original: str = "", data=None, require_coords: bool = False) -> str | None:
    """The answer cleaned up if it may be used, else None.

    Rejected: empty, over MAX_WORDS words or MAX_CHARS characters, markup, any number or coordinate that is neither in the
    `original` text nor in `data`; and, with `require_coords`, an answer that drops a coordinate of the original."""
    text = clean(answer)
    if not text or len(text) > MAX_CHARS or len(text.split()) > MAX_WORDS or _MARKUP.search(text):
        return None
    allowed = numbers_in(original) | _data_numbers(data)
    if not numbers_in(text) <= allowed:
        return None
    if require_coords and not set(_COORD.findall(original)) <= set(_COORD.findall(text)):
        return None
    return text
