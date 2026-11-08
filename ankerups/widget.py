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
.b i{display:block;height:5px;border-radius:3px;background:#2ea043}
.l{display:flex;align-items:center;gap:6px;margin-top:4px}
.l span{width:54px;font-size:11px;color:var(--tg-theme-hint-color)}
.x{display:flex;gap:1px;flex:1;height:11px}
.x i{flex:1;border-radius:1px;background:var(--tg-theme-section-separator-color)}
.x .on{background:#2ea043}.x .off{background:#da3633}.x .may{background:#d29922}
.x .now{outline:1.5px solid var(--tg-theme-text-color)}
.h{display:flex;justify-content:space-between;margin-left:60px;font-size:10px;color:var(--tg-theme-hint-color)}
.h i{font-style:normal}
</style>
<div class=r><span class=t>__NAME__</span><span class=p id=p></span></div>
<div class=g><div class=c><div class=k>🔋 __BAT__</div><div class=v id=b></div><div class=b><i id=bb></i></div><div class=s id=bs></div></div>
<div class=c><div class=k>🏠 __LOAD__</div><div class=v id=o></div><div class=s id=os></div></div></div>
<div class=c style="margin-top:6px"><div class=r><span class=k id=gr></span><span class=s id=nx></span></div>
<div id=rows></div><div class=h><i>0</i><i>6</i><i>12</i><i>18</i><i>24</i></div></div>
<div class=s id=u style="text-align:right;margin-top:3px"></div>
<script>
var L=__LABELS__,W=TelegramWidget,D={},$=function(i){return document.getElementById(i)};
function dur(s){var m=Math.max(0,Math.floor(s/60)),h=Math.floor(m/60),d=Math.floor(h/24),u=L.u;
if(m<60)return m+u[2];if(h<24)return h+u[1]+" "+String(m%60).padStart(2,"0")+u[2];return d+u[0]+" "+h%24+u[1]}
function now(){return Date.now()/1000}
function hm(t){var d=new Date(t*1000);return String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0")}
function strip(name,s,cur){var h="<div class=l><span>"+name+"</span><div class=x>";
for(var i=0;i<48;i++){var c=s[i],k=c=="1"?"on":c=="0"?"off":c=="?"?"may":"";h+="<i class='"+k+(i==cur?" now":"")+"'></i>"}
return h+"</div></div>"}
function draw(){var d=D,t=now();if(!d.t)return;
var g=d.g,p=$("p");p.textContent=(g==null?"?":g?L.on:L.off)+(d.c?" · "+dur(t-d.c):"");
p.style.background=g==null?"#6e7681":g?"#2ea043":"#da3633";
var b=d.b||[],avg=b.length?Math.round(b.reduce(function(a,x){return a+x},0)/b.length):null;
$("b").textContent=avg==null?"—":avg+"%";$("bb").style.width=(avg||0)+"%";
var bs=b.length>1?b.map(function(x,i){return(i?L.e+(b.length>2?" "+i:""):L.m)+" "+x+"%"}).join(" · "):"";
if(g===0&&d.r)bs=(bs?bs+" · ":"")+"~"+dur(d.r*3600);$("bs").textContent=bs;
$("o").textContent=d.o==null?"—":d.o+" W";$("os").textContent=L.in+": "+(d.i==null?"—":d.i+" W");
var cur=Math.floor((d.m+(t-d.t)/60)/30),rows=strip(L.fact,d.f||"",cur);
if(d.e)rows+="<div class=l><span>"+L.today+"</span><b style='font-size:12px;color:#da3633'>⚠️ "+L.emg+"</b></div>";
else if(d.p)rows+=strip(L.today,d.p,cur);if(d.q)rows+=strip(L.tmr,d.q,-1);$("rows").innerHTML=rows;
$("gr").textContent=d.gr?"📅 "+L.grp+" "+d.gr:"📊 "+L.day;
var nx="";if(d.x&&d.x[0]>t){nx=(d.x[1]=="1"?L.onat:L.offat)+" "+hm(d.x[0])+" · "+dur(d.x[0]-t);
if(g===0&&d.x[1]=="1"&&d.r&&avg!=null){var left=d.r*3600-(t-d.t),need=d.x[0]-t;
nx+=need<left?" · ✓ ≈"+Math.round(avg*(1-need/left))+"%":" · ✗"}}
$("nx").textContent=nx;$("u").textContent="⟳ "+dur(t-d.u)}
function load(){try{D=JSON.parse(W.chatData||"{}")}catch(e){D={}}draw()}
load();W.onEvent("chatDataChanged",load);setInterval(draw,10000);W.ready()
</script>"""


def labels(L: Lang) -> dict:
    return {"on": L.t("w_on"), "off": L.t("w_off"), "m": L.t("bat_main"), "e": L.t("bat_expansion"),
            "in": L.t("w_input"), "fact": L.t("w_fact"), "today": L.t("w_today"), "tmr": L.t("w_tomorrow"),
            "grp": L.t("w_group"), "day": L.t("w_day"), "onat": L.t("w_on_at"), "offat": L.t("w_off_at"), "emg": L.t("w_emergency"),
            "u": list(UNITS[L.code][:3])}


def widget_html(L: Lang, name: str) -> str:
    html = TEMPLATE.replace("__NAME__", _escape(name)).replace("__BAT__", L.t("w_battery"))
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
                emergency: bool = False) -> str:
    data = {"t": int(now), "u": int(telemetry_at or now), "g": None if grid is None else int(grid),
            "c": int(since) if since else None, "b": list((levels or {}).values()), "i": ac_in, "o": out,
            "r": round(remaining_h, 2) if remaining_h else None, "f": fact, "p": plan_today,
            "q": plan_tomorrow, "x": [int(next_change[0]), next_change[1]] if next_change else None,
            "gr": group, "m": minute_of_day, "e": 1 if emergency else None}
    text = json.dumps({k: v for k, v in data.items() if v not in (None, "")}, separators=(",", ":"))
    assert len(text.encode()) <= MAX_DATA, len(text)
    return text
