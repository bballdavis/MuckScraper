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


@pytest.mark.parametrize("template", ["article", "story"])
@pytest.mark.parametrize("same_task", [False, True])
def test_late_poll_failure_cannot_cancel_another_poll(template, same_task):
    playwright = pytest.importorskip("playwright.sync_api")
    source = (ROOT / "aggregator/templates" / (template + ".html")).read_text()
    source = source[source.index("    const aiTaskStartUrl"):].split("</script>", 1)[0]
    source = re.sub(r"{{.*?}}", "/task", source)
    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content('<meta name="csrf-token" content="fixture"><div id="ai-task-status"></div><button id="a">A</button><button id="b">B</button>')
        page.evaluate("""() => {
            window.polls = []; window.cancelled = [];
            window.setInterval = fn => { polls.push(fn); return polls.length; };
            window.clearInterval = id => cancelled.push(id);
        }""")
        page.add_script_tag(content=source)
        result = page.evaluate("""async sameTask => {
            let rejectOld;
            window.fetch = async (url, options) => options
                ? {ok: true, status: 200, json: async () => ({status: {status: 'running'}})}
                : new Promise((resolve, reject) => { rejectOld = reject; });
            const a = document.querySelector('#a'), b = document.querySelector('#b');
            await startAiTask('article_summary', 7, a);
            const oldRequest = polls[0]();
            await startAiTask('article_summary', sameTask ? 7 : 8, b);
            rejectOld(Error('Late failure'));
            await oldRequest;
            const newerStillRunning = !cancelled.includes(2) && b.disabled;
            window.fetch = async () => ({ok: true, json: async () => ({status: 'error', message: 'Finished newer task'})});
            await polls[1]();
            return {newerStillRunning, aDisabled: a.disabled, bDisabled: b.disabled,
                    label: b.textContent, message: document.querySelector('#ai-task-status').textContent};
        }""", same_task)
        assert result["newerStillRunning"]
        assert not result["aDisabled"] and not result["bDisabled"]
        assert result["label"] == "B"
        assert result["message"] == "Finished newer task"
        browser.close()
