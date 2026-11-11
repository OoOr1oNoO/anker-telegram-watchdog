"""The 📺 TV remote widget: power, a round pad with the arrows and OK, back, home, play / pause,
mute, a volume slider and an off timer slider (0-4 h in 10 min steps). A tap sends "tv:power",
"tv:k<button>" (tv.KEYS), "tv:vol:<level>" or "tv:timer:<minutes>" to the bot, which acts on the
TV over ADB (a slider when it is let go). Its data, set when it is sent: {"at": the off timer's
Unix time or 0, "v": the volume, "vm": its maximum (none when not known)}; status updates skip it.
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
}

TEMPLATE = """<style>
body{margin:12px 14px;user-select:none;-webkit-user-select:none}
.r{display:flex;justify-content:space-between;align-items:center}
.t{font-weight:700;font-size:17px}
b{box-sizing:border-box;display:flex;align-items:center;justify-content:center;border-radius:50%;cursor:pointer;
background:var(--tg-theme-secondary-bg-color);color:var(--tg-theme-text-color);transition:transform .08s,background .08s}
b.p{transform:scale(.9);background:#3a3a3c}
.pw{width:44px;height:44px;color:#ff453a}
.pad{position:relative;width:210px;height:210px;margin:14px auto 0;border-radius:50%;background:var(--tg-theme-secondary-bg-color)}
.pad b{position:absolute;width:58px;height:58px;background:transparent}
.pad .ok{left:62px;top:62px;width:86px;height:86px;background:var(--tg-theme-bg-color);font-weight:700;font-size:17px}
.row{display:flex;justify-content:space-between;margin:16px 6px 0}
.row b{width:56px;height:56px}
.cd{background:var(--tg-theme-secondary-bg-color);border-radius:16px;padding:10px 12px;margin-top:16px}
.k{font-size:13px;font-weight:600}.v{font-size:15px;font-weight:700}
.tr{position:relative;height:30px;margin:8px 4px 0;touch-action:none}
.tr .bg{position:absolute;left:0;right:0;top:12px;height:6px;border-radius:3px;background:#3a3a3c}
.tr .fl{position:absolute;top:12px;height:6px;border-radius:3px;background:#ff9f0a}#vf{background:#0a84ff}
.tr .kn{position:absolute;top:3px;width:24px;height:24px;margin-left:-12px;border-radius:12px;background:#fff}
</style>
<div class=r><span class=t>__NAME__</span><b class=pw data-k=power>__power__</b></div>
<div class=pad>
<b data-k=up style="left:76px;top:4px">__up__</b><b data-k=left style="left:4px;top:76px">__left__</b>
<b data-k=right style="right:4px;top:76px">__right__</b><b data-k=down style="left:76px;bottom:4px">__down__</b>
<b class=ok data-k=ok>OK</b></div>
<div class=row><b data-k=back>__back__</b><b data-k=home>__home__</b><b data-k=play>__play__</b><b data-k=mute>__mute__</b></div>
<div class=cd id=vc><div class=r><span class=k>__VOLUME__</span><span class=v id=vv></span></div>
<div class=tr id=vt><div class=bg></div><div class=fl id=vf></div><div class=kn id=vk></div></div></div>
<div class=cd><div class=r><span class=k>__TIMER__</span><span class=v id=tv></span></div>
<div class=tr id=tt><div class=bg></div><div class=fl id=tf></div><div class=kn id=tk></div></div></div>
<script>
var W=TelegramWidget,L=__LABELS__,$=function(i){return document.getElementById(i)},D={},AT=0,M=0,V=0;
document.querySelectorAll("b[data-k]").forEach(function(b){b.addEventListener("click",function(){
var k=b.getAttribute("data-k");b.className+=" p";setTimeout(function(){b.className=b.className.replace(" p","")},150);
W.request(k=="power"?"tv:power":"tv:k"+k)})});
function p2(n){return String(n).padStart(2,"0")}
function left(){return AT?Math.min(240,Math.max(0,Math.ceil((AT-Date.now()/1000)/600)*10)):0}
function bar(i,f){$(i+"k").style.left=f*100+"%";$(i+"f").style.width=f*100+"%"}
function draw(){var t=new Date(AT*1000);bar("t",M/240);
$("tv").textContent=M?Math.floor(M/60)+":"+p2(M%60)+" → "+p2(t.getHours())+":"+p2(t.getMinutes()):L.off;
if(D.vm){bar("v",V/D.vm);$("vv").textContent=V}}
function slide(i,set,done){var el=$(i+"t"),on=false;function x(e){var r=el.getBoundingClientRect();return Math.max(0,Math.min(1,(e.clientX-r.left)/r.width))}
el.addEventListener("pointerdown",function(e){on=true;set(x(e));draw()});el.addEventListener("pointermove",function(e){if(on){set(x(e));draw()}});
function end(){if(on){on=false;done()}}el.addEventListener("pointerup",end);el.addEventListener("pointercancel",end)}
slide("t",function(f){M=Math.round(f*24)*10;AT=M?Date.now()/1000+M*60:0},function(){W.request("tv:timer:"+M)});
slide("v",function(f){V=Math.round(f*D.vm)},function(){W.request("tv:vol:"+V)});
try{D=JSON.parse(W.chatData||"{}")}catch(e){}AT=D.at||0;M=left();V=D.v||0;if(!D.vm)$("vc").style.display="none";draw();
setInterval(function(){if(AT){M=left();if(!M)AT=0;draw()}},30000);W.ready()
</script>"""


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def widget_html(L: Lang, name: str) -> str:
    html = (TEMPLATE.replace("__NAME__", "📺 " + _esc(name or L.t("tv")))
            .replace("__TIMER__", "⏲ " + _esc(L.t("pl_r_timer"))).replace("__VOLUME__", "🔊 " + _esc(L.t("tv_volume")))
            .replace("__LABELS__", json.dumps({"off": L.t("off")}, ensure_ascii=False)))
    for key, path in ICONS.items():
        html = html.replace(f"__{key}__", S + path + "</svg>")
    return html


def remote_data(off_at: float, volume: tuple[int, int] | None = None) -> str:
    """The widget's data: when the off timer fires (0: no timer) and the volume with its maximum."""
    data = {"at": int(off_at)} | ({"v": volume[0], "vm": volume[1]} if volume else {})
    return json.dumps(data, separators=(",", ":"))
