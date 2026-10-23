# aggregator/outlet_origins.py
"""Where an outlet is based, and who funds it.

AllSides rates outlets on a US left/right scale. That scale means little for
most outlets based abroad: an LLM asked whether NHK or Deutsche Welle is "Lean
Left" in American terms will still answer with a number, and that number would
then move a story's Left/Center/Right counts and its enrichment direction.

Decided with Philip 2026-10-23: every outlet gets a country. A non-US outlet
that AllSides rates (BBC, Guardian, Al Jazeera...) keeps that rating, since
AllSides rates it for US readers. One that Media Bias/Fact Check or Ad Fontes
rates on the US scale takes that lean from LEANS_ABROAD. Any other non-US
outlet is never sent to the LLM, keeps no bias score (bias_source
"international"), stays out of the Left/Center/Right counts, and shows as
International.

`funding` is a second, optional label for outlets where who pays matters more
than left/right: "public" (a public broadcaster with editorial independence)
or "state" (owned or run by a government).

The country comes from ORIGINS by outlet name, else from the country-code
top-level domain of the outlet's URL (bbc.co.uk -> GB). Domains that are
used as generic TLDs (.io, .tv, .co...) are not read as countries.
"""

from urllib.parse import urlparse

US = "US"

# Outlet name (as normalize_source_name() leaves it) -> (country, funding).
# Only non-US outlets, and US outlets with a funding label, need a row.
ORIGINS = {
    # UK
    "BBC": ("GB", "public"),
    "BBC News": ("GB", "public"),
    "The Guardian": ("GB", None),
    "Guardian": ("GB", None),
    "The Telegraph": ("GB", None),
    "Telegraph": ("GB", None),
    "The Independent": ("GB", None),
    "Daily Mail": ("GB", None),
    "The Daily Mail": ("GB", None),
    "The Economist": ("GB", None),
    "Sky News": ("GB", None),
    # Europe
    "Deutsche Welle": ("DE", "public"),
    "DW": ("DE", "public"),
    "France 24": ("FR", "public"),
    "Kyiv Independent": ("UA", None),
    "The Kyiv Independent": ("UA", None),
    "RTÉ": ("IE", "public"),
    "Politico Europe": ("BE", None),
    # Canada
    "National Post": ("CA", None),
    "Toronto Sun": ("CA", None),
    "CBC News": ("CA", "public"),
    "CBC": ("CA", "public"),
    # Middle East
    "Al Jazeera": ("QA", "state"),
    "Al Jazeera English": ("QA", "state"),
    "The Times of Israel": ("IL", None),
    "Times of Israel": ("IL", None),
    # Asia-Pacific
    "NHK World": ("JP", "public"),
    "NHK WORLD-JAPAN": ("JP", "public"),
    "CNA": ("SG", "state"),
    "ABC News (Australia)": ("AU", "public"),
    # Africa
    "Daily Maverick": ("ZA", None),
}

# US-scale lean (1 Left ... 5 Right) for outlets based abroad that AllSides
# doesn't rate but Media Bias/Fact Check or Ad Fontes Media do (Philip,
# 2026-10-23: rated on the US scale anywhere, so count it). MBFC's label
# maps Left 1, Left-Center 2, Least Biased 3, Right-Center 4, Right 5; Ad
# Fontes' bias score (-42 ... 42) breaks ties: under -18 is 1, -18 to -6 is
# 2, -6 to 6 is 3, 6 to 18 is 4, over 18 is 5. Looked up 2026-10-23.
LEANS_ABROAD = {
    "Deutsche Welle": 2,        # MBFC Left-Center
    "DW": 2,
    "Kyiv Independent": 2,      # MBFC Left-Center
    "The Kyiv Independent": 2,
    "CNA": 3,                   # MBFC Least Biased
    "ABC News (Australia)": 2,  # MBFC Left-Center
    "CBC News": 2,              # MBFC Left-Center (Ad Fontes -5.0)
    "CBC": 2,
    "Daily Maverick": 3,        # MBFC Least Biased
}

COUNTRY_NAMES = {
    "AU": "Australia", "BE": "Belgium", "CA": "Canada", "CN": "China",
    "DE": "Germany", "FR": "France", "GB": "UK", "IE": "Ireland",
    "IL": "Israel", "IN": "India", "JP": "Japan", "KR": "South Korea",
    "MX": "Mexico", "NG": "Nigeria", "NZ": "New Zealand", "PH": "Philippines",
    "QA": "Qatar", "SG": "Singapore", "UA": "Ukraine", "US": "US",
    "ZA": "South Africa",
}

FUNDING_NAMES = {"public": "Public broadcaster", "state": "State-owned"}

# Country-code TLDs sold as generic domains, which say nothing about where
# an outlet is based.
_GENERIC_CCTLDS = {
    "ai", "cc", "co", "fm", "gg", "io", "ly", "me", "nu", "sh", "tv", "ws",
}


def _country_from_url(url):
    if not url:
        return None
    host = urlparse(url).netloc.lower().split(":")[0]
    tld = host.rsplit(".", 1)[-1] if "." in host else ""
    if len(tld) != 2 or tld in _GENERIC_CCTLDS:
        return None
    return "GB" if tld == "uk" else tld.upper()


def origin_for(name, url=None):
    """(country, funding) for an outlet; country None when unknown (read as US)."""
    if name:
        hit = ORIGINS.get(name)
        if hit is None:
            lower = name.lower().strip()
            hit = next((v for k, v in ORIGINS.items() if k.lower() == lower), None)
        if hit is not None:
            return hit
    return _country_from_url(url), None


def lean_abroad_for(name):
    """1-5 from LEANS_ABROAD for an outlet name, or None."""
    if not name:
        return None
    lean = LEANS_ABROAD.get(name)
    if lean is None:
        lower = name.lower().strip()
        lean = next((v for k, v in LEANS_ABROAD.items() if k.lower() == lower), None)
    return lean


def is_international(country):
    return bool(country) and country != US


def country_name(country):
    return COUNTRY_NAMES.get(country, country) if country else None


def funding_name(funding):
    return FUNDING_NAMES.get(funding)
