"""The OpenAPI 3.1 description of /api/v1, served at /api/v1/openapi.json
for generating the iOS client (swift-openapi-generator). Kept by hand next to
routes.py and serializers.py; tests/test_api.py checks every route is in it."""
from flask import jsonify

from . import api, API_VERSION


def _ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def _obj(required, **props):
    return {"type": "object", "required": list(required), "properties": props}


_S = {"type": "string"}
_NS = {"type": ["string", "null"]}
_I = {"type": "integer"}
_NI = {"type": ["integer", "null"]}
_B = {"type": "boolean"}
_DT = {"type": "string", "format": "date-time"}
_NDT = {"type": ["string", "null"], "format": "date-time"}
_URL = {"type": ["string", "null"], "format": "uri"}
# A card's image chain: `image_url` (the story's newest article image, or
# this article's), then the outlet logo, then the app's own placeholder.
# Logos are square icons: draw them fitted on a tile, not cropped.
_LOGO = {"type": ["string", "null"], "format": "uri",
         "description": "Outlet logo for when image_url is null; fit it, don't crop it"}
_LEANING = {"type": "string", "enum": ["left", "center", "right", "unrated"]}
_PARAS = {"type": "array", "items": _S}


def _by_leaning(item):
    sides = ("left", "center", "right", "international", "unrated")
    return _obj(list(sides), **{side: {"type": "array", "items": item} for side in sides})


def _page(item):
    return _obj(["items", "next_cursor"], items={"type": "array", "items": item}, next_cursor=_NS)


SCHEMAS = {
    "Error": _obj(["error"], error=_obj(["code", "message"], code=_S, message=_S)),
    "Health": _obj(["ok", "version"], ok=_B, version=_S),
    "Coverage": _obj(["left", "center", "right", "international"], left=_I, center=_I, right=_I,
                     international={"type": "integer", "description": "Articles from unrated outlets based "
                                    "outside the US; not part of left/center/right"}),
    "Outlet": _obj(["id", "name", "leaning", "logo_url", "country", "funding", "reporting"], id=_NI, name=_S,
                   leaning=_LEANING, logo_url=_LOGO,
                   country={"type": ["string", "null"], "description": "ISO 3166 code for an outlet based "
                            "outside the US; null for US outlets. Such outlets are 'unrated' unless "
                            "AllSides rates them."},
                   funding={"type": ["string", "null"], "enum": ["public", "state", None]},
                   reporting={"type": ["string", "null"], "enum": ["very_high", "high", "mostly_factual", "mixed", "low", None],
                              "description": "Hand-kept grade of how reliable the outlet's reporting is, "
                              "separate from its lean; null when not graded."}),
    "StorySummary": _obj(
        ["id", "section", "headline", "dek", "image_url", "logo_url", "coverage", "source_count", "updated_at",
         "is_summarized", "has_analysis", "has_ai_headline", "has_updates"],
        id=_I, section=_S, headline=_S, dek=_NS, image_url=_URL, logo_url=_LOGO, coverage=_ref("Coverage"),
        source_count=_I, updated_at=_DT, is_summarized=_B, has_analysis=_B, has_ai_headline=_B, has_updates=_B,
        is_read={"type": "boolean", "description": "Marked read by this user (editions, /stories, /stories/{id})"}),
    "HeadlineCard": {"allOf": [_ref("StorySummary"), _obj(
        ["why", "newest_at", "group", "framing_gap", "opeds"],
        why={"type": ["string", "null"], "description": "Short reason the story is in the view, for beside the "
             "L/C/R pill: \"4 outlets in 6h\" (Trending), \"High gap · 2 op-eds\" (Controversial); null on Latest"},
        newest_at=_NDT,
        group={"type": ["string", "null"], "enum": ["Last hour", "Last 6 hours", "Earlier", None],
               "description": "Latest only: the section header the story falls under"},
        framing_gap={"type": ["string", "null"], "enum": ["high", "moderate", "low", None],
                     "description": "Controversial only: how differently left and right outlets write it up"},
        opeds={"type": "integer", "description": "Linked op-eds (Controversial only; 0 elsewhere)"})]},
    "ArticleCard": _obj(
        ["id", "story_id", "title", "outlet", "leaning", "url", "published_at", "image_url", "summary", "copy_of"],
        id=_I, story_id=_NI, title=_S, outlet=_ref("Outlet"), leaning=_LEANING, url=_URL,
        published_at=_NDT, image_url=_URL, summary=_NS, copy_of=_NS),
    "OpinionPiece": _obj(
        ["id", "title", "outlet", "leaning", "url", "published_at", "stance", "story_id"],
        id=_I, title=_S, outlet=_ref("Outlet"), leaning=_LEANING, url=_URL, published_at=_NDT,
        stance=_NS, story_id=_NI),
    "Topic": _obj(["id", "name", "icon", "sort_order"], id=_I, name=_S, icon=_NS, sort_order=_NI),
    "EditionBrief": _obj(["id", "label", "kind", "date", "published_at", "story_count"],
                         id=_I, label=_S, kind=_S, date={"type": "string", "format": "date"},
                         published_at=_DT, story_count=_NI),
    "Device": _obj(["id", "name", "scopes", "created_at", "expires_at"],
                   id=_I, name=_S, kind={"type": "string", "enum": ["device", "key"]},
                   scopes={"type": "array", "items": _S}, created_at=_DT, expires_at=_NDT),
    "Preferences": _obj(["start_page", "range", "hide_single"],
                        start_page={"type": "string", "enum": ["headlines", "all", "grouped"]},
                        range={"type": "string", "enum": ["24h", "7d", "30d", "all"]},
                        hide_single=_B),
    "SearchHit": {"type": "object", "required": ["kind", "id", "title"], "properties": {
        "kind": {"type": "string", "enum": ["story", "article"]}, "id": _I, "story_id": _NI, "title": _S,
        "image_url": _URL, "logo_url": _LOGO, "source_count": _I, "updated_at": _NDT, "outlet": _ref("Outlet"),
        "leaning": _LEANING, "published_at": _NDT}},
    "FeedHeadline": _obj(["title", "outlet", "url", "published_at"], title=_S, outlet=_S, url=_URL, published_at=_DT),
    "BreakingItem": _obj(
        ["id", "title", "outlet", "url", "published_at", "image_url", "outlet_count", "state", "story_id", "error"],
        id=_I, title=_S, outlet=_S, url=_URL, published_at=_DT, image_url=_URL, outlet_count=_I,
        state={"type": "string", "enum": ["new", "scraping", "scraped", "summarizing", "summarized", "failed"]},
        story_id=_NI, error=_NS),
    "NewsBoxItem": _obj(
        ["id", "story", "headline", "origin", "state", "step", "progress", "is_unread", "run_id", "added_at",
         "ready_at", "error"],
        id=_I,
        story={"oneOf": [_ref("StorySummary"), {"type": "null"}],
               "description": "Null while a Breaking headline is still being scraped; headline stands in"},
        headline={"oneOf": [_ref("FeedHeadline"), {"type": "null"}]},
        origin={"type": "string", "enum": ["requested", "saved", "scheduled", "breaking"]},
        state={"type": "string", "enum": ["pending", "in_progress", "ready", "failed"]},
        step={"type": ["string", "null"], "enum": ["scrape", "group", "summary", "analysis", None],
              "description": "What an unfinished item is waiting on"},
        progress={"type": ["number", "null"]}, is_unread=_B, run_id=_NS, added_at=_DT, ready_at=_NDT,
        error=_NS),
    "Schedule": _obj(
        ["is_enabled", "timezone", "run_times", "sections", "depth", "skip_already_summarized",
         "minimum_sources", "notify_when_done", "next_run_at", "last_run_at"],
        is_enabled=_B, timezone={"type": "string", "description": "IANA time zone for run times"},
        run_times={"type": "array", "maxItems": 6, "items": _obj(
            ["id", "time", "days", "is_enabled"], id=_S, time={"type": "string", "pattern": "^([01]\\d|2[0-3]):[0-5]\\d$"},
            days={"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 7}}, is_enabled=_B)},
        sections={"type": "array", "maxItems": 30, "items": _obj(
            ["section", "is_enabled", "amount"], section=_S, is_enabled=_B,
            amount={"type": "integer", "minimum": 1, "maximum": 100,
                    "description": "Number of stories: the top N of the latest edition for Headlines, "
                                   "the N most-covered in the last day for a topic"})},
        depth={"type": "string", "enum": ["summary", "analysis", "summary_analysis"]}, skip_already_summarized=_B,
        minimum_sources=_NI, notify_when_done=_B, next_run_at=_NDT, last_run_at=_NDT),
    "BookmarkVersion": _obj(
        ["version", "status", "captures", "size_bytes", "asset_count", "created_at", "finished_at", "error"],
        version=_I, status={"type": "string", "enum": ["pending", "building", "ready", "failed", "over_quota"]},
        captures={"type": "string", "enum": ["pending", "running", "done", "partial", "unavailable", "failed", "over_quota"]},
        size_bytes=_I, asset_count=_I, created_at=_DT, finished_at=_NDT, error=_NS),
    "TimeCapsule": {"type": "object", "description": (
        "One saved version (format muckscraper-time-capsule v1): story (StoryDetail plus image), articles "
        "(ArticleDetail with full content_html, bias scores and image), opinion (columns with their text), "
        "captures (page PDF and screenshot per article), and assets (each saved file: source_url, content_type, "
        "bytes, kind). Every file is referenced as assets/<name>."),
        "required": ["format", "format_version", "version", "captured_at", "story", "articles", "opinion",
                     "captures", "assets"]},
}
SCHEMAS["Bookmark"] = _obj(
    ["id", "story_id", "title", "story", "saved_at", "latest", "version_count", "size_bytes"],
    id=_I, story_id=_NI, title=_S, story={"oneOf": [_ref("StorySummary"), {"type": "null"}]}, saved_at=_DT,
    latest={"oneOf": [_ref("BookmarkVersion"), {"type": "null"}]}, version_count=_I, size_bytes=_I,
    versions={"type": "array", "items": _ref("BookmarkVersion")})
SCHEMAS["Edition"] = {"allOf": [_ref("EditionBrief"), _obj(["stories"], stories={"type": "array", "items": _ref("StorySummary")})]}
SCHEMAS["StoryDetail"] = {"allOf": [_ref("StorySummary"), _obj(
    ["title", "topics", "created_at", "summary", "deep_report", "opinion_roundup", "articles", "copies_hidden", "opinion"],
    title=_S, topics=_PARAS, created_at=_NDT, summary=_PARAS, deep_report=_PARAS, opinion_roundup=_PARAS,
    articles=_by_leaning(_ref("ArticleCard")), copies_hidden=_I, opinion=_by_leaning(_ref("OpinionPiece")),
    is_skipped={"type": "boolean", "description": "Passed on in Swipe: scheduled runs won't summarize it"})]}
SCHEMAS["ArticleDetail"] = {"allOf": [_ref("ArticleCard"), _obj(
    ["topics", "analysis", "content_html"], topics=_PARAS, analysis=_PARAS,
    content_html={"type": ["string", "null"], "description": "Sanitized scraped HTML; null unless the server allows full text."},
    is_read={"type": "boolean", "description": "Marked read by this user"},
    is_summarizing={"type": "boolean", "description": "Its summary is queued or being written"},
    is_rating_bias={"type": "boolean", "description": "Its lean rating is queued or running"})]}
SCHEMAS["ReadItem"] = _obj(
    ["kind", "id", "read_at", "story", "article"],
    kind={"type": "string", "enum": ["story", "article"]}, id=_I, read_at=_DT,
    story={"oneOf": [_ref("StorySummary"), {"type": "null"}], "description": "Set when kind is story"},
    article={"oneOf": [_ref("ArticleCard"), {"type": "null"}], "description": "Set when kind is article"})
SCHEMAS["DeckCard"] = {"allOf": [_ref("StorySummary"), _obj(
    ["tier"], tier={"type": "string", "enum": ["headlines", "more"],
                    "description": "headlines: in the latest edition; more: another story from the past week"})]}
SCHEMAS["StorySkip"] = _obj(["story_id", "skipped_at"], story_id=_I, skipped_at=_DT)


def _json(schema, description="OK"):
    return {"description": description, "content": {"application/json": {"schema": schema}}}


_ERRORS = {code: _json(_ref("Error"), text) for code, text in
           (("400", "Invalid parameter"), ("401", "Missing or invalid token"), ("429", "Rate limited"),
            ("503", "API turned off"))}
_NOT_FOUND = {"404": _json(_ref("Error"), "Not found")}


def _q(name, schema, description=None):
    p = {"name": name, "in": "query", "required": False, "schema": schema}
    if description:
        p["description"] = description
    return p


def _path_id(name):
    return {"name": name, "in": "path", "required": True, "schema": _I}


_CURSOR = _q("cursor", _S, "next_cursor from the previous page")
_LIMIT = _q("limit", {"type": "integer", "minimum": 1, "maximum": 50})
_READ_KIND = {"name": "kind", "in": "path", "required": True, "schema": {"type": "string", "enum": ["stories", "articles"]}}
_RANGE = _q("range", {"type": "string", "enum": ["24h", "7d", "30d", "all"]})


def _get(op_id, ok, params=(), extra=None, auth=True):
    op = {"operationId": op_id, "parameters": list(params), "responses": {"200": ok, **_ERRORS, **(extra or {})}}
    if not auth:
        op["security"] = []
    return {"get": op}


PATHS = {
    "/health": _get("health", _json(_ref("Health")), auth=False),
    "/devices/pair": {"post": {
        "operationId": "pairDevice", "security": [],
        "requestBody": {"required": True, "content": {"application/json": {"schema": _obj(
            ["code"], code=_S, device_name=_S)}}},
        "responses": {"201": _json(_obj(["token", "device", "user"], token=_S, device=_ref("Device"),
                                        user=_obj(["username"], username=_S)), "Paired; the token is shown once"),
                      **_ERRORS}}},
    "/me": _get("me", _json(_obj(["user", "device"], user=_obj(["username", "role"], username=_S, role=_S),
                                 device=_ref("Device")))),
    "/me/devices/current": {"delete": {"operationId": "signOut", "responses": {"204": {"description": "Signed out"}, **_ERRORS}}},
    "/me/preferences": {
        **_get("getPreferences", _json(_ref("Preferences"))),
        "put": {"operationId": "putPreferences",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {
                    "type": "object", "properties": SCHEMAS["Preferences"]["properties"], "additionalProperties": False}}}},
                "responses": {"200": _json(_ref("Preferences")), **_ERRORS}},
    },
    "/topics": _get("topics", _json(_obj(["items"], items={"type": "array", "items": _ref("Topic")}))),
    "/editions": _get("editions", _json(_page(_ref("EditionBrief"))), [_CURSOR, _LIMIT]),
    "/editions/latest": _get("latestEdition", _json(_ref("Edition")), extra=_NOT_FOUND),
    "/editions/{edition_id}": _get("edition", _json(_ref("Edition")), [_path_id("edition_id")], _NOT_FOUND),
    "/opinion": _get("opinion", _json(_obj(["edition", "items"], edition=_ref("EditionBrief"), items={
        "type": "array", "items": _obj(["story", "columns"], story=_ref("StorySummary"),
                                       columns=_by_leaning(_ref("OpinionPiece")))})),
        [_q("edition", _I)], _NOT_FOUND),
    "/stories": _get("stories", _json(_page(_ref("StorySummary"))),
                     [_q("topic", _S), _RANGE, _q("single", _B, "Include single-article stories"), _CURSOR, _LIMIT],
                     _NOT_FOUND),
    "/headlines": _get("headlines", _json(_obj(
        ["view", "views", "hidden_read", "items", "next_cursor"],
        view={"type": "string", "enum": ["trending", "latest", "controversial"]},
        views={"type": "array", "items": _obj(["key", "label", "description", "count"], key=_S, label=_S,
                                              description=_S, count=_I)},
        hidden_read={"type": "integer", "description": "Stories in this view left out because the user read them"},
        items={"type": "array", "items": _ref("HeadlineCard")}, next_cursor=_NS)),
        [_q("view", {"type": "string", "enum": ["trending", "latest", "controversial"]}, "Defaults to trending"),
         _CURSOR, _LIMIT]),
    "/stories/{story_id}": _get("story", _json(_ref("StoryDetail")), [_path_id("story_id")], _NOT_FOUND),
    "/articles/{article_id}": _get("article", _json(_ref("ArticleDetail")), [_path_id("article_id")], _NOT_FOUND),
    "/search": _get("search", _json(_obj(["items"], items={"type": "array", "items": _ref("SearchHit")})),
                    [_q("q", _S), _q("kind", {"type": "string", "enum": ["stories", "articles"]}), _RANGE, _LIMIT]),
    "/openapi.json": _get("openapiSpec", {"description": "This document"}, auth=False),
    "/stories/{story_id}/summarize": {"post": {
        "operationId": "summarizeStory", "parameters": [_path_id("story_id")],
        "description": "Needs the summarize scope. Adds the story to the user's NewsBox.",
        "requestBody": {"required": True, "content": {"application/json": {"schema": {
            "type": "object", "additionalProperties": False, "properties": {
                "depth": {"type": "string", "enum": ["summary", "analysis", "summary_analysis"]},
                "origin": {"type": "string", "enum": ["requested", "saved"],
                           "description": "Defaults to requested"}}}}}},
        "responses": {"200": _json(_ref("NewsBoxItem"), "Already summarized: ready"),
                      "202": _json(_ref("NewsBoxItem"), "Summary queued: pending"),
                      **_ERRORS, **_NOT_FOUND}}},
    "/articles/{article_id}/summarize": {"post": {
        "operationId": "summarizeArticle", "parameters": [_path_id("article_id")],
        "description": "Needs the summarize scope. Queues the article's own summary; poll /articles/{id}.",
        "responses": {"200": _json(_ref("ArticleDetail"), "Already summarized"),
                      "202": _json(_ref("ArticleDetail"), "Summary queued (is_summarizing)"),
                      "409": _json(_ref("Error"), "No text to summarize"),
                      **_ERRORS, **_NOT_FOUND}}},
    "/articles/{article_id}/rate-bias": {"post": {
        "operationId": "rateArticleBias", "parameters": [_path_id("article_id")],
        "description": "Needs the summarize scope. Queues a rating of the article's own lean, which then "
                       "replaces its outlet's in leaning and the story's coverage counts.",
        "responses": {"202": _json(_ref("ArticleDetail"), "Rating queued (is_rating_bias)"),
                      **_ERRORS, **_NOT_FOUND}}},
    "/me/newsbox": _get("newsbox", _json({"allOf": [_page(_ref("NewsBoxItem")), _obj(["unread_count"], unread_count=_I)]}),
                        [_q("state", {"type": "string", "enum": ["pending", "in_progress", "ready", "failed"]}),
                         _q("origin", {"type": "string", "enum": ["requested", "saved", "scheduled", "breaking"]}),
                         _q("unread", _B), _CURSOR, _LIMIT]),
    "/me/newsbox/{story_id}": {
        "patch": {"operationId": "updateNewsBoxItem", "parameters": [_path_id("story_id")],
                  "requestBody": {"required": True, "content": {"application/json": {"schema": {
                      "type": "object", "additionalProperties": False, "properties": {"is_unread": _B}}}}},
                  "responses": {"200": _json(_ref("NewsBoxItem")), **_ERRORS, **_NOT_FOUND}},
        "delete": {"operationId": "removeNewsBoxItem", "parameters": [_path_id("story_id")],
                   "responses": {"204": {"description": "Removed"}, **_ERRORS, **_NOT_FOUND}},
    },
    "/me/newsbox/read": {"post": {"operationId": "markNewsBoxRead",
                                  "responses": {"200": _json(_obj(["updated"], updated=_I)), **_ERRORS}}},
    "/me/read": _get("readItems", _json(_page(_ref("ReadItem"))),
                     [_q("kind", {"type": "string", "enum": ["story", "article"]}), _CURSOR, _LIMIT]),
    "/me/read/{kind}/{item_id}": {
        "put": {"operationId": "markRead", "description": "Mark a story or article read; again moves it to the top.",
                "parameters": [_READ_KIND, _path_id("item_id")],
                "responses": {"200": _json(_ref("ReadItem")), **_ERRORS, **_NOT_FOUND}},
        "delete": {"operationId": "markUnread", "parameters": [_READ_KIND, _path_id("item_id")],
                   "responses": {"204": {"description": "Unread (whether or not it was read)"}, **_ERRORS, **_NOT_FOUND}},
    },
    "/me/deck": _get("deck", _json(_page(_ref("DeckCard"))), [_CURSOR, _LIMIT]),
    "/me/skips": _get("skips", _json(_page(_ref("StorySkip"))), [_CURSOR, _LIMIT]),
    "/me/skips/{story_id}": {
        "put": {"operationId": "skipStory", "description": "Pass on a story: scheduled runs won't summarize it.",
                "parameters": [_path_id("story_id")],
                "responses": {"200": _json(_ref("StorySkip")), **_ERRORS, **_NOT_FOUND}},
        "delete": {"operationId": "unskipStory", "parameters": [_path_id("story_id")],
                   "responses": {"204": {"description": "Unskipped (whether or not it was skipped)"}, **_ERRORS}},
    },
    "/me/schedules": {
        **_get("getSchedule", _json(_ref("Schedule"))),
        "put": {"operationId": "putSchedule", "description": "Needs the summarize scope. Replaces the schedule.",
                "requestBody": {"required": True, "content": {"application/json": {"schema": _ref("Schedule")}}},
                "responses": {"200": _json(_ref("Schedule")), **_ERRORS}},
    },
    "/me/bookmarks": {
        **_get("bookmarks", _json(_obj(["items", "used_bytes", "quota_bytes"], items={"type": "array", "items": _ref("Bookmark")},
                                       used_bytes=_I, quota_bytes=_NI))),
        "post": {"operationId": "saveBookmark", "description": "Needs the summarize scope. Saves a new version.",
                 "requestBody": {"required": True, "content": {"application/json": {"schema": _obj(["story_id"], story_id=_I)}}},
                 "responses": {"202": _json(_ref("Bookmark"), "Version being written"), **_ERRORS, **_NOT_FOUND}},
    },
    "/me/bookmarks/{bookmark_id}": {
        **_get("bookmark", _json(_ref("Bookmark")), [_path_id("bookmark_id")], _NOT_FOUND),
        "delete": {"operationId": "deleteBookmark", "parameters": [_path_id("bookmark_id")],
                   "responses": {"204": {"description": "Deleted with its files"}, **_ERRORS, **_NOT_FOUND}},
    },
    "/me/bookmarks/{bookmark_id}/versions": {"post": {
        "operationId": "captureAgain", "description": "Needs the summarize scope.", "parameters": [_path_id("bookmark_id")],
        "responses": {"202": _json(_ref("Bookmark"), "Version being written"), **_ERRORS, **_NOT_FOUND}}},
    "/me/bookmarks/{bookmark_id}/versions/{version}": _get(
        "bookmarkVersion", _json(_ref("TimeCapsule")), [_path_id("bookmark_id"), _path_id("version")], _NOT_FOUND),
    "/me/bookmarks/{bookmark_id}/versions/{version}/assets/{name}": _get(
        "bookmarkAsset", {"description": "The file", "content": {"application/octet-stream": {}}},
        [_path_id("bookmark_id"), _path_id("version"), {"name": "name", "in": "path", "required": True, "schema": _S}],
        _NOT_FOUND),
    "/me/bookmarks/{bookmark_id}/export": _get(
        "exportBookmark", {"description": "Every ready version as a zip", "content": {"application/zip": {}}},
        [_path_id("bookmark_id")], _NOT_FOUND),
    "/breaking": _get("breaking", _json(_obj(["items"], items={"type": "array", "items": _ref("BreakingItem")}))),
    "/breaking/{entry_id}/newsbox": {"post": {
        "operationId": "sendBreakingToNewsBox", "parameters": [_path_id("entry_id")],
        "description": "Needs the summarize scope. Scrapes the headline if needed and summarizes its story.",
        "requestBody": {"required": True, "content": {"application/json": {"schema": {
            "type": "object", "additionalProperties": False, "properties": {
                "depth": {"type": "string", "enum": ["summary", "analysis", "summary_analysis"]}}}}}},
        "responses": {"200": _json(_ref("NewsBoxItem"), "Already summarized: ready"),
                      "202": _json(_ref("NewsBoxItem"), "Scraping or summarizing: pending"),
                      **_ERRORS, **_NOT_FOUND}}},
    "/me/schedules/run": {"post": {
        "operationId": "runSchedule", "description": "Needs the summarize scope. Runs the schedule once, now.",
        "responses": {"202": _json(_obj(["run_id", "added"], run_id=_S, added=_I), "Run queued"), **_ERRORS}}},
}

SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "MuckScraper API", "version": API_VERSION},
    "servers": [{"url": "/api/v1"}],
    "security": [{"deviceToken": []}],
    "components": {
        "securitySchemes": {"deviceToken": {"type": "http", "scheme": "bearer"}},
        "schemas": SCHEMAS,
    },
    "paths": PATHS,
}


@api.route("/openapi.json")
def openapi_spec():
    return jsonify(SPEC)
