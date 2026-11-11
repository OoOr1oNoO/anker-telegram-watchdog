"""Draft: the "clock" status widget, a 24-hour dial with the schedule and the actual grid.

Outer ring: the planned schedule, inner ring: what really happened, the hand: now (00:00 at
the top). Same chat data as widget.py; the dial is redrawn only when the data or the half
hour change.
"""

import json

from .i18n import Lang
from .translations import UNITS

TEMPLATE = """<style>
body{margin:0;background:linear-gradient(160deg,#252a33,#111317);color:#f2f2f7}
.w{padding:14px}.r{display:flex;justify-content:space-between;align-items:center}
.nm{font-weight:700;font-size:16px}
.ch{font-size:11px;font-weight:700;padding:3px 9px;border-radius:10px;color:#111}
.m{display:flex;align-items:center;gap:6px;margin-top:6px}
#dl{flex:0 0 176px;height:176px}.c{flex:1;display:flex;flex-direction:column;gap:9px}
.k{font-size:10px;font-weight:600;color:#8e8e93}
.io{display:flex;align-items:center;gap:8px;font-size:20px;font-weight:700}.io svg{flex:0 0 22px}
.big{font-size:24px;font-weight:800}.sm{font-size:12px;color:#8e8e93}
.bt{display:flex;align-items:center;gap:8px;margin-top:8px;font-size:12px;color:#c7c7cc}
.bt span{width:74px}.bt b{width:38px;text-align:right;font-weight:600;color:#f2f2f7}
.bar{flex:1;height:8px;border-radius:4px;background:#2c2f36}.bar i{display:block;height:8px;border-radius:4px}
.k2{display:flex;gap:10px;justify-content:flex-end;margin-top:10px}
.k2 button{width:46px;height:46px;border-radius:23px;padding:0;display:flex;align-items:center;justify-content:center;
background:#2c2f36;color:#f2f2f7}.k2 button.a{background:#f2f2f7;color:#111}
</style>
<div class=w><div class=r><span class=nm>__NAME__</span><span class=ch id=ch></span></div>
<div class=m><div id=dl></div><div class=c>
<div><div class=io>__PLUG__<span id=i></span></div><div class=io>__HOME__<span id=o></span></div></div>
<div class=io id=pw style=display:none>__SOCK__<span id=pv></span></div>
<div><div class=k id=nk></div><div class=big id=nt></div><div class=sm id=fc></div></div></div></div>
<div id=bt></div><div class=r><span class=sm id=u></span><div class=k2 id=ctl></div></div></div>
<script>
var L=__LABELS__,W=TelegramWidget,D={},$=function(i){return document.getElementById(i)},key="",shown="";
function dur(s){var m=Math.max(0,Math.floor(s/60)),h=Math.floor(m/60),d=Math.floor(h/24),u=L.u;
if(m<60)return m+u[2];if(h<24)return h+u[1]+" "+m%60+u[2];return d+u[0]+" "+h%24+u[1]}
function now(){return Date.now()/1000}
function hm(t){var d=new Date(t*1000);return String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0")}
function avg(){var b=D.b||[];return b.length?Math.round(b.reduce(function(a,x){return a+x},0)/b.length):null}
function pt(r,a){a=(a-90)*Math.PI/180;return(100+r*Math.cos(a)).toFixed(1)+" "+(100+r*Math.sin(a)).toFixed(1)}
function arc(r,i,j,col){var a0=i*7.5+.6,a1=j*7.5-.6;
return "<path d='M"+pt(r,a0)+"A"+r+" "+r+" 0 "+(a1-a0>180?1:0)+" 1 "+pt(r,a1)+"' stroke='"+col+"' pathLength='"+Math.round((a1-a0)*10)+"' stroke-dasharray='63 12'/>"}
function ring(r,col){var h="",i=0;while(i<48){var j=i+1,c=col(i);while(j<48&&col(j)==c&&j-i<24)j++;h+=arc(r,i,j,c);i=j}return h}
var SG={a:[3,0,12,3.5],b:[14.5,3,3.5,12],c:[14.5,18,3.5,12],d:[3,29.5,12,3.5],e:[0,18,3.5,12],f:[0,3,3.5,12],g:[3,14.75,12,3.5]},
DG=["abcdef","bc","abdeg","abcdg","bcfg","acdfg","acdefg","abcf","abcdefg","abcdfg"];
function lcd(txt,x,y,c){var lit="",dim="";txt.split("").forEach(function(ch,k){var on=DG[+ch]||"";
for(var s in SG){var q=SG[s],r="M"+(x+k*22+q[0])+" "+(y+q[1])+"h"+q[2]+"v"+q[3]+"h"+(-q[2])+"z";if(on.indexOf(s)>=0)lit+=r;else dim+=r}});
return "<path d='"+dim+"' fill='#262a31'/><path d='"+lit+"' fill='"+c+"'/>"}
function T(x,y,size,col,txt,ex){return "<text x='"+x+"' y='"+y+"' text-anchor='middle' font-size='"+size+"' fill='"+col+"'"+(ex||"")+">"+txt+"</text>"}
function dial(){var d=D,t=now(),mn=d.m+(t-d.t)/60,cur=Math.floor(mn/30),f=d.f||"",p=d.p||"",s="",a=mn/4,g=d.g,
c=CC=g===0?"#ff9f0a":"#34c759",av=avg(),tk="",n=av==null?"":String(Math.min(av,100)),i;
var P={"1":"#2d6a40","0":"#6b2b29","?":"#6e5b22"},F={"1":"#34c759","0":"#ff453a","p":"#ff9f0a"};
s=ring(80,function(i){return P[p[i]]||"#2a2e36"})+ring(68,function(i){return i<=cur&&F[f[i]]||"#2a2e36"});
for(i=0;i<24;i++)tk+="M"+pt(87,i*15)+"L"+pt(i%3?90:94,i*15);
var lb=["0","6","12","18"].map(function(x,j){var q=pt(102,j*90).split(" ");return T(q[0],+q[1]+3,"9","#8e8e93",x)}).join("");
var hp=pt(91,a).split(" "),dt=new Date(),hh=String(dt.getHours()).padStart(2,"0"),mm=String(dt.getMinutes()).padStart(2,"0"),
ctr=pm?lcd(n,100-(n.length*22-4)/2-5,74,c)+"<text x='"+(100+(n.length*22-4)/2-1)+"' y='106' font-size='11' font-weight='700' fill='"+c+"'>%</text>"
:lcd(hh,53,74,c)+"<g id='cl' fill='"+c+"'><path d='M98 82h4v4h-4zM98 96h4v4h-4z'/></g>"+lcd(mm,109,74,c);
$("dl").innerHTML="<svg viewBox='-12 -12 224 224' width='176' height='176'>"+
"<g fill='none' stroke-width='8' stroke-linecap='round'>"+s+"</g>"+
"<path d='"+tk+"' stroke='#636366' stroke-width='1.4' stroke-linecap='round'/>"+lb+
"<circle cx='100' cy='100' r='56' fill='"+(g===0?"#231c12":"#121f17")+"'/>"+
"<path d='M"+pt(60,a)+"L"+pt(91,a)+"' stroke='#f2f2f7' stroke-width='2.4' stroke-linecap='round'/>"+
"<circle cx='"+hp[0]+"' cy='"+hp[1]+"' r='3.5' fill='#f2f2f7'/>"+
ctr+
T(100,124,"8.5",c,g==null?"":(g?L.on:L.off).toUpperCase()," font-weight='700'")+
T(100,136,"9","#8e8e93",""," id='sn'")+"</svg>"}
function bats(){var b=D.b||[],h="";b.forEach(function(x,i){var col=x<25?"#ff453a":x<50?"#ff9f0a":"#34c759";
h+="<div class='bt'><span>"+(i?L.e+(b.length>2?" "+i:""):L.m)+"</span><div class='bar'><i style='width:"+x+"%;background:linear-gradient(90deg,"+col+",#e9ffe9)'></i></div><b>"+x+"%</b></div>"});
$("bt").innerHTML=h}
function tick(){var d=D,t=now(),g=d.g,a=avg();if(!d.t)return;
var k=[d.f,d.p,Math.floor((d.m+(t-d.t)/60)/30),g,Math.floor((t-d.t)/600),a,(d.b||[]).join(),pm,pm||new Date().getMinutes()].join();if(k!=key){key=k;dial();bats()}
var ch=$("ch");ch.textContent=d.e?"⚠️ "+L.emg:d.gr?L.grp+" "+d.gr:"";ch.style.background=d.e?"#ff453a":"#f2f2f7";ch.style.display=ch.textContent?"":"none";
var sn=$("sn");if(sn)sn.textContent=(pm||a==null?"":a+"%"+(d.c?" · ":""))+(d.c?dur(t-d.c):"");
$("i").textContent=(d.i||0)+" W";$("o").textContent=(d.o||0)+" W";$("pw").style.display=d.pw==null?"none":"";$("pv").textContent=d.pw+" W"+" · "+d.pv+" V";
var nx=d.x&&d.x[0]>t?d.x:null;$("nk").textContent=nx?(nx[1]=="1"?L.onat:L.offat).toUpperCase()+" "+hm(nx[0]):g===0&&d.r?L.rem:"";
$("nt").textContent=nx?dur(nx[0]-t):g===0&&d.r?"~"+dur(d.r*3600):"";
var fc="";if(g===0&&nx&&nx[1]=="1"&&d.r&&a!=null){var left=d.r*3600-(t-d.t),need=nx[0]-t;fc=need<left?"≈ "+Math.round(a*(1-need/left))+"% "+L.atOn:"✗"}
$("fc").textContent=fc;$("u").textContent="⟳ "+hm(d.u||d.t)}
var SV="<svg viewBox='0 0 24 24' width='22' height='22' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'>",
IL=SV+"<path d='M8 2.5h8v3.5l-2 3.2V21a1 1 0 0 1-1 1h-2a1 1 0 0 1-1-1V9.2L8 6z'/><path d='M8 6h8'/><circle cx='12' cy='13' r='1.3' fill='currentColor'/></svg>",
ID=SV+"<circle cx='12' cy='12' r='9'/><circle cx='12' cy='12' r='4.5'/><circle cx='12' cy='12' r='1.4' fill='currentColor'/><path d='M12 3v2.5'/></svg>";
function ctl(){var d=D,k=[d.lm,d.dc].join();if(k==shown)return;shown=k;var c=$("ctl");c.innerHTML="";
if(d.lm!=null)add(c,"light",IL,d.lm);if(d.dc!=null)add(c,"dc",ID,d.dc);add(c,"qs",SV+"<circle cx='12' cy='12' r='6.3'/><circle cx='12' cy='12' r='2.4'/><path stroke-width='3.2' stroke-linecap='butt' d='M12 2.4v3.4M12 18.2v3.4M2.4 12h3.4M18.2 12h3.4M5.2 5.2l2.4 2.4M16.4 16.4l2.4 2.4M5.2 18.8l2.4-2.4M16.4 7.6l2.4-2.4'/></svg>",0)}
function add(c,a,t,on){var b=document.createElement("button");b.innerHTML=t;if(on)b.className="a";
b.addEventListener("click",function(){b.className=on?"":"a";W.request(a).then(function(){shown="";setTimeout(ctl,1500)},
function(){b.textContent="⚠️";shown="";setTimeout(ctl,2500)})});c.appendChild(b)}
function load(){try{D=JSON.parse(W.chatData||"{}")}catch(e){D={}}tick();ctl()}
var pm=false,CC="",on=true;
setInterval(function(){var e=$("cl");if(e&&!document.hidden){on=!on;e.setAttribute("fill",on?CC:"#262a31")}},1000);try{pm=localStorage.getItem("pm")=="1"}catch(e){}
$("dl").addEventListener("click",function(){pm=!pm;try{localStorage.setItem("pm",pm?"1":"0")}catch(e){}tick()});
load();W.onEvent("chatDataChanged",load);function vf(){if(!document.hidden)tick()}setInterval(vf,10000);document.addEventListener("visibilitychange",vf);W.ready()
</script>"""

PLUG = ("<svg viewBox='0 0 24 24' fill='none' stroke='#8e8e93' stroke-width='1.8' stroke-linecap='round' "
        "stroke-linejoin='round'><rect x='6' y='7' width='12' height='9' rx='3'/><path d='M9 7V3M15 7V3M12 16v2q0 3 3 3h3'/></svg>")
SOCKET = ("<svg viewBox='0 0 24 24' fill='none' stroke='#8e8e93' stroke-width='1.8' stroke-linecap='round' "
          "stroke-linejoin='round'><rect x='3' y='4' width='18' height='16' rx='3'/><circle cx='12' cy='12' r='5'/><path d='M10 11v2M14 11v2'/></svg>")
HOME = ("<svg viewBox='0 0 24 24' fill='none' stroke='#8e8e93' stroke-width='1.8' stroke-linecap='round' "
        "stroke-linejoin='round'><path d='M3 11l9-7 9 7'/><path d='M5.5 9.5V20h13V9.5'/><path d='M10 20v-5h4v5'/></svg>")


def labels(L: Lang) -> dict:
    return {"on": L.t("w_on"), "off": L.t("w_off"), "grp": L.t("w_group"), "emg": L.t("w_emergency"),
            "onat": L.t("w_on_at"), "offat": L.t("w_off_at"), "rem": L.t("wi_remaining"), "atOn": L.t("wc_at_on"),
            "m": L.t("bat_main"), "e": L.t("bat_expansion"), "u": list(UNITS[L.code][:3])}


def widget_html(L: Lang, name: str) -> str:
    html = TEMPLATE.replace("__NAME__", name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    html = html.replace("__PLUG__", PLUG).replace("__HOME__", HOME)
    html = html.replace("__SOCK__", SOCKET)
    return html.replace("__LABELS__", json.dumps(labels(L), ensure_ascii=False, separators=(",", ":")))
