"""Keyboard and pointer navigation using the actual shared sidebar."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("width,height", [(393, 852), (852, 393), (800, 1024), (1440, 1000)])
def test_navigation_does_not_cover_brand_and_restores_focus(width, height):
    playwright = pytest.importorskip("playwright.sync_api")
    env = Environment(loader=FileSystemLoader(ROOT / "aggregator/templates"))
    env.globals.update(url_for=lambda endpoint, **kw: "/" + endpoint, can=lambda role: False,
                       current_user=SimpleNamespace(is_authenticated=False))
    html = env.from_string("""<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
      <style>body {margin:0} .layout {display:flex} .page-body {flex:1;min-width:0} .topbar {display:flex;align-items:center;padding:12px}
      {% include 'sidebar_styles.html' %}</style></head><body><div class="layout">{% include 'sidebar_nav.html' %}
      <div class="page-body"><header class="topbar"><strong id="brand">MuckScraper</strong></header><main><h1>Example</h1><button>Read story</button></main></div></div>
      <script>{% include 'sidebar_script.html' %}</script></body></html>""").render()
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_content(html)
        button = page.locator('#sidebar-toggle')
        brand = page.locator('#brand').bounding_box()
        box = button.bounding_box()
        assert box['x'] + box['width'] <= brand['x']
        assert box['width'] >= 44 and box['height'] >= 44
        if width <= 768 or height <= 500:
            assert button.get_attribute('aria-expanded') == 'false'
            button.click()
            assert button.get_attribute('aria-expanded') == 'true'
            assert page.locator('.sidebar-close').evaluate('(el) => el === document.activeElement')
            page.keyboard.press('Shift+Tab')
            assert page.locator('#sidebar a').last.evaluate('(el) => el === document.activeElement')
            page.keyboard.press('Escape')
            assert button.evaluate('(el) => el === document.activeElement')
            assert page.locator('#sidebar').evaluate('(el) => el.inert')
        else:
            button.click()
            assert page.locator('#sidebar').evaluate('(el) => el.inert')
            button.click()
            assert not page.locator('#sidebar').evaluate('(el) => el.inert')
        assert not errors
        browser.close()


@pytest.mark.parametrize("width,height", [(393, 852), (1440, 1000)])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_menu_uses_header_foreground_on_real_headlines_page(width, height, theme):
    playwright = pytest.importorskip("playwright.sync_api")
    env = Environment(loader=FileSystemLoader(ROOT / "aggregator/templates"))
    env.filters["get_big_picture"] = lambda value: value
    env.globals.update(url_for=lambda endpoint, **kw: "/static/" + kw["filename"] if endpoint == "static" else "/" + endpoint,
                       can=lambda role: False, current_user=SimpleNamespace(is_authenticated=False),
                       request=SimpleNamespace(query_string="", path="/headlines"),
                       config={"DISPLAY_TIMEZONE": "UTC"}, current_back=lambda: "/headlines")
    html = env.get_template("headlines_feed.html").render(stories=[], page=1, total_pages=1,
        edition=None, edition_label="Synthetic review edition", is_past_edition=False)
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height})
        def route(request):
            if request.request.url == "https://reader.example/":
                request.fulfill(body=html, content_type="text/html")
            elif "/static/css/" in request.request.url:
                path = ROOT / "aggregator/static/css" / request.request.url.rsplit("/", 1)[-1]
                request.fulfill(body=path.read_bytes(), content_type="text/css")
            else:
                request.abort()
        page.route("**/*", route)
        page.goto("https://reader.example/")
        page.evaluate("theme => document.documentElement.dataset.theme = theme", theme)
        button = page.locator("#sidebar-toggle")
        assert button.evaluate("el => getComputedStyle(el).color") == page.locator(".topbar-title").evaluate("el => getComputedStyle(el).color")
        box = button.bounding_box()
        brand = page.locator(".topbar-title").bounding_box()
        assert box["x"] + box["width"] <= brand["x"]
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        browser.close()
