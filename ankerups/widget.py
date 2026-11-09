"""The status dashboard as a Telegram widget (an HTML bubble the bot updates in place).

The HTML is fixed per message (labels in the chat's language); the numbers travel as the
widget's chat data (JSON, at most 1024 bytes), which the bot edits every few seconds. The JS
redraws on each change and ticks the durations itself between edits.
"""

import json
import zlib

from .i18n import Lang
from .translations import UNITS

MAX_DATA = 1024
MAX_HTML_COMPRESSED = 4096

TEMPLATE = """<style>
body{margin:10px 12px}
.r{display:flex;justify-content:space-between;align-items:center;gap:6px}
.t{font-weight:600;font-size:15px}
.p{font-size:12px;font-weight:600;padding:2px 8px;border-radius:9px;color:#fff;background:#6e7681}
.g{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:8px}
.c{background:var(--tg-theme-secondary-bg-color);border-radius:10px;padding:7px 9px}
.k{font-size:11px;color:var(--tg-theme-hint-color)}
.v{font-size:19px;font-weight:600;line-height:1.3}
.s{font-size:12px;color:var(--tg-theme-hint-color)}
.b{height:5px;background:var(--tg-theme-section-separator-color);border-radius:3px;margin-top:3px}
.b i{display:block;height:5px;border-radius:3px;background:#2ea043}.k svg{vertical-align:-2px}.sb{margin-top:6px;padding:4px 8px 4px 6px;border-radius:10px;border:1.5px solid var(--tg-theme-section-separator-color)}
.l{display:flex;align-items:center;gap:6px;margin-top:4px}
.l span{flex:0 0 62px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font-size:11px;color:var(--tg-theme-hint-color)}
.x{flex:1;min-width:0;width:100%;display:block;box-sizing:border-box;padding:1px;border-radius:4px;border:1px solid var(--tg-theme-section-separator-color)}.x line{stroke:var(--tg-theme-section-separator-color)}
.x .on{stroke:#2ea043}.x .off{stroke:#da3633}.x .may{stroke:#e3b341}.x .part{stroke:#f0883e}
.tk{margin:1px 2px 0 70px;height:7px;color:var(--tg-theme-hint-color)}.tk svg{display:block}
.h{display:flex;margin-left:65px;font-size:10px;color:var(--tg-theme-hint-color)}
.h i{flex:1;font-style:normal;display:flex;justify-content:space-between}
.k2{display:flex;gap:6px;margin-top:8px}
.k2 b{display:flex;align-items:center;gap:6px;height:32px;padding:0 11px;border-radius:8px;font-size:13px;
background:var(--tg-theme-secondary-bg-color);color:var(--tg-theme-text-color)}.k2 b.a{background:#2ea043;color:#fff}
.k2 svg{display:block}
</style>
<div class=r><span class=t>__NAME__</span><span class=p id=p></span></div>
<div class=g><div class=c><div class=k>__BATI__ __BAT__</div><div class=v id=b></div><div class=b><i id=bb></i></div><div class=s id=bs></div></div>
<div class=c><div class=k>__LOADI__ __LOAD__</div><div class=v id=o></div><div class=b><i id=ob></i></div><div class=s id=os></div></div></div>
<div class=c style="margin-top:6px"><div class=r><span class=k>__CALI__ <span id=gr></span></span><span class=s id=nx></span></div>
<div class=sb><div id=rows></div><div class=tk id=tk></div><div class=h><i>0</i><i>6</i><i>12</i><i>18<b style="font-weight:400;margin-right:-3px">24</b></i></div></div></div>
<div class=k2 id=ctl></div>
<div class=s id=u style="margin-top:3px"></div>
<script>
var L=__LABELS__,W=TelegramWidget,D={},$=function(i){return document.getElementById(i)};
function dur(s){var m=Math.max(0,Math.floor(s/60)),h=Math.floor(m/60),d=Math.floor(h/24),u=L.u;
if(m<60)return m+u[2];if(h<24)return h+u[1]+" "+String(m%60).padStart(2,"0")+u[2];return d+u[0]+" "+h%24+u[1]}
function now(){return Date.now()/1000}
function hm(t){var d=new Date(t*1000);return String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0")}
function cls(c){return c=="1"?"on":c=="0"?"off":c=="?"?"may":c=="p"?"part":"e"}
function strip(name,s,cur){var h="<div class='l'><span>"+name+"</span><svg class='x' viewBox='0 0 480 11' preserveAspectRatio='none' width='100%' height='15'>",i=0;
while(i<48){var k=cls(s[i]),j=i+1;while(j<48&&cls(s[j])==k)j++;
h+="<line x1='"+i*10+"' y1='5.5' x2='"+j*10+"' y2='5.5' stroke-width='11' stroke-dasharray='8.6 1.4' class='"+k+"'/>";i=j}
if(cur>=0&&cur<48)h+="<rect x='"+(cur*10+.4)+"' y='.75' width='7.8' height='9.5' fill='none' stroke='currentColor' stroke-width='1.5' vector-effect='non-scaling-stroke'/>";
return h+"</svg></div>"}
function draw(){var d=D,t=now();if(!d.t)return;
var g=d.g,p=$("p");p.textContent=(g==null?"?":g?L.on:L.off)+(d.c?" · "+dur(t-d.c):"");
p.style.background=g==null?"#6e7681":g?"#2ea043":"#da3633";
var b=d.b||[],avg=b.length?Math.round(b.reduce(function(a,x){return a+x},0)/b.length):null;
$("b").textContent=avg==null?"—":avg+"%";$("bb").style.width=(avg||0)+"%";
var bs=b.length>1?b.map(function(x,i){return(i?L.e+(b.length>2?" "+i:""):L.m)+" "+x+"%"}).join(" · "):"";
if(g===0&&d.r)bs=(bs?bs+" · ":"")+"~"+dur(d.r*3600);$("bs").textContent=bs;
$("o").textContent=d.o==null?"—":d.o+" W";var lp=Math.min(100,(d.o||0)/(d.mx||2000)*100),ob=$("ob");
ob.style.width=lp+"%";ob.style.background=lp>90?"#da3633":lp>70?"#f0883e":"#2ea043";$("os").textContent=L.in+": "+(d.i==null?"—":d.i+" W");
var cur=Math.floor((d.m+(t-d.t)/60)/30),rk=[d.f,d.p,d.q,cur].join();
if(rk!=rkey){rkey=rk;var rows=strip(L.fact,d.f||"",cur);
if(d.p)rows+=strip(L.today,d.p,cur);if(d.q)rows+=strip(L.tmr,d.q,-1);$("rows").innerHTML=rows}
var gr=$("gr");gr.textContent=d.e?"⚠️ "+L.emg+" · "+d.gr:d.gr?L.grp+" "+d.gr:L.day;
gr.style.color=d.e?"#da3633":"";
var nx="";if(d.x&&d.x[0]>t){nx=(d.x[1]=="1"?L.onat:L.offat)+" "+hm(d.x[0])+" · "+dur(d.x[0]-t);
if(g===0&&d.x[1]=="1"&&d.r&&avg!=null){var left=d.r*3600-(t-d.t),need=d.x[0]-t;
nx+=need<left?" · ✓ ≈"+Math.round(avg*(1-need/left))+"%":" · ✗"}}
$("nx").textContent=nx;$("u").textContent="⟳ "+dur(t-d.u)}
var shown="",rkey="";
function ctl(){var d=D,key=[d.lm,d.dc].join();if(key==shown)return;shown=key;var c=$("ctl");c.innerHTML="";
if(d.lm!=null)add(c,"light",IC[0]+(d.lm?L.y:L.n),d.lm);if(d.dc!=null)add(c,"dc",IC[1]+"12V "+(d.dc?L.y:L.n),d.dc);add(c,"qs",IC[2],0)}
var SV="<svg viewBox='0 0 24 24' width='18' height='18' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>",
IC=[SV+"<path d='M8 2.5h8v3.5l-2 3.2V21a1 1 0 0 1-1 1h-2a1 1 0 0 1-1-1V9.2L8 6z'/><path d='M8 6h8'/></svg>",
SV+"<circle cx='12' cy='12' r='9'/><circle cx='12' cy='12' r='4.5'/><circle cx='12' cy='12' r='1.4' fill='currentColor'/></svg>",
SV+"<circle cx='12' cy='12' r='6.3'/><circle cx='12' cy='12' r='2.4'/><path stroke-width='3.2' stroke-linecap='butt' d='M12 2.4v3.4M12 18.2v3.4M2.4 12h3.4M18.2 12h3.4M5.2 5.2l2.4 2.4M16.4 16.4l2.4 2.4M5.2 18.8l2.4-2.4M16.4 7.6l2.4-2.4'/></svg>"];
function add(c,a,t,on){var b=document.createElement("b");b.innerHTML=t;if(on)b.className="a";
b.addEventListener("click",function(){if(a!="qs")b.className=on?"":"a";W.request(a).then(function(){shown="";setTimeout(ctl,1500)},
function(){shown="";setTimeout(ctl,2500)})});c.appendChild(b)}
function ticks(){var t="";for(var i=0;i<=48;i+=2)t+="M"+Math.min(i*10,479.5)+" 0v"+(i%12==0?7:i%6==0?5:3);
$("tk").innerHTML="<svg viewBox='0 0 480 7' preserveAspectRatio='none' width='100%' height='7'><path d='"+t+
"' stroke='currentColor' stroke-width='1' vector-effect='non-scaling-stroke'/></svg>"}
ticks();
function load(){try{D=JSON.parse(W.chatData||"{}")}catch(e){D={}}draw();ctl()}
load();W.onEvent("chatDataChanged",load);function vf(){if(!document.hidden)draw()}setInterval(vf,10000);document.addEventListener("visibilitychange",vf);W.ready()
</script>"""


def labels(L: Lang) -> dict:
    return {"on": L.t("w_on"), "off": L.t("w_off"), "m": L.t("bat_main"), "e": L.t("bat_expansion"),
            "in": L.t("w_input"), "fact": L.t("w_fact"), "today": L.t("w_today"), "tmr": L.t("w_tomorrow"),
            "grp": L.t("w_group"), "day": L.t("w_day"), "onat": L.t("w_on_at"), "offat": L.t("w_off_at"), "emg": L.t("w_emergency"),
            "y": L.t("m_on"), "n": L.t("m_off"),
            "u": list(UNITS[L.code][:3])}


ICON = ("<svg viewBox='0 0 24 24' width='14' height='14' fill='none' stroke='currentColor' stroke-width='2.2' "
        "stroke-linecap='round' stroke-linejoin='round'>{}</svg>")
BATTERY = "<rect x='3' y='7' width='16' height='10' rx='3'/><path d='M22 10.5v3'/><path d='M7 10.5v3M11 10.5v3'/>"
BOLT = "<path d='M13 2L5 14h6l-1 8 8-12h-6z'/>"
CALENDAR = "<rect x='3' y='5' width='18' height='16' rx='3'/><path d='M3 10h18M8 3v4M16 3v4'/>"

# rated AC output of the station models (for the load bar); others count as 2000 W
MAX_OUTPUT = {"A1760": 1800, "A1761": 1800, "A1763": 2000, "A1722": 300, "A1723": 300, "A1753": 1200,
              "A1754": 1200, "A1783": 2400, "A1790": 6000}


def widget_html(L: Lang, name: str) -> str:
    html = TEMPLATE.replace("__NAME__", _escape(name)).replace("__BAT__", L.t("w_battery"))
    html = html.replace("__BATI__", ICON.format(BATTERY)).replace("__LOADI__", ICON.format(BOLT))
    html = html.replace("__CALI__", ICON.format(CALENDAR))
    html = html.replace("__LOAD__", L.t("w_load"))
    return html.replace("__LABELS__", json.dumps(labels(L), ensure_ascii=False, separators=(",", ":")))


def compressed_size(html: str) -> int:
    """What Telegram counts against its 4 KB limit (zlib level 9)."""
    return len(zlib.compress(html.encode(), 9))


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def widget_data(now: float, telemetry_at: float | None, grid: bool | None, since: float | None,
                levels: dict[str, int] | None, ac_in: int | None, out: int | None,
                remaining_h: float | None, fact: str, plan_today: str, plan_tomorrow: str,
                next_change: tuple[float, str] | None, group: str, minute_of_day: int,
                emergency: bool = False, light: int | None = None, dc: int | None = None,
                max_out: int | None = None) -> str:
    data = {"t": int(now), "u": int(telemetry_at or now), "g": None if grid is None else int(grid),
            "c": int(since) if since else None, "b": list((levels or {}).values()), "i": ac_in, "o": out,
            "r": round(remaining_h, 2) if remaining_h else None, "f": fact, "p": plan_today,
            "q": plan_tomorrow, "x": [int(next_change[0]), next_change[1]] if next_change else None,
            "gr": group, "m": minute_of_day, "e": 1 if emergency else None, "lm": light, "dc": dc, "mx": max_out}
    text = json.dumps({k: v for k, v in data.items() if v not in (None, "")}, separators=(",", ":"))
    assert len(text.encode()) <= MAX_DATA, len(text)
    return text
