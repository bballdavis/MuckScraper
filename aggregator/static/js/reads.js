/*
 * Marking stories and articles read (aggregator/reads.py, /read/<kind>/<id>).
 *
 * - Headlines: the reading-glasses button beside each card (or in its action
 *   row on touch screens) marks the story read. The card slides out, the
 *   ones below slide up, and a toast offers Undo for five seconds.
 * - Recently Read: the Mark unread button takes a row off the list the same
 *   way, with the same Undo.
 * - Story and article pages: .read-toggle flips the read state in place.
 *   With data-auto-read="<seconds>" on the script tag (the reader's Reading
 *   choice, aggregator/reading_prefs.py; 0 is off), an unread page is marked
 *   read once it has been visible that long, with the same toast and Undo.
 *
 * Load with data-read-url (see urlTemplate). Needs <meta name="csrf-token">. Styles: static/css/reads.css.
 */
(function () {
    const UNDO_MS = 5000;
    // url_for('reading.toggle_read', kind='KIND', item_id=0), from the script tag.
    const script = document.currentScript;
    const urlTemplate = (script && script.dataset.readUrl) || '/read/KIND/0';
    const autoReadSeconds = parseInt((script && script.dataset.autoRead) || '0', 10) || 0;
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

    function readUrl(kind, id) {
        return urlTemplate.replace('KIND', encodeURIComponent(kind)).replace(/0$/, encodeURIComponent(id));
    }

    async function setRead(kind, id, read) {
        const token = document.querySelector('meta[name="csrf-token"]');
        const response = await fetch(readUrl(kind, id), {
            method: read ? 'POST' : 'DELETE',
            headers: { 'X-CSRFToken': token ? token.content : '', 'Accept': 'application/json' },
            credentials: 'same-origin',
        });
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
    }

    function wait(ms) {
        return new Promise(resolve => setTimeout(resolve, reduceMotion.matches ? 0 : ms));
    }

    // Slide a row out to the right, then close the gap it leaves.
    async function slideOut(el) {
        el.style.height = el.offsetHeight + 'px';
        void el.offsetHeight;
        el.classList.add('read-leaving');
        await wait(220);
        el.classList.add('read-collapsing');
        await wait(260);
        el.hidden = true;
    }

    // The reverse, for Undo: open the gap, then slide the row back in.
    async function slideIn(el) {
        el.hidden = false;
        void el.offsetHeight;
        el.classList.remove('read-collapsing');
        await wait(260);
        el.classList.remove('read-leaving');
        await wait(220);
        el.style.height = '';
    }

    // ── Toast ──
    let toast = null;
    let toastTimer = null;
    let pending = null;   // { el, onExpire } for the change the toast can undo; el is the row it brings back, if any
    const UNDO_ICON = '<svg class="icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' +
        '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5a5.5 5.5 0 0 1-5.5 5.5H11"/></svg>';

    function ensureToast() {
        if (toast) return toast;
        toast = document.createElement('div');
        toast.className = 'read-toast';
        toast.setAttribute('role', 'status');
        toast.setAttribute('aria-live', 'polite');
        toast.innerHTML = '<span class="read-toast-text"></span>' +
            '<button type="button" class="read-toast-undo" aria-label="Undo" title="Undo">' + UNDO_ICON + '</button>' +
            '<span class="read-toast-bar" aria-hidden="true"></span>';
        document.body.appendChild(toast);
        return toast;
    }

    function finishPending() {
        clearTimeout(toastTimer);
        if (pending) {
            const done = pending;
            pending = null;
            if (done.el) done.el.remove();
            if (done.onExpire) done.onExpire();
        }
        if (toast) toast.classList.remove('show');
    }

    function showToast(message, el, undo, onExpire) {
        finishPending();
        const t = ensureToast();
        t.querySelector('.read-toast-text').textContent = message;
        const button = t.querySelector('.read-toast-undo');
        button.hidden = !undo;
        const entry = undo ? { el, onExpire } : null;
        button.onclick = async () => {
            if (!entry || pending !== entry) return;
            clearTimeout(toastTimer);
            pending = null;
            t.classList.remove('show');
            try {
                await undo();
                if (el) await slideIn(el);
            } catch (e) {
                showToast("Couldn't undo. Try again from Recently Read.", null, null, null);
            }
        };
        pending = entry;
        // Restart the countdown bar.
        t.classList.remove('show');
        void t.offsetWidth;
        t.classList.add('show');
        toastTimer = setTimeout(finishPending, UNDO_MS);
    }

    // Take a row away after a successful request, with Undo.
    async function dismiss(el, request, undo, message, onExpire) {
        if (el.dataset.busy) return;
        el.dataset.busy = '1';
        try {
            await request();
        } catch (e) {
            delete el.dataset.busy;
            showToast("Couldn't save that. Try again.", null, null, null);
            return;
        }
        await slideOut(el);
        delete el.dataset.busy;
        showToast(message, el, undo, onExpire);
    }

    // ── Headlines ──
    function remainingCards() {
        return document.querySelectorAll('.hl-item:not([hidden])').length;
    }

    document.addEventListener('click', event => {
        const button = event.target.closest('[data-mark-read]');
        if (button) {
            event.preventDefault();
            const item = button.closest('.hl-item');
            const id = item.dataset.storyId;
            button.blur();
            dismiss(item,
                () => setRead('story', id, true),
                () => setRead('story', id, false),
                'Marked as read',
                // The last card on the page went: load the page again so the
                // next stories move up into it.
                () => { if (!remainingCards()) window.location.reload(); });
            return;
        }

        const unmark = event.target.closest('[data-mark-unread]');
        if (unmark) {
            event.preventDefault();
            const row = unmark.closest('.rr-row');
            const kind = row.dataset.kind, id = row.dataset.id;
            unmark.blur();
            dismiss(row,
                () => setRead(kind, id, false),
                () => setRead(kind, id, true),
                'Marked as unread',
                () => {
                    if (!document.querySelector('.rr-row')) window.location.reload();
                });
            return;
        }

        const toggle = event.target.closest('.read-toggle');
        if (toggle) {
            event.preventDefault();
            flipToggle(toggle);
        }
    });

    // ── Story and article pages ──
    function showToggleState(toggle, read) {
        toggle.setAttribute('aria-pressed', String(read));
        toggle.classList.toggle('is-read', read);
        const title = toggle.querySelector('.tip strong');
        const body = toggle.querySelector('.tip-body');
        if (title) title.textContent = read ? toggle.dataset.readTitle : toggle.dataset.unreadTitle;
        if (body) body.textContent = read ? toggle.dataset.readBody : toggle.dataset.unreadBody;
        toggle.setAttribute('aria-label', read ? toggle.dataset.readTitle : toggle.dataset.unreadTitle);
    }

    async function flipToggle(toggle) {
        if (toggle.disabled) return;
        // The reader chose for themselves: the auto timer stands down.
        stopAutoRead();
        const next = toggle.getAttribute('aria-pressed') !== 'true';
        toggle.disabled = true;
        try {
            await setRead(toggle.dataset.kind, toggle.dataset.id, next);
            showToggleState(toggle, next);
        } catch (e) {
            showToast("Couldn't save that. Try again.", null, null, null);
        } finally {
            toggle.disabled = false;
        }
    }

    // Auto mark-as-read: count the seconds the page is visible (a hidden or
    // backgrounded tab doesn't count), then mark it read once, with Undo.
    // Pages that open already read, or with the setting off, never start.
    let autoTimer = null;

    function stopAutoRead() {
        clearInterval(autoTimer);
        autoTimer = null;
    }

    function startAutoRead() {
        const toggle = document.querySelector('.read-toggle');
        if (!autoReadSeconds || !toggle || toggle.getAttribute('aria-pressed') === 'true') return;
        let visibleMs = 0;
        let last = document.visibilityState === 'visible' ? Date.now() : null;
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible') {
                last = Date.now();
            } else if (last !== null) {
                visibleMs += Date.now() - last;
                last = null;
            }
        });
        autoTimer = setInterval(async () => {
            const seen = visibleMs + (last !== null ? Date.now() - last : 0);
            if (seen < autoReadSeconds * 1000) return;
            stopAutoRead();
            if (toggle.getAttribute('aria-pressed') === 'true') return;
            const kind = toggle.dataset.kind, id = toggle.dataset.id;
            try {
                await setRead(kind, id, true);
            } catch (e) {
                return;   // Quietly: the reader didn't ask for this one.
            }
            showToggleState(toggle, true);
            showToast(kind === 'story' ? 'Story marked as read' : 'Article marked as read', null, async () => {
                await setRead(kind, id, false);
                showToggleState(toggle, false);
            }, null);
        }, 1000);
    }

    startAutoRead();

    // Leaving the page while a toast is up keeps the change (it is already
    // saved); nothing to flush.
})();
