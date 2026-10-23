# aggregator/outlet_reporting.py
"""How reliable an outlet's reporting is, on five levels.

A hand-kept grade, separate from lean (Philip, 2026-10-23). Lean says where
an outlet sits left to right; this says how far its reporting can be taken
at face value, which is the more useful signal for outlets AllSides doesn't
rate and for the ones abroad.

Each grade is our own call, set by reading two public ratings side by side.
We don't copy either dataset:
  - Media Bias/Fact Check's "Factual Reporting" (Very High ... Very Low)
  - Ad Fontes Media's reliability score (0-64; 40+ is "reliable")
The levels follow MBFC's scale (Philip asked for more than three, since the
sources spread widely), with its Very Low folded into Low. A grade is MBFC's
level, moved one step where Ad Fontes clearly disagrees: up when Ad Fontes
scores the outlet 42 or more (or 40+ against an MBFC Mixed), down when it
scores under 32. Those moves are marked below. Looked up 2026-10-23 on each
outlet's MBFC and Ad Fontes page.

An outlet missing from GRADES has no grade and shows none; nothing is ever
guessed by an LLM. Names are as normalize_source_name() leaves them.
"""

VERY_HIGH, HIGH, MOSTLY, MIXED, LOW = "very_high", "high", "mostly_factual", "mixed", "low"

# Best first; the API and templates use these keys.
LEVELS = (VERY_HIGH, HIGH, MOSTLY, MIXED, LOW)

GRADES = {
    # Wires and public media
    "Associated Press": HIGH,
    "AP": HIGH,
    "Reuters": VERY_HIGH,
    "NPR": HIGH,
    "PBS News": HIGH,
    "BBC News": HIGH,               # MBFC Mostly Factual, Ad Fontes 44.6
    "BBC": HIGH,
    # Networks and national papers
    "ABC News": HIGH,
    "CBS News": MOSTLY,
    "NBC News": HIGH,
    "CNN": MOSTLY,
    "New York Times": HIGH,
    "Washington Post": MOSTLY,
    "Los Angeles Times": HIGH,
    "USA Today": MOSTLY,
    "Christian Science Monitor": HIGH,
    "The Christian Science Monitor": HIGH,
    "Politico": HIGH,
    "Axios": HIGH,
    "The Hill": MOSTLY,
    "Semafor": HIGH,
    "NewsNation": HIGH,
    "Courthouse News Service": HIGH,
    # Business
    "WSJ": HIGH,                    # MBFC Mostly Factual, Ad Fontes 42.9
    "Wall Street Journal": HIGH,
    "The Wall Street Journal": HIGH,
    "Bloomberg": MOSTLY,
    "CNBC": HIGH,                   # MBFC Mostly Factual, Ad Fontes 43.7
    "The Economist": HIGH,
    "Fox Business": MIXED,
    # Right of center
    "Fox News": LOW,
    "New York Post": MIXED,
    "The Washington Times": MIXED,
    "Washington Examiner": MOSTLY,
    "National Review": MIXED,       # MBFC Mostly Factual, Ad Fontes 31.4
    "Reason": HIGH,
    "The Dispatch": HIGH,
    "Washington Free Beacon": MIXED,
    "The Daily Caller": MIXED,
    "Daily Caller": MIXED,
    "Breitbart": MIXED,
    "Newsmax": MIXED,
    "The Daily Wire": MIXED,
    "The Center Square": MOSTLY,
    # Nonprofit newsrooms
    "The Texas Tribune": HIGH,
    "ProPublica": HIGH,
    "CalMatters": HIGH,
    "KFF Health News": HIGH,
    "Inside Climate News": HIGH,
    "Stateline": HIGH,
    "The 19th": HIGH,
    # Abroad
    "The Guardian": HIGH,
    "Al Jazeera": MOSTLY,           # MBFC Mixed, Ad Fontes 41.3
    "The Telegraph": MIXED,
    "The Independent": MIXED,
    "Daily Mail": MIXED,            # MBFC Mostly Factual, Ad Fontes 30.7
    "National Post": HIGH,
    "Toronto Sun": MOSTLY,
    "Deutsche Welle": HIGH,
    "Kyiv Independent": HIGH,
    "CNA": HIGH,
    "ABC News (Australia)": HIGH,
    "CBC News": HIGH,
    "Daily Maverick": MOSTLY,
}


def reporting_for(name):
    """One of LEVELS for an outlet name, or None when ungraded."""
    if not name:
        return None
    grade = GRADES.get(name)
    if grade is None:
        lower = name.lower().strip()
        grade = next((v for k, v in GRADES.items() if k.lower() == lower), None)
    return grade
