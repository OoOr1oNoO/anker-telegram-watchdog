"""The 📺 TV remote widget: power, a round pad with the arrows and OK, back, home, play / pause,
mute, 🔎 HDrezka search, a volume slider and an off timer slider (0-4 h in 10 min steps). A tap sends "tv:power",
"tv:search" (the bot asks what to find in the chat), "tv:k<button>" (tv.KEYS), an arrow held over 0.4 s
"tv:hold:<arrow>" (again every 2 s, keeps it down on the TV) and "tv:rel" when let go, "tv:vol:<level>" or "tv:timer:<minutes>" to the bot, which acts on the
TV over ADB (a slider when it is let go). Its data, set when it is sent: {"at": the off timer's
Unix time or 0, "v": the volume, "vm": its maximum, "b": the backlight, "s": 1 on / 0 off (each none when
not known)}; the power button is green when on, red off, grey unknown; status updates skip it.
"""

import json

from .i18n import Lang

S = "<svg viewBox='0 0 24 24' width='22' height='22' fill='none' stroke='currentColor' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'>"
ICONS = {
    "power": "<path d='M12 3v9'/><path d='M6.3 6.8a8 8 0 1 0 11.4 0'/>",
    "up": "<path d='M6 15l6-6 6 6'/>", "down": "<path d='M6 9l6 6 6-6'/>",
    "left": "<path d='M15 6l-6 6 6 6'/>", "right": "<path d='M9 6l6 6-6 6'/>",
    "back": "<path d='M9 14L4 9l5-5'/><path d='M4 9h10.5a5.5 5.5 0 0 1 0 11H11'/>",
    "home": "<path d='M3 11l9-8 9 8'/><path d='M5 10v10h14V10'/>",
    "play": "<path d='M5 5v14l8-7z'/><path d='M16 5v14M20 5v14'/>",
    "mute": "<path d='M4 9h3l5-4v14l-5-4H4z'/><path d='M16 9l5 6M21 9l-5 6'/>",
    "search": "<circle cx='11' cy='11' r='6.5'/><path d='M16 16l5 5'/>",
}

TEMPLATE = """<style>
body{margin:10px 12px;user-select:none;-webkit-user-select:none}
.r{display:flex;justify-content:space-between;align-items:center}
.t{font-weight:700;font-size:17px}
b{box-sizing:border-box;display:flex;align-items:center;justify-content:center;border-radius:50%;cursor:pointer;
background:var(--tg-theme-secondary-bg-color);color:var(--tg-theme-text-color);transition:transform .08s,background .08s}
b.p{transform:scale(.9);background:#3a3a3c}
.pw{flex:none;width:40px;height:40px;color:#8e8e93;box-shadow:0 0 0 2px currentColor,0 0 12px -2px currentColor}
.pad{position:relative;width:170px;height:170px;margin:8px auto 0;border-radius:50%;background:var(--tg-theme-secondary-bg-color)}
.pad b{position:absolute;width:48px;height:48px;background:transparent;touch-action:none}
.pad .ok{left:50px;top:50px;width:70px;height:70px;background:var(--tg-theme-bg-color);font-weight:700;font-size:17px}
.row{display:flex;justify-content:space-between;margin:10px 6px 0}
.row b{width:46px;height:46px}
.cd{background:var(--tg-theme-secondary-bg-color);border-radius:14px;padding:6px 12px;margin-top:8px}
.k{font-size:13px;font-weight:600}.v{font-size:15px;font-weight:700}
.tr{position:relative;height:24px;margin:2px 4px 0;touch-action:none}
.tr .bg{position:absolute;left:0;right:0;top:9px;height:6px;border-radius:3px;background:#3a3a3c}
.tr .fl{position:absolute;top:9px;height:6px;border-radius:3px;background:#ff9f0a}#vf{background:#0a84ff}#bf{background:#ffd60a}
.tr .kn{position:absolute;top:2px;width:20px;height:20px;margin-left:-10px;border-radius:10px;background:#fff}
</style>
<div class=r><span class=t>__NAME__</span><b class=pw id=pw data-k=power>__power__</b></div>
<div class=pad>
<b data-k=up style="left:61px;top:3px">__up__</b><b data-k=left style="left:3px;top:61px">__left__</b>
<b data-k=right style="right:3px;top:61px">__right__</b><b data-k=down style="left:61px;bottom:3px">__down__</b>
<b class=ok data-k=ok>OK</b></div>
<div class=row><b data-k=back>__back__</b><b data-k=home>__home__</b><b data-k=play>__play__</b><b data-k=mute>__mute__</b><b data-k=search>__search__</b></div>
<div class=cd id=vc><div class=r><span class=k>__VOLUME__</span><span class=v id=vv></span></div>
<div class=tr id=vt><div class=bg></div><div class=fl id=vf></div><div class=kn id=vk></div></div></div>
<div class=cd id=bc><div class=r><span class=k>__LIGHT__</span><span class=v id=bv></span></div>
<div class=tr id=bt><div class=bg></div><div class=fl id=bf></div><div class=kn id=bk></div></div></div>
<div class=cd><div class=r><span class=k>__TIMER__</span><span class=v id=tv></span></div>
<div class=tr id=tt><div class=bg></div><div class=fl id=tf></div><div class=kn id=tk></div></div></div>
<script>
var W=TelegramWidget,L=__LABELS__,$=function(i){return document.getElementById(i)},D={},AT=0,M=0,V=0,B=0;
function fx(b){b.className+=" p";setTimeout(function(){b.className=b.className.replace(" p","")},150)}
document.querySelectorAll("b[data-k]").forEach(function(b){var k=b.getAttribute("data-k");
if("updownleftright".indexOf(k)>-1){var on=0,h=0,T,I;b.addEventListener("pointerdown",function(){on=1;h=0;fx(b);
T=setTimeout(function(){h=1;W.request("tv:hold:"+k);I=setInterval(function(){W.request("tv:hold:"+k)},2000)},400)});
function up(){if(!on)return;on=0;clearTimeout(T);clearInterval(I);W.request(h?"tv:rel":"tv:k"+k)}
["pointerup","pointercancel","pointerleave"].forEach(function(e){b.addEventListener(e,up)});return}
b.addEventListener("click",function(){fx(b);
W.request(k=="power"||k=="search"?"tv:"+k:"tv:k"+k).then(function(r){if(k=="power"&&!(typeof r=="string"&&r)&&D.s!=null){D.s=1-D.s;st()}})})});
function st(){$("pw").style.color=D.s==null?"#8e8e93":D.s?"#30d158":"#ff453a"}
function p2(n){return String(n).padStart(2,"0")}
function left(){return AT?Math.min(240,Math.max(0,Math.ceil((AT-Date.now()/1000)/600)*10)):0}
function bar(i,f){$(i+"k").style.left=f*100+"%";$(i+"f").style.width=f*100+"%"}
function draw(){var t=new Date(AT*1000);bar("t",M/240);
$("tv").textContent=M?Math.floor(M/60)+":"+p2(M%60)+" → "+p2(t.getHours())+":"+p2(t.getMinutes()):L.off;
if(D.vm){bar("v",V/D.vm);$("vv").textContent=V}if(D.b!=null){bar("b",B/100);$("bv").textContent=B||L.dark}}
function slide(i,set,done){var el=$(i+"t"),on=false;function x(e){var r=el.getBoundingClientRect();return Math.max(0,Math.min(1,(e.clientX-r.left)/r.width))}
el.addEventListener("pointerdown",function(e){on=true;set(x(e));draw()});el.addEventListener("pointermove",function(e){if(on){set(x(e));draw()}});
function end(){if(on){on=false;done()}}el.addEventListener("pointerup",end);el.addEventListener("pointercancel",end)}
slide("t",function(f){M=Math.round(f*24)*10;AT=M?Date.now()/1000+M*60:0},function(){W.request("tv:timer:"+M)});
slide("v",function(f){V=Math.round(f*D.vm)},function(){W.request("tv:vol:"+V)});
slide("b",function(f){B=Math.round(f*20)*5},function(){W.request("tv:bl:"+B)});
try{D=JSON.parse(W.chatData||"{}")}catch(e){}AT=D.at||0;M=left();V=D.v||0;B=D.b||0;if(!D.vm)$("vc").style.display="none";if(D.b==null)$("bc").style.display="none";st();draw();
setInterval(function(){if(AT){M=left();if(!M)AT=0;draw()}},30000);W.ready()
</script>"""


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def widget_html(L: Lang, name: str) -> str:
    html = (TEMPLATE.replace("__NAME__", "📺 " + _esc(name or L.t("tv")))
            .replace("__TIMER__", "⏲ " + _esc(L.t("pl_r_timer"))).replace("__VOLUME__", "🔊 " + _esc(L.t("tv_volume")))
            .replace("__LIGHT__", "☀️ " + _esc(L.t("tv_backlight")))
            .replace("__LABELS__", json.dumps({"off": L.t("off"), "dark": L.t("tv_dark")}, ensure_ascii=False)))
    for key, path in ICONS.items():
        html = html.replace(f"__{key}__", S + path + "</svg>")
    return html


def remote_data(off_at: float, volume: tuple[int, int] | None = None, backlight: int | None = None,
                on: bool | None = None) -> str:
    """The widget's data: when the off timer fires (0: no timer), the volume with its maximum, the
    backlight and whether the TV is on (each left out when not known)."""
    data = ({"at": int(off_at)} | ({"v": volume[0], "vm": volume[1]} if volume else {})
            | ({"b": backlight} if backlight is not None else {}) | ({"s": int(on)} if on is not None else {}))
    return json.dumps(data, separators=(",", ":"))
