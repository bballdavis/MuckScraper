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
_LEANING = {"type": "string", "enum": ["left", "center", "right", "unrated"]}
_PARAS = {"type": "array", "items": _S}


def _by_leaning(item):
    return _obj(["left", "center", "right", "unrated"],
                **{side: {"type": "array", "items": item} for side in ("left", "center", "right", "unrated")})


def _page(item):
    return _obj(["items", "next_cursor"], items={"type": "array", "items": item}, next_cursor=_NS)


SCHEMAS = {
    "Error": _obj(["error"], error=_obj(["code", "message"], code=_S, message=_S)),
    "Health": _obj(["ok", "version"], ok=_B, version=_S),
    "Coverage": _obj(["left", "center", "right"], left=_I, center=_I, right=_I),
    "Outlet": _obj(["id", "name", "leaning"], id=_NI, name=_S, leaning=_LEANING),
    "StorySummary": _obj(
        ["id", "section", "headline", "dek", "image_url", "coverage", "source_count", "updated_at",
         "is_summarized", "has_updates"],
        id=_I, section=_S, headline=_S, dek=_NS, image_url=_URL, coverage=_ref("Coverage"),
        source_count=_I, updated_at=_DT, is_summarized=_B, has_updates=_B),
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
                   id=_I, name=_S, scopes={"type": "array", "items": _S}, created_at=_DT, expires_at=_NDT),
    "Preferences": _obj(["start_page", "range", "hide_single"],
                        start_page={"type": "string", "enum": ["headlines", "all", "grouped"]},
                        range={"type": "string", "enum": ["24h", "7d", "30d", "all"]},
                        hide_single=_B),
    "SearchHit": {"type": "object", "required": ["kind", "id", "title"], "properties": {
        "kind": {"type": "string", "enum": ["story", "article"]}, "id": _I, "story_id": _NI, "title": _S,
        "image_url": _URL, "source_count": _I, "updated_at": _NDT, "outlet": _ref("Outlet"),
        "leaning": _LEANING, "published_at": _NDT}},
}
SCHEMAS["Edition"] = {"allOf": [_ref("EditionBrief"), _obj(["stories"], stories={"type": "array", "items": _ref("StorySummary")})]}
SCHEMAS["StoryDetail"] = {"allOf": [_ref("StorySummary"), _obj(
    ["title", "topics", "created_at", "summary", "deep_report", "opinion_roundup", "articles", "copies_hidden", "opinion"],
    title=_S, topics=_PARAS, created_at=_NDT, summary=_PARAS, deep_report=_PARAS, opinion_roundup=_PARAS,
    articles=_by_leaning(_ref("ArticleCard")), copies_hidden=_I, opinion=_by_leaning(_ref("OpinionPiece")))]}
SCHEMAS["ArticleDetail"] = {"allOf": [_ref("ArticleCard"), _obj(
    ["topics", "analysis", "content_html"], topics=_PARAS, analysis=_PARAS,
    content_html={"type": ["string", "null"], "description": "Sanitized scraped HTML; null unless the server allows full text."})]}


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
    "/stories/{story_id}": _get("story", _json(_ref("StoryDetail")), [_path_id("story_id")], _NOT_FOUND),
    "/articles/{article_id}": _get("article", _json(_ref("ArticleDetail")), [_path_id("article_id")], _NOT_FOUND),
    "/search": _get("search", _json(_obj(["items"], items={"type": "array", "items": _ref("SearchHit")})),
                    [_q("q", _S), _q("kind", {"type": "string", "enum": ["stories", "articles"]}), _RANGE, _LIMIT]),
    "/openapi.json": _get("openapiSpec", {"description": "This document"}, auth=False),
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
