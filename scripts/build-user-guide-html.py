#!/usr/bin/env python3
"""
Render docs/LAMA-USER-GUIDE.md -> docs/setup-guide.html, styled with LAMA's
own design tokens so the printed PDF is visually the same product as the app.

Tokens below are transcribed from frontend/src/index.css. The brand rule in
that file is load-bearing and is honoured here:

    "Yellow is a FILL and an ACCENT BAR. Never text on a light surface,
     and never a focus ring - it measures 1.27:1 on white."

    ./.venv/bin/python scripts/build-user-guide-html.py
"""
import html
import re
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "LAMA-USER-GUIDE.md"
OUT = ROOT / "docs" / "setup-guide.html"

# ── LAMA design tokens — mirrored from frontend/src/index.css ──────────────
CSS = """
:root{
  /* Surfaces */
  --bg:#f6f6fa; --surface:#ffffff; --surface-2:#f2f2f7; --surface-3:#e9e9f0;
  --border:#e6e6e6; --border-strong:#c9c9d2;
  /* Text */
  --fg:#2e2e38; --fg-muted:#4a4a57; --fg-subtle:#5e5e6b;
  --fg-onDark:#f4f4f7; --fg-onDark-muted:#b8b8c4; --fg-onDark-subtle:#8a8a99;
  /* Brand — FILL and ACCENT BAR only, never text on a light surface */
  --brand:#ffe600; --brand-hover:#f0d900; --brand-fg:#2e2e38;
  --brand-tint:#fffce6; --brand-edge:#d4c000;
  /* Ink */
  --ink:#2e2e38; --ink-hover:#1a1a24; --ink-fg:#ffffff;
  /* Status */
  --ok:#0f6b4f; --ok-bg:#e4f2ec; --ok-edge:#7cbfa6;
  --warn:#8a5a00; --warn-bg:#fbf2df; --warn-edge:#d4ac55;
  --crit:#b3200e; --crit-bg:#fbeae7; --crit-edge:#d98577;
  --info:#1f4f87; --info-bg:#e7eef7; --info-edge:#86aad4;
  /* Radii — 4 / 6 / 10 */
  --radius-sm:4px; --radius:6px; --radius-lg:10px;
  /* Type */
  --sans:"IBM Plex Sans",system-ui,-apple-system,sans-serif;
  --display:"Chivo","IBM Plex Sans",sans-serif;
  --mono:"IBM Plex Mono",Menlo,monospace;
}
*{box-sizing:border-box;-webkit-print-color-adjust:exact;print-color-adjust:exact}
@page{size:A4;margin:15mm 13mm 14mm}
@page cover{size:A4;margin:0}
.page-cover{page:cover}
html,body{margin:0;padding:0}
body{font-family:var(--sans);color:var(--fg);background:var(--surface);
     font-size:9.5pt;line-height:1.58;-webkit-font-smoothing:antialiased}

/* Headings use Chivo with the app's negative tracking. */
h1,h2,h3,h4,h5{font-family:var(--display);letter-spacing:-.01em;margin:0}

/* Keep atomic blocks whole across page boundaries. */
pre,.callout,.cover-card,.toc-group,.kicker{break-inside:avoid}
/* Long tables may split across pages - keeping them whole strands half a page.
   Rows stay atomic and the header repeats on each continuation. */
tr,h2,h3,h4{break-inside:avoid}
thead{display:table-header-group}
tbody{break-inside:auto}
h2,h3,h4{break-after:avoid}

/* ── Cover ─────────────────────────────────────────────────────────────── */
.cover{position:relative;height:297mm;background:var(--ink);color:var(--fg-onDark);
       padding:26mm 20mm;overflow:hidden}
/* Yellow as a fill: a single wide accent bar, the way the app uses it. */
.cover .accent{position:absolute;left:0;top:0;width:100%;height:9mm;background:var(--brand)}
.brand{display:flex;align-items:center;gap:10px;margin-top:4mm}
.brand .mark{width:13mm;height:13mm;border-radius:var(--radius-sm);background:var(--brand);
       color:var(--brand-fg);font-family:var(--display);font-weight:700;font-size:20pt;
       display:flex;align-items:center;justify-content:center;line-height:1}
.brand .wm{font-family:var(--display);font-weight:700;font-size:21pt;
       letter-spacing:-.02em;line-height:1;color:#fff}
.brand .tag{font-size:6.8pt;letter-spacing:.18em;text-transform:uppercase;
       color:var(--fg-onDark-muted);margin-top:2mm;line-height:1.35}
.cover h1{font-size:38pt;font-weight:700;line-height:1.05;margin:44mm 0 0;
       max-width:150mm;color:#fff}
.cover .lede{font-size:11.5pt;color:var(--fg-onDark-muted);max-width:130mm;
       margin-top:8mm;line-height:1.62}
.cover-cards{display:flex;gap:5mm;margin-top:15mm}
.cover-card{flex:1;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.14);
       border-radius:var(--radius-lg);padding:6mm 5mm}
.cover-card .k{font-family:var(--mono);font-size:7pt;letter-spacing:.14em;
       color:var(--brand);text-transform:uppercase}
.cover-card .t{font-family:var(--display);font-weight:700;font-size:12pt;
       margin-top:2.5mm;color:#fff}
.cover-card .d{font-size:8.4pt;color:var(--fg-onDark-muted);margin-top:1.5mm;line-height:1.5}
.cover .strip{position:absolute;left:20mm;right:20mm;bottom:34mm;
       display:flex;gap:0;align-items:stretch}
.cover .strip .s{flex:1;border-top:2px solid rgba(255,255,255,.14);padding-top:3mm}
.cover .strip .s:first-child{border-top-color:var(--brand)}
.cover .strip .s .sn{font-family:var(--mono);font-size:6.6pt;color:var(--fg-onDark-subtle);
       letter-spacing:.12em}
.cover .strip .s .st{font-family:var(--display);font-weight:700;font-size:8.6pt;
       color:var(--fg-onDark-muted);margin-top:1mm}
.cover .foot{position:absolute;left:20mm;right:20mm;bottom:18mm;display:flex;
       justify-content:space-between;align-items:flex-end;
       border-top:1px solid rgba(255,255,255,.16);padding-top:4.5mm;
       font-family:var(--mono);font-size:7.6pt;color:var(--fg-onDark-subtle)}

/* ── Part pages ────────────────────────────────────────────────────────── */
.part{page-break-before:always}
.part:first-of-type{page-break-before:avoid}
.kicker{border-bottom:2px solid var(--border);padding-bottom:4mm;margin-bottom:7mm}
.kicker .eyebrow{font-family:var(--mono);font-size:7pt;letter-spacing:.17em;
       text-transform:uppercase;color:var(--fg-subtle);display:flex;
       align-items:center;gap:3mm}
/* Yellow accent bar — a fill, not text. */
.kicker .eyebrow::before{content:"";width:9mm;height:3px;background:var(--brand);
       border-radius:2px;display:inline-block}
.kicker h1{font-size:25pt;font-weight:700;color:var(--fg);margin-top:3mm;line-height:1.12}

h2{font-size:14pt;font-weight:700;color:var(--fg);margin:9mm 0 3mm}
h3{font-size:10.6pt;font-weight:700;color:var(--fg);margin:6.5mm 0 2mm}
h4{font-size:9.5pt;font-weight:700;color:var(--fg-muted);margin:5mm 0 1.5mm}
p{margin:0 0 2.8mm}
a{color:var(--info);text-decoration:none}
strong{font-weight:600;color:var(--fg)}
hr{border:0;border-top:1px solid var(--border);margin:7mm 0}

ul,ol{margin:0 0 3mm;padding-left:5.5mm}
li{margin-bottom:1.4mm}
li::marker{color:var(--fg-subtle)}

/* ── Tables ────────────────────────────────────────────────────────────── */
table{width:100%;border-collapse:collapse;margin:0 0 4mm;font-size:8.6pt}
thead th{background:var(--surface-2);color:var(--fg);font-weight:600;text-align:left;
       font-size:7.8pt;letter-spacing:.04em;text-transform:uppercase;
       padding:2.2mm 2.6mm;border-bottom:1px solid var(--border-strong)}
tbody td{padding:2.2mm 2.6mm;border-bottom:1px solid var(--border);
       vertical-align:top;line-height:1.5}
tbody tr:nth-child(even){background:var(--surface-2)}
table code{font-size:7.9pt}

/* ── Code ──────────────────────────────────────────────────────────────── */
pre{font-family:var(--mono);font-size:8pt;line-height:1.6;background:var(--ink);
    color:var(--fg-onDark);border-radius:var(--radius);padding:4mm 4.5mm;
    margin:0 0 3.5mm;white-space:pre-wrap;word-wrap:break-word;
    border-left:3px solid var(--brand)}
pre code{background:none;padding:0;color:inherit;font-size:inherit;border:0}
code{font-family:var(--mono);font-size:8.3pt;background:var(--surface-2);
     border:1px solid var(--border);border-radius:var(--radius-sm);
     padding:.4mm 1.1mm;color:var(--fg)}

/* ── Callouts (from blockquotes) ───────────────────────────────────────── */
.callout{border-radius:var(--radius);padding:3.4mm 4mm;margin:0 0 3.5mm;
     font-size:8.8pt;line-height:1.55;border:1px solid;border-left-width:3px}
.callout p:last-child{margin-bottom:0}
.callout .lbl{font-family:var(--mono);font-size:6.8pt;letter-spacing:.15em;
     text-transform:uppercase;display:block;margin-bottom:1.4mm;font-weight:500}
.callout code{background:rgba(255,255,255,.65)}
.c-note{background:var(--info-bg);border-color:var(--info-edge);color:var(--fg)}
.c-note .lbl{color:var(--info)}
.c-warn{background:var(--warn-bg);border-color:var(--warn-edge);color:var(--fg)}
.c-warn .lbl{color:var(--warn)}
.c-crit{background:var(--crit-bg);border-color:var(--crit-edge);color:var(--fg)}
.c-crit .lbl{color:var(--crit)}

/* Project-type identity block. Brand yellow used the way the app uses it:
   a tint fill and an accent bar, never as text. */
.typecard{background:var(--brand-tint);border:1px solid var(--brand-edge);
     border-left:4px solid var(--brand);border-radius:var(--radius);
     padding:3.4mm 4mm;margin:0 0 4mm}
.typecard p{margin:0}
.typecard .tl{font-family:var(--mono);font-size:8.4pt;color:var(--fg);
     font-weight:500;letter-spacing:.01em}
.typecard .ds{font-size:8.7pt;color:var(--fg-muted);margin-top:1.4mm;line-height:1.5}

/* ── Contents page ─────────────────────────────────────────────────────── */
.toc{page-break-after:always}
.toc-group{margin-bottom:6mm}
.toc-group .gh{font-family:var(--mono);font-size:7pt;letter-spacing:.16em;
     text-transform:uppercase;color:var(--fg-subtle);margin-bottom:2.5mm;
     display:flex;align-items:center;gap:3mm}
.toc-group .gh::before{content:"";width:7mm;height:3px;background:var(--brand);
     border-radius:2px;display:inline-block}
.toc-row{display:flex;gap:4mm;padding:2.2mm 0;border-bottom:1px solid var(--border);
     align-items:baseline}
.toc-row .n{font-family:var(--mono);font-size:8.4pt;color:var(--fg-subtle);
     min-width:7mm;font-weight:500}
.toc-row .t{font-family:var(--display);font-weight:700;font-size:10pt;color:var(--fg)}
.toc-row .d{font-size:8.2pt;color:var(--fg-muted);margin-left:auto;text-align:right;
     max-width:82mm;line-height:1.45}
details{margin:0 0 3mm}
summary{font-weight:600;font-size:8.8pt;color:var(--fg-muted);margin-bottom:2mm}
"""

# ── Cover + contents, hand-built ───────────────────────────────────────────
COVER = """
<section class="page-cover">
  <div class="cover">
    <div class="accent"></div>
    <div class="brand">
      <div class="mark">L</div>
      <div>
        <div class="wm">LAMA</div>
        <div class="tag">Legacy Application<br>Modernisation AI Studio</div>
      </div>
    </div>
    <h1>Prerequisites &amp;<br>User Guide</h1>
    <div class="lede">Everything needed to go from a bare machine to a working
      migration — what to install, how to install it, and how to use the
      product once it is running.</div>
    <div class="cover-cards">
      <div class="cover-card"><div class="k">Install</div><div class="t">Windows</div>
        <div class="d">Docker or local terminal, with the WSL&nbsp;2, HOME and line-ending specifics.</div></div>
      <div class="cover-card"><div class="k">Install</div><div class="t">macOS</div>
        <div class="d">Docker or local terminal, including Apple Silicon.</div></div>
      <div class="cover-card"><div class="k">Install</div><div class="t">Linux</div>
        <div class="d">Docker Engine or local terminal, plus server hardening.</div></div>
    </div>
    <div class="strip">
      <div class="s"><div class="sn">01</div><div class="st">Discovery</div></div>
      <div class="s"><div class="sn">02</div><div class="st">Data Model</div></div>
      <div class="s"><div class="sn">03</div><div class="st">Architecture</div></div>
      <div class="s"><div class="sn">04</div><div class="st">CodeGen</div></div>
      <div class="s"><div class="sn">05</div><div class="st">Living</div></div>
    </div>
    <div class="foot"><span>Four project types · five stages · Windows · macOS · Linux</span>
      <span>{date}</span></div>
  </div>
</section>
"""

TOC = """
<section class="toc">
  <div class="kicker"><div class="eyebrow">Contents</div>
    <h1>What is in this guide</h1></div>
  <div class="toc-group"><div class="gh">Getting installed</div>
    <div class="toc-row"><span class="n">1</span><span class="t">What LAMA is</span><span class="d">The problem it solves, its features, the four project types</span></div>
    <div class="toc-row"><span class="n">2</span><span class="t">Prerequisites</span><span class="d">Docker vs local, versions, what to have ready</span></div>
    <div class="toc-row"><span class="n">3</span><span class="t">Installation on Windows</span><span class="d">Both paths, plus Windows-specific troubleshooting</span></div>
    <div class="toc-row"><span class="n">4</span><span class="t">Installation on macOS</span><span class="d">Both paths, plus Apple Silicon notes</span></div>
    <div class="toc-row"><span class="n">5</span><span class="t">Installation on Linux</span><span class="d">Both paths, plus running it as a team server</span></div>
    <div class="toc-row"><span class="n">6</span><span class="t">First boot and sign-in</span><span class="d">Including the LLM provider setup, which is mandatory</span></div>
  </div>
  <div class="toc-group"><div class="gh">Using the product</div>
    <div class="toc-row"><span class="n">7</span><span class="t">Using LAMA</span><span class="d">The workspace, all five stages, the other project types, the admin pages</span></div>
  </div>
  <div class="toc-group"><div class="gh">Keeping it running</div>
    <div class="toc-row"><span class="n">8</span><span class="t">Day-to-day operations</span><span class="d">Lifecycle, logs, backup, upgrade</span></div>
    <div class="toc-row"><span class="n">9</span><span class="t">Troubleshooting</span><span class="d">General, plus a table per platform</span></div>
    <div class="toc-row"><span class="n">10</span><span class="t">Reference</span><span class="d">Settings, ports, glossary, verification notes</span></div>
  </div>
  <p style="margin-top:7mm;color:var(--fg-muted);font-size:8.8pt">Each operating-system
  section covers <strong>both</strong> installation paths — Docker and local terminal — and
  ends with troubleshooting specific to that platform. Read only your own.</p>
</section>
"""


def classify(text: str) -> tuple[str, str]:
    """Pick a callout severity from a blockquote's own words."""
    t = text.lower()
    if any(k in t for k in ("destroys", "never in production", "do not freeze",
                            "change it before", "published default",
                            "do not expose", "back up first")):
        return "c-crit", "Important"
    if any(k in t for k in ("do not", "does not", "never", "must", "required",
                            "matters", "warning", "fails", "gotcha", "⚠")):
        return "c-warn", "Watch out"
    return "c-note", "Note"


# A project-type tagline block is a blockquote holding a single paragraph that
# opens with the italic tagline, e.g.
#     > *Cross-stack transformation*
#     > KB-anchored per-file transformation ...
# It is product identity, not an aside, so it must not become a "Note" card.
TAGLINE = re.compile(
    r"<blockquote>\s*<p><em>(?P<tag>.*?)</em>\s*(?:<br\s*/?>)?\s*(?P<desc>.*?)</p>\s*</blockquote>",
    re.S,
)


def to_callouts(html_str: str) -> str:
    """Blockquotes become themed cards: identity blocks first, then callouts."""
    def as_typecard(m):
        desc = m.group("desc").strip()
        tail = f'<div class="ds">{desc}</div>' if desc else ""
        return f'<div class="typecard"><div class="tl">{m.group("tag")}</div>{tail}</div>'

    html_str = TAGLINE.sub(as_typecard, html_str)

    def as_callout(m):
        inner = m.group(1)
        cls, label = classify(re.sub(r"<[^>]+>", " ", inner))
        return f'<div class="callout {cls}"><span class="lbl">{label}</span>{inner}</div>'
    return re.sub(r"<blockquote>(.*?)</blockquote>", as_callout, html_str, flags=re.S)


def build() -> None:
    md_text = SRC.read_text(encoding="utf-8")

    # Drop the markdown front matter: title block, the plain-text Contents list
    # and the "How to read" table. The cover and the designed TOC replace them.
    body = md_text.split("\n# Part 1", 1)
    if len(body) != 2:
        raise SystemExit("Could not find '# Part 1' — has the guide's structure changed?")
    md_body = "# Part 1" + body[1]

    # Split into Parts so each one can start on a fresh page with a header.
    chunks = re.split(r"^# (Part \d+) — (.+)$", md_body, flags=re.M)[1:]
    if not chunks:
        raise SystemExit("No '# Part N — Title' headings found.")

    conv = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists",
                                         "md_in_html", "attr_list"])

    sections = []
    for i in range(0, len(chunks), 3):
        eyebrow, title, content = chunks[i], chunks[i + 1], chunks[i + 2]
        conv.reset()
        inner = to_callouts(conv.convert(content))
        # The guide's trailing '---' rules become page furniture we do not want.
        inner = re.sub(r"<hr\s*/?>\s*(?=(<h2|$))", "", inner)
        sections.append(
            f'<section class="part">'
            f'<div class="kicker"><div class="eyebrow">{html.escape(eyebrow)}</div>'
            f"<h1>{html.escape(title)}</h1></div>\n{inner}\n</section>"
        )

    from datetime import date
    page = (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<title>LAMA — Prerequisites &amp; User Guide</title>\n"
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        '<link href="https://fonts.googleapis.com/css2?'
        "family=Chivo:wght@400;700&family=IBM+Plex+Sans:wght@400;500;600&"
        'family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">\n'
        f"<style>{CSS}</style>\n</head>\n<body>\n"
        + COVER.replace("{date}", date.today().strftime("%B %Y"))
        + TOC
        + "\n".join(sections)
        + "\n</body>\n</html>\n"
    )
    OUT.write_text(page, encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)} "
          f"({OUT.stat().st_size / 1024:.0f} KB, {len(sections)} parts)")


if __name__ == "__main__":
    build()
