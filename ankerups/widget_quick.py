"""The ⚙️ Quick settings widget: light, DC 12V, fast charging, display, the AC input limit (a
slider) and the auto light hours (a two-handle range), for admins.

The data is the station state (chat data, JSON); a tap or a drag sends a request to the bot
("light", "dc", "fast", "disp", "limit:600", "auto:18:30-00:30", "auto:off"), and the new state
comes back as data. Controls the station does not report are hidden.
"""

import json

from .i18n import Lang

AUTO_FROM = 12 * 60  # the auto light range starts at 12:00 and covers 16 hours in half hours
AUTO_SLOTS = 32

TEMPLATE = """<style>
body{margin:12px 14px}
.t{font-weight:700;font-size:17px}.s{font-size:12px;color:var(--tg-theme-hint-color)}
.g{display:flex;justify-content:space-around;gap:10px;margin-top:12px}
.tl{display:flex;flex-direction:column;align-items:center;gap:5px;font-size:11px;color:var(--tg-theme-hint-color)}
.tl b{box-sizing:border-box;width:58px;height:58px;border-radius:18px;padding:0;display:flex;align-items:center;justify-content:center;background:#3a3a3c;color:#fff}
.tl b.a{background:#fff;color:#1c1c1e}
.cd{background:var(--tg-theme-secondary-bg-color);border-radius:16px;padding:10px 12px;margin-top:10px}
.r{display:flex;justify-content:space-between;align-items:center}
.k{font-size:13px;font-weight:600}.k svg{vertical-align:-4px;margin-right:6px}.v{font-size:15px;font-weight:700}
.tr{position:relative;height:30px;margin:8px 4px 0;touch-action:none}
.tr .bg{position:absolute;left:0;right:0;top:12px;height:6px;border-radius:3px;background:#3a3a3c}
.tr .fl{position:absolute;top:12px;height:6px;border-radius:3px;background:#30d158}
.tr .kn{position:absolute;top:3px;width:24px;height:24px;margin-left:-12px;border-radius:12px;background:#fff}
.tk{display:flex;justify-content:space-between;margin:2px 4px 0;font-size:10px;color:var(--tg-theme-hint-color)}.tk i{font-style:normal}
.sw{display:block;width:46px;height:28px;border-radius:14px;background:#3a3a3c;position:relative}
.sw i{position:absolute;top:2px;left:2px;width:24px;height:24px;border-radius:12px;background:#fff}
.sw.a{background:#30d158}.sw.a i{left:20px}
.sg{display:flex;gap:3px;margin-top:8px;padding:3px;border-radius:12px;background:#3a3a3c}
.sg b{flex:1;text-align:center;border-radius:9px;padding:6px 0;font-size:12px;font-weight:600;background:#3a3a3c;color:#fff}.sg b.a{background:#fff;color:#1c1c1e}
</style>
<div class=r><span class=t>⚙️ __TITLE__</span><span class=s>__NAME__</span></div>
<div class=g id=g></div>
<div class=cd id=lc><div class=r><span class=k>__PLUG__</span><span class=v id=lv></span></div>
<div class=tr id=lt><div class=bg></div><div class=fl id=lf></div><div class=kn id=lk></div></div>
<div class=tk id=lks></div></div>
<div class=cd id=ac><div class=r><span class=k>__BULB__</span><b class=sw id=sw><i></i></b></div>
<div class=r style="margin-top:2px"><span class=s>__WHEN__</span><span class=v id=av></span></div>
<div class=tr id=at><div class=bg></div><div class=fl id=af></div><div class=kn id=a1></div><div class=kn id=a2></div></div>
<div class=tk><i>12</i><i>16</i><i>20</i><i>00</i><i>04</i></div></div>
<div class=cd><span class=k>📊 __LOOK__</span><div class=sg id=sg></div></div>
<script>
var L=__LABELS__,W=TelegramWidget,$=function(i){return document.getElementById(i)},D={},S={},busy=0;
var SV="<svg viewBox='0 0 24 24' width='26' height='26' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'>",
IC={lm:SV+"<path d='M8 2.5h8v3.5l-2 3.2V21a1 1 0 0 1-1 1h-2a1 1 0 0 1-1-1V9.2L8 6z'/><path d='M8 6h8'/></svg>",
dc:SV+"<circle cx='12' cy='12' r='9'/><circle cx='12' cy='12' r='4.5'/><circle cx='12' cy='12' r='1.4' fill='currentColor'/></svg>",
fast:SV+"<path d='M13 2L5 14h6l-1 8 8-12h-6z'/></svg>",
disp:SV+"<circle cx='12' cy='12' r='4'/><path d='M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5'/></svg>"},
RQ={lm:"light",dc:"dc",fast:"fast",disp:"disp"};
function ask(d){busy++;W.request(d).then(fin,fin)}function fin(){setTimeout(function(){busy--;if(!busy)load()},1500)}
function tiles(){var g=$("g");g.innerHTML="";["lm","dc","fast","disp"].forEach(function(k){if(S[k]==null)return;
var d=document.createElement("div");d.className="tl";var b=document.createElement("b");b.innerHTML=IC[k];if(S[k])b.className="a";
b.addEventListener("click",function(){S[k]=k=="disp"?(S[k]+1)%4:S[k]?0:1;tiles();ask(RQ[k])});
d.appendChild(b);var l=document.createElement("span");l.textContent=L[k]+(k=="disp"?" "+(S.disp?S.disp:L.n):"");d.appendChild(l);g.appendChild(d)})}
function hh(i){var m=(720+i*30)%1440;return String(Math.floor(m/60)).padStart(2,"0")+":"+String(m%60).padStart(2,"0")}
function draw(){var lc=S.lims||[];$("lc").style.display=S.lim==null?"none":"";
if(S.lim!=null){var n=lc.length-1,k=Math.max(0,lc.indexOf(S.lim)),p=n?k/n*100:0;$("lk").style.left=p+"%";$("lf").style.width=p+"%";
$("lv").textContent=S.lim+" W";$("lks").innerHTML=lc.map(function(x){return "<i>"+x+"</i>"}).join("")}
$("ac").style.display=S.a==null?"none":"";if(S.a==null)return;
var a=S.a/32*100,b=S.b/32*100;$("a1").style.left=a+"%";$("a2").style.left=b+"%";$("af").style.left=a+"%";$("af").style.width=(b-a)+"%";
$("af").style.background=S.on?"#ff9f0a":"#636366";$("av").textContent=S.on?hh(S.a)+" – "+hh(S.b):L.n;$("sw").className=S.on?"sw a":"sw"}
function drag(el,set,done){var on=false;function x(e){var r=el.getBoundingClientRect();return Math.max(0,Math.min(1,(e.clientX-r.left)/r.width))}
el.addEventListener("pointerdown",function(e){on=true;busy++;set(x(e),true);draw()});
el.addEventListener("pointermove",function(e){if(on){set(x(e),false);draw()}});
function end(){if(on){on=false;busy--;done()}}el.addEventListener("pointerup",end);el.addEventListener("pointercancel",end)}
drag($("lt"),function(f){var lc=S.lims||[];S.lim=lc[Math.round(f*(lc.length-1))]},function(){ask("limit:"+S.lim)});
var which="b";drag($("at"),function(f,first){var i=Math.round(f*32);if(first)which=Math.abs(i-S.a)<Math.abs(i-S.b)?"a":"b";
if(which=="a")S.a=Math.min(i,S.b-1);else S.b=Math.max(i,S.a+1)},function(){S.on=1;draw();ask("auto:"+hh(S.a)+"-"+hh(S.b))});
$("sw").addEventListener("click",function(){S.on=S.on?0:1;draw();ask(S.on?"auto:"+hh(S.a)+"-"+hh(S.b):"auto:off")});
function looks(){var g=$("sg");g.innerHTML="";L.st.forEach(function(x){var b=document.createElement("b");b.textContent=x[1];
if(S.st==x[0])b.className="a";b.addEventListener("click",function(){S.st=x[0];looks();ask("look:"+x[0])});g.appendChild(b)})}
function load(){try{D=JSON.parse(W.chatData||"{}")}catch(e){D={}}if(busy)return;S=JSON.parse(JSON.stringify(D));tiles();draw();looks()}
load();W.onEvent("chatDataChanged",load);W.ready()
</script>"""

ICON = ("<svg viewBox='0 0 24 24' width='20' height='20' fill='none' stroke='{}' stroke-width='2' "
        "stroke-linecap='round' stroke-linejoin='round'>{}</svg>")
PLUG = ICON.format("#30d158", "<rect x='6' y='7' width='12' height='9' rx='3'/><path d='M9 7V3M15 7V3M12 16v2q0 3 3 3h3'/>")
BULB = ICON.format("#ff9f0a", "<path d='M9 18h6M10 21h4'/><path d='M12 3a6 6 0 0 0-3.6 10.8c.8.6 1.1 1.4 1.1 2.2v.5h5"
                              "V16c0-.8.3-1.6 1.1-2.2A6 6 0 0 0 12 3z'/>")


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def widget_html(L: Lang, name: str) -> str:
    from .users import STATUS_STYLES  # noqa: PLC0415

    labels = {"lm": L.t("qs_light"), "dc": "12V", "fast": L.t("qs_fast"), "disp": L.t("qs_disp"), "n": L.t("m_off"),
              "st": [[s, L.t(f"st_{s}")] for s in STATUS_STYLES]}
    html = (TEMPLATE.replace("__NAME__", _esc(name)).replace("__TITLE__", _esc(L.t("qs_title")))
            .replace("__PLUG__", PLUG + _esc(L.t("qs_limit"))).replace("__BULB__", BULB + _esc(L.t("qs_auto")))
            .replace("__WHEN__", _esc(L.t("qs_when"))).replace("__LOOK__", _esc(L.t("qs_look"))))
    return html.replace("__LABELS__", json.dumps(labels, ensure_ascii=False, separators=(",", ":")))


def auto_slots(window: str) -> tuple[int, int, int]:
    """(start, end, on) of the auto light window in half hours from 12:00, for the range."""
    from .menus import parse_window  # noqa: PLC0415 - menus imports the bot's settings

    span = parse_window(window) if window else None
    if not span:
        return 13, 25, 0  # 18:30-00:30, off
    start, end = ((m - AUTO_FROM) % 1440 // 30 for m in span)
    start, end = min(start, AUTO_SLOTS - 1), min(end if end > start else AUTO_SLOTS, AUTO_SLOTS)
    return start, end, 1


def with_look(data: str, look: str) -> str:
    """The quick settings data with the chat's status look (the data is shared by all chats)."""
    return data[:-1] + ("," if len(data) > 2 else "") + f'"st":"{look}"}}'


def quick_data(controls: dict[str, int | None], limits: list[int] | None, autolight: str | None) -> str:
    """controls: lm (light mode), dc, fast, disp, lim (None: not reported); autolight: the
    window setting, None when the station has no DC 12V output."""
    data = {k: v for k, v in controls.items() if v is not None}
    if controls.get("lim") is not None and limits:
        data["lims"] = limits
    if autolight is not None:
        data["a"], data["b"], data["on"] = auto_slots(autolight)
    return json.dumps(data, separators=(",", ":"))
