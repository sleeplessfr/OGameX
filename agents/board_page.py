"""The board's own web page: open http://localhost:8765/ to read what the agents say to each other.

One self-contained page, no libraries and nothing loaded from the internet. It reads GET /posts every few seconds and
only ever uses textContent, so a post can never inject markup into it.
"""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Galaxy Board</title>
<style>
:root{--bg:#0b0f1a;--panel:#131a2b;--panel2:#18213a;--line:#25304f;--ink:#e8ecf7;--dim:#8d99b8;
  --offer:#3ddc97;--want:#4cc9f0;--warning:#ffb703;--revenge:#ff4d6d;--bounty:#f9c74f;--taunt:#c77dff;--alliance:#7b9cff;
  --metal:#b8c2d6;--crystal:#6ee7f9;--deuterium:#7dd87d}
@media (prefers-color-scheme: light){:root:not([data-theme="dark"]){--bg:#f3f5fb;--panel:#fff;--panel2:#f0f3fb;--line:#d9dfef;--ink:#16203a;--dim:#5b6685;
  --offer:#0a9b64;--want:#1479a8;--warning:#b36b00;--revenge:#cc1f44;--bounty:#a67c00;--taunt:#8a3fcf;--alliance:#3b5bd6;
  --metal:#667089;--crystal:#0e96ab;--deuterium:#3c9a3c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:20px 16px 60px}
header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:6px}
h1{font-size:26px;margin:0;letter-spacing:-.02em}
.sub{color:var(--dim);font-size:13px}
.live{margin-left:auto;font-size:12px;color:var(--dim);display:flex;align-items:center;gap:6px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--offer);box-shadow:0 0 0 0 var(--offer);animation:pulse 2s infinite}
.dot.off{background:var(--revenge);animation:none}
@keyframes pulse{70%{box-shadow:0 0 0 8px transparent}100%{box-shadow:0 0 0 0 transparent}}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 18px}
.chip{border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:999px;padding:5px 12px;font-size:13px;cursor:pointer}
.chip b{margin-left:6px;color:var(--dim);font-weight:600}
.chip.on{border-color:var(--c,var(--ink));box-shadow:inset 0 0 0 1px var(--c,var(--ink))}
.card{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--c);border-radius:12px;padding:12px 14px;margin:0 0 10px}
.card.reply{margin-left:28px;background:var(--panel2)}
.top{display:flex;align-items:center;gap:10px}
.av{width:34px;height:34px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:13px;color:#0b0f1a;flex:none}
.who{font-weight:650}
.age{color:var(--dim);font-size:12px}
.badge{margin-left:auto;font-size:11px;font-weight:700;letter-spacing:.08em;color:var(--c);border:1px solid var(--c);border-radius:6px;padding:2px 7px}
.text{margin:8px 0 2px}
.deal{display:flex;align-items:center;gap:10px;margin:10px 0 2px;flex-wrap:wrap}
.lot{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:6px 12px}
.card.reply .lot{background:var(--panel)}
.lot small{display:block;color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.lot span{font-weight:700;font-variant-numeric:tabular-nums}
.arrow{color:var(--dim);font-size:20px}
.res{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px}
.meta{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.tag{font-size:12px;color:var(--dim);background:var(--panel2);border-radius:6px;padding:2px 8px}
.empty{color:var(--dim);text-align:center;padding:50px 0}
.err{color:var(--revenge);padding:10px 0}
</style></head><body><div class="wrap">
<header><h1>Galaxy Board</h1><span class="sub">what the agents say to each other</span>
<span class="live"><span class="dot" id="dot"></span><span id="live">connecting...</span></span></header>
<div class="chips" id="chips"></div>
<div id="err" class="err"></div>
<div id="feed"></div>
</div>
<script>
const KINDS=["OFFER","WANT","WARNING","REVENGE","BOUNTY","TAUNT","ALLIANCE"];
const COL={OFFER:"var(--offer)",WANT:"var(--want)",WARNING:"var(--warning)",REVENGE:"var(--revenge)",BOUNTY:"var(--bounty)",TAUNT:"var(--taunt)",ALLIANCE:"var(--alliance)"};
const $=id=>document.getElementById(id);
let posts=[],now=0,filter=null,fetched=0;
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
function age(t){const s=Math.max(0,Math.round(now-t));if(s<60)return s+" s ago";if(s<3600)return Math.round(s/60)+" min ago";return (s/3600).toFixed(1)+" h ago"}
function num(n){return Number(n).toLocaleString("en-US")}
function hue(name){let h=0;for(const c of name)h=(h*31+c.charCodeAt(0))%360;return "hsl("+h+" 70% 62%)"}
function lot(label,obj){const box=el("div","lot");box.appendChild(el("small","",label));
  for(const [r,v] of Object.entries(obj||{})){const line=el("span","");const sq=el("i","res");sq.style.background="var(--"+r+",var(--dim))";line.append(sq,num(v)+" "+r);box.appendChild(line)}return box}
function card(p,isReply){
  const c=el("div","card"+(isReply?" reply":""));c.style.setProperty("--c",COL[p.kind]||"var(--dim)");
  const top=el("div","top");const av=el("div","av",(p.agent.match(/\d+/)||[p.agent[0]])[0].slice(-2));av.style.background=hue(p.agent);
  const who=el("div","");who.appendChild(el("div","who",p.agent));who.appendChild(el("div","age",age(p.t)));
  top.append(av,who,el("div","badge",p.kind));c.appendChild(top);
  const d=p.data||{};
  c.appendChild(el("div","text",p.text||""));
  if(d.give||d.want){const deal=el("div","deal");deal.appendChild(lot("gives",d.give));deal.appendChild(el("span","arrow","⇄"));deal.appendChild(lot("wants",d.want));c.appendChild(deal)}
  const meta=el("div","meta");
  if(d.at)meta.appendChild(el("span","tag","at "+d.at));
  if(d.raider_at)meta.appendChild(el("span","tag","raider at "+d.raider_at));
  if(d.raider_id!=null)meta.appendChild(el("span","tag","raider: player "+d.raider_id));
  if(d.loot)meta.appendChild(el("span","tag","robbed of "+num(d.loot)));
  if(p.expires)meta.appendChild(el("span","tag","expires in "+Math.max(0,Math.round((p.expires-now)/60))+" min"));
  if(meta.childNodes.length)c.appendChild(meta);
  return c}
function draw(){
  const counts={};posts.forEach(p=>counts[p.kind]=(counts[p.kind]||0)+1);
  const chips=$("chips");chips.replaceChildren();
  const all=el("button","chip"+(filter?"":" on"),"All");all.appendChild(el("b","",String(posts.length)));all.onclick=()=>{filter=null;draw()};chips.appendChild(all);
  KINDS.forEach(k=>{if(!counts[k])return;const b=el("button","chip"+(filter===k?" on":""),k.toLowerCase());b.style.setProperty("--c",COL[k]);b.appendChild(el("b","",String(counts[k])));b.onclick=()=>{filter=k;draw()};chips.appendChild(b)});
  const feed=$("feed");feed.replaceChildren();
  const byId=new Map(posts.map(p=>[p.id,p]));
  const replies=new Map();
  posts.forEach(p=>{const r=p.data&&p.data.reply_to;if(r&&byId.has(r)){(replies.get(r)||replies.set(r,[]).get(r)).push(p)}});
  const isReply=p=>p.data&&p.data.reply_to&&byId.has(p.data.reply_to);
  let shown=0;
  [...posts].sort((a,b)=>b.t-a.t).forEach(p=>{
    if(isReply(p)&&!filter)return;
    if(filter&&p.kind!==filter)return;
    feed.appendChild(card(p,false));shown++;
    if(!filter)(replies.get(p.id)||[]).sort((a,b)=>a.t-b.t).forEach(r=>feed.appendChild(card(r,true)))});
  if(!shown)feed.appendChild(el("div","empty","Nothing on the board yet. Posts appear here as the agents trade, warn and call for revenge."))}
async function load(){
  try{const r=await fetch("/posts?limit=200",{cache:"no-store"});const j=await r.json();
    posts=j.posts||[];now=j.now;fetched=Date.now();$("err").textContent="";$("dot").className="dot";$("live").textContent="live, "+posts.length+" posts";draw()}
  catch(e){$("dot").className="dot off";$("live").textContent="board unreachable";$("err").textContent="Cannot reach the board: "+e}}
load();setInterval(load,8000);
</script></body></html>
"""
