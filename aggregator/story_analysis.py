"""The story page's multi-source analysis, split into its labelled sections.

The deep_report prompts (seed_defaults.py, deep_report.*) ask for a fixed set
of "Label: text" paragraphs. The story page lays these out as cards or as
titled subsections, so the text is parsed once here instead of in Jinja.

Each section gets a `kind`, which decides where it goes and how it looks:
"lead" (the first section), "left" / "center" / "right" (the politics
prompt's coverage buckets), "next" (What's next) or "other", plus an icon
name from icons.html. Every "other" section also has a `size`: "long" ones
stack full width and "short" ones sit side by side below them, so each
section type reads the same way. Text that doesn't follow the format still renders:
a line with no label continues the section before it, or starts an
unlabelled one.
"""
import re

# A colon further in than this is part of a sentence, not a label. Business's
# "What companies or sectors are affected" is 38 characters, so it was folded
# into the section above it at the old cutoff of 35.
MAX_LABEL = 45

# An "other" section with more text than this is long: it stacks full width.
# The prompts ask for one or two sentences in the short ones.
LONG_CHARS = 300

_BUCKETS = (
    ("left", re.compile(r"\bthe left\b|\bleft[- ]leaning\b")),
    ("center", re.compile(r"\bthe cent(er|re)\b")),
    ("right", re.compile(r"\bthe right\b|\bright[- ]leaning\b")),
)
_EMPTY_BUCKET = re.compile(r"^no (left|right|center|centre)[- \w]* sources were found", re.I)

# Label words → icon, first match wins. Covers the labels of every
# deep_report prompt; anything else gets file-text.
_ICONS = (
    ("contested", "scale"),
    ("perspectives", "scale"),
    ("being covered", "scale"),
    ("missing", "eye-off"),
    ("unknown", "eye-off"),
    ("next", "circle-arrow-right"),
    ("why it matters", "zap"),
    ("matters", "zap"),
    ("experts", "message-square"),
    ("analysts", "message-square"),
    ("numbers", "table"),
    ("research shows", "flask-conical"),
    ("key details", "list"),
    ("market", "gauge"),
    ("companies", "building-2"),
    ("sectors", "building-2"),
    ("risks", "circle-alert"),
    ("picture", "layers"),
    ("impact", "users"),
    ("involved", "users"),
    ("performances", "trophy"),
)


def _icon_for(label):
    low = label.lower()
    for needle, name in _ICONS:
        if needle in low:
            return name
    return "file-text"


def _kind_for(label):
    low = label.lower()
    if low.startswith("what's next") or low.startswith("what’s next"):
        return "next"
    for bucket, pattern in _BUCKETS:
        if pattern.search(low):
            return bucket
    return "other"


def _split(text):
    """[(label or None, [paragraph, ...]), ...] from the raw report."""
    sections = []
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        head, sep, rest = line.partition(":")
        if sep and 0 < len(head.strip()) < MAX_LABEL and rest.strip():
            sections.append((head.strip(), [rest.strip()]))
        elif sep and 0 < len(head.strip()) < MAX_LABEL and not rest.strip():
            # A label on a line of its own; its text follows.
            sections.append((head.strip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            sections.append((None, [line]))
    return [(label, paras) for label, paras in sections if paras]


def parse(text):
    """The report's sections, in order, as dicts:
    label, paragraphs, kind, icon, empty (an L/C/R bucket with no sources)."""
    out = []
    for i, (label, paras) in enumerate(_split(text)):
        kind = _kind_for(label) if label else "other"
        if i == 0 and kind == "other":
            kind = "lead"
        out.append({
            "label": label,
            "paragraphs": paras,
            "kind": kind,
            "size": "long" if len(" ".join(paras)) > LONG_CHARS else "short",
            "icon": "newspaper" if kind == "lead" else _icon_for(label or ""),
            "empty": kind in ("left", "center", "right") and bool(_EMPTY_BUCKET.match(paras[0])),
        })
    return out


def layout(text):
    """What the story page needs to lay the report out:

    sections  every section, in order (the Sections layout uses these)
    lead      the first section, if it is a plain one
    buckets   the L/C/R sections that have coverage, left to right
    empty     the L/C/R sections whose bucket had no sources
    others    every other section but What's next, in order
    longs     the long ones of those, stacked full width, in order
    shorts    the short ones, side by side below them, in order
    next      the What's next section, if any
    """
    sections = parse(text)
    lead = sections[0] if sections and sections[0]["kind"] == "lead" else None
    order = {"left": 0, "center": 1, "right": 2}
    lcr = sorted((s for s in sections if s["kind"] in order), key=lambda s: order[s["kind"]])
    buckets = [s for s in lcr if not s["empty"]]
    others = [s for s in sections if s["kind"] == "other"]
    nxt = next((s for s in sections if s["kind"] == "next"), None)
    return {
        "sections": sections,
        "lead": lead,
        "buckets": buckets,
        "empty": [s for s in lcr if s["empty"]],
        "others": others,
        "longs": [s for s in others if s["size"] == "long"],
        "shorts": [s for s in others if s["size"] == "short"],
        "next": nxt,
    }
