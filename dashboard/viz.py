"""
Neural-circuit hybrid visualization — Voronoi core + Manhattan-routed traces + hexagonal agent nodes.
Everything in one D3 SVG. No split coordinate systems.
"""
import json

_BG = "#0f172a"

_C = {
    "pending":  {"fill": "#0f1f35", "stroke": "#1e3a5f", "text": "#475569"},
    "active":   {"fill": "#0c2d4a", "stroke": "#38bdf8", "text": "#7dd3fc"},
    "complete": {"fill": "#0a2a1f", "stroke": "#34d399", "text": "#6ee7b7"},
    "failed":   {"fill": "#2a0f0f", "stroke": "#f87171", "text": "#fca5a5"},
}

_AGENT_META = {
    "DISCOVER":  {"badge": "A1", "model": "Ultra",    "angle": -90},   # 12 o'clock
    "ATTACK":    {"badge": "A2", "model": "Ultra",    "angle": -141},  # 10 o'clock
    "GUARD":     {"badge": "A3", "model": "Nano",     "angle": -39},   # 2 o'clock
    "VERIFY":    {"badge": "A4", "model": "Ultra",    "angle": 180},   # 9 o'clock
    "DIAGNOSE":  {"badge": "A5", "model": "Ultra",    "angle": 129},   # 7-8 o'clock
    "REMEDIATE": {"badge": "A6", "model": "DeepSeek", "angle": 39},    # 4 o'clock
    "VALIDATE":  {"badge": "",   "model": "",          "angle": 90},    # 6 o'clock
}

_MODEL_COL = {"Ultra": "#818cf8", "Nano": "#fbbf24", "DeepSeek": "#34d399", "": "#475569"}


def render(state: dict, score_before=None, score_after=None, dim: bool = False, height: int = 680) -> str:
    state_json = json.dumps({
        k: {
            "status":      v.get("status", "pending"),
            "elapsed_ms":  v.get("elapsed_ms"),
            "context_msg": v.get("context_msg"),
        }
        for k, v in state.items()
    })
    agents_json    = json.dumps(_AGENT_META)
    colors_json    = json.dumps(_C)
    model_col_json = json.dumps(_MODEL_COL)
    sb = "null" if score_before is None else str(score_before)
    sa = "null" if score_after  is None else str(score_after)

    return f"""<!DOCTYPE html><html><head>
<meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;900&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:100%;height:{height}px;background:{_BG};overflow:hidden}}
svg{{display:block;width:100%;height:{height}px}}
</style>
</head><body>
<svg id="viz"></svg>
<script>
(function(){{

var STATE     = {state_json};
var AGENTS    = {agents_json};
var COLORS    = {colors_json};
var MODEL_COL = {model_col_json};
var scoreBefore = {sb};
var scoreAfter  = {sa};

var W  = document.body.clientWidth || 1014;
var H  = {height};
var cx = W * 0.5;
var cy = H * 0.5 - 10;
// Keep orbit tight enough that DISCOVER (top) and VALIDATE (bottom) both fit
// DISCOVER top edge = cy - ORBIT - HEX_R - 20 > 40 (branding bar)
// VALIDATE bottom edge = cy + ORBIT + HEX_R + 20 < H - 40 (watermark)
var ORBIT  = Math.min(W * 0.36, (H - 120) * 0.44);
var CORE_R = ORBIT * 0.52;
var HEX_R  = Math.min(26, ORBIT * 0.11);

var svg = d3.select("#viz").attr("width",W).attr("height",H);
var defs = svg.append("defs");

// Radial core gradient
var rg = defs.append("radialGradient").attr("id","coreglow").attr("cx","50%").attr("cy","50%").attr("r","50%");
rg.append("stop").attr("offset","0%").attr("stop-color","#38bdf8").attr("stop-opacity","0.22");
rg.append("stop").attr("offset","55%").attr("stop-color","#0e4f6e").attr("stop-opacity","0.10");
rg.append("stop").attr("offset","100%").attr("stop-color","{_BG}").attr("stop-opacity","0");

// Glow filters
["active","complete","failed"].forEach(function(s){{
  var f = defs.append("filter").attr("id","gf_"+s)
    .attr("x","-80%").attr("y","-80%").attr("width","260%").attr("height","260%");
  var sd = s==="active"?8:s==="complete"?5:5;
  f.append("feGaussianBlur").attr("in","SourceGraphic").attr("stdDeviation",sd).attr("result","b");
  var m=f.append("feMerge"); m.append("feMergeNode").attr("in","b"); m.append("feMergeNode").attr("in","SourceGraphic");
}});

// Subtle grid
defs.append("pattern").attr("id","grid").attr("width",48).attr("height",48)
  .attr("patternUnits","userSpaceOnUse")
  .append("path").attr("d","M 48 0 L 0 0 0 48").attr("fill","none")
  .attr("stroke","#1a2740").attr("stroke-width","0.5");

// ── Background ──
svg.append("rect").attr("width",W).attr("height",H).attr("fill","{_BG}");
svg.append("rect").attr("width",W).attr("height",H).attr("fill","url(#grid)");
svg.append("ellipse").attr("cx",cx).attr("cy",cy)
  .attr("rx",CORE_R*1.5).attr("ry",CORE_R*1.3)
  .attr("fill","url(#coreglow)");

// ── VORONOI CORE ──
// Seed points for voronoi — biased toward center with Gaussian-ish scatter
var seed = 7331;
function rand(){{ seed=(seed*1664525+1013904223)&0xffffffff; return (seed>>>0)/0xffffffff; }}
function randn(){{
  var u=rand(), v=rand();
  return Math.sqrt(-2*Math.log(u+1e-9))*Math.cos(2*Math.PI*v);
}}

var voronoiPts = [];
for(var i=0;i<32;i++){{
  var x = cx + randn()*CORE_R*0.38;
  var y = cy + randn()*CORE_R*0.32;
  voronoiPts.push([x,y]);
}}
// Always include center
voronoiPts.push([cx, cy]);

var delaunay = d3.Delaunay.from(voronoiPts);
var voronoi  = delaunay.voronoi([cx-CORE_R, cy-CORE_R*0.88, cx+CORE_R, cy+CORE_R*0.88]);

var coreG = svg.append("g").attr("class","core");

// Clip to ellipse
defs.append("clipPath").attr("id","coreClip")
  .append("ellipse").attr("cx",cx).attr("cy",cy)
  .attr("rx",CORE_R).attr("ry",CORE_R*0.88);

var clippedCore = coreG.append("g").attr("clip-path","url(#coreClip)");

// Draw voronoi cells
for(var i=0;i<voronoiPts.length;i++){{
  var cell = voronoi.cellPolygon(i);
  if(!cell) continue;
  var dx = voronoiPts[i][0]-cx, dy = voronoiPts[i][1]-cy;
  var dist = Math.sqrt(dx*dx+dy*dy);
  var t = Math.max(0, 1 - dist/CORE_R);
  var edgeOp  = 0.5 + t*0.45;
  var fillOp  = 0.03 + t*0.12;
  var fillCol = t > 0.7 ? "#0e4f6e" : "#0a2a40";
  clippedCore.append("polygon")
    .attr("points", cell.map(function(p){{return p[0]+","+p[1]}}).join(" "))
    .attr("fill", fillCol).attr("fill-opacity", fillOp)
    .attr("stroke","#38bdf8").attr("stroke-opacity", edgeOp*0.7).attr("stroke-width","0.8");
}}

// Voronoi vertex dots (circuit junction points)
var vertexSet = {{}};
for(var i=0;i<voronoiPts.length;i++){{
  var cell = voronoi.cellPolygon(i);
  if(!cell) continue;
  cell.forEach(function(p){{
    var key = Math.round(p[0])+","+Math.round(p[1]);
    if(!vertexSet[key]){{
      vertexSet[key] = p;
      var dx=p[0]-cx, dy=p[1]-cy;
      var dist=Math.sqrt(dx*dx+dy*dy);
      if(dist < CORE_R*0.96){{
        var t = Math.max(0,1-dist/CORE_R);
        clippedCore.append("circle").attr("cx",p[0]).attr("cy",p[1]).attr("r",1.5)
          .attr("fill","#7dd3fc").attr("opacity",0.25+t*0.65);
      }}
    }}
  }});
}}

// Core center pulse
var coreCenter = coreG.append("circle").attr("cx",cx).attr("cy",cy).attr("r",6)
  .attr("fill","#ffffff").attr("opacity","0.9");
coreG.append("circle").attr("cx",cx).attr("cy",cy).attr("r",3)
  .attr("fill","#38bdf8").attr("opacity","1");

// Animate center pulse ring
(function pulsate(t){{
  var ph = (t % 2200) / 2200;
  coreCenter.attr("r", 6 + ph*18).attr("opacity", 0.7*(1-ph));
  requestAnimationFrame(pulsate);
}})(0);

// ── AGENT NODE POSITIONS ──
var nodeNames = Object.keys(AGENTS);
var nodePos = {{}};
nodeNames.forEach(function(name){{
  var a = AGENTS[name].angle * Math.PI / 180;
  nodePos[name] = {{ x: cx + Math.cos(a)*ORBIT, y: cy + Math.sin(a)*ORBIT }};
}});

// ── MANHATTAN TRACES ──
var traceG = svg.append("g").attr("class","traces");

nodeNames.forEach(function(name){{
  var p    = nodePos[name];
  var st   = (STATE[name]||{{}}).status || "pending";
  var isDimmed = {json.dumps(dim)} && st==="complete";
  var col  = st==="active"?"#38bdf8":st==="complete"?"#34d399":st==="failed"?"#f87171":"#1e3a5f";
  var op   = st==="pending" ? 0.35 : isDimmed ? 0.25 : 0.9;
  var sw   = st==="active"||st==="complete" ? 2 : 1;

  // Compute exit point on core ellipse boundary
  var dx = p.x-cx, dy = p.y-cy;
  var len = Math.sqrt(dx*dx+dy*dy);
  var ex = cx + (dx/len)*CORE_R;
  var ey = cy + (dy/len)*CORE_R*0.88;

  // Manhattan bend: one midpoint
  // Route: exit point → bend → node edge
  var nx = p.x - (dx/len)*HEX_R;
  var ny = p.y - (dy/len)*HEX_R;

  // Pick bend: if more horizontal movement, go horizontal first
  var mx, my;
  if(Math.abs(dx) >= Math.abs(dy)){{
    mx = nx; my = ey;
  }} else {{
    mx = ex; my = ny;
  }}

  // Solder pad at exit
  traceG.append("circle").attr("cx",ex).attr("cy",ey).attr("r",3.5)
    .attr("fill","none").attr("stroke",col).attr("stroke-width","1.5").attr("opacity",op);
  traceG.append("circle").attr("cx",ex).attr("cy",ey).attr("r",1.5)
    .attr("fill",col).attr("opacity",op);

  // Trace segments
  var dash = st==="pending" ? "6,4" : null;
  var seg1 = traceG.append("line")
    .attr("x1",ex).attr("y1",ey).attr("x2",mx).attr("y2",my)
    .attr("stroke",col).attr("stroke-width",sw).attr("opacity",op);
  var seg2 = traceG.append("line")
    .attr("x1",mx).attr("y1",my).attr("x2",nx).attr("y2",ny)
    .attr("stroke",col).attr("stroke-width",sw).attr("opacity",op);
  if(dash){{ seg1.attr("stroke-dasharray",dash); seg2.attr("stroke-dasharray",dash); }}

  // Junction dot at bend
  if(st!=="pending"){{
    traceG.append("circle").attr("cx",mx).attr("cy",my).attr("r",2.5)
      .attr("fill",col).attr("opacity",0.8);
  }}

  // Solder pad at node end
  traceG.append("circle").attr("cx",nx).attr("cy",ny).attr("r",3)
    .attr("fill","none").attr("stroke",col).attr("stroke-width","1.2").attr("opacity",op);

  // Traveling dot on active trace
  if(st==="active"){{
    var totalLen = Math.sqrt((mx-ex)*(mx-ex)+(my-ey)*(my-ey))
                + Math.sqrt((nx-mx)*(nx-mx)+(ny-my)*(ny-my));
    [0, 0.45].forEach(function(phase){{
      var dot = traceG.append("circle").attr("r",3.5).attr("fill",col).attr("opacity","0.95");
      (function animDot(t){{
        var f = ((t/1600) + phase) % 1;
        var seg1L = Math.sqrt((mx-ex)*(mx-ex)+(my-ey)*(my-ey)) / (totalLen||1);
        var px2, py2;
        if(f < seg1L){{
          var ff = seg1L > 0 ? f/seg1L : 0;
          px2 = ex+(mx-ex)*ff; py2 = ey+(my-ey)*ff;
        }} else {{
          var ff = seg1L < 1 ? (f-seg1L)/(1-seg1L) : 1;
          px2 = mx+(nx-mx)*ff; py2 = my+(ny-my)*ff;
        }}
        dot.attr("cx",px2).attr("cy",py2);
        requestAnimationFrame(animDot);
      }})(0);
    }});
  }}
}});

// ── HEXAGONAL AGENT NODES ──
function hexPath(cx2,cy2,r){{
  var pts=[];
  for(var i=0;i<6;i++){{
    var a=(i*60-30)*Math.PI/180;
    pts.push((cx2+r*Math.cos(a))+","+(cy2+r*Math.sin(a)));
  }}
  return "M"+pts.join("L")+"Z";
}}

var nodeG = svg.append("g").attr("class","nodes");

nodeNames.forEach(function(name){{
  var p    = nodePos[name];
  var meta = AGENTS[name];
  var st   = (STATE[name]||{{}}).status || "pending";
  var c    = COLORS[st]||COLORS.pending;
  var mc   = MODEL_COL[meta.model]||"#475569";

  var g = nodeG.append("g");

  // Outer pulse for active
  if(st==="active"){{
    var ph2 = g.append("polygon")
      .attr("points", hexPath(p.x,p.y,HEX_R+8).replace(/M|Z/g,"").replace(/L/g," "))
      .attr("fill","none").attr("stroke",c.stroke).attr("stroke-width","1.5").attr("opacity","0.5");
    // Rewrite as path for animation
    ph2.remove();
    var outerHex = g.append("path").attr("d",hexPath(p.x,p.y,HEX_R+8))
      .attr("fill","none").attr("stroke",c.stroke).attr("stroke-width","1.5").attr("opacity","0.5");
    (function animHex(t){{
      var ph3 = (t%2000)/2000;
      // Scale pulse using transform
      var s = 1 + ph3*0.18;
      outerHex.attr("transform","translate("+p.x+","+p.y+") scale("+s+") translate("+(-(p.x))+","+(-(p.y))+")");
      outerHex.attr("opacity", 0.5*(1-ph3));
      requestAnimationFrame(animHex);
    }})(0);
  }}

  // Hex body with glow for active/complete
  var dimmed = {json.dumps(dim)} && st==="complete";
  var nodeOp = dimmed ? 0.35 : 1.0;
  var body = g.append("path").attr("d",hexPath(p.x,p.y,HEX_R))
    .attr("fill",c.fill).attr("stroke",c.stroke).attr("stroke-width","1.8")
    .attr("opacity", nodeOp);
  if(st!=="pending" && !dimmed) body.attr("filter","url(#gf_"+st+")");

  // Status icon
  if(st==="complete"){{
    g.append("text").attr("x",p.x).attr("y",p.y+5)
      .attr("text-anchor","middle").attr("font-size","15").attr("fill",c.stroke)
      .attr("opacity", nodeOp)
      .attr("font-family","Inter,sans-serif").text("✓");
  }} else if(st==="failed"){{
    g.append("text").attr("x",p.x).attr("y",p.y+5)
      .attr("text-anchor","middle").attr("font-size","15").attr("fill",c.stroke)
      .attr("font-family","Inter,sans-serif").text("✗");
  }} else if(st==="active"){{
    var dot2 = g.append("circle").attr("cx",p.x).attr("cy",p.y).attr("r",5).attr("fill",c.stroke);
    (function animDot2(t){{
      dot2.attr("opacity", 0.2+0.8*Math.abs(Math.sin(t/380)));
      requestAnimationFrame(animDot2);
    }})(0);
  }} else {{
    g.append("circle").attr("cx",p.x).attr("cy",p.y).attr("r",4)
      .attr("fill","none").attr("stroke",c.stroke).attr("stroke-width","1").attr("opacity","0.4");
  }}


  // Name label below hex
  g.append("text").attr("x",p.x).attr("y",p.y+HEX_R+14)
    .attr("text-anchor","middle").attr("font-size","9").attr("font-weight","700")
    .attr("fill",c.text).attr("font-family","JetBrains Mono,monospace")
    .attr("letter-spacing","0.12em").text(name);
}});

// ── SCORES ──
if(scoreBefore!==null){{
  var scoreCol=function(v){{return v>=70?"#34d399":v>=40?"#fbbf24":"#f87171";}};
  ["BEFORE","AFTER"].forEach(function(lbl,i){{
    var val=i===0?scoreBefore:scoreAfter;
    var sx=i===0?HEX_R*2.8:W-HEX_R*2.8;
    if(val===null) return;
    var col=scoreCol(val);
    svg.append("text").attr("x",sx).attr("y",cy-20).attr("text-anchor","middle")
      .attr("font-size","9").attr("fill","#475569")
      .attr("font-family","JetBrains Mono,monospace").attr("letter-spacing","0.12em").text(lbl);
    svg.append("text").attr("x",sx).attr("y",cy+18).attr("text-anchor","middle")
      .attr("font-size","40").attr("font-weight","900").attr("fill",col)
      .attr("font-family","Inter,sans-serif").attr("font-variant-numeric","tabular-nums").text(val);
    svg.append("text").attr("x",sx).attr("y",cy+34).attr("text-anchor","middle")
      .attr("font-size","9").attr("fill","#475569").attr("font-family","Inter,sans-serif").text("/100");
  }});
}}

// Score reveal banner — anchored below VALIDATE node
if(scoreAfter!==null && (STATE.VALIDATE||{{}}).status==="complete"){{
  var col2=scoreAfter>=70?"#34d399":"#fbbf24";
  var valY = cy + ORBIT;
  var banY = valY + HEX_R + 18;
  var banH = 46;
  svg.append("rect").attr("x",cx-150).attr("y",banY).attr("width",300).attr("height",banH)
    .attr("rx",6).attr("fill","#0a1929").attr("stroke",col2).attr("stroke-width","1");
  svg.append("text").attr("x",cx).attr("y",banY+14).attr("text-anchor","middle")
    .attr("font-size","9").attr("fill","#475569").attr("font-family","JetBrains Mono,monospace")
    .attr("letter-spacing","0.14em").text("RESILIENCE SCORE");
  svg.append("text").attr("x",cx).attr("y",banY+38).attr("text-anchor","middle")
    .attr("font-size","22").attr("font-weight","900").attr("fill",col2)
    .attr("font-family","Inter,sans-serif").text(scoreBefore+" → "+scoreAfter);
}}

// ── BRANDING ──
svg.append("text").attr("x",20).attr("y",28)
  .attr("font-size","15").attr("font-weight","900").attr("fill","#f1f5f9").attr("opacity","0.92")
  .attr("font-family","Inter,sans-serif").attr("letter-spacing","-0.02em")
  .text("👻 GhostVendor");

var badges=[
  {{label:"Nemotron Ultra",col:"#a78bfa",bg:"rgba(139,92,246,0.12)",bc:"rgba(139,92,246,0.35)"}},
  {{label:"Nemotron Nano", col:"#fbbf24",bg:"rgba(245,158,11,0.10)",bc:"rgba(245,158,11,0.35)"}},
  {{label:"DeepSeek Pro",  col:"#4ade80",bg:"rgba(34,197,94,0.08)", bc:"rgba(34,197,94,0.30)"}},
];
var bx=W-20;
badges.slice().reverse().forEach(function(b){{
  var tw=b.label.length*7.2+20;
  bx-=tw;
  svg.append("rect").attr("x",bx).attr("y",8).attr("width",tw).attr("height",24)
    .attr("rx",12).attr("fill",b.bg).attr("stroke",b.bc).attr("stroke-width","1");
  svg.append("text").attr("x",bx+tw/2).attr("y",24).attr("text-anchor","middle")
    .attr("font-size","11").attr("font-weight","700").attr("fill",b.col)
    .attr("font-family","JetBrains Mono,monospace").text(b.label);
  bx-=8;
}});

// Watermark — anchored to bottom of visible area (H - 60 clears any clipping)
var wmY = H - 18;
svg.append("text").attr("x",20).attr("y",wmY)
  .attr("font-size","14").attr("font-weight","900").attr("fill","#cbd5e1").attr("opacity","0.88")
  .attr("font-family","Inter,sans-serif").attr("letter-spacing","-0.02em")
  .text("GhostVendor");
svg.append("text").attr("x",20).attr("y",wmY+16)
  .attr("font-size","10").attr("fill","#94a3b8").attr("opacity","0.82")
  .attr("font-family","JetBrains Mono,monospace").attr("letter-spacing","0.06em")
  .text("Autonomous Resilience Engineer");

}})();
</script>
</body></html>"""


def render_idle(last_run_state: dict, score_before=None, score_after=None, height: int = 680) -> str:
    return render(last_run_state, None, None, dim=True, height=height)
