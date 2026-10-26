"""The language model's log as a web page: what each agent asked, what the model answered, and whether the answer was used.

Read-only and fail-soft. observatory.py serves it at /llm (page) and /api/llm (data). It only reads
<logs>/llm.jsonl (written by swarm/llm.py) and the voice_post / diary events in <logs>/agent*.jsonl, to tell which answers
were really used. With no llm.jsonl (the --llm flag was never used) the page says so instead of failing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

TAIL_BYTES = 4_000_000      # only the end of a big file is read
MAX_CALLS = 400


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().strip("\"'").strip()


def _read_lines(path: Path, keep_bytes: int = TAIL_BYTES) -> list[dict]:
    out: list[dict] = []
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > keep_bytes:
                f.seek(size - keep_bytes)
                f.readline()          # drop a cut line
            data = f.read()
    except OSError:
        return out
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            e = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def used_texts(folder: Path) -> set[tuple[str, str]]:
    """{(agent, text)} of every board rewrite, diary line, hello, announcement and diplomacy answer that was really used."""
    used: set[tuple[str, str]] = set()
    try:
        files = sorted(folder.glob("agent*.jsonl"))
    except OSError:
        return used
    for p in files:
        for e in _read_lines(p, 1_000_000):
            if e.get("event") in ("voice_post", "diary", "diplo_hello", "diplo_announce") and e.get("text"):
                used.add((str(e.get("agent") or p.stem), _norm(e["text"])))
            elif e.get("event") == "diplo_answer" and e.get("raw"):      # the model's own words that decided a diplomacy answer
                used.add((str(e.get("agent") or p.stem), _norm(e["raw"])))
    return used


def llm_view(logs: str | Path, limit: int = 200) -> dict:
    """{"available": bool, "calls": [newest first], "totals": {...}, "agents": [...], "purposes": [...]}. Never raises."""
    folder = Path(logs)
    path = folder / "llm.jsonl"
    try:
        entries = [e for e in _read_lines(path) if e.get("kind") == "llm_call"]
    except Exception:
        entries = []
    if not path.exists() and not entries:
        return {"available": False, "calls": [], "totals": {}, "agents": [], "purposes": []}
    try:
        used = used_texts(folder)
    except Exception:
        used = set()
    limit = max(1, min(int(limit), MAX_CALLS))
    calls = []
    for e in entries[-limit:][::-1]:
        answer = e.get("answer")
        ok = bool(e.get("ok"))
        agent = str(e.get("agent") or "")
        calls.append({
            "t": int(e.get("t") or 0), "agent": agent, "purpose": str(e.get("purpose") or ""),
            "prompt": str(e.get("prompt") or "")[:2000], "answer": None if answer is None else str(answer)[:2000],
            "latency": e.get("latency") if isinstance(e.get("latency"), (int, float)) else None,
            "tokens": e.get("tokens") if isinstance(e.get("tokens"), (int, float)) else None,
            "ok": ok, "error": str(e["error"])[:200] if e.get("error") else None,
            "used": bool(ok and answer is not None and (agent, _norm(answer)) in used),
        })
    lat = [c["latency"] for c in calls if c["latency"] is not None]
    totals = {"calls": len(entries), "shown": len(calls), "failed": sum(1 for e in entries if not e.get("ok")),
              "used": sum(1 for c in calls if c["used"]),
              "avg_latency": round(sum(lat) / len(lat), 2) if lat else None,
              "tokens": int(sum(c["tokens"] for c in calls if c["tokens"]))}
    return {"available": True, "calls": calls, "totals": totals,
            "agents": sorted({c["agent"] for c in calls}), "purposes": sorted({c["purpose"] for c in calls})}


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LLM log</title>
<style>
:root{--bg:#0b0f1a;--panel:#131a2b;--panel2:#18213a;--line:#25304f;--ink:#e8ecf7;--dim:#8d99b8;--good:#3ddc97;--bad:#ff4d6d;--warn:#ffb703;--acc:#7b9cff}
@media (prefers-color-scheme: light){:root:not([data-theme="dark"]){--bg:#f3f5fb;--panel:#fff;--panel2:#f0f3fb;--line:#d9dfef;--ink:#16203a;--dim:#5b6685;--good:#0a9b64;--bad:#cc1f44;--warn:#b36b00;--acc:#3b5bd6}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:20px 16px 60px}
header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
h1{font-size:26px;margin:0;letter-spacing:-.02em}
a{color:var(--acc)}
.sub,.dim{color:var(--dim);font-size:13px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 14px}
.stat b{display:block;font-size:20px;font-variant-numeric:tabular-nums}
.stat small{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}
select,input[type=search]{background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font:inherit}
.call{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--c,var(--dim));border-radius:12px;padding:10px 14px;margin:0 0 10px}
.call.ok{--c:var(--good)} .call.unused{--c:var(--warn)} .call.fail{--c:var(--bad)}
.top{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.who{font-weight:650}
.tag{font-size:12px;color:var(--dim);background:var(--panel2);border-radius:6px;padding:2px 8px}
.badge{margin-left:auto;font-size:11px;font-weight:700;letter-spacing:.06em;color:var(--c);border:1px solid var(--c);border-radius:6px;padding:2px 7px}
.answer{margin:8px 0 4px;font-size:16px;white-space:pre-wrap}
details{margin-top:4px} summary{cursor:pointer;color:var(--dim);font-size:13px}
pre{white-space:pre-wrap;word-break:break-word;background:var(--panel2);border-radius:8px;padding:8px 10px;margin:6px 0;font:12px/1.4 ui-monospace,Consolas,monospace}
.empty{color:var(--dim);text-align:center;padding:50px 0}
.err{color:var(--bad);padding:6px 0}
</style></head><body><div class="wrap">
<header><h1>LLM log</h1><span class="sub">what the agents asked the language model, and what it said</span>
<span class="dim" style="margin-left:auto"><a href="/">Observatory</a> <span id="live"></span></span></header>
<div class="stats" id="stats"></div>
<div class="bar"><select id="fa"></select><select id="fp"></select><select id="fs">
<option value="">any result</option><option value="used">used</option><option value="unused">answered, not used</option><option value="fail">failed</option></select>
<input type="search" id="q" placeholder="search text"></div>
<div id="err" class="err"></div><div id="feed"></div></div>
<script>
const $=id=>document.getElementById(id);
let D=null,last='',opened=new Set();
function el(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
function age(t){const s=Math.max(0,Math.round(Date.now()/1000-t));if(s<60)return s+" s ago";if(s<3600)return Math.round(s/60)+" min ago";if(s<86400)return (s/3600).toFixed(1)+" h ago";return Math.round(s/86400)+" d ago"}
function opts(sel,label,list,name){const cur=sel.value;sel.replaceChildren();const o=el("option","",label);o.value="";sel.appendChild(o);list.forEach(x=>{const e=el("option","",name?name(x):x);e.value=x;sel.appendChild(e)});if(list.includes(cur))sel.value=cur}
const PURPOSE={hello:"hello (diplomacy)",answer:"answer (diplomacy)",announce:"announcement (diplomacy)",diary:"diary",alliance_name:"alliance name and charter",alliance_choice:"alliance choice (join or not)"};
function purposeName(p){return PURPOSE[p]||(String(p).startsWith("rewrite:")?"rewrite "+String(p).slice(8):p)}
function stat(label,v){const s=el("div","stat");s.append(el("b","",String(v)),el("small","",label));return s}
function draw(){
  const T=D.totals||{};const st=$("stats");st.replaceChildren();
  st.append(stat("calls",T.calls??0),stat("used",T.used??0),stat("failed",T.failed??0),stat("avg answer time",T.avg_latency==null?"-":T.avg_latency+" s"),stat("tokens (shown)",T.tokens??0));
  const fa=$("fa").value,fp=$("fp").value,fs=$("fs").value,q=$("q").value.toLowerCase();
  const feed=$("feed");feed.replaceChildren();let n=0;
  D.calls.forEach(c=>{
    const state=!c.ok?"fail":c.used?"used":"unused";
    if(fa&&c.agent!==fa)return;if(fp&&c.purpose!==fp)return;if(fs&&fs!==state)return;
    if(q&&!((c.answer||"")+" "+c.prompt+" "+c.agent+" "+(c.error||"")).toLowerCase().includes(q))return;
    n++;const box=el("div","call "+(state==="used"?"ok":state));
    const top=el("div","top");top.append(el("span","who",c.agent),el("span","tag",purposeName(c.purpose)),el("span","tag",age(c.t)));
    if(c.latency!=null)top.appendChild(el("span","tag",c.latency+" s"));
    if(c.tokens!=null)top.appendChild(el("span","tag",c.tokens+" tokens"));
    top.appendChild(el("span","badge",state==="used"?"USED":state==="fail"?"FAILED":"NOT USED"));box.appendChild(top);
    if(c.answer!=null)box.appendChild(el("div","answer",c.answer));
    if(c.error)box.appendChild(el("div","err","error: "+c.error));
    const key=c.t+"|"+c.agent+"|"+c.purpose;
    const d=el("details");d.open=opened.has(key);d.ontoggle=()=>{if(d.open)opened.add(key);else opened.delete(key)};
    d.appendChild(el("summary","","prompt"));d.appendChild(el("pre","",c.prompt));box.appendChild(d);
    feed.appendChild(box)});
  if(!n)feed.appendChild(el("div","empty",D.available?"No call matches.":"No llm.jsonl yet. Start the agents with --llm (or --llm fake) and the calls appear here."))}
async function load(){
  try{const r=await fetch("/api/llm?limit=200",{cache:"no-store"});const j=await r.json();$("err").textContent="";
    const sig=JSON.stringify([j.totals,(j.calls[0]||{}).t,j.calls.length]);$("live").textContent=" - live";if(sig===last&&D)return;last=sig;D=j;
    opts($("fa"),"all agents",D.agents||[]);opts($("fp"),"all purposes",D.purposes||[],purposeName);draw()}
  catch(e){$("err").textContent="Cannot reach the server: "+e;$("live").textContent=""}}
["fa","fp","fs"].forEach(i=>$(i).onchange=()=>D&&draw());$("q").oninput=()=>D&&draw();
load();setInterval(load,6000);
</script></body></html>
"""
