"""Prompt pieces for the tools the assistant can hand the family: their markers
and the results the browser sends back (SAI calculator, College Aid comparison).

Results arrive validated (``schemas``) and become plain prompt lines; nothing
here is stored.
"""

from __future__ import annotations

# The UI turns this line into a button that opens the calculator, and strips it
# from the text. Keep it in sync with app/lib/assistant/assistant-calculator-marker.ts.
CALCULATOR_MARKER = "[[calculator:fafsa_sai]]"
_CALCULATOR_NAMES = {"fafsa_sai": "FAFSA SAI calculator"}

# The UI turns this line into a button that opens the College Aid Explorer filtered
# to the places named. Region names are expanded to states by the UI; keep both in
# sync with app/lib/assistant/assistant-college-aid-places.ts.
COLLEGE_AID_MARKER = "[[college_aid:TX,east_coast]]"
COLLEGE_AID_ANYWHERE_MARKER = "[[college_aid]]"
COLLEGE_AID_REGIONS = (
    "east_coast", "west_coast", "northeast", "midwest", "central", "south", "west",
    "new_england", "mid_atlantic", "great_lakes", "plains", "rocky_mountains", "southeast", "southwest",
)


def _calculator_value(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def calculator_section(result: dict | None) -> str:
    """The family's calculator result as prompt lines, or a plain "(none)"."""
    if not result:
        return "(none)"
    values = result["values"]
    lines = [f"{_CALCULATOR_NAMES.get(result['type'], result['type'])}, award year {values.get('award_year') or 'unknown'}:"]
    lines += [f"- {key}: {_calculator_value(value)}" for key, value in values.items() if key != "award_year"]
    return "\n".join(lines)


def _money_range(low, high) -> str:
    if low is None or high is None:
        return "not enough data"
    return f"${low:,}/yr" if low == high else f"${low:,}-${high:,}/yr"


def college_aid_section(result: dict | None) -> str:
    """The colleges the family compared, one prompt line each, or a plain "(none)"."""
    if not result:
        return "(none)"
    lines = []
    for c in result["colleges"]:
        basis = {"in_state": " in-state", "out_of_state": " out-of-state"}.get(c.get("coa_basis"), "")
        coa = f"${c['coa']:,}{basis}" if c.get("coa") is not None else "not reported"
        need = f"{round(c['pct_need_met'] * 100)}%" if c.get("pct_need_met") is not None else "not reported"
        lines.append(
            f"- {c['name']} ({c['state']}, {c['control']}): cost of attendance {coa}; "
            f"net price after grants {_money_range(c.get('net_low'), c.get('net_high'))}; "
            f"family pays incl. loans and work-study {_money_range(c.get('family_low'), c.get('family_high'))}; "
            f"need met {need}"
        )
    return "\n".join(lines)
