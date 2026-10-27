TOPICS = [
    {"label": "US Politics", "icon": "UP"},
    {"label": "US News", "icon": "UN"},
    {"label": "International News", "icon": "IN"},
    {"label": "Sci/Tech", "icon": "ST"},
    {"label": "Sports", "icon": "SP"},
    {"label": "Buss/Fin", "icon": "BF"},
    {"label": "Other", "icon": "OT"},
]

# What readers see for a topic: the sidebar, section headers and empty
# states spell the short keys out (Philip, 2026-10-27). Topic.name stays the
# key everywhere else (URLs, the API, the classifier prompt).
SECTION_NAMES = {
    "Sci/Tech": "Science & Technology",
    "Buss/Fin": "Business & Finance",
}
OPED_SECTION_NAME = "Opinion & Editorials"


def section_name(topic_name):
    """The reader-facing name for a topic key; unknown keys show as they are."""
    return SECTION_NAMES.get(topic_name, topic_name)


AGGREGATORS = ["Yahoo News", "Google News", "MSN", "AOL", "Startups"]
