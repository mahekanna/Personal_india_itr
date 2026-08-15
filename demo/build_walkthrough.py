#!/usr/bin/env python3
"""Bundle the running application's real pages into one browsable HTML file.

    python demo/seed_demo.py
    python run.py --port 8790 --no-browser &
    python demo/build_walkthrough.py --port 8790 --out demo/walkthrough.html

This is not a mock-up. Every screen below is the actual HTML the application
served, with its own stylesheet inlined; only the navigation between screens is
added, because a single file has nowhere to navigate to. Forms are left in place
and made inert — they show the real controls without pretending to submit.
"""

from __future__ import annotations

import argparse
import re
import urllib.request
from html import escape
from pathlib import Path

SCREENS = [
    ("home", "Start", "/", "Your returns, and a what-if regime check"),
    ("documents", "1 · Documents", "/returns/{filing}/documents",
     "Drop everything in at once — nothing needs labelling"),
    ("review", "2 · Review", "/returns/{filing}/review",
     "Every extracted figure, with its confidence, before it touches the return"),
    ("income", "3 · Income", "/returns/{filing}/income",
     "Pre-filled from the documents, and editable"),
    ("foreign", "4 · Foreign", "/returns/{filing}/foreign",
     "Vesting, ESPP, dividends, exchange rates and Schedule FA"),
    ("compare", "5 · Regime", "/returns/{filing}/compare",
     "Both regimes computed in full, line by line"),
    ("file", "6 · File", "/returns/{filing}/file",
     "The ITR form, the JSON, and what to do in order"),
    ("planner", "Advance tax", "/returns/{planning}/planner?as_of=2025-12-01",
     "The year in progress — what to pay by 15 December"),
]


def fetch(base: str, path: str) -> str:
    with urllib.request.urlopen(base + path, timeout=30) as response:
        return response.read().decode("utf-8")


def body_of(html: str) -> str:
    match = re.search(r"<main class=\"shell\">(.*?)</main>", html, re.S)
    shell = match.group(1) if match else ""
    stepper = re.search(r"<nav class=\"stepper\".*?</nav>", html, re.S)
    return (stepper.group(0) if stepper else "") + shell


def build(base: str, filing: str, planning: str) -> str:
    css = fetch(base, "/static/app.css")

    panels = []
    tabs = []
    for index, (key, label, path, blurb) in enumerate(SCREENS):
        page = fetch(base, path.format(filing=filing, planning=planning))
        tabs.append(
            f'<button class="tab" data-screen="{key}" role="tab" '
            f'aria-selected="{"true" if index == 0 else "false"}" '
            f'aria-controls="screen-{key}">{escape(label)}</button>'
        )
        panels.append(
            f'<section class="screen" id="screen-{key}" data-screen="{key}" '
            f'role="tabpanel" aria-label="{escape(label)}"'
            f'{"" if index == 0 else " hidden"}>'
            f'<p class="screen-blurb">{escape(blurb)}</p>'
            f'<div class="frame">{body_of(page)}</div>'
            f"</section>"
        )

    return TEMPLATE.format(
        css=css, tabs="\n".join(tabs), panels="\n".join(panels)
    )


TEMPLATE = """<title>Personal India ITR Walkthrough</title>
<style>
/* ---- the application's own stylesheet, verbatim ---------------------- */
{css}

/* ---- the shell added around it --------------------------------------- */
:root {{
  --shell-ground: #0e1f18;
  --shell-ground-soft: #163025;
  --shell-ink: #eef2ee;
  --shell-ink-dim: #9db3a7;
  --shell-line: #234435;
}}
html {{ background: var(--shell-ground); }}
body {{
  background: var(--shell-ground);
  color: var(--shell-ink);
  margin: 0;
}}
.walk-head {{
  padding: 1.3rem 1.5rem .9rem;
  max-width: 1180px;
  margin: 0 auto;
}}
.walk-head h1 {{
  font-size: 1.5rem; font-weight: 640; letter-spacing: -.02em;
  color: var(--shell-ink); margin: 0 0 .35rem; text-wrap: balance;
}}
.walk-head p {{
  color: var(--shell-ink-dim); font-size: .93rem; max-width: 62ch;
  margin: 0 0 .3rem; line-height: 1.55;
}}
.walk-note {{
  display: inline-flex; align-items: center; gap: .45rem;
  margin-top: .8rem; padding: .4rem .7rem;
  border: 1px solid var(--shell-line); border-radius: 999px;
  background: var(--shell-ground-soft);
  color: var(--shell-ink-dim); font-size: .78rem;
}}
.walk-note b {{ color: var(--shell-ink); font-weight: 600; }}

.tabs {{
  position: sticky; top: 0; z-index: 20;
  display: flex; gap: .3rem; overflow-x: auto;
  padding: .7rem 1.5rem;
  background: rgba(14, 31, 24, .94);
  backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--shell-line);
}}
.tabs-inner {{ display: flex; gap: .3rem; max-width: 1180px;
  margin: 0 auto; width: 100%; }}
.tab {{
  flex: 0 0 auto;
  padding: .42rem .8rem; border-radius: 7px;
  border: 1px solid transparent; background: transparent;
  color: var(--shell-ink-dim); font: inherit; font-size: .84rem;
  font-weight: 550; cursor: pointer; white-space: nowrap;
  font-variant-numeric: tabular-nums;
}}
.tab:hover {{ background: var(--shell-ground-soft); color: var(--shell-ink); }}
.tab[aria-selected="true"] {{
  background: var(--shell-ink); color: var(--shell-ground);
  border-color: var(--shell-ink);
}}
.tab:focus-visible {{ outline: 2px solid #6fd3a4; outline-offset: 2px; }}

.screen {{ max-width: 1180px; margin: 0 auto; padding: 1.2rem 1.5rem 3rem; }}
.screen-blurb {{
  color: var(--shell-ink-dim); font-size: .87rem;
  margin: 0 0 .9rem; padding-left: .1rem;
}}
/* The application paints itself on a light ground; give it that ground
   explicitly rather than letting it inherit the shell's. */
.frame {{
  background: #f6f7f5;
  color: #16181d;
  border: 1px solid var(--shell-line);
  border-radius: 12px;
  overflow: hidden;
  box-shadow: 0 24px 60px -30px rgba(0,0,0,.65);
}}
.frame :where(h1, h2, h3) {{ color: #16181d; }}
/* The page's own <main> wrapper is dropped when the body is extracted, so its
   horizontal padding is restored here. The stepper brings its own. */
.frame > :not(.stepper) {{ padding-inline: 1.5rem; }}
.frame > h1 {{ padding-top: 1.4rem; }}
.frame::after {{ content: ""; display: block; height: 1.5rem; }}

.walk-foot {{
  max-width: 1180px; margin: 0 auto;
  padding: 1.4rem 1.5rem 3rem;
  color: var(--shell-ink-dim); font-size: .82rem;
  border-top: 1px solid var(--shell-line);
  line-height: 1.6;
}}
.walk-foot code {{
  background: var(--shell-ground-soft); padding: .1rem .35rem;
  border-radius: 4px; color: var(--shell-ink);
}}
@media (prefers-reduced-motion: reduce) {{
  * {{ animation: none !important; transition: none !important; }}
}}
</style>

<header class="walk-head">
  <h1>Personal India ITR — the interface, screen by screen</h1>
  <p>
    Every panel below is the real HTML the application served, with its own
    stylesheet inlined. Only the navigation between screens is added, because a
    single file has nowhere to navigate to.
  </p>
  <p>
    An invented Bengaluru engineer, AY 2026-27 — a four-year grant vesting
    quarterly, an ESPP purchase with a lookback, dividends part reinvested, two
    sales, an Indian dividend, a home loan. No real PAN, TAN or account number
    appears anywhere. <b style="color:var(--shell-ink)">Forms are inert:</b>
    they show the real controls, but nothing submits.
  </p>
</header>

<nav class="tabs" role="tablist" aria-label="Screens">
  <div class="tabs-inner">
{tabs}
  </div>
</nav>

{panels}

<footer class="walk-foot">
  Run it yourself with <code>python demo/seed_demo.py</code> then
  <code>python run.py</code>. It binds to localhost only — a Form 16 has no
  business being reachable from the network.
</footer>

<script>
const tabs = document.querySelectorAll(".tab");
const screens = document.querySelectorAll(".screen");
function show(key) {{
  tabs.forEach(t => t.setAttribute("aria-selected", String(t.dataset.screen === key)));
  screens.forEach(s => s.hidden = s.dataset.screen !== key);
  window.scrollTo({{ top: 0, behavior: "instant" }});
}}
tabs.forEach(tab => tab.addEventListener("click", () => show(tab.dataset.screen)));

// Keep the real controls visible but harmless.
document.querySelectorAll(".frame form").forEach(form => {{
  form.addEventListener("submit", event => event.preventDefault());
}});
document.querySelectorAll(".frame a").forEach(link => {{
  const href = link.getAttribute("href") || "";
  const match = href.match(/\\/(documents|review|income|foreign|compare|file|planner)/);
  link.addEventListener("click", event => {{
    event.preventDefault();
    if (match) show(match[1]);
    else if (href === "/") show("home");
  }});
}});
</script>
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--filing", required=True)
    parser.add_argument("--planning", required=True)
    parser.add_argument("--out", default="demo/walkthrough.html")
    args = parser.parse_args()

    html = build(f"http://127.0.0.1:{args.port}", args.filing, args.planning)
    Path(args.out).write_text(html, encoding="utf-8")
    print(f"{args.out}  ({len(html) // 1024} KB)")


if __name__ == "__main__":
    main()
