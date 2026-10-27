// Light/Dark for pages in the app frame (templates/_app_page.html): loaded
// in <head> so the saved theme applies before first paint. The button's Dark/Light
// text is switched by CSS (theme_toggle_label in icons.html), so this
// never rewrites it.
// It also sets html[data-sidebar] to the saved menu state, so the sidebar
// (sidebar_styles.html) and the centred search (feed_header.css) are in place
// at first paint instead of sliding there once sidebar_script.html runs at the
// end of the page.
(function () {
    try {
        document.documentElement.setAttribute('data-theme', localStorage.getItem('muckscraper-theme') || 'light');
        document.documentElement.setAttribute('data-sidebar',
            localStorage.getItem('muckscraper-sidebar') === 'collapsed' ? 'collapsed' : 'expanded');
    } catch (e) {}
})();

function toggleTheme() {
    const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('muckscraper-theme', next); } catch (e) {}
}
