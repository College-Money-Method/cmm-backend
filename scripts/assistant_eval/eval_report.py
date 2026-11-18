"""The markdown summary of one counselor eval run: totals, per mode, per theme, failures."""

from __future__ import annotations

from collections import defaultdict


def _rate(rows: list[dict]) -> str:
    passed = sum(r["passed"] for r in rows)
    return f"{passed}/{len(rows)} ({passed * 100 // max(len(rows), 1)}%)"


def _score(rows: list[dict]) -> str:
    scores = [r["verdict"]["score"] for r in rows if isinstance(r.get("verdict", {}).get("score"), int)]
    return f"{sum(scores) / len(scores):.1f}" if scores else "-"


def _hits(rows: list[dict]) -> str:
    hits = [r["checks"]["retrieval_hit"] for r in rows if r.get("checks", {}).get("retrieval_hit") is not None]
    return f"{sum(hits)}/{len(hits)}" if hits else "-"


def _problem(r: dict) -> str:
    if "error" in r:
        return r["error"]
    checks, verdict = r["checks"], r.get("verdict") or {}
    found = [f"{k}: {', '.join(map(str, checks[k]))}" for k in
             ("bad_citations", "invented_urls", "invented_money", "invented_contacts") if checks[k]]
    if verdict.get("violations"):
        found.append("violations: " + "; ".join(verdict["violations"]))
    if verdict.get("notes"):
        found.append(verdict["notes"])
    return " · ".join(found).replace("|", "/").replace("\n", " ")


def summary_markdown(meta: dict, results: list[dict]) -> str:
    graded = [r for r in results if "error" not in r]
    lines = [
        f"# Counselor assistant eval — {meta['school']} — {meta['run_at']}",
        "",
        (
            f"Model `{meta['model']}` · judge {'on' if meta['judge'] else 'off'}"
            + (f" · guidance file `{meta['guidance_file']}`" if meta["guidance_file"] else "")
        ),
        "",
        f"**Pass: {_rate(results)}** · avg score {_score(graded)} · retrieval hits {_hits(graded)}",
        (
            f"Content in place: {_rate([r for r in results if not r.get('needs_policy')])} · "
            f"waiting on CMM content (needs_policy): {_rate([r for r in results if r.get('needs_policy')])}"
        ),
        "",
        "## By mode",
        "",
        "| Mode | Pass | Avg score |",
        "|---|---|---|",
    ]
    by_mode: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_mode[r.get("mode", "?")].append(r)
    lines += [f"| {mode} | {_rate(rows)} | {_score(rows)} |" for mode, rows in sorted(by_mode.items())]

    lines += ["", "## By theme", "", "| Theme | Name | Mode | Pass | Avg score | Retrieval |", "|---|---|---|---|---|---|"]
    by_theme: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_theme[r.get("theme", "?")].append(r)
    for theme, rows in by_theme.items():
        first = rows[0]
        lines.append(f"| {theme} | {first.get('theme_name', '')} | {first.get('mode', '')} | {_rate(rows)} "
                     f"| {_score(rows)} | {_hits(rows)} |")

    failures = [r for r in results if not r["passed"]]
    lines += ["", f"## Failures ({len(failures)})", "", "| Case | Question | Problem |", "|---|---|---|"]
    lines += [f"| {r['id']} | {r.get('message', '')} | {_problem(r)} |" for r in failures]
    return "\n".join(lines) + "\n"
