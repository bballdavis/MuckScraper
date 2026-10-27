# What readers see for a topic when its section has no display name of its
# own (Settings › Sections sets one; aggregator/sections.py section_label).
# Topic.name stays the key everywhere else (URLs, the API, the classifier
# prompt).
SECTION_NAMES = {
    "Sci/Tech": "Science & Technology",
    "Buss/Fin": "Business & Finance",
}
OPED_SECTION_NAME = "Opinion & Editorials"


def section_name(topic_name):
    """The reader-facing name for a topic key; unknown keys show as they are."""
    return SECTION_NAMES.get(topic_name, topic_name)


AGGREGATORS = ["Yahoo News", "Google News", "MSN", "AOL", "Startups"]
