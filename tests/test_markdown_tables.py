#!/usr/bin/env python3
"""Assistant answers must render as real tables, with distinct columns.

Reported symptom: table headers rendered run together --
"StoreLane avg timeDisastrous %Real revenueReal ordersAOV" -- instead of six
columns.

The markdown itself was never the problem. Every stored answer parses into a
proper <table>; the defect was CSS. `w-full` (width:100%) pinned the table to
its container, so the wrapper's overflow-x-auto could never engage, and with
`whitespace-nowrap` headers that cannot wrap the browser squeezed the columns
until the labels touched.

This is a presentation test: it checks the markdown pipeline still produces
tables, and pins the layout classes that keep the columns distinct. No backend
data logic is involved.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


src = open("frontend/src/components/Markdown.tsx").read()
pkg = json.load(open("frontend/package.json"))

# --- the parser must support GFM tables at all -----------------------------
deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
check("remark-gfm is a dependency", "remark-gfm" in deps)
check("remark-gfm is actually passed to ReactMarkdown",
      "remarkPlugins={[remarkGfm]}" in src)
check("table/thead/th/td all have renderers",
      all(f"{t}: (p)" in src for t in ("table", "thead", "th", "td")))

# --- the layout bug itself -------------------------------------------------
check("the table is NOT pinned to the container width with w-full",
      'className="w-full border-collapse' not in src)
check("the table may exceed its container so the wrapper can scroll",
      "w-max" in src and "min-w-full" in src)
check("a narrow table still fills the card", "min-w-full" in src)
check("the table sits in a horizontally scrolling wrapper",
      "overflow-x-auto" in src and "max-w-full" in src)

# --- columns must be visually distinct -------------------------------------
th_block = src[src.index("th: (p)"):src.index("td: (p)")]
td_block = src[src.index("td: (p)"):src.index("code: (")]
for label, block in (("header", th_block), ("body", td_block)):
    check(f"{label} cells carry a vertical rule", "border-r" in block)
    check(f"{label} cells drop the rule on the last column",
          "last:border-r-0" in block)
    check(f"{label} cells keep horizontal padding", "px-3" in block)
check("headers stay on one line so they are not broken mid-label",
      "whitespace-nowrap" in th_block)
check("numeric body cells stay tabular so columns align",
      "tabular-nums" in td_block)

# --- other block types must be untouched -----------------------------------
for el in ("p:", "h1:", "h2:", "h3:", "ul:", "ol:", "li:", "blockquote:", "code:"):
    check(f"{el.rstrip(':')} renderer still present", el in src)
check("code blocks still scroll independently of tables",
      "overflow-x-auto rounded-xl border border-line bg-app p-3" in src)

# --- the markdown pipeline still yields a real table -----------------------
SAMPLE = """Here are the stores:

| Store | Lane avg time | Disastrous % | Real revenue |
|---|---|---|---|
| **Nederland** | 210s | 25.0% | $2,666 |
| **Mission Bend** | 206s | 19.0% | $5,225 |

And a closing paragraph.
"""
probe = """
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
const md = process.argv[2]
const html = renderToStaticMarkup(
  React.createElement(ReactMarkdown, { remarkPlugins: [remarkGfm] }, md))
const n = (t) => (html.match(new RegExp('<' + t + '[ >]', 'g')) || []).length
const text = html.replace(/<[^>]*>/g, '')
console.log(JSON.stringify({ table: n('table'), th: n('th'), td: n('td'),
                             p: n('p'), strong: n('strong'),
                             leftover_pipes: (text.match(/\\|/g) || []).length }))
"""
open("frontend/.md_probe.mjs", "w").write(probe)
try:
    out = subprocess.run(["node", ".md_probe.mjs", SAMPLE], cwd="frontend",
                         capture_output=True, text=True, timeout=180)
    data = json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else {}
    check("a GFM table parses into a real <table>", data.get("table") == 1, str(data))
    check("every header cell becomes a <th>", data.get("th") == 4, str(data))
    check("every body cell becomes a <td>", data.get("td") == 8, str(data))
    check("no literal pipes survive into the rendered text",
          data.get("leftover_pipes") == 0, str(data))
    check("surrounding paragraphs still render", data.get("p", 0) >= 2, str(data))
    check("inline emphasis inside cells still renders",
          data.get("strong", 0) >= 2, str(data))
finally:
    if os.path.exists("frontend/.md_probe.mjs"):
        os.remove("frontend/.md_probe.mjs")

# --- it must still build ---------------------------------------------------
b = subprocess.run(["npm", "run", "build"], cwd="frontend",
                   capture_output=True, text=True, timeout=900)
check("the frontend builds", b.returncode == 0,
      (b.stderr or b.stdout)[-200:] if b.returncode else "")

passed = sum(1 for _, ok, _ in results if ok)
failed = len(results) - passed
print(f"\n{'='*66}\nmarkdown table rendering: {passed} passed, {failed} failed, {len(results)} total")
if failed:
    for n_, ok, d in results:
        if not ok:
            print(f"  FAILED: {n_} -- {d}")
sys.exit(1 if failed else 0)
