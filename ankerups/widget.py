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
body{margin:10px 12px;overflow:hidden}
.r{display:flex;justify-content:space-between;align-items:center;gap:6px;white-space:nowrap}
.t{font-weight:600;font-size:15px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis}
.p{font-size:12px;font-weight:600;padding:2px 8px;border-radius:9px;color:#fff;background:#6e7681}
.g{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:8px}
.c{background:var(--tg-theme-secondary-bg-color);border-radius:10px;padding:7px 9px}
.k{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--tg-theme-hint-color)}.k svg{flex:none}
.v{font-size:19px;font-weight:600;line-height:1.3}
.s{font-size:12px;color:var(--tg-theme-hint-color)}
.b{height:5px;background:var(--tg-theme-section-separator-color);border-radius:3px;margin-top:3px}
.b i{display:block;height:5px;border-radius:3px;background:#2ea043}.sb{margin-top:6px;padding:4px 8px 4px 6px;border-radius:10px;border:1.5px solid var(--tg-theme-section-separator-color)}
.l{display:flex;align-items:center;gap:6px;margin-top:4px}
.l span{flex:0 0 62px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font-size:11px;color:var(--tg-theme-hint-color)}
.x{flex:1;min-width:0;display:flex;gap:1px;height:11px;padding:1px;border-radius:4px;border:1px solid var(--tg-theme-section-separator-color)}
.x i{flex:1;border-radius:2px;background:var(--tg-theme-section-separator-color)}
.x .on{background:#2ea043}.x .off{background:#da3633}.x .may{background:#e3b341}.x .part{background:#f0883e}
.x .now{outline:1.5px solid var(--tg-theme-text-color)}
.tk{margin:1px 2px 0 70px;height:7px;color:var(--tg-theme-hint-color)}.tk svg{display:block}
.h{display:flex;margin-left:65px;font-size:10px;color:var(--tg-theme-hint-color)}
.h i{flex:1;font-style:normal;display:flex;justify-content:space-between}
.bx{display:flex;align-items:center;gap:3px;font-style:normal;padding:0 5px;border-radius:7px;border:1.5px solid;white-space:nowrap}
.k2{display:flex;gap:6px;margin-top:8px}
.k2 b{display:flex;align-items:center;gap:6px;height:32px;padding:0 11px;border-radius:8px;font-size:13px;
background:var(--tg-theme-secondary-bg-color);color:var(--tg-theme-text-color)}.k2 b.a{background:#2ea043;color:#fff}
</style>
<div class=r><span class=t>__NAME__</span><span class="p al" id=al style="display:none"></span><span class=p id=p></span></div>
<div class=g><div class=c><div class=k><span id=bi></span>__BAT__</div><div class=v id=b></div><div class=b><i id=bb></i></div><div class=s id=bs></div></div>
<div class=c><div class=k>__LOADI__<span style="flex:1">__LOAD__</span><span class=k id=wx style="color:var(--tg-theme-text-color);font-size:12px"></span></div><div class=v id=o></div><div class=b><i id=ob></i></div><div class="k s" id=os></div></div></div>
<div class=c style="margin-top:6px"><div class=r><span class=k>__CALI__<span id=gi class=k></span><span id=gr></span></span><span class=k>__CLKI__<b id=ck style="font-size:15px;color:var(--tg-theme-text-color)"></b></span></div>
<div class=s id=nx></div>
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
function strip(name,s,cur){var h="<div class='l'><span>"+name+"</span><div class='x'>";
for(var i=0;i<48;i++)h+="<i class='"+cls(s[i])+(i==cur?" now":"")+"'></i>";return h+"</div></div>"}
var SK="<circle cx='12' cy='12' r='9'/><path d='M8.5 12h.1M15.5 12h.1' stroke-width='3'/></svg>",
I14="<svg viewBox='0 0 24 24' width='14' height='14' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>",
CL="<path d='M7 16h10a4 4 0 0 0 .5-8A6 6 0 0 0 6 9a3.5 3.5 0 0 0 1 7z'/>";
function WX(c){return I14+(c<1?"<circle cx='12' cy='12' r='4'/><path d='M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5'/>"
:c<3?"<circle cx='8' cy='7' r='3'/><path d='M8 1.5v1M2.5 7h1M4 3l.7.7'/>"+CL:c<4?CL:c<50?"<path d='M4 9h16M4 13h16M6 17h12'/>"
:c<70||c>79&&c<83?CL+"<path d='M8 21l1-2M12 21l1-2M16 21l1-2'/>":c<90?CL+"<path d='M8 20v.01M12 22v.01M16 20v.01'/>":CL+"<path d='M12 16l-2 3h3l-2 3'/>")+"</svg>"}
function alarm(d,t){var a=$("al");if(!d.al){a.style.display="none";return}a.style.display="";var k=d.al[0];
a.textContent=(k=="n"?"⚠️ ":"🚨 ")+dur(t-d.al[1]);a.style.background={r:"#da3633",y:"#e3b341",o:"#f0883e"}[k]||"#3a3a3c";
a.style.color=k=="y"?"#111":"#fff"}
function draw(){var d=D,t=now();if(!d.t)return;alarm(d,t);$("wx").innerHTML=d.w?"<i class='bx' style='border-color:"+d.w[3]+"'>"+WX(d.w[1])+(d.w[0]>0?"+":"")+d.w[0]+"°</i>"+(d.w[2]!=null?"<i class='bx' style='border-color:"+
["#2ea043","#7ee2a0","#e3b341","#f0883e","#da3633","#8b1a1a"][Math.min(5,Math.floor(d.w[2]/20))]+"'>AQI "+d.w[2]+"</i>":""):"";
var g=d.g,p=$("p");p.textContent=(g==null?"?":g?L.on:L.off)+(d.c?" · "+dur(t-d.c):"");
p.style.background=g==null?"#6e7681":g?"#2ea043":"#da3633";
var b=d.b||[],avg=b.length?Math.round(b.reduce(function(a,x){return a+x},0)/b.length):null;
$("b").textContent=avg==null?"—":avg+"%";$("bb").style.width=(avg||0)+"%";
var bs=b.length>1?b.map(function(x,i){return(i?L.e+(b.length>2?" "+i:""):L.m)+" "+x+"%"}):[];
if(g===0&&d.r)bs.push("~"+dur(d.r*3600));$("bs").textContent=bs.join("·").replace(/ /g,"\u00a0").replace(/·/g," · ");
$("o").textContent=d.o==null?"—":d.o+" W";var lp=Math.min(100,(d.o||0)/(d.mx||2000)*100),ob=$("ob");
ob.style.width=lp+"%";ob.style.background=lp>90?"#da3633":lp>70?"#f0883e":"#2ea043";$("os").innerHTML=L.in+": "+(d.i==null?"—":d.i+" W")+(d.pw!=null?" · "+I14+SK+" "+d.pw+" W"+" · "+d.pv+" V":"");
var cur=Math.floor((d.m+(t-d.t)/60)/30),rk=[d.f,d.p,d.q,cur].join();
if(rk!=rkey){rkey=rk;var rows=strip(L.fact,d.f||"",cur);
if(d.p)rows+=strip(L.today,d.p,cur);if(d.q)rows+=strip(L.tmr,d.q,-1);$("rows").innerHTML=rows}
var gr=$("gr");gr.textContent=d.e?L.emg+" · "+d.gr:d.gr?L.grp+" "+d.gr:L.day;
$("gi").innerHTML=d.e?I14+"<path d='M12 3l10 18H2z'/><path d='M12 10v5M12 18v.01'/></svg>":"";
gr.style.color=d.e?"#da3633":"";
var nx="";if(d.x&&d.x[0]>t){nx=(d.x[1]=="1"?L.onat:L.offat)+" "+hm(d.x[0])+" · "+dur(d.x[0]-t);
if(g===0&&d.x[1]=="1"&&d.r&&avg!=null){var left=d.r*3600-(t-d.t),need=d.x[0]-t;
nx+=need<left?" · ✓ ≈"+Math.round(avg*(1-need/left))+"%":" · ✗"}}
$("nx").textContent=nx;$("u").textContent="⟳ "+dur(t-d.u);$("ck").textContent=hm(t);
var lv=avg==null?-1:avg>=75?3:avg>=50?2:avg>=25?1:0;if(lv!=blv){blv=lv;var col=["#da3633","#f0883e","#e3b341","#2ea043"][lv]||"currentColor",
fh=Math.round(13*(avg||0)/100);$("bb").style.background=col;$("bi").innerHTML="<svg viewBox='0 0 24 24' width='14' height='14' fill='none' stroke='currentColor' stroke-width='2.2'>"+
"<rect x='7' y='4' width='10' height='18' rx='3'/><path d='M10 2h4'/><rect x='9.5' y='"+(19.5-fh)+"' width='5' height='"+fh+"' rx='1' fill='"+col+"' stroke='none'/></svg>"}}
var shown="",rkey="",blv=-2;
function ctl(){var d=D,key=[d.lm,d.dc,d.ps].join();if(key==shown)return;shown=key;var c=$("ctl");c.innerHTML="";
if(d.lm!=null)add(c,"light",IC[0]+(d.lm?L.y:L.n),d.lm);if(d.dc!=null)add(c,"dc",IC[1]+"12V "+(d.dc?L.y:L.n),d.dc);if(d.ps!=null)add(c,"plug",SV+SK+(d.ps?L.y:L.n),d.ps);add(c,"qs",IC[2],0)}
var SV="<svg viewBox='0 0 24 24' width='18' height='18' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'>",
IC=[SV+"<path d='M8 2.5h8v3.5l-2 3.2V21a1 1 0 0 1-1 1h-2a1 1 0 0 1-1-1V9.2L8 6z'/><path d='M8 6h8'/></svg>",
SV+"<circle cx='12' cy='12' r='9'/><circle cx='12' cy='12' r='4.5'/><circle cx='12' cy='12' r='1.4' fill='currentColor'/></svg>",
SV+"<circle cx='12' cy='12' r='6.3'/><circle cx='12' cy='12' r='2.4'/><path stroke-width='3.2' stroke-linecap='butt' d='M12 2v4M12 18v4M2 12h4M18 12h4M5 5l2.5 2.5M16.5 16.5L19 19M5 19l2.5-2.5M16.5 7.5L19 5'/></svg>"];
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
BOLT = "<path d='M13 2L5 14h6l-1 8 8-12h-6z'/>"
CLOCK = "<circle cx='12' cy='12' r='9'/><path d='M12 7v5l3 2'/>"
CALENDAR = "<rect x='3' y='5' width='18' height='16' rx='3'/><path d='M3 10h18M8 3v4M16 3v4'/>"


# rated AC output of the station models (for the load bar); others count as 2000 W
MAX_OUTPUT = {"A1760": 1800, "A1761": 1800, "A1763": 2000, "A1722": 300, "A1723": 300, "A1753": 1200,
              "A1754": 1200, "A1783": 2400, "A1790": 6000}


def widget_html(L: Lang, name: str) -> str:
    html = TEMPLATE.replace("__NAME__", _escape(name)).replace("__BAT__", L.t("w_battery"))
    html = html.replace("__LOADI__", ICON.format(BOLT))
    html = html.replace("__CALI__", ICON.format(CALENDAR)).replace("__CLKI__", ICON.format(CLOCK))
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
                max_out: int | None = None, extra: dict | None = None) -> str:
    data = {"t": int(now), "u": int(telemetry_at or now), "g": None if grid is None else int(grid),
            "c": int(since) if since else None, "b": list((levels or {}).values()), "i": ac_in, "o": out,
            "r": round(remaining_h, 2) if remaining_h else None, "f": fact, "p": plan_today,
            "q": plan_tomorrow, "x": [int(next_change[0]), next_change[1]] if next_change else None,
            "gr": group, "m": minute_of_day, "e": 1 if emergency else None, "lm": light, "dc": dc, "mx": max_out} | (extra or {})
    text = json.dumps({k: v for k, v in data.items() if v not in (None, "")}, separators=(",", ":"))
    assert len(text.encode()) <= MAX_DATA, len(text)
    return text
