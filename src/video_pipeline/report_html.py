"""Local contact sheet for eyeballing a pipeline run.

Scanning 150 frames in a grid takes a minute; clicking through a folder takes
twenty. This is also the view the admin monitoring screen needs later — each
chapter beside the frame it was derived from.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

from src.video_pipeline.chapter_build import Chapter
from src.video_pipeline.frame_classify import ERROR
from src.video_pipeline.transcript import format_timestamp

_CSS = """
:root { color-scheme: light dark;
  --bg:#f2f5f7; --card:#fff; --ink:#101319; --dim:#5c6876; --rule:#d3dae3;
  --accent:#1b47c4; --warn:#8a5a00; --bad:#a81f35; }
@media (prefers-color-scheme: dark) { :root {
  --bg:#0c0f14; --card:#14181f; --ink:#e7ecf3; --dim:#8a97a6; --rule:#262d38;
  --accent:#7c9bff; --warn:#d9a227; --bad:#ec6b7d; } }
* { box-sizing:border-box }
body { margin:0; padding:2rem; background:var(--bg); color:var(--ink);
  font:15px/1.55 system-ui,-apple-system,sans-serif }
h1 { font-size:1.5rem; margin:0 0 .25rem }
h2 { font-size:1rem; margin:2.5rem 0 .75rem; text-transform:uppercase;
  letter-spacing:.08em; color:var(--dim) }
.sub { color:var(--dim); margin:0 0 1.5rem; font-variant-numeric:tabular-nums }
.summary { display:flex; flex-wrap:wrap; gap:1px; background:var(--rule);
  border:1px solid var(--rule); border-radius:4px; overflow:hidden }
.summary div { background:var(--card); padding:.7rem 1rem; flex:1 1 8rem }
.summary dt { font-size:.66rem; text-transform:uppercase; letter-spacing:.09em;
  color:var(--dim) }
.summary dd { margin:.25rem 0 0; font-size:1.05rem; font-weight:600;
  font-variant-numeric:tabular-nums }
table { border-collapse:collapse; width:100%; background:var(--card);
  border:1px solid var(--rule); border-radius:4px; overflow:hidden }
td,th { text-align:left; padding:.5rem .8rem; border-bottom:1px solid var(--rule) }
tr:last-child td { border-bottom:0 }
th { font-size:.66rem; text-transform:uppercase; letter-spacing:.09em;
  color:var(--dim) }
.tc { font-family:ui-monospace,Menlo,monospace; font-variant-numeric:tabular-nums;
  white-space:nowrap }
.grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr));
  gap:1rem }
figure { margin:0; background:var(--card); border:1px solid var(--rule);
  border-radius:4px; overflow:hidden }
figure.title_card { border-color:var(--accent); border-width:2px }
figure.error { border-color:var(--bad); border-width:2px }
img { width:100%; display:block; background:#000 }
figcaption { padding:.55rem .7rem; font-size:.82rem }
.meta { display:flex; justify-content:space-between; gap:.5rem; color:var(--dim);
  font-family:ui-monospace,Menlo,monospace; font-size:.72rem }
.heading { margin-top:.3rem; font-weight:600 }
.warn { color:var(--warn) } .bad { color:var(--bad) }
"""


def _summary_cell(label: str, value: str, cls: str = "") -> str:
    klass = f' class="{cls}"' if cls else ""
    return (
        f"<div><dt>{escape(label)}</dt>"
        f"<dd{klass}>{escape(value)}</dd></div>"
    )


def render_report(
    *,
    out_dir: Path,
    source_name: str,
    trim: dict,
    frames: list[dict],
    chapters: list[Chapter],
    frames_subdir: str = "frames",
) -> Path:
    """Write report.html into `out_dir` and return its path."""
    offset = float(trim.get("offset") or 0.0)
    fallback = bool(trim.get("fallback"))
    errors = sum(1 for frame in frames if frame.get("type") == ERROR)

    cells = [
        _summary_cell("Candidates", str(len(frames))),
        _summary_cell("Chapters", str(len(chapters))),
        _summary_cell(
            "Trim offset",
            f"{format_timestamp(offset)} ({offset:.2f}s)",
            "warn" if fallback else "",
        ),
        _summary_cell(
            "Detection", "FALLBACK" if fallback else "ok", "warn" if fallback else ""
        ),
        _summary_cell("Unreadable", str(errors), "bad" if errors else ""),
    ]

    chapter_rows = "".join(
        f'<tr><td class="tc">{format_timestamp(c.timecode)}</td>'
        f"<td>{escape(c.title)}</td>"
        f'<td class="tc">{escape(c.source)}</td></tr>'
        for c in chapters
    ) or '<tr><td colspan="3">No chapters produced.</td></tr>'

    tiles = []
    for frame in frames:
        ftype = str(frame.get("type") or "")
        heading = str(frame.get("heading") or "")
        error = str(frame.get("error") or "")
        note = (
            f'<div class="heading">{escape(heading)}</div>' if heading
            else (f'<div class="heading bad">{escape(error)}</div>' if error else "")
        )
        tiles.append(
            f'<figure class="{escape(ftype)}">'
            f'<img loading="lazy" src="{frames_subdir}/{escape(str(frame["file"]))}" '
            f'alt="frame at {format_timestamp(float(frame["timestamp"]))}">'
            f"<figcaption><div class=\"meta\">"
            f'<span>{format_timestamp(float(frame["timestamp"]))}</span>'
            f"<span>{escape(ftype)}</span></div>{note}</figcaption></figure>"
        )

    reason = str(trim.get("reason") or "")
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pipeline run — {escape(source_name)}</title><style>{_CSS}</style></head>
<body>
<h1>Pipeline run</h1>
<p class="sub">{escape(source_name)}</p>
<dl class="summary">{''.join(cells)}</dl>
<h2>Trim point</h2>
<p class="sub">{escape(reason) or 'no reason recorded'}</p>
<h2>Chapters as they would be published</h2>
<table><thead><tr><th>Timecode</th><th>Title</th><th>Rule</th></tr></thead>
<tbody>{chapter_rows}</tbody></table>
<h2>Candidate frames &mdash; {len(frames)}</h2>
<div class="grid">{''.join(tiles)}</div>
</body></html>
"""
    dest = out_dir / "report.html"
    dest.write_text(html, encoding="utf-8")
    return dest
