"""The /galaxy page of the observatory: a zoomable star map with the fleets in flight (Stellaris style).

`fleets_from(events, origin_of, now)` turns the agent logs into fleet movements (attack, spy, expedition, colony ship, recycler).
The log has the target and, for most missions, the flight time (`seconds`); the origin is the agent's main planet (or the event's own
`from`). The page (PAGE) draws systems on a grid, planets around the star when zoomed in, and animated, blinking arrows per mission.
"""
from __future__ import annotations

MISSIONS = {
    "attack": ("Attack", "#ff4d4d", True),
    "spy": ("Spy probe", "#ffd24a", False),
    "expedition_sent": ("Expedition", "#4ad8ff", True),
    "colony_sent": ("Colony ship", "#5dff8a", False),
    "recycle_sent": ("Recycler", "#ff9a3c", True),
}
SPEED_S_PER_SYSTEM = 6.0     # flight time estimate when the log does not say: 30 s plus this per system of distance
KEEP_S = 3600.0              # fleets older than this are dropped
GHOST_S = 90.0               # a fleet that landed is still shown this long, fading


def _coord(text):
    try:
        g, s, p = (int(x) for x in str(text).split(":"))
        return [g, s, p]
    except (ValueError, TypeError):
        return None


def _total(ships) -> int:
    try:
        return sum(int(v or 0) for v in ships.values()) if isinstance(ships, dict) else int(ships or 0)
    except (TypeError, ValueError):
        return 0


def fleets_from(events: list[dict], origin_of, now: float, persona_of=lambda a: None, limit: int = 300) -> list[dict]:
    """Fleet movements from the agent logs, newest first. `origin_of(agent)` -> [g, s, p] or None."""
    out = []
    for e in events:
        kind = e.get("event")
        if kind not in MISSIONS or now - e["t"] > KEEP_S:
            continue
        to = _coord(e.get("at"))
        frm = _coord(e.get("from")) or origin_of(e.get("agent"))
        if not to or not frm:
            continue
        dist = abs(to[1] - frm[1]) if to[0] == frm[0] else 50
        try:
            secs = float(e.get("seconds"))
        except (TypeError, ValueError):
            secs = 0.0
        if secs <= 0:
            secs = 30.0 + SPEED_S_PER_SYSTEM * dist
        label, color, comes_back = MISSIONS[kind]
        hold = float(e.get("hours") or 0) * 3600.0 if kind == "expedition_sent" else 0.0
        out.append({"agent": e.get("agent"), "persona": persona_of(e.get("agent")), "kind": kind, "label": label, "color": color,
                    "from": frm, "to": to, "t": e["t"], "secs": secs, "back": bool(comes_back), "hold": min(hold, 7200.0),
                    "ships": _total(e.get("ships")),
                    "dodged": kind == "attack" and False})
    out.sort(key=lambda f: -f["t"])
    live = [f for f in out if now <= f["t"] + (2 * f["secs"] + f["hold"] if f["back"] else f["secs"]) + GHOST_S]
    return live[:limit]


DEFENCE = ("rocket_launcher", "light_laser", "heavy_laser", "gauss_cannon", "ion_cannon", "plasma_turret", "small_shield_dome",
           "large_shield_dome", "anti_ballistic_missile", "interplanetary_missile")


DEBRIS_KEEP_S = 24 * 3600.0


def debris_from(events: list[dict], now: float) -> list[dict]:
    """Debris fields as the agents last knew them (the game's galaxy view does not list them): a battle adds its debris, a recycler
    trip takes what it hauled, a skipped trip says how much was there. Fields untouched for a day are forgotten."""
    fields: dict[tuple, list] = {}
    for e in sorted(events, key=lambda x: x["t"]):
        kind = e.get("event")
        if kind not in ("battle", "recycle_sent", "recycle_skipped"):
            continue
        c = _coord(e.get("at"))
        if not c:
            continue
        key = tuple(c)
        try:
            amount = float(e.get("debris") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        cur = fields.get(key, [0.0, 0.0])
        if kind == "battle":
            cur = [cur[0] + amount, e["t"]]
        elif kind == "recycle_sent":
            try:
                haul = float(e.get("haul") or 0)
            except (TypeError, ValueError):
                haul = 0.0
            cur = [max(0.0, (amount or cur[0]) - haul), e["t"]]
        else:
            cur = [amount, e["t"]]
        fields[key] = cur
    return [{"at": list(k), "amount": int(v[0]), "t": v[1]} for k, v in fields.items()
            if v[0] >= 1000 and now - v[1] <= DEBRIS_KEEP_S]


def planet_view(d: dict) -> dict:
    """The GET /planets/{id} answer, cut into what the map shows: summary, economy, buildings, ships, defence, queues."""
    units = d.get("units") or {}
    return {"public": False, "id": d.get("id"), "name": d.get("name"), "type": d.get("type"), "coordinates": d.get("coordinates"),
            "fields": d.get("fields"), "resources": d.get("resources"), "storage": d.get("storage"),
            "production": d.get("production_per_hour"), "energy": d.get("energy"),
            "buildings": dict(sorted((d.get("buildings") or {}).items())),
            "defence": {k: v for k, v in units.items() if k in DEFENCE},
            "ships": {k: v for k, v in units.items() if k not in DEFENCE},
            "building_queue": d.get("building_queue") or [], "unit_queue": d.get("unit_queue") or []}


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Galaxy map</title><style>
:root{--bg:#05070f;--panel:#0c1222cc;--ink:#dbe4ff;--dim:#7d8bb0;--accent:#6cb4ff}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);font:14px system-ui,sans-serif;overflow:hidden}
canvas{position:absolute;inset:0;width:100%;height:100%;cursor:grab;display:block}canvas.drag{cursor:grabbing}
header{position:absolute;top:0;left:0;right:0;display:flex;gap:14px;align-items:center;padding:8px 14px;background:linear-gradient(#05070fee,#05070f00);z-index:2;pointer-events:none}
header>*{pointer-events:auto}header a{color:var(--accent);text-decoration:none}h1{font-size:16px;margin:0}
.panel{position:absolute;background:var(--panel);border:1px solid #223;border-radius:10px;padding:10px 12px;backdrop-filter:blur(6px);z-index:2}
#legend{left:14px;top:48px;display:flex;flex-direction:column;gap:5px}
#legend label{display:flex;align-items:center;gap:8px;cursor:pointer;user-select:none}
#legend i{display:inline-block;width:22px;height:4px;border-radius:2px}
#list{right:14px;top:48px;width:310px;max-height:46vh;overflow:auto}
#list div{padding:4px 0;border-bottom:1px solid #1a2238;cursor:pointer;font-size:13px}#list div:hover{background:#16203a}
#info{left:14px;bottom:14px;max-width:420px;min-height:40px}
#tip{position:absolute;pointer-events:none;background:#0c1222f0;border:1px solid #334;border-radius:8px;padding:6px 9px;font-size:12px;display:none;z-index:3;max-width:300px}
#pl{right:14px;top:48px;width:340px;max-height:80vh;overflow:auto;display:none}#pl h3{margin:0 0 4px}#pl table{width:100%;border-collapse:collapse}#pl td{padding:1px 4px;font-size:12px}#pl td+td{text-align:right}#pl .x{float:right;cursor:pointer;color:var(--dim)}#pl h4{margin:8px 0 2px;font-size:12px;color:var(--accent)}
#hint{right:14px;bottom:14px;color:var(--dim);font-size:12px}.dim{color:var(--dim)}
button{background:#16203a;color:var(--ink);border:1px solid #2a3558;border-radius:6px;padding:3px 9px;cursor:pointer}
</style></head><body>
<canvas id="c"></canvas>
<header><h1>Galaxy map</h1><a href="/">Observatory</a><a href="/llm">LLM log</a><button id="fit">Fit all</button><button id="whole">Whole galaxy</button><span id="status" class="dim"></span></header>
<div class="panel" id="legend"></div>
<div class="panel" id="list"><b>Fleets in flight</b><div class="dim">loading...</div></div>
<div class="panel" id="info"><span class="dim">Click a system to zoom in, double-click empty space to fit all.</span></div>
<div class="panel" id="hint">wheel = zoom, drag = move</div>
<div class="panel" id="pl"></div>
<div id="tip"></div>
<script>
function loadHidden(){try{const v=localStorage.getItem("galaxyHidden");if(v!==null)return new Set(JSON.parse(v))}catch(e){}return new Set(["spy"])}
function loadBack(){try{const v=localStorage.getItem("galaxyBack");if(v!==null)return v==="1"}catch(e){}return true}
function saveView(){try{localStorage.setItem("galaxyHidden",JSON.stringify([...hidden]));localStorage.setItem("galaxyBack",showBack?"1":"0")}catch(e){}}
const COLORS={miner:"#d9a441",scientist:"#4aa3ff",turtle:"#4fbf7a",raider:"#e5534b",balanced:"#b07cf0",other:"#6b7280"};
const $=i=>document.getElementById(i),cv=$("c"),g=cv.getContext("2d");
let D=null,offset=0,cam={x:0,y:0,k:1},hidden=loadHidden(),showBack=loadBack(),hover=null,focusSys=null,sysPos={},sysInfo={},stars=[];
// The galaxy is an ELLIPTICAL one: a round, dense core that thins out towards the edge. System 1 is the centre and the numbers wind
// outwards CLOCKWISE in one tight spiral, so the last systems (near 499) are the outermost. Radius r = A*(n-1)^0.65 (dense core,
// sparse rim); the angle advances so that neighbours along the path and neighbours between two turns are about equally far apart.
const A=74,POWER=.65,MAXSYS=499,POS=[null];
(function(){let th=-Math.PI/2;for(let n=1;n<=MAXSYS+60;n++){const r=n===1?0:A*Math.pow(n-1,POWER);
  if(n>1){const sp=Math.sqrt(2*Math.PI*POWER)*A*Math.pow(n,POWER-.5);th+=Math.min(1.3,sp/Math.max(r,1))}
  POS.push({x:Math.cos(th)*r,y:Math.sin(th)*r})}})();   // y points down on the screen: a growing angle turns clockwise
function pos(s){return POS[Math.max(1,Math.min(POS.length-1,s))]}
function orbitR(p){return 12+p*4.6}   // slot 1 is close to the star, slot 15 far away
function moonOff(p){const a=p*2.4-1+.9;return {x:Math.cos(a)*6,y:Math.sin(a)*6}}
function debrisOff(p){const a=p*2.4-1-.9;return {x:Math.cos(a)*9,y:Math.sin(a)*9}}
function planetOff(p){const a=p*2.4-1;const r=orbitR(p);return {x:Math.cos(a)*r,y:Math.sin(a)*r}}
function resize(){cv.width=innerWidth*devicePixelRatio;cv.height=innerHeight*devicePixelRatio}
addEventListener("resize",resize);resize();
function w2s(x,y){return {x:(x-cam.x)*cam.k+innerWidth/2,y:(y-cam.y)*cam.k+innerHeight/2}}
function s2w(x,y){return {x:(x-innerWidth/2)/cam.k+cam.x,y:(y-innerHeight/2)/cam.k+cam.y}}
function fit(){const ss=Object.keys(sysInfo).map(Number);if(!ss.length)return;let a=1e9,b=1e9,c=-1e9,d=-1e9;
  ss.forEach(s=>{const p=pos(s);a=Math.min(a,p.x);b=Math.min(b,p.y);c=Math.max(c,p.x);d=Math.max(d,p.y)});
  cam.x=(a+c)/2;cam.y=(b+d)/2;cam.k=Math.min(3,Math.min(innerWidth*.8/(c-a+300),innerHeight*.8/(d-b+300)))}
function zoomTo(s){const p=pos(s);cam.x=p.x;cam.y=p.y;cam.k=Math.max(cam.k,3.8);focusSys=s}
$("fit").onclick=()=>{fit();focusSys=null};
$("whole").onclick=()=>{const p=pos(MAXSYS);cam.x=0;cam.y=0;cam.k=Math.min(innerWidth,innerHeight)*.46/(A*Math.pow(MAXSYS,POWER)*1.1);focusSys=null};
let drag=null;
cv.onmousedown=e=>{drag={x:e.clientX,y:e.clientY,cx:cam.x,cy:cam.y,moved:false};cv.classList.add("drag")};
addEventListener("mouseup",e=>{if(drag&&!drag.moved)clickAt(e.clientX,e.clientY);drag=null;cv.classList.remove("drag")});
addEventListener("mousemove",e=>{if(drag){const dx=e.clientX-drag.x,dy=e.clientY-drag.y;if(Math.abs(dx)+Math.abs(dy)>3)drag.moved=true;cam.x=drag.cx-dx/cam.k;cam.y=drag.cy-dy/cam.k}hoverAt(e.clientX,e.clientY)});
cv.addEventListener("wheel",e=>{e.preventDefault();const b=s2w(e.clientX,e.clientY);cam.k=Math.min(14,Math.max(.06,cam.k*Math.exp(-e.deltaY*.0015)));const a=s2w(e.clientX,e.clientY);cam.x+=b.x-a.x;cam.y+=b.y-a.y},{passive:false});
cv.ondblclick=()=>{fit();focusSys=null};
function nearestSys(mx,my,r){let best=null,bd=r;for(const s in sysInfo){const q=w2s(pos(+s).x,pos(+s).y),d=Math.hypot(q.x-mx,q.y-my);if(d<bd){bd=d;best=+s}}return best}
function planetAt(mx,my){const mix=Math.max(0,Math.min(1,(cam.k-1.4)/1.6));if(mix<.5)return null;let best=null,bd=14;
  for(const s in sysInfo){const c=pos(+s);sysInfo[s].slots.forEach(sl=>{const o=planetOff(sl.position),q=w2s(c.x+o.x,c.y+o.y),d=Math.hypot(q.x-mx,q.y-my);if(d<bd){bd=d;best={sys:+s,sl}}})}return best}
const nf=n=>n==null?"-":Number(n).toLocaleString("en"),tt=n=>String(n).replace(/_/g," ");
function tbl(o){const e=Object.entries(o||{});return e.length?"<table>"+e.map(([k,v])=>`<tr><td>${tt(k)}</td><td>${nf(v)}</td></tr>`).join("")+"</table>":'<span class="dim">none</span>'}
function queueTxt(q){return q&&q.length?q.map(i=>tt(i.machine_name||i.title||"?")+(i.level_target?" \u2192 "+i.level_target:"")+(i.amount?" x"+i.amount:"")).join(", "):'<span class="dim">empty</span>'}
async function showPlanet(h){const pl=$("pl");pl.style.display="block";$("list").style.display="none";const sl=h.sl;
  pl.innerHTML=`<span class="x" onclick="closePlanet()">\u2715</span><h3>${sl.planet||"planet"}</h3><div class="dim">${1}:${h.sys}:${sl.position} - ${sl.owner||"?"}${sl.agent?` (${sl.agent}, ${sl.persona||"?"})`:""}${sl.moon?" - has a moon":""}</div><div class="dim">loading...</div>`;
  try{const r=await fetch(`/api/planet?id=${sl.planet_id||0}&owner=${encodeURIComponent(sl.owner||"")}&name=${encodeURIComponent(sl.planet||"")}`);const d=await r.json();
    let html=pl.innerHTML.replace('<div class="dim">loading...</div>',"");
    if(sl.debris&&d.public)html+=`<h4>Debris field here</h4>about ${nf(sl.debris)} metal+crystal (as last known from the logs)`;
    if(d.public){html+=`<p class="dim">${d.note||"public information only"}</p>`}
    else{const f=d.fields||{},rs=d.resources||{},pr=d.production||{},st=d.storage||{};
      if(sl.debris)html+=`<h4>Debris field here</h4>about ${nf(sl.debris)} metal+crystal (as last known from the logs)`;html+=`<h4>Fields</h4>${f.used}/${f.max} used`;
      html+=`<h4>Resources (stock / hourly production / storage)</h4><table><tr><td>metal</td><td>${nf(rs.metal)} / +${nf(pr.metal)} / ${nf(st.metal)}</td></tr><tr><td>crystal</td><td>${nf(rs.crystal)} / +${nf(pr.crystal)} / ${nf(st.crystal)}</td></tr><tr><td>deuterium</td><td>${nf(rs.deuterium)} / +${nf(pr.deuterium)} / ${nf(st.deuterium)}</td></tr><tr><td>energy</td><td>${nf(d.energy)}</td></tr></table>`;
      html+=`<h4>Buildings</h4>${tbl(d.buildings)}<h4>Defence</h4>${tbl(d.defence)}<h4>Ships</h4>${tbl(d.ships)}`;
      html+=`<h4>Building queue</h4>${queueTxt(d.building_queue)}<h4>Unit queue</h4>${queueTxt(d.unit_queue)}`;
      const a=sl.agent&&D&&D.agents&&D.agents[sl.agent];if(a&&a.last_events&&a.last_events.length)html+="<h4>Latest events of "+sl.agent+"</h4>"+a.last_events.slice(-5).reverse().map(e=>`<div class="dim">${e.text}</div>`).join("")}
    pl.innerHTML=html}catch(e){pl.innerHTML+='<p class="dim">cannot read the planet</p>'}}
function closePlanet(){$("pl").style.display="none";$("list").style.display="block"}
function clickAt(mx,my){const hp=planetAt(mx,my);if(hp){showPlanet(hp);return}
const s=nearestSys(mx,my,28*Math.max(1,cam.k/2));if(s!=null){zoomTo(s);showSys(s)}}
function showSys(s){const i=sysInfo[s];if(!i)return;const sl=i.slots.map(x=>`${x.position}: ${x.owner||"empty"}${x.agent?" ("+x.persona+")":""}${x.battle?" ⚔":""}`).join("<br>");$("info").innerHTML=`<b>System ${s}</b><br><span class="dim">${sl}</span>`}
function ends(f,mix){const a=pos(f.from[1]),b=pos(f.to[1]);const pa=planetOff(f.from[2]),pb=planetOff(f.to[2]);
  return [{x:a.x+pa.x*mix,y:a.y+pa.y*mix},{x:b.x+pb.x*mix,y:b.y+pb.y*mix}]}
function phase(f,now){const el=now-f.t;if(el<0)return {leg:"wait",p:0};
  if(el<f.secs)return {leg:"out",p:el/f.secs};
  if(f.back){const e2=el-f.secs-f.hold;if(e2<0)return {leg:"hold",p:1};if(e2<f.secs)return {leg:"back",p:1-e2/f.secs};
    if(e2<f.secs+90)return {leg:"done",p:0,fade:1-(e2-f.secs)/90}}
  else if(el<f.secs+90)return {leg:"done",p:1,fade:1-(el-f.secs)/90};
  return {leg:"gone",p:0}}
function bez(A,B,t,bulge){const mx=(A.x+B.x)/2,my=(A.y+B.y)/2,dx=B.x-A.x,dy=B.y-A.y,L=Math.hypot(dx,dy)||1;const cx=mx-dy/L*bulge,cy=my+dx/L*bulge;
  const u=1-t;return {x:u*u*A.x+2*u*t*cx+t*t*B.x,y:u*u*A.y+2*u*t*cy+t*t*B.y,cx,cy}}
function draw(ts){requestAnimationFrame(draw);const dpr=devicePixelRatio;g.setTransform(dpr,0,0,dpr,0,0);g.clearRect(0,0,innerWidth,innerHeight);
  const blink=.65+.35*Math.sin(ts/140);
  g.fillStyle="#fff";for(const st of stars){g.globalAlpha=st.a;g.fillRect((st.x*innerWidth+(-cam.x*cam.k*.02))%innerWidth,st.y*innerHeight,st.r,st.r)}g.globalAlpha=1;
  if(!D)return;const now=Date.now()/1000+offset;const mix=Math.max(0,Math.min(1,(cam.k-1.4)/1.6));
  g.save();g.translate(innerWidth/2,innerHeight/2);g.scale(cam.k,cam.k);g.translate(-cam.x,-cam.y);
  const RMAX=A*Math.pow(MAXSYS,POWER)*1.15;const core=g.createRadialGradient(0,0,0,0,0,RMAX);core.addColorStop(0,"rgba(255,214,150,.42)");core.addColorStop(.12,"rgba(255,190,120,.22)");core.addColorStop(.4,"rgba(150,130,200,.09)");core.addColorStop(1,"rgba(0,0,0,0)");g.fillStyle=core;g.beginPath();g.arc(0,0,RMAX,0,7);g.fill();
  g.fillStyle="#6c7fb8";for(let n=1;n<=MAXSYS;n++){if(sysInfo[n])continue;const q=POS[n];g.globalAlpha=.85;g.beginPath();g.arc(q.x,q.y,Math.max(2,3/Math.sqrt(cam.k)),0,7);g.fill()}g.globalAlpha=1;
  g.strokeStyle="rgba(110,140,220,.10)";g.lineWidth=1/cam.k;g.beginPath();for(let n=1;n<=MAXSYS;n++){const q=POS[n];n===1?g.moveTo(q.x,q.y):g.lineTo(q.x,q.y)}g.stroke();
  const ss=Object.keys(sysInfo).map(Number).sort((a,b)=>a-b);
  
  ss.forEach(s=>{const p=pos(s),i=sysInfo[s];const owners=i.slots.filter(x=>x.owner);const col=COLORS[(owners.find(x=>x.persona)||{}).persona]||"#8aa4d8";
    const z=1/Math.sqrt(Math.max(.6,cam.k));const rad=(7+Math.min(8,owners.length*1.4))*Math.max(z,.9/Math.sqrt(cam.k)*1.0)*(cam.k<.5?2.2:1);const gr=g.createRadialGradient(p.x,p.y,0,p.x,p.y,rad*2.6);gr.addColorStop(0,col+"cc");gr.addColorStop(.35,col+"44");gr.addColorStop(1,col+"00");
    g.fillStyle=gr;g.beginPath();g.arc(p.x,p.y,rad*2.6,0,7);g.fill();g.fillStyle="#fff";g.beginPath();g.arc(p.x,p.y,2.4*z,0,7);g.fill();
    if(cam.k>.12){g.fillStyle="#9fb2e8";g.font=`${11/cam.k}px system-ui`;g.textAlign="center";g.fillText(""+s,p.x,p.y+rad+13/cam.k)}
    if(mix>0){g.strokeStyle=`rgba(140,170,240,${.22*mix})`;g.lineWidth=.7/cam.k;for(let q=1;q<=15;q++){g.beginPath();g.arc(p.x,p.y,orbitR(q),0,7);g.stroke()}
      i.slots.forEach(sl=>{const o=planetOff(sl.position);g.globalAlpha=mix;g.fillStyle=sl.owner?(COLORS[sl.persona]||COLORS.other):"#334";
        g.beginPath();g.arc(p.x+o.x,p.y+o.y,(sl.owner?3.2:1.4)*Math.min(1,2.2/Math.pow(cam.k,.45)),0,7);g.fill();
        if(sl.battle){g.strokeStyle="#ff4d4d";g.lineWidth=1.2/cam.k;g.beginPath();g.arc(p.x+o.x,p.y+o.y,5.5+Math.sin(ts/120),0,7);g.stroke()}
        if(sl.moon&&sl.owner){const mo=moonOff(sl.position);g.fillStyle="#b9c0d0";g.beginPath();g.arc(p.x+o.x+mo.x,p.y+o.y+mo.y,Math.max(1.2,1.8*Math.min(1,2.2/Math.pow(cam.k,.45))),0,7);g.fill();g.strokeStyle="rgba(185,192,208,.35)";g.lineWidth=.5/cam.k;g.beginPath();g.arc(p.x+o.x,p.y+o.y,Math.hypot(mo.x,mo.y),0,7);g.stroke()}
        if(sl.debris){const db=debrisOff(sl.position),n=Math.min(14,4+Math.round(Math.log10(sl.debris+1)*2)),R=2+Math.log10(sl.debris+1);
          g.fillStyle="#c9a27a";for(let i=0;i<n;i++){const a=i*2.4+sl.position,rr=R*(.35+((i*37)%10)/10);g.globalAlpha=mix*(.45+.4*((i*13)%5)/5);g.beginPath();g.arc(p.x+o.x+db.x+Math.cos(a)*rr,p.y+o.y+db.y+Math.sin(a)*rr*.6,.7,0,7);g.fill()}g.globalAlpha=mix}
        if(sl.colony&&sl.owner){g.fillStyle="#05070f";g.beginPath();g.arc(p.x+o.x,p.y+o.y,1.2,0,7);g.fill()}
        if(cam.k>3.5){g.fillStyle="#7d8bb0";g.font=`${11/cam.k}px system-ui`;g.textAlign="center";g.fillText(""+sl.position,p.x+o.x*(1+9/orbitR(sl.position)),p.y+o.y*(1+9/orbitR(sl.position))+4/cam.k)}g.globalAlpha=1})}});
  D.fleets.forEach(f=>{if(hidden.has(f.kind))return;const ph=phase(f,now);if(ph.leg==="gone"||ph.leg==="wait"||(!showBack&&(ph.leg==="back"||ph.leg==="hold"||ph.leg==="done"&&f.back)))return;
    const [A,B]=ends(f,mix);if(A.x===B.x&&A.y===B.y)return;const L=Math.hypot(B.x-A.x,B.y-A.y);const bulge=Math.min(40,L*.18)*(f.agent&&f.agent.charCodeAt(f.agent.length-1)%2?1:-1);
    const fade=ph.fade==null?1:Math.max(0,ph.fade);const back=ph.leg==="back"||ph.leg==="done"&&f.back;
    g.save();g.globalAlpha=(back?.55:.9)*fade;g.strokeStyle=f.color;g.lineWidth=(back?1.2:1.8)/Math.max(1,cam.k*.7);g.setLineDash([6/cam.k*1.4,5/cam.k*1.4]);g.lineDashOffset=-(ts/45)/cam.k*(back?-1:1);
    g.beginPath();g.moveTo(A.x,A.y);const m=bez(A,B,.5,bulge);g.quadraticCurveTo(m.cx,m.cy,B.x,B.y);g.stroke();g.setLineDash([]);
    if(ph.leg!=="done"&&ph.leg!=="hold"){const q=bez(A,B,ph.p,bulge),q2=bez(A,B,Math.min(1,ph.p+.01),bulge),q1=bez(A,B,Math.max(0,ph.p-.01),bulge);let ang=Math.atan2(q2.y-q1.y,q2.x-q1.x);if(back)ang+=Math.PI;
      const sz=7/Math.max(1,cam.k*.6);g.globalAlpha=blink*fade;g.shadowColor=f.color;g.shadowBlur=14;g.fillStyle=f.color;g.translate(q.x,q.y);g.rotate(ang);
      g.beginPath();g.moveTo(sz,0);g.lineTo(-sz*.8,sz*.6);g.lineTo(-sz*.4,0);g.lineTo(-sz*.8,-sz*.6);g.closePath();g.fill()}
    else if(ph.leg==="hold"){g.globalAlpha=blink;g.fillStyle=f.color;g.beginPath();g.arc(B.x,B.y,4/Math.max(1,cam.k*.6),0,7);g.fill()}
    g.restore()});
  g.restore()}
function hoverAt(mx,my){const tip=$("tip");if(!D||drag){tip.style.display="none";return}
  const now=Date.now()/1000+offset;const mix=Math.max(0,Math.min(1,(cam.k-1.4)/1.6));let best=null,bd=14;
  D.fleets.forEach(f=>{if(hidden.has(f.kind))return;const ph=phase(f,now);if(ph.leg==="gone"||ph.leg==="wait"||ph.leg==="done"||(!showBack&&(ph.leg==="back"||ph.leg==="hold")))return;const [A,B]=ends(f,mix);const q=bez(A,B,ph.p,0),sp=w2s(q.x,q.y),d=Math.hypot(sp.x-mx,sp.y-my);if(d<bd){bd=d;best=[f,ph]}});
  if(best){const [f,ph]=best;tip.style.display="block";tip.style.left=Math.min(innerWidth-310,mx+14)+"px";tip.style.top=(my+14)+"px";
    const left=Math.max(0,Math.round((ph.leg==="back"?ph.p:1-ph.p)*f.secs));tip.innerHTML=`<b style="color:${f.color}">${f.label}</b> ${ph.leg==="back"?"(returning)":ph.leg==="hold"?"(on site)":""}<br>${f.agent} (${f.persona||"?"})<br>${f.from.join(":")} → ${f.to.join(":")}<br>${f.ships?f.ships+" ships, ":""}~${left} s to go`}
  else tip.style.display="none"}
function fmtLeft(f,now){const ph=phase(f,now);return ph}
function lists(){const now=Date.now()/1000+offset;const box=$("list");box.innerHTML="<b>Fleets in flight</b>";let n=0;
  D.fleets.forEach(f=>{if(hidden.has(f.kind))return;const ph=phase(f,now);if(ph.leg==="gone"||ph.leg==="done"||ph.leg==="wait"||(!showBack&&(ph.leg==="back"||ph.leg==="hold"))||n>=40)return;n++;
    const d=document.createElement("div");d.innerHTML=`<span style="color:${f.color}">●</span> ${f.label} ${ph.leg==="back"?"↩":""} <span class="dim">${f.agent} ${f.from[1]}→${f.to[1]}:${f.to[2]}</span>`;
    d.onclick=()=>{zoomTo(f.to[1]);showSys(f.to[1])};box.appendChild(d)});
  if(!n)box.innerHTML+='<div class="dim">no fleet in flight right now</div>'}
function legend(){const L=$("legend");L.innerHTML="";const kinds=D.missions;Object.keys(kinds).forEach(k=>{const lab=document.createElement("label");const c=document.createElement("input");c.type="checkbox";c.checked=!hidden.has(k);
  c.onchange=()=>{c.checked?hidden.delete(k):hidden.add(k);saveView();lists()};const i=document.createElement("i");i.style.background=kinds[k][1];lab.append(c,i,kinds[k][0]);L.appendChild(lab)});
  const bl=document.createElement("label");const bc=document.createElement("input");bc.type="checkbox";bc.checked=showBack;bc.onchange=()=>{showBack=bc.checked;saveView();lists()};bl.append(bc,document.createTextNode(" return trips"));bl.style.marginTop="4px";L.appendChild(bl);
  const p=document.createElement("div");p.className="dim";p.style.marginTop="6px";p.innerHTML=Object.keys(COLORS).map(k=>`<span style="color:${COLORS[k]}">●</span> ${k}`).join("<br>");L.appendChild(p)}
let first=true;
async function load(){try{const r=await fetch("/api/galaxy");const j=await r.json();if(j.error){$("status").textContent="error: "+j.error}else{D=j;offset=j.now-Date.now()/1000;
  sysInfo={};j.systems.forEach(s=>sysInfo[s.system]=s);(j.debris||[]).forEach(d=>{const si=sysInfo[d.at[1]];if(!si)return;let sl=si.slots.find(x=>x.position===d.at[2]);if(!sl){sl={position:d.at[2],planet:null,owner:null,agent:null,persona:null,colony:false,moon:false,battle:false};si.slots.push(sl)}sl.debris=d.amount});j.fleets.forEach(f=>[f.from[1],f.to[1]].forEach(s=>{if(!sysInfo[s])sysInfo[s]={system:s,slots:[]}}));
  if(first){first=false;legend();fit()}lists();$("status").textContent=`${j.systems.length} systems, ${j.fleets.length} fleets`}}catch(e){$("status").textContent="cannot reach the observatory"}
  setTimeout(load,5000)}
for(let i=0;i<160;i++)stars.push({x:Math.random(),y:Math.random(),r:Math.random()*1.6+.3,a:Math.random()*.7+.15});
load();requestAnimationFrame(draw);
</script></body></html>
"""
