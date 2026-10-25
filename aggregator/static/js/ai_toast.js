/* Progress for the AI tasks (summaries, multi-source analysis) as a toast at
 * the bottom of the screen, like the read Undo toast (Philip, 2026-10-25:
 * not a box at the top of the page). Pages call aiToast(message, isError);
 * it stays while the task runs (the page reloads when it is done) and an
 * error fades after a few seconds. Styles: theme.css .ai-toast. */
(function () {
    let el = null;
    let timer = null;

    window.aiToast = function (message, isError) {
        if (!el) {
            el = document.createElement('div');
            el.className = 'ai-toast';
            el.setAttribute('role', 'status');
            el.setAttribute('aria-live', 'polite');
            document.body.appendChild(el);
        }
        clearTimeout(timer);
        el.textContent = message || 'Working…';
        el.classList.toggle('is-error', !!isError);
        el.classList.add('show');
        if (isError) timer = setTimeout(() => el.classList.remove('show'), 8000);
    };

    const NAMES = {
        story_summary: 'executive summary',
        story_deep_report: 'multi-source analysis',
        article_summary: 'article summary',
    };

    // A task's status from /ai-task status: says which task while it is
    // queued or running, the server's own message when it failed.
    window.aiTaskToast = function (taskType, status, isError) {
        status = status || {};
        const name = NAMES[taskType];
        if (!isError && name && status.status !== 'success') {
            aiToast('Generating ' + name + '… the page reloads when it is done.', false);
        } else {
            aiToast(status.message || 'AI task update received.', isError);
        }
    };
})();
