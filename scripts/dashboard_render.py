#!/usr/bin/env python3
"""Render the collected quality signals into a self-contained D3 dashboard.

``render(data)`` returns a complete HTML document with the vendored D3 source
and the data JSON inlined, so the page opens offline in any browser. Colours,
marks, and dark mode follow the data-viz skill's reference palette: status
hues (good/warning/critical) for gates, one sequential blue for magnitude bars,
a ratchet reference line on coverage, hover tooltips, and a selectable theme.
"""

from __future__ import annotations

import json
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"

# --- CSS: palette roles as custom properties, light + dark (both scopes) -----
_CSS = """
:root {
  color-scheme: light;
  --page:#f9f9f7; --surface:#fcfcfb;
  --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,0.10);
  --series:#2a78d6; --series-soft:#9ec5f4;
  --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
  --neutral:#c3c2b7;
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page:#0d0d0d; --surface:#1a1a19;
  --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
  --series:#3987e5; --series-soft:#184f95;
  --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
  --neutral:#383835;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page:#0d0d0d; --surface:#1a1a19;
    --ink:#ffffff; --ink-2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
    --series:#3987e5; --series-soft:#184f95; --neutral:#383835;
  }
}
* { box-sizing:border-box; }
body {
  margin:0; background:var(--page); color:var(--ink);
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif; font-size:14px;
}
.wrap { max-width:1180px; margin:0 auto; padding:28px 22px 60px; }
header { display:flex; align-items:baseline; justify-content:space-between; gap:16px; flex-wrap:wrap; }
h1 { font-size:20px; margin:0; font-weight:650; }
.sub { color:var(--muted); font-size:12.5px; }
.theme-btn {
  border:1px solid var(--border); background:var(--surface); color:var(--ink-2);
  border-radius:8px; padding:5px 11px; font:inherit; font-size:12.5px; cursor:pointer;
}
h2 { font-size:13px; font-weight:600; letter-spacing:.02em; margin:0 0 12px;
     color:var(--ink-2); text-transform:uppercase; }
.card { background:var(--surface); border:1px solid var(--border);
        border-radius:14px; padding:18px 18px 16px; }
.grid { display:grid; gap:16px; }
.tiles { grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); margin:22px 0; }
.panels { grid-template-columns:1fr 1fr; margin-top:2px; }
@media (max-width:760px){ .panels{ grid-template-columns:1fr; } }
.tile { background:var(--surface); border:1px solid var(--border);
        border-radius:14px; padding:15px 16px; border-left:4px solid var(--neutral); }
.tile.good{ border-left-color:var(--good); }
.tile.warn{ border-left-color:var(--warning); }
.tile.bad{ border-left-color:var(--critical); }
.tile .label { color:var(--ink-2); font-size:12px; }
.tile .value { font-size:27px; font-weight:660; margin-top:4px; letter-spacing:-.01em; }
.tile .note { color:var(--muted); font-size:11.5px; margin-top:3px; }
.status-row { display:flex; align-items:center; gap:9px; padding:7px 0;
              border-bottom:1px solid var(--grid); }
.status-row:last-child{ border-bottom:0; }
.status-row .ico { width:17px; height:17px; flex:none; }
.status-row .name { flex:1; }
.status-row .meta { color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums; }
.legend { display:flex; gap:14px; margin:0 0 10px; font-size:12px; color:var(--ink-2); }
.legend span { display:inline-flex; align-items:center; gap:6px; }
.swatch { width:11px; height:11px; border-radius:3px; display:inline-block; }
.bar { rx:2; }
.bar.seq { fill:var(--series); }
.bar.good { fill:var(--good); }
.bar.bad { fill:var(--critical); }
.bar-track { fill:var(--grid); }
.tick text, .axis-label { fill:var(--muted); font-size:11px; }
.tick line { stroke:var(--grid); }
.bar-label { fill:var(--ink-2); font-size:11px; font-variant-numeric:tabular-nums; }
.threshold { stroke:var(--serious); stroke-width:1.5; stroke-dasharray:4 3; }
.threshold-label { fill:var(--serious); font-size:10.5px; font-weight:600; }
.dead-bar rect { stroke:var(--surface); stroke-width:2; }
.dead-legend { display:flex; flex-wrap:wrap; gap:6px 14px; margin-top:11px; font-size:12px; }
.tooltip {
  position:fixed; pointer-events:none; opacity:0; transition:opacity .08s;
  background:var(--ink); color:var(--surface); border-radius:7px;
  padding:7px 9px; font-size:12px; line-height:1.5; z-index:9; max-width:260px;
  box-shadow:0 4px 16px rgba(0,0,0,.22);
}
.tooltip b { font-weight:640; }
footer { color:var(--muted); font-size:11.5px; margin-top:26px; text-align:center; }
"""

# --- JS: two d3 bar charts + a dead-code stacked bar + tooltip/theme ---------
# Setup runs once; drawAll() only clears + redraws the charts (which read CSS
# custom properties, so a theme swap needs only a redraw).
_JS = r"""
const DATA = window.__DASHBOARD__;
const $ = (sel) => document.querySelector(sel);
const tip = d3.select("body").append("div").attr("class","tooltip");
function showTip(html, ev){ tip.html(html).style("opacity",1)
  .style("left",(ev.clientX+14)+"px").style("top",(ev.clientY+14)+"px"); }
function hideTip(){ tip.style("opacity",0); }

// Horizontal bar chart. rows:[{label,value,cls,tip}], opts:{fmt,threshold,thLabel,domainMax}
function hbar(mount, rows, opts={}){
  const fmt = opts.fmt || (v=>v);
  const W = mount.clientWidth, rowH = 26, padTop = opts.threshold!=null?16:6,
        padBottom = 22, m = {left:Math.min(190, Math.max(...rows.map(r=>r.label.length))*6.6+12), right:46};
  const H = padTop + rows.length*rowH + padBottom;
  const svg = d3.select(mount).append("svg").attr("width",W).attr("height",H)
      .attr("role","img");
  const x = d3.scaleLinear().domain([0, opts.domainMax ?? Math.max(...rows.map(r=>r.value),1)])
      .range([m.left, W-m.right]);
  const y = d3.scaleBand().domain(rows.map(r=>r.label)).range([padTop, H-padBottom]).padding(0.28);

  // gridlines (recessive)
  svg.append("g").attr("class","tick").selectAll("line").data(x.ticks(4)).join("line")
     .attr("x1",d=>x(d)).attr("x2",d=>x(d)).attr("y1",padTop-2).attr("y2",H-padBottom)
     .attr("stroke-width",1);
  svg.selectAll(".xt").data(x.ticks(4)).join("text").attr("class","tick")
     .attr("x",d=>x(d)).attr("y",H-padBottom+13).attr("text-anchor","middle").text(d=>fmt(d));

  // category labels
  svg.append("g").selectAll("text.cat").data(rows).join("text").attr("class","bar-label")
     .attr("x",m.left-9).attr("y",d=>y(d.label)+y.bandwidth()/2+3.5).attr("text-anchor","end")
     .text(d=>d.label);

  // track + bars (4px rounded data-end via rx; anchored at x0)
  const x0 = x(0);
  svg.append("g").selectAll("rect.trk").data(rows).join("rect").attr("class","bar-track")
     .attr("x",x0).attr("y",d=>y(d.label)).attr("height",y.bandwidth())
     .attr("width",W-m.right-x0).attr("rx",4).attr("opacity",.5);
  svg.append("g").selectAll("rect.bar").data(rows).join("rect")
     .attr("class",d=>"bar "+(d.cls||"seq"))
     .attr("x",x0).attr("y",d=>y(d.label)).attr("height",y.bandwidth())
     .attr("width",d=>Math.max(2,x(d.value)-x0)).attr("rx",4)
     .on("mousemove",(ev,d)=>showTip(d.tip,ev)).on("mouseleave",hideTip);

  // direct value labels (always visible — relief for contrast)
  svg.append("g").selectAll("text.val").data(rows).join("text").attr("class","bar-label")
     .attr("x",d=>x(d.value)+5).attr("y",d=>y(d.label)+y.bandwidth()/2+3.5).text(d=>fmt(d.value));

  // ratchet reference line
  if(opts.threshold!=null){
    svg.append("line").attr("class","threshold").attr("x1",x(opts.threshold))
       .attr("x2",x(opts.threshold)).attr("y1",padTop-4).attr("y2",H-padBottom);
    svg.append("text").attr("class","threshold-label").attr("x",x(opts.threshold))
       .attr("y",padTop-6).attr("text-anchor","middle").text(opts.thLabel||"");
  }
}

function drawCoverage(){
  const c = DATA.coverage;
  const rows = c.components.map(d=>({
    label:d.component, value:d.pct, cls:d.pct<c.min?"bad":"seq",
    tip:`<b>${d.component}</b><br>${d.pct}% covered<br>${d.covered}/${d.statements} statements`
  }));
  hbar($("#chart-coverage"), rows, {fmt:v=>v+"%", domainMax:100,
       threshold:c.min, thLabel:`min ${c.min}%`});
}

function drawTests(){
  const rows = DATA.tests.by_module.map(d=>({
    label:d.module.replace(/^tests\./,""), value:d.total,
    cls:d.failed>0?"bad":"good",
    tip:`<b>${d.module}</b><br>${d.passed} passed`+
        (d.failed?`, <b>${d.failed} failed</b>`:"")+
        (d.skipped?`, ${d.skipped} skipped`:"")+`<br>${d.time.toFixed(2)}s`
  }));
  hbar($("#chart-tests"), rows, {fmt:v=>v});
}

function drawDeadcode(){
  const d = DATA.dead_code, mount = $("#chart-deadcode");
  const testsOnly = d.test_reached || 0;
  const reachable = d.universe - d.uncovered - d.allowlisted
                    - d.truly_dead.length - testsOnly;
  const segs = [
    {k:"reachable (product)", v:reachable,           fill:"var(--good)"},
    {k:"tests-only (review)", v:testsOnly,           fill:"var(--serious)"},
    {k:"allowlisted",         v:d.allowlisted,       fill:"var(--neutral)"},
    {k:"uncovered",           v:d.uncovered,         fill:"var(--warning)"},
    {k:"truly-dead",          v:d.truly_dead.length, fill:"var(--critical)"},
  ].filter(s=>s.v>0);
  const W = mount.clientWidth, H = 34, x = d3.scaleLinear().domain([0,d.universe]).range([0,W]);
  const svg = d3.select(mount).append("svg").attr("width",W).attr("height",H).attr("class","dead-bar");
  let acc=0;
  svg.selectAll("rect").data(segs).join("rect")
     .attr("x",s=>{const xx=x(acc); acc+=s.v; return xx;})
     .attr("y",0).attr("height",H).attr("width",s=>x(s.v)).attr("fill",s=>s.fill).attr("rx",3)
     .on("mousemove",(ev,s)=>showTip(`<b>${s.k}</b><br>${s.v} definitions`,ev)).on("mouseleave",hideTip);
  const leg = d3.select(mount).append("div").attr("class","dead-legend");
  segs.forEach(s=>leg.append("span").html(
    `<span class="swatch" style="background:${s.fill}"></span>${s.k} · ${s.v}`));
}

function drawAll(){
  ["#chart-coverage","#chart-tests","#chart-deadcode"].forEach(s=>{ $(s).innerHTML=""; });
  drawCoverage(); drawTests(); drawDeadcode();
}

// One-time bindings
$("#theme").addEventListener("click", ()=>{
  const root = document.documentElement;
  const dark = !(root.getAttribute("data-theme")==="dark" ||
    (!root.getAttribute("data-theme") && matchMedia("(prefers-color-scheme:dark)").matches));
  root.setAttribute("data-theme", dark?"dark":"light");
  drawAll();
});
window.addEventListener("resize", drawAll);
drawAll();
"""

_CHECK = ('<svg class="ico" viewBox="0 0 20 20" fill="none" stroke="{c}" '
          'stroke-width="2.4"><path d="M4 10.5l4 4 8-9"/></svg>')
_CROSS = ('<svg class="ico" viewBox="0 0 20 20" fill="none" stroke="{c}" '
          'stroke-width="2.4"><path d="M5 5l10 10M15 5L5 15"/></svg>')


def _icon(ok: bool) -> str:
    return (_CHECK if ok else _CROSS).format(c="var(--good)" if ok else "var(--critical)")


def _tile(label: str, value: str, note: str, cls: str) -> str:
    return (f'<div class="tile {cls}"><div class="label">{label}</div>'
            f'<div class="value">{value}</div><div class="note">{note}</div></div>')


def _stat_tiles(data: dict) -> str:
    t = data["tests"]["totals"]
    cov = data["coverage"]
    dc = data["dead_code"]
    model_ok = sum(1 for g in data["model"] if g["ok"])
    lint_ok = all(x["ok"] for x in data["lint"])
    lint_issues = sum(x["issues"] for x in data["lint"])
    tiles = [
        _tile("Tests", f'{t["passed"]}/{t["total"]}',
              (f'{t["failed"]} failed' if t["failed"] else "all passing")
              + (f' · {t["skipped"]} skipped' if t["skipped"] else ""),
              "bad" if t["failed"] else "good"),
        _tile("Coverage", f'{cov["overall"]}%', f'ratchet min {cov["min"]}%',
              "good" if cov["overall"] >= cov["min"] else "bad"),
        _tile("Model gates", f'{model_ok}/{len(data["model"])}',
              "consistent" if model_ok == len(data["model"]) else "drift",
              "good" if model_ok == len(data["model"]) else "bad"),
        _tile("Dead code", str(len(dc["truly_dead"])),
              f'truly-dead · {dc["uncovered"]} uncovered', "good" if not dc["truly_dead"] else "bad"),
        _tile("Lint", "clean" if lint_ok else str(lint_issues),
              "mypy + ruff" if lint_ok else "issues", "good" if lint_ok else "bad"),
    ]
    return '<div class="grid tiles">' + "".join(tiles) + "</div>"


def _status_list(rows: list[tuple[bool, str, str]]) -> str:
    out = []
    for ok, name, meta in rows:
        out.append(f'<div class="status-row">{_icon(ok)}<span class="name">{name}</span>'
                   f'<span class="meta">{meta}</span></div>')
    return "".join(out)


def render(data: dict) -> str:
    d3_src = (ASSETS / "d3.v7.min.js").read_text(encoding="utf-8")
    model_rows = [(g["ok"], g["label"], "pass" if g["ok"] else "FAIL") for g in data["model"]]
    lint_rows = [(x["ok"], x["tool"], "clean" if x["ok"] else f'{x["issues"]} issue(s)')
                 for x in data["lint"]]
    t = data["tests"]["totals"]

    body = f"""
<div class="wrap">
  <header>
    <div>
      <h1>mdg-drawio · quality dashboard</h1>
      <div class="sub">Aggregated Makefile signals · generated {data["generated_at"]}</div>
    </div>
    <button class="theme-btn" id="theme">Toggle theme</button>
  </header>

  {_stat_tiles(data)}

  <div class="grid panels">
    <div class="card">
      <h2>Coverage by Component</h2>
      <div id="chart-coverage"></div>
    </div>
    <div class="card">
      <h2>Tests by module ({t["passed"]}/{t["total"]} passing)</h2>
      <div class="legend">
        <span><span class="swatch" style="background:var(--good)"></span>all passing</span>
        <span><span class="swatch" style="background:var(--critical)"></span>has failures</span>
      </div>
      <div id="chart-tests"></div>
    </div>
    <div class="card">
      <h2>Dead code</h2>
      <div id="chart-deadcode"></div>
      <div class="sub" style="margin-top:10px">{data["dead_code"]["universe"]} definitions.
        <b>reachable (product)</b> = run by the CLI action sweep; <b>tests-only</b> = exercised
        only by the unit suite (review: intentional API or dead?). Matches
        <code>make dead-code</code>.</div>
    </div>
    <div class="card">
      <h2>Model consistency &amp; lint</h2>
      {_status_list(model_rows)}
      <div style="height:10px"></div>
      {_status_list(lint_rows)}
    </div>
  </div>

  <footer>Static overview · rebuild with <code>make dashboard</code>. Advisory only.</footer>
</div>
"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>mdg-drawio quality dashboard</title>
<style>{_CSS}</style>
</head>
<body>
{body}
<script>{d3_src}</script>
<script>window.__DASHBOARD__ = {json.dumps(data)};</script>
<script>{_JS}</script>
</body>
</html>
"""
