/*
 * Rate lean dialog: openLeanModal(articleId) from the article page's gauge
 * button and the Articles list's Rate bias.
 *
 * Opens with one Left-to-Right scale and asks for the AI's rating
 * (admin.article_lean_ai), which comes back with a one-line reason. The AI's
 * pick is filled in with the reason underneath; picking another lean says
 * "You changed it from the AI's ..." and opens an optional note. Save posts
 * the lean (admin.save_article_lean) and reloads, so the chip and the
 * confirmation note update. Nothing changes until Save.
 *
 * Load with data-ai-url and data-save-url (url_for with article_id=0), and
 * data-report="1" where the page shows a "Rated this article ..." note.
 * Needs <meta name="csrf-token">. Styles: static/css/lean_modal.css.
 */
(function () {
    const script = document.currentScript;
    const aiUrl = (script && script.dataset.aiUrl) || '/admin/article/0/lean/ai';
    const saveUrl = (script && script.dataset.saveUrl) || '/admin/article/0/lean';
    const report = !!(script && script.dataset.report);
    const LABELS = { 1: 'Left', 2: 'Lean Left', 3: 'Center', 4: 'Lean Right', 5: 'Right' };
    const CLOSE_ICON = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';

    function urlFor(template, id) {
        return template.replace('/0/', '/' + encodeURIComponent(id) + '/');
    }

    async function post(url, body) {
        const token = document.querySelector('meta[name="csrf-token"]');
        const response = await fetch(url, {
            method: 'POST',
            headers: { 'X-CSRFToken': token ? token.content : '', 'Accept': 'application/json', 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(body || {}),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
        return data;
    }

    function el(tag, attrs, text) {
        const node = document.createElement(tag);
        Object.entries(attrs || {}).forEach(([k, v]) => node.setAttribute(k, v));
        if (text != null) node.textContent = text;
        return node;
    }

    function build() {
        const dialog = el('dialog', { class: 'lean-modal', 'aria-labelledby': 'lean-modal-title' });
        dialog.innerHTML =
            '<div class="lean-modal-head"><h2 id="lean-modal-title">How does this article lean?</h2>' +
            '<button type="button" class="lean-modal-x" aria-label="Close">' + CLOSE_ICON + '</button></div>' +
            '<div class="lean-scale" role="radiogroup" aria-label="Lean"></div>' +
            '<div class="lean-under" aria-live="polite"></div>' +
            '<textarea class="lean-note" rows="2" maxlength="500" placeholder="Why? (optional)" aria-label="Why? (optional)" hidden></textarea>' +
            '<p class="lean-error" role="alert" hidden></p>' +
            '<div class="lean-btns"><button type="button" class="lean-btn lean-btn-ghost">Cancel</button>' +
            '<button type="button" class="lean-btn lean-btn-primary" disabled>Save</button></div>';
        const scale = dialog.querySelector('.lean-scale');
        for (let score = 1; score <= 5; score++) {
            scale.appendChild(el('button', { type: 'button', role: 'radio', 'aria-checked': 'false', 'data-score': score, disabled: '' }, LABELS[score]));
        }
        document.body.appendChild(dialog);
        return dialog;
    }

    let dialog = null;

    window.openLeanModal = function (articleId) {
        if (!dialog) dialog = build();
        const scale = dialog.querySelector('.lean-scale');
        const under = dialog.querySelector('.lean-under');
        const note = dialog.querySelector('.lean-note');
        const error = dialog.querySelector('.lean-error');
        const save = dialog.querySelector('.lean-btn-primary');
        const buttons = Array.from(scale.querySelectorAll('button'));
        let ai = null, own = null, aiError = null, picked = null, saving = false;

        function line(sub, why) {
            under.replaceChildren();
            if (sub) under.appendChild(el('p', { class: 'lean-sub' }, sub));
            if (why) under.appendChild(el('p', { class: 'lean-why' }, why));
        }

        function render() {
            buttons.forEach(b => {
                const on = Number(b.dataset.score) === picked;
                b.setAttribute('aria-checked', on ? 'true' : 'false');
                b.tabIndex = on || (picked === null && b.dataset.score === '1') ? 0 : -1;
            });
            const aiSays = ai && ai.reason ? 'AI: ' + ai.reason : null;
            if (picked === null) {
                line(aiError, null);
            } else if (own && own.source === 'reader' && picked === own.score) {
                line('Your rating', ai ? 'AI said ' + ai.label + (ai.reason ? ': ' + ai.reason : '.') : null);
            } else if (ai && picked === ai.score) {
                line('Picked by the AI', ai.reason);
            } else if (ai) {
                line("You changed it from the AI's " + ai.label, aiSays);
            } else {
                line(aiError, null);
            }
            note.hidden = picked === null || (ai && picked === ai.score && !(own && own.source === 'reader' && own.score === picked));
            save.disabled = picked === null || saving;
            save.textContent = picked === null ? 'Save' : 'Save ' + LABELS[picked];
        }

        // Reset for this article, then ask for the AI's rating.
        picked = null;
        note.value = '';
        error.hidden = true;
        buttons.forEach(b => { b.disabled = true; });
        save.disabled = true;
        save.textContent = 'Save';
        note.hidden = true;
        under.replaceChildren(el('div', { class: 'lean-loading' }));
        under.firstChild.append(el('span', { class: 'lean-spin', 'aria-hidden': 'true' }), 'Reading the article…');

        dialog.onclose = () => { dialog.dataset.article = ''; };
        dialog.querySelector('.lean-modal-x').onclick = () => dialog.close();
        dialog.querySelector('.lean-btn-ghost').onclick = () => dialog.close();
        // A click on the backdrop lands on the dialog itself, outside its box.
        dialog.onclick = e => {
            if (e.target !== dialog) return;
            const r = dialog.getBoundingClientRect();
            if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) dialog.close();
        };
        buttons.forEach(b => {
            b.onclick = () => { picked = Number(b.dataset.score); render(); };
            b.onkeydown = e => {
                const step = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[e.key];
                if (!step || b.disabled) return;
                e.preventDefault();
                picked = Math.min(5, Math.max(1, (picked || Number(b.dataset.score)) + (picked ? step : 0)));
                render();
                buttons[picked - 1].focus();
            };
        });
        save.onclick = async () => {
            if (picked === null || saving) return;
            saving = true;
            render();
            try {
                await post(urlFor(saveUrl, articleId), { score: picked, note: note.hidden ? '' : note.value, report: report });
                window.location.reload();
            } catch (e) {
                saving = false;
                error.textContent = "Couldn't save the rating. " + (e.message || '');
                error.hidden = false;
                render();
            }
        };

        dialog.dataset.article = String(articleId);
        if (!dialog.open) dialog.showModal();

        post(urlFor(aiUrl, articleId)).then(data => {
            if (dialog.dataset.article !== String(articleId)) return;
            ai = data.ai; own = data.own; aiError = data.error || null;
            picked = own ? own.score : (ai ? ai.score : null);
            if (own && own.source === 'reader' && own.note) note.value = own.note;
            if (!ai && !aiError) aiError = 'Pick a lean.';
        }).catch(() => {
            aiError = "The AI couldn't be reached. You can still pick a lean.";
        }).finally(() => {
            if (dialog.dataset.article !== String(articleId)) return;
            buttons.forEach(b => { b.disabled = false; });
            render();
        });
    };
})();
