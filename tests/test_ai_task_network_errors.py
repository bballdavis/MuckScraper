"""Exercise the actual reader scripts when task HTTP boundaries fail."""
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("template", ["article", "story"])
@pytest.mark.parametrize("failure", ["network", "non_json", "poll_http", "poll_network", "already_running", "terminal_error"])
def test_task_controls_recover(template, failure):
    playwright = pytest.importorskip("playwright.sync_api")
    source = (ROOT / "aggregator/templates" / (template + ".html")).read_text()
    source = source[source.index("    const aiTaskStartUrl"):]
    source = source[:source.index("</script>")]
    source = re.sub(r"{{.*?}}", "/task", source)
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(3000)
        page.set_content('<meta name="csrf-token" content="fixture"><div id="ai-task-status"></div><button id="run">Summarize</button>')
        page.evaluate("""() => {
            window.polls = [];
            window.setInterval = fn => { polls.push(fn); return polls.length; };
            window.clearInterval = () => {};
        }""")
        page.add_script_tag(content=source)
        result = page.evaluate("""async failure => {
            let starts = 0;
            window.fetch = async (url, options) => {
                if (options) {
                    starts++;
                    if (failure === 'network') throw Error('Disconnected');
                    if (failure === 'non_json') return {json: async () => {throw Error('Invalid JSON');}};
                    return {ok: failure !== 'already_running', status: failure === 'already_running' ? 409 : 200,
                            json: async () => ({status: {status: 'running', message: 'Running'}})};
                }
                if (failure === 'poll_network') throw Error('Disconnected');
                return {ok: failure !== 'poll_http', json: async () => ({status: 'error', message: 'Task stopped'})};
            };
            const button = document.querySelector('#run');
            await startAiTask('article_summary', 7, button);
            if (polls.length) await polls[0]();
            return {disabled: button.disabled, label: button.textContent, starts,
                    message: document.querySelector('#ai-task-status').textContent};
        }""", failure)
        assert result["starts"] == 1  # Never automatically repeat a chargeable POST.
        assert result["disabled"] is False
        assert result["label"] == "Summarize"
        assert result["message"]
        if failure in {"network", "non_json"}:
            assert "could not be confirmed" in result["message"]
        if failure in {"poll_http", "poll_network"}:
            assert "Could not check progress" in result["message"]
        browser.close()
