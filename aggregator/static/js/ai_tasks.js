/* Generate summary / analysis on the story and article pages. startAiTask()
 * queues the task, polls it with the bottom toast (ai_toast.js) and reloads
 * the page when it is done. While it runs, a shimmering skeleton stands in
 * the section the text will fill ([data-ai-slot="<task type>"]); if it fails
 * or times out, the section says so and offers Retry (Philip, 2026-10-27).
 *
 * Each task polls on its own, so Generate summary and Generate analysis can
 * run together. A task this tab started is remembered in sessionStorage:
 * when the first one finishes and the page reloads, the other's skeleton
 * comes back and its polling carries on.
 *
 * Loaded with data-start-url and data-status-url (TASK_TYPE / 999999 are
 * replaced). Styles: theme.css .ai-skeleton / .ai-slot-error. */
(function () {
    const script = document.currentScript;
    const startUrl = script.dataset.startUrl;
    const statusTemplate = script.dataset.statusUrl;
    const POLL_MS = 2000;
    const TIMEOUT_MS = 15 * 60 * 1000;
    const MAX_FETCH_FAILURES = 3;
    const STORE_KEY = 'muckscraper-ai-tasks:' + location.pathname;
    const polls = new Map();

    function statusUrl(taskType, resourceId) {
        return statusTemplate.replace('TASK_TYPE', taskType).replace('999999', String(resourceId));
    }

    function taskKey(taskType, resourceId) { return taskType + ':' + resourceId; }

    // Tasks this tab started on this page: {key: {taskType, resourceId, startedAt}}.
    function loadInFlight() {
        try { return JSON.parse(sessionStorage.getItem(STORE_KEY)) || {}; } catch (e) { return {}; }
    }
    function saveInFlight(tasks) {
        try {
            if (Object.keys(tasks).length) sessionStorage.setItem(STORE_KEY, JSON.stringify(tasks));
            else sessionStorage.removeItem(STORE_KEY);
        } catch (e) { /* private mode: the skeleton just won't survive a reload */ }
    }
    function remember(taskType, resourceId, startedAt) {
        const tasks = loadInFlight();
        tasks[taskKey(taskType, resourceId)] = { taskType, resourceId, startedAt };
        saveInFlight(tasks);
    }
    function forget(taskType, resourceId) {
        const tasks = loadInFlight();
        delete tasks[taskKey(taskType, resourceId)];
        saveInFlight(tasks);
    }

    // Every control that starts this task: the round rail button and the
    // one beside the section heading run the same task, so they go busy and
    // come back together (Philip, 2026-10-25: one consistent behaviour).
    function taskButtons(taskType, resourceId, button) {
        const call = "startAiTask('" + taskType + "', " + resourceId + ",";
        const found = [...document.querySelectorAll('button[onclick]')]
            .filter(b => b.getAttribute('onclick').startsWith(call));
        if (button && !found.includes(button)) found.push(button);
        return found;
    }

    function setBusy(buttons, busy) {
        buttons.forEach(b => {
            b.disabled = busy;
            // Icon buttons (ai_actions.html) pulse; text buttons say so.
            if (b.classList.contains('act')) b.classList.toggle('busy', busy);
            else if (busy) { if (!b.dataset.originalText) b.dataset.originalText = b.textContent; b.textContent = 'Working...'; }
            else if (b.dataset.originalText) b.textContent = b.dataset.originalText;
        });
    }

    function slotFor(taskType) {
        return document.querySelector('[data-ai-slot="' + taskType + '"]');
    }

    function line(cls) {
        const el = document.createElement('span');
        el.className = 'ai-skel-line' + (cls ? ' ' + cls : '');
        return el;
    }

    // The analysis gets a few headed blocks like its L/C/R sections; a
    // summary is one paragraph's worth of lines.
    function buildSkeleton(taskType) {
        const box = document.createElement('div');
        box.className = 'ai-skeleton';
        box.setAttribute('role', 'status');
        box.setAttribute('aria-label', 'Generating…');
        const blocks = taskType === 'story_deep_report' ? 3 : 1;
        for (let i = 0; i < blocks; i++) {
            const block = document.createElement('div');
            block.className = 'ai-skel-block';
            if (blocks > 1) block.appendChild(line('w30'));
            block.append(line(), line(), line(), line('w60'));
            box.appendChild(block);
        }
        return box;
    }

    // Hides what the section showed (the "not generated yet" line, or the
    // old text when regenerating) behind the skeleton, so an error can
    // bring it back.
    function showSkeleton(taskType) {
        const slot = slotFor(taskType);
        if (!slot) return;
        clearSlotState(slot);
        [...slot.children].forEach(c => { c.hidden = true; c.dataset.aiHidden = '1'; });
        slot.prepend(buildSkeleton(taskType));
    }

    function clearSlotState(slot) {
        slot.querySelectorAll(':scope > .ai-skeleton, :scope > .ai-slot-error').forEach(el => el.remove());
    }

    function restoreSlot(slot) {
        clearSlotState(slot);
        slot.querySelectorAll(':scope > [data-ai-hidden]').forEach(c => { c.hidden = false; delete c.dataset.aiHidden; });
    }

    function showError(taskType, resourceId, message) {
        const slot = slotFor(taskType);
        if (!slot) return;
        restoreSlot(slot);
        const box = document.createElement('div');
        box.className = 'ai-slot-error';
        box.setAttribute('role', 'alert');
        const text = document.createElement('span');
        text.textContent = message;
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'pill';
        retry.textContent = 'Retry';
        retry.addEventListener('click', () => startAiTask(taskType, resourceId, null));
        box.append(text, retry);
        slot.prepend(box);
    }

    function fail(taskType, resourceId, buttons, message) {
        stopPoll(taskType, resourceId);
        forget(taskType, resourceId);
        setBusy(buttons, false);
        showError(taskType, resourceId, message);
    }

    function stopPoll(taskType, resourceId) {
        const key = taskKey(taskType, resourceId);
        clearInterval(polls.get(key));
        polls.delete(key);
    }

    // resuming: polling a task this tab started before the page reloaded.
    // If the server has nothing running for it any more, drop it quietly
    // rather than showing a skeleton for a task that is not there.
    function poll(taskType, resourceId, buttons, startedAt, resuming) {
        stopPoll(taskType, resourceId);
        const key = taskKey(taskType, resourceId);
        let failures = 0;
        let checking = false;
        let first = true;
        polls.set(key, setInterval(async () => {
            if (checking) return;
            if (Date.now() - startedAt > TIMEOUT_MS) {
                aiToast('The ' + (taskType === 'story_deep_report' ? 'analysis' : 'summary') + ' is taking too long.', true);
                fail(taskType, resourceId, buttons, 'This is taking much longer than usual, so the page stopped waiting. It may still finish in the background; reload to check, or try again.');
                return;
            }
            checking = true;
            let status;
            try {
                const response = await fetch(statusUrl(taskType, resourceId));
                if (!response.ok) throw new Error('HTTP ' + response.status);
                status = await response.json();
                failures = 0;
            } catch (e) {
                checking = false;
                if (++failures >= MAX_FETCH_FAILURES) {
                    aiToast('Lost contact with the server.', true);
                    fail(taskType, resourceId, buttons, 'Lost contact with the server while this was being written. Reload to check, or try again.');
                }
                return;
            }
            checking = false;
            const wasFirst = first;
            first = false;

            if (status.status === 'success') {
                stopPoll(taskType, resourceId);
                forget(taskType, resourceId);
                window.location.reload();
                return;
            }
            if (status.status === 'error') {
                aiTaskToast(taskType, status, true);
                fail(taskType, resourceId, buttons, (status.message || 'It did not finish.') + ' Try again?');
                return;
            }
            if (resuming && wasFirst && status.status === 'idle') {
                stopPoll(taskType, resourceId);
                forget(taskType, resourceId);
                setBusy(buttons, false);
                const slot = slotFor(taskType);
                if (slot) restoreSlot(slot);
                return;
            }
            aiTaskToast(taskType, status, false);
        }, POLL_MS));
    }

    window.startAiTask = async function (taskType, resourceId, button) {
        const buttons = taskButtons(taskType, resourceId, button);
        setBusy(buttons, true);
        showSkeleton(taskType);
        aiTaskToast(taskType, { status: 'queued' }, false);

        let response, payload;
        try {
            response = await fetch(startUrl, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': document.querySelector('meta[name="csrf-token"]').content,
                },
                body: JSON.stringify({ task_type: taskType, resource_id: resourceId }),
            });
            payload = await response.json();
        } catch (e) {
            aiToast('Could not reach the server.', true);
            fail(taskType, resourceId, buttons, 'Could not reach the server to start this. Try again?');
            return;
        }

        aiTaskToast(taskType, payload.status || payload, response.status >= 400);

        // 409: it is already running, so follow that one.
        if (!response.ok && response.status !== 409) {
            fail(taskType, resourceId, buttons, ((payload.status || payload).message || 'Could not start it.') + ' Try again?');
            return;
        }

        const startedAt = Date.now();
        remember(taskType, resourceId, startedAt);
        poll(taskType, resourceId, buttons, startedAt, false);
    };

    // Pick up the tasks this tab was waiting on before the page reloaded.
    Object.values(loadInFlight()).forEach(t => {
        if (!slotFor(t.taskType) && !taskButtons(t.taskType, t.resourceId, null).length) {
            forget(t.taskType, t.resourceId);
            return;
        }
        const buttons = taskButtons(t.taskType, t.resourceId, null);
        setBusy(buttons, true);
        showSkeleton(t.taskType);
        poll(t.taskType, t.resourceId, buttons, t.startedAt || Date.now(), true);
    });
})();
