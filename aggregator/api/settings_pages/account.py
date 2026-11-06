"""Your account: what the web app's profile page sets, for any paired device.
The app's own Reading page (auto mark-as-read) goes through /me/preferences,
because the phone acts on it."""
from flask import g

from aggregator import appearance_prefs, feed_prefs, source_priority
from aggregator.accounts import email_error, password_error

from . import page
from . import schema as s
from .schema import FieldError


def _has_password():
    return bool(g.user.password_hash)


@page("profile", "Profile", "account", "user", detail=lambda: g.user.username)
def profile(_=None):
    user = g.user
    sections = [s.section("who", facts=[
        s.fact("Username", user.username),
        s.fact("Role", user.role_label),
        s.fact("Signs in with", "Password" if _has_password() else "Sign-in provider"),
    ])]
    if _has_password():
        sections += [
            s.section("email", "Email", fields=[
                s.email("email", "Email", user.email),
                s.password("current_password", "Current password"),
            ], footer="Changing your email asks for your current password."),
            s.section("password", "Password", fields=[
                s.password("current_password", "Current password"),
                s.password("new_password", "New password", help="At least 8 characters."),
                s.password("confirm_password", "New password again"),
            ]),
        ]
    else:
        sections.append(s.section("email", "Email", facts=[s.fact("Email", user.email)],
                                  footer="Your sign-in provider looks after your email and password."))
    return s.page("profile", "Profile", sections)


@profile.save
def save_profile(_, values, section):
    user = g.user
    if section not in ("email", "password") or not _has_password():
        raise FieldError("Nothing to save here.")
    if not user.check_password(values.get("current_password") or ""):
        raise FieldError("Your current password was not correct.", "current_password")
    if section == "email":
        address = s.get_str(values, "email", 254, required=True, label="Email")
        error = email_error(address, exclude_id=user.id)
        if error:
            raise FieldError(error, "email")
        user.email = address
        return "Email address updated."
    new = values.get("new_password") or ""
    error = password_error(new, values.get("confirm_password") or "")
    if error:
        raise FieldError(error, "new_password")
    user.set_password(new)
    return "Password changed."


@page("feed-defaults", "Web feed defaults", "account", "feed")
def feed_defaults(_=None):
    prefs = feed_prefs.load(g.user)
    return s.page("feed-defaults", "Web feed defaults", [
        s.section("feed", fields=[
            s.choice("start_page", "Start page", prefs["start_page"], feed_prefs.START_PAGES.items()),
            s.choice("range", "Time range", prefs["range"], feed_prefs.RANGES.items()),
            s.toggle("hide_single", "Hide single-source stories", prefs["hide_single"]),
        ], save="auto", footer="Where the web app opens and how its story lists filter when you sign in there."),
    ])


@feed_defaults.save
def save_feed_defaults(_, values, section):
    prefs = feed_prefs.load(g.user)
    if "start_page" in values:
        prefs["start_page"] = s.get_choice(values, "start_page", feed_prefs.START_PAGES)
    if "range" in values:
        prefs["range"] = s.get_choice(values, "range", feed_prefs.RANGES)
    if "hide_single" in values:
        prefs["hide_single"] = s.get_bool(values, "hide_single")
    feed_prefs.save(g.user, prefs)
    return None


@page("appearance", "Web appearance", "account", "appearance")
def appearance(_=None):
    prefs = appearance_prefs.load(g.user)
    return s.page("appearance", "Web appearance", [
        s.section("layout", fields=[
            s.choice("analysis_layout", "Story analysis", prefs["analysis_layout"],
                     appearance_prefs.ANALYSIS_LAYOUTS.items()),
        ], save="auto", footer="How the web app's story page lays out its analysis. The app has its own layout."),
    ])


@appearance.save
def save_appearance(_, values, section):
    layout = s.get_choice(values, "analysis_layout", appearance_prefs.ANALYSIS_LAYOUTS)
    appearance_prefs.save(g.user, {"analysis_layout": layout})
    return None


@page("boosted", "Boosted sources", "account", "boost",
      detail=lambda: str(len(source_priority.effective_names(g.user.id))))
def boosted(_=None):
    names = source_priority.effective_names(g.user.id)
    return s.page("boosted", "Boosted sources", [
        s.section("boosted", items=[s.item(name, name, remove="remove") for name in names],
                  empty="No outlets boosted.",
                  actions=[s.action("add", "Boost an outlet", fields=[
                      s.text("name", "Outlet", placeholder="The Texas Tribune", required=True,
                             help="As it's named on its stories."),
                  ])],
                  footer="A boosted outlet's stories show in Trending even alone, and come first in Latest "
                         "and the Swipe deck. The server's admin starts everyone with a list; yours changes it "
                         "for you only."),
    ])


@boosted.action("add")
def boost_outlet(_, values):
    name = s.get_str(values, "name", 120, required=True, label="Outlet")
    source_priority.set_boost(g.user.id, name, True)
    return {"message": f"Boosted {name}."}


@boosted.action("remove")
def unboost_outlet(_, values):
    name = s.get_str(values, "id", 120, required=True, label="Outlet")
    source_priority.set_boost(g.user.id, name, False)
    return {"message": f"Stopped boosting {name}."}
