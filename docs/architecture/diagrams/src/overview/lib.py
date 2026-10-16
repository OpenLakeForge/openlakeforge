"""Drawing helpers and the static SVG exporter for the overview charts.

The charts are hand-placed inline SVG. Every logo comes from `logos.json`, which
records the official source of each one; nothing is redrawn. `export` writes one
self-contained SVG per theme: colours are resolved to literals and fonts fall
back to the viewer's system faces, because GitHub serves README images through
`<img>`, which runs no script and loads no web font.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

HERE = Path(__file__).parent
ASSETS = HERE.parents[3] / "assets"

_LOGOS = json.loads((HERE / "logos.json").read_text())
L = {name: f"data:{entry['type']};base64,{entry['data']}" for name, entry in _LOGOS.items()}

E = html.escape
svg: list[str] = []
a = svg.append
used: set[str] = set()

SANS = "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace"

THEMES = {
    "light": {
        "surface": "#ffffff", "zone": "#e9edf2", "ink": "#17202b", "muted": "#5b6878",
        "line": "#c9d1dc", "accent": "#2f6fd6", "lineage": "#7a52d1", "prod": "#b4235a",
        "bronze": "#b06a35", "bronze-bg": "#f6e7da", "silver": "#6f7f92", "silver-bg": "#e8edf3",
        "gold": "#b98b0e", "gold-bg": "#fbf1d0", "ops-bg": "#eef0f3",
        "chip": "#ffffff", "chip-line": "#dbe1e9",
    },
    "dark": {
        "surface": "#18202a", "zone": "#141b24", "ink": "#e6ebf2", "muted": "#96a3b4",
        "line": "#334152", "accent": "#6ea2ff", "lineage": "#b096f5", "prod": "#f27aa6",
        "bronze": "#e09a63", "bronze-bg": "#3a2a1e", "silver": "#aab6c5", "silver-bg": "#262f3a",
        "gold": "#e7c048", "gold-bg": "#3a3218", "ops-bg": "#1f2731",
        "chip": "#f4f6f9", "chip-line": "#2b3644",
    },
}

CSS = """
text { font-family: SANS; font-variant-ligatures: none; }
.bg { fill: var(--surface); stroke: var(--line); }
.zone { fill: var(--zone); }
.zlabel { font: 500 11px MONO; letter-spacing: .1em; fill: var(--muted); }
.card, .band, .tab, .ns { fill: var(--surface); stroke: var(--line); stroke-width: 1.2; }
.card.opt { stroke-dasharray: 6 5; stroke-width: 1.5; }
.ns.dev, .step.accent { stroke: var(--accent); stroke-width: 1.8; }
.ns.prod { stroke: var(--prod); stroke-width: 1.8; }
.chip { fill: var(--chip); stroke: var(--chip-line); }
.chip.opt { stroke: var(--muted); stroke-dasharray: 4 3; }
.stage { font: 500 10px MONO; letter-spacing: .08em; fill: var(--muted); }
.title { font-size: 17px; font-weight: 650; fill: var(--ink); }
.title.big { font-size: 22px; }
.title.sm { font-size: 15px; }
.mtitle { font-size: 15px; font-weight: 650; fill: var(--ink); }
.star { fill: var(--accent); }
.sub, .tsub { font-size: 12.5px; fill: var(--muted); }
.tname { font-size: 13.5px; font-weight: 600; fill: var(--ink); }
.mono { font: 500 11.5px MONO; fill: var(--ink); }
.elabel { font: 500 11px MONO; fill: var(--muted); paint-order: stroke; stroke: var(--zone);
  stroke-width: 5px; stroke-linejoin: round; }
.bname { font: 500 11.5px MONO; fill: var(--ink); }
.bsub { font-size: 12px; fill: var(--muted); }
.cyl, .cyltop { stroke-width: 1.4; }
.bronze .cyl, .bronze .cyltop { fill: var(--bronze-bg); stroke: var(--bronze); }
.silver .cyl, .silver .cyltop { fill: var(--silver-bg); stroke: var(--silver); }
.gold .cyl, .gold .cyltop { fill: var(--gold-bg); stroke: var(--gold); }
.ops .cyl, .ops .cyltop { fill: var(--ops-bg); stroke: var(--line); }
.olbg { fill: var(--chip); stroke: var(--lineage); stroke-width: 1.2; }
.edge path { fill: none; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
.edge.data path { stroke: var(--ink); }
.edge.ctl path { stroke: var(--muted); stroke-dasharray: 2 5; stroke-width: 1.6; }
.edge.lin path { stroke: var(--lineage); stroke-dasharray: 7 5; }
.ah-data { fill: var(--ink); } .ah-ctl { fill: var(--muted); } .ah-lin { fill: var(--lineage); }
.step { fill: var(--zone); }
.step.accent { fill: var(--surface); }
.num { fill: var(--surface); stroke: var(--line); stroke-width: 1.2; }
.num.accent, .num.dev { stroke: var(--accent); } .num.prod { stroke: var(--prod); }
.numt { font: 600 13px MONO; fill: var(--ink); }
.pill { fill: var(--surface); stroke: var(--line); stroke-width: 1.2; }
.pill.strong { stroke: var(--ink); }
.pill.gate { stroke: var(--prod); stroke-width: 1.6; }
.ptxt { font-size: 13px; font-weight: 500; fill: var(--ink); }
.ptxt.mono { font: 500 12px MONO; }
.legend { font-size: 13px; fill: var(--muted); }
""".replace("SANS", SANS).replace("MONO", MONO)


def reset() -> None:
    svg.clear()
    used.clear()


def defs() -> None:
    a("<defs>")
    for k in ("data", "ctl", "lin"):
        a(f'<marker id="ah-{k}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
          f'orient="auto-start-reverse"><path d="M0,1 L9,5 L0,9 Z" class="ah-{k}"/></marker>')
    a("</defs>")


def text(x: float, y: float, s: str, cls: str = "sub", anchor: str = "start") -> None:
    a(f'<text class="{cls}" x="{x}" y="{y}" text-anchor="{anchor}">{E(s)}</text>')


def image(logo: str, x: float, y: float, w: float, h: float) -> None:
    """Place a logo. Each logo is embedded once per file and reused through `<use>`."""
    used.add(logo)
    a(f'<use href="#lg-{logo}" x="{x}" y="{y}" width="{w}" height="{h}"/>')


def chip(x: float, y: float, s: float, logo: str, pad: float = 7, w: float | None = None,
         optional: bool = False) -> None:
    w = w or s
    a(f'<rect class="chip{" opt" if optional else ""}" x="{x}" y="{y}" width="{w}" height="{s}" rx="10"/>')
    image(logo, x + pad, y + pad, w - 2 * pad, s - 2 * pad)


def node(
    x: float, y: float, w: float, h: float, logo: str | None, stage: str, title: str, subs: list[str],
    optional: bool = False, tcls: str = "title", cs: float = 50,
) -> None:
    a(f'<rect class="card{" opt" if optional else ""}" x="{x}" y="{y}" width="{w}" height="{h}" rx="14"/>')
    if logo:
        chip(x + 12, y + 12, cs, logo)
        tx = x + cs + 22
    else:
        tx = x + 14
    text(tx, y + 29, stage, "stage")
    star = '<tspan class="star"> *</tspan>' if optional else ""
    a(f'<text class="{tcls}" x="{tx}" y="{y+52}">{E(title)}{star}</text>')
    for i, s in enumerate(subs):
        text(x + 14, y + 82 + i * 16, s)


def edge(d: str, kind: str, label: str | None = None, lx: float = 0, ly: float = 0, anchor: str = "middle") -> None:
    a(f'<g class="edge {kind}"><path d="{d}" marker-end="url(#ah-{kind})"/></g>')
    if label:
        text(lx, ly, label, "elabel", anchor)


def zone(x: float, y: float, w: float, h: float, label: str) -> None:
    a(f'<rect class="zone" x="{x}" y="{y}" width="{w}" height="{h}" rx="18"/>')
    text(x + 16, y + 22, label, "zlabel")


def bucket(cx: float, top: float, w: float, name: str, sub: str, tier: str, iceberg: bool = False) -> None:
    rx, ry, h = w / 2, 9, 78
    x0, x1 = cx - rx, cx + rx
    a(f'<g class="{tier}">')
    a(f'<path class="cyl" d="M{x0},{top} L{x0},{top+h} A{rx},{ry} 0 0 0 {x1},{top+h} L{x1},{top} Z"/>')
    a(f'<ellipse class="cyltop" cx="{cx}" cy="{top}" rx="{rx}" ry="{ry}"/>')
    a("</g>")
    text(cx, top + 32, name, "bname", "middle")
    text(cx, top + 50, sub, "bsub", "middle")
    if iceberg:
        image("iceberg", cx - 11, top + 55, 22, 22)


def tool(x: float, y: float, logo: str, name: str, sub: str, pad: float = 7) -> None:
    chip(x, y, 58, logo, pad=pad)
    text(x + 29, y + 76, name, "tname", "middle")
    if sub:
        text(x + 29, y + 92, sub, "tsub", "middle")


# Legend entries: (kind, label). Drawn as one row under the chart.
LEGEND = {
    "data": "Data or request flow",
    "ctl": "Control: launches, deploys, catalog metadata",
    "lin": "Lineage to governance",
    "opt": "Optional module (full preset)",
    "dev": "DEV stage",
    "prod": "PROD stage and its approval",
}


def legend(y: float, kinds: tuple[str, ...], x: float = 24) -> None:
    for kind in kinds:
        if kind in ("data", "ctl", "lin"):
            a(f'<g class="edge {kind}"><path d="M{x},{y} L{x+30},{y}" marker-end="url(#ah-{kind})"/></g>')
        else:
            cls = {"opt": "card opt", "dev": "ns dev", "prod": "ns prod"}[kind]
            a(f'<rect class="{cls}" x="{x}" y="{y-8}" width="30" height="16" rx="4"/>')
        label = LEGEND[kind]
        text(x + 40, y + 4, label, "legend")
        x += 40 + int(len(label) * 7.0) + 28


def export(name: str, width: int, height: int, title: str, desc: str) -> list[Path]:
    """Write `<name>-light.svg` and `<name>-dark.svg` from the current drawing."""
    body = "".join(svg)
    symbols = "".join(
        f'<symbol id="lg-{n}"><image href="{L[n]}" width="100%" height="100%" '
        'preserveAspectRatio="xMidYMid meet"/></symbol>'
        for n in sorted(used)
    )
    written: list[Path] = []
    for theme, tokens in THEMES.items():
        css = CSS
        for token, value in tokens.items():
            css = css.replace(f"var(--{token})", value)
        css = re.sub(r"\s+", " ", css).strip()
        doc = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" '
            f'height="{height}" role="img" aria-labelledby="t d">'
            f'<title id="t">{E(title)}</title><desc id="d">{E(desc)}</desc><style>{css}</style>'
            f'<defs>{symbols}</defs>'
            f'<rect class="bg" x="0.5" y="0.5" width="{width-1}" height="{height-1}" rx="20"/>{body}</svg>\n'
        )
        path = ASSETS / f"{name}-{theme}.svg"
        path.write_text(doc)
        written.append(path)
    reset()
    return written
