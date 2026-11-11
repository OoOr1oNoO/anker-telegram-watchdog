"""Draft: the status widget in the style of the Anker / EcoFlow apps (a test, not used by the bot yet).

Same chat data as widget.py; the strips and the flow picture are redrawn only when the data or
the half hour change, the durations tick every 10 s.
"""

import json

from .i18n import Lang
from .translations import UNITS

TEMPLATE = """<style>
body{margin:12px 14px}
.r{display:flex;justify-content:space-between;align-items:center}
.nm{font-weight:700;font-size:17px}.hn{font-size:12px;color:var(--tg-theme-hint-color)}
.big{text-align:center;font-size:56px;font-weight:700;line-height:1.05;margin-top:8px}
.st{text-align:center;font-weight:700;font-size:13px;margin-top:4px}
.sb{text-align:center;font-size:13px;font-weight:600;margin-top:2px}
.bs{text-align:center;font-size:12px;color:var(--tg-theme-hint-color);margin-top:3px}
#fl{margin-top:12px;height:54px}
.io{display:flex;justify-content:space-between;margin-top:6px}
.io div{display:flex;align-items:baseline;gap:7px;padding:5px 12px;border-radius:14px;border:2px solid #636366}
.io small,.k{font-size:11px;font-weight:600;color:var(--tg-theme-hint-color)}
#pw svg{width:15px;height:15px;vertical-align:-4px}
.io b{font-size:20px}
.fc{border-top:1px solid var(--tg-theme-section-separator-color);margin-top:10px;padding-top:8px;text-align:center}
.fc b{display:block;font-size:26px;margin-top:2px}.fc i{font-style:normal;font-size:12px;color:var(--tg-theme-hint-color)}
.cd{background:var(--tg-theme-secondary-bg-color);border-radius:16px;padding:10px 12px;margin-top:10px}
.cd .t{font-size:26px;font-weight:700;margin-top:2px}
#x{position:relative;margin-top:14px;height:14px;border-radius:9px;border:2px solid #f2f2f7}
#mk{position:absolute;top:-5px;width:4px;height:24px;margin-left:-2px;border-radius:2px;background:#fff}
.h{display:flex;justify-content:space-between;font-size:10px;color:var(--tg-theme-hint-color);margin-top:3px}.h i{font-style:normal}
.al{font-size:12px;font-weight:600;padding:2px 8px;border-radius:9px;white-space:nowrap}
.k2{display:flex;gap:8px;margin-top:10px;justify-content:flex-end}
.k2 button{width:48px;height:48px;border-radius:24px;padding:0;display:flex;align-items:center;justify-content:center;
background:#3a3a3c;color:#fff}.k2 button.a{background:#fff;color:#1c1c1e}
</style>
<div class=r><span class=nm>__NAME__</span><span class=al id=al style="display:none"></span><span class=hn id=u></span></div>
<div class=big id=b></div><div class=bs id=bs></div><div class=st id=s></div><div class=sb id=sb></div>
<div id=fl></div>
<div class=io><div id=ib><small>IN</small><b id=i></b></div><div id=ob><small>OUT</small><b id=o></b></div></div>
<div id=pw class=bs></div>
<div class=fc id=fc><span class=k id=fk></span><b id=fv></b><i id=fi></i></div>
<div class=cd><div class=r><span class=k id=gk></span><span class=k id=gr></span></div><div class=t id=nt></div>
<div id=x></div><div class=h><i>00</i><i>06</i><i>12</i><i>18</i><i>24</i></div></div>
<div class=k2 id=ctl></div>
<script>
var L=__LABELS__,W=TelegramWidget,D={},$=function(i){return document.getElementById(i)},key="",shown="";
function dur(s){var m=Math.max(0,Math.floor(s/60)),h=Math.floor(m/60),d=Math.floor(h/24),u=L.u;
if(m<60)return m+u[2];if(h<24)return h+u[1]+" "+m%60+u[2];return d+u[0]+" "+h%24+u[1]}
function now(){return Date.now()/1000}
function hm(t){var d=new Date(t*1000);return String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0")}
function avg(){var b=D.b||[];return b.length?Math.round(b.reduce(function(a,x){return a+x},0)/b.length):null}
function flow(){var g=D.g,a=avg()||0,ch=g===1&&(D.i||0)>(D.o||0)+20,c=g===0?"#ff9f0a":"#30d158",gr="#636366",
inC=g?c:gr,outC=(D.o||0)>0?c:gr,w=Math.round(76*a/100),
ln=function(d,col,on){return "<path d='"+d+"' stroke='"+col+"' stroke-width='2.4' stroke-linecap='round'"+(on?" stroke-dasharray='3 7'":"")+"/>"};
$("ib").style.borderColor=inC;$("ob").style.borderColor=outC;
$("fl").innerHTML="<svg viewBox='0 0 300 54' width='100%' height='54' fill='none' stroke-linecap='round' stroke-linejoin='round'>"+
"<g stroke='"+inC+"' stroke-width='2.2'><rect x='16' y='17' width='26' height='20' rx='6'/><path d='M23 17v-7M35 17v-7M29 37v4q0 5 5 5h6'/></g>"+
ln("M54 27H92",inC,g===1)+
"<rect x='100' y='8' width='88' height='38' rx='11' stroke='"+c+"' stroke-width='2.2'/><path d='M193 21v12' stroke='"+c+"' stroke-width='3.4'/>"+
"<rect x='106' y='14' width='"+w+"' height='26' rx='7' fill='"+c+"'"+"/>"+
(ch?"<path d='M147 15l-8 13h7l-2 11 10-14h-7l3-10z' fill='#fff' stroke='none'/>":"")+
ln("M201 27H237",outC,(D.o||0)>0)+"<path d='M232 21l6 6-6 6' stroke='"+outC+"' stroke-width='2.4'/>"+
"<g stroke='"+outC+"' stroke-width='2.2'><path d='M252 27l18-14 18 14'/><path d='M257 23v18a2 2 0 0 0 2 2h22a2 2 0 0 0 2-2V23'/><path d='M266 43v-8a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v8'/></g></svg>"}
var CO={on:"#30d158",off:"#ff453a",part:"#ff9f0a",pon:"#1d4f2c",poff:"#5c2523",may:"#5e4c1c",e:"#3a3a3c"};
function strip(){var d=D,t=now(),np=Math.min(100,(d.m+(t-d.t)/60)/14.4),f=d.f||"",p=d.p||"",iv=[],st=[];
function pc(i){var q=p[i];return CO[q=="1"?"pon":q=="0"?"poff":q=="?"?"may":"e"]}
function fc(i){var c=f[i];return c=="1"?CO.on:c=="0"?CO.off:c=="p"?CO.part:pc(i)}
function add(a,b,c){if(b<=a)return;var l=iv[iv.length-1];if(l&&l[2]==c)l[1]=b;else iv.push([a,b,c])}
for(var i=0;i<48;i++){var a=i*100/48,b=(i+1)*100/48;add(a,Math.min(b,np),fc(i));add(Math.max(a,np),b,pc(i))}
iv.forEach(function(v,k){st.push(v[2]+" "+(k?(v[0]+.4).toFixed(1)+"%":"0"),v[2]+" "+(k<iv.length-1?(v[1]-.4).toFixed(1)+"%":"100%"))});
var x=$("x");x.style.background="linear-gradient(90deg,"+st.join(",")+")";
x.innerHTML="<i id='mk' style='left:"+np.toFixed(1)+"%'></i>"}
function wi(c){return c<1?"☀️":c<3?"⛅":c<4?"☁️":c<50?"🌫":c<70?"🌧":c<80?"❄️":c<83?"🌦":c<90?"🌨":"⛈"}
function alarm(d,t){var a=$("al");if(!d.al){a.style.display="none";return}a.style.display="";var k=d.al[0];
a.textContent=(k=="n"?L.aln:L.alr)+" · "+dur(t-d.al[1]);a.style.background={r:"#da3633",y:"#e3b341",o:"#f0883e"}[k]||"#3a3a3c";
a.style.color=k=="y"?"#111":"#fff"}
function tick(){var d=D,t=now(),g=d.g,a=avg();if(!d.t)return;
$("b").textContent=a==null?"—":a+"%";var bb=d.b||[];
$("bs").textContent=bb.length>1?bb.map(function(x,i){return(i?L.e+(bb.length>2?" "+i:""):L.m)+" "+x+"%"}).join(" · "):"";
var s=$("s");s.textContent=(g==null?"?":g?L.on:L.off).toUpperCase();s.style.color=g===0?"#ff9f0a":"#30d158";
$("sb").textContent=d.c?dur(t-d.c):"";$("sb").style.color=s.style.color;
$("i").textContent=(d.i||0)+" W";$("o").textContent=(d.o||0)+" W";$("pw").innerHTML=d.pw==null?"":SV+"<rect x='3' y='4' width='18' height='16' rx='3'/><circle cx='12' cy='12' r='5'/><path d='M10 11v2M14 11v2'/></svg> "+d.pw+" W"+" · "+d.pv+" V";
var nx=d.x&&d.x[0]>t?d.x:null,fc=$("fc");
if(g===0&&nx&&d.r&&a!=null){var left=d.r*3600-(t-d.t),need=nx[0]-t;fc.style.display="";
$("fk").textContent=L.leftk;$("fk").style.color="#ff9f0a";$("fv").textContent=need<left?"≈ "+Math.round(a*(1-need/left))+"%":"✗";
$("fi").textContent="~"+dur(d.r*3600)+" · "+(d.o||0)+" W"}else if(g===0&&d.r){fc.style.display="";$("fk").textContent=L.rem;
$("fk").style.color="";$("fv").textContent="~"+dur(d.r*3600);$("fi").textContent=""}else fc.style.display="none";
$("gk").textContent=d.gr?(d.e?"⚠️ "+L.emg:L.grp)+" "+d.gr:L.day;$("gk").style.color=d.e?"#ff453a":"";
$("gr").textContent=nx?(nx[1]=="1"?L.onat:L.offat)+" "+hm(nx[0]):"";
$("nt").textContent=nx?dur(nx[0]-t):"—";$("u").textContent=(d.w?wi(d.w[1])+" "+(d.w[0]>0?"+":"")+d.w[0]+"°"+(d.w[2]!=null?" AQI "+d.w[2]:"")+" · ":"")+"⟳ "+hm(d.u||d.t);alarm(d,t);
var k2=[d.f,d.p,Math.floor((d.m+(t-d.t)/60)/5),d.g,d.o>0,avg(),(d.b||[]).join(),d.i>(d.o||0)+20].join();if(k2!=key){key=k2;strip();flow()}}
function ctl(){var d=D,k=[d.lm,d.dc].join();if(k==shown)return;shown=k;var c=$("ctl");c.innerHTML="";
if(d.lm!=null)add(c,"light",IL,d.lm);if(d.dc!=null)add(c,"dc",ID,d.dc);add(c,"qs",SV+"<circle cx='12' cy='12' r='6.3'/><circle cx='12' cy='12' r='2.4'/><path stroke-width='3.2' stroke-linecap='butt' d='M12 2.4v3.4M12 18.2v3.4M2.4 12h3.4M18.2 12h3.4M5.2 5.2l2.4 2.4M16.4 16.4l2.4 2.4M5.2 18.8l2.4-2.4M16.4 7.6l2.4-2.4'/></svg>",0)}
var SV="<svg viewBox='0 0 24 24' width='24' height='24' fill='none' stroke='currentColor' stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'>",
IL=SV+"<path d='M8 2.5h8v3.5l-2 3.2V21a1 1 0 0 1-1 1h-2a1 1 0 0 1-1-1V9.2L8 6z'/><path d='M8 6h8'/><circle cx='12' cy='13' r='1.3' fill='currentColor'/></svg>",
ID=SV+"<circle cx='12' cy='12' r='9'/><circle cx='12' cy='12' r='4.5'/><circle cx='12' cy='12' r='1.4' fill='currentColor'/><path d='M12 3v2.5'/></svg>";
function add(c,a,t,on){var b=document.createElement("button");b.innerHTML=t;if(on)b.className="a";
b.addEventListener("click",function(){b.className=on?"":"a";W.request(a).then(function(){shown="";setTimeout(ctl,1500)},
function(){b.textContent="⚠️";shown="";setTimeout(ctl,2500)})});c.appendChild(b)}
function load(){try{D=JSON.parse(W.chatData||"{}")}catch(e){D={}}tick();ctl()}
load();W.onEvent("chatDataChanged",load);function vf(){if(!document.hidden)tick()}setInterval(vf,10000);document.addEventListener("visibilitychange",vf);W.ready()
</script>"""



def labels(L: Lang) -> dict:
    return {"on": L.t("w_on"), "off": L.t("w_off"), "leftk": L.t("wi_left_on"),
            "rem": L.t("wi_remaining"), "grp": L.t("w_group"), "day": L.t("w_day"), "emg": L.t("w_emergency"), "alr": L.t("w_alert"), "aln": L.t("w_alert_near"),
            "m": L.t("bat_main"), "e": L.t("bat_expansion"), "onat": L.t("w_on_at"), "offat": L.t("w_off_at"), "y": L.t("m_on"), "n": L.t("m_off"),
            "u": list(UNITS[L.code][:3])}


def widget_html(L: Lang, name: str) -> str:
    html = TEMPLATE.replace("__NAME__", name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return html.replace("__LABELS__", json.dumps(labels(L), ensure_ascii=False, separators=(",", ":")))
