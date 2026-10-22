// Light/Dark for pages in the app frame (templates/_app_page.html): loaded
// in <head> so the saved theme applies before first paint. The button's Dark/Light
// text is switched by CSS (theme_toggle_label in icons.html), so this
// never rewrites it.
(function () {
    try {
        document.documentElement.setAttribute('data-theme', localStorage.getItem('muckscraper-theme') || 'light');
    } catch (e) {}
})();

function toggleTheme() {
    const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('muckscraper-theme', next); } catch (e) {}
}
