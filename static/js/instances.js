/* Запуск дополнительных аккаунтов из одного проекта. */
(function () {
    'use strict';
    const $ = id => document.getElementById(id);
    let fetching = false, timer = null;
    const busy = new Set();
    function el(tag, text, className) {
        const node = document.createElement(tag);
        if (text !== undefined) node.textContent = text;
        if (className) node.className = className;
        return node;
    }
    function action(row, label, operation, primary) {
        const button = el('button', label, primary ? 'btn btn-primary' : 'btn');
        button.type = 'button'; button.disabled = busy.has(row.id);
        button.addEventListener('click', async () => {
            if (busy.has(row.id)) return;
            busy.add(row.id); button.disabled = true;
            try { await App.postJSON('/api/instances/' + row.id + '/' + operation, {}); }
            finally { busy.delete(row.id); await refresh(); }
        });
        return button;
    }
    function render(data) {
        const main = $('instances-main'); main.replaceChildren();
        main.append(el('strong', data.main.name), el('p', data.main.account_name || 'Основной аккаунт', 'hint'));
        const list = $('instances-list'); list.replaceChildren();
        data.instances.forEach(row => {
            const card = el('div', undefined, 'instance-card');
            card.append(el('strong', row.name), el('p', row.account_name + ' · порт ' + row.port, 'hint'), el('p', row.message));
            card.append(el('p', 'Ключи: Gemini — ' + row.keys.gemini + ', OpenAI — ' + row.keys.openai, 'hint'));
            if (row.state === 'error' || row.state === 'unavailable' || row.state === 'stopped') card.append(el('p', 'Журнал запуска: ' + row.log, 'hint'));
            const actions = el('div', undefined, 'instance-actions');
            if (row.state === 'stopped') {
                actions.append(action(row, 'Запустить', 'start', true), action(row, 'Убрать из списка', 'delete'));
            } else {
                if (row.state !== 'starting' && row.state !== 'unavailable') {
                    const link = el('a', row.state === 'login' ? 'Войти в аккаунт' : 'Открыть', 'btn btn-primary');
                    link.href = row.url; link.target = '_blank'; link.rel = 'noopener'; actions.append(link);
                }
                actions.append(action(row, 'Остановить', 'stop'));
            }
            card.append(actions); list.append(card);
        });
        if (!data.instances.length) list.append(el('p', 'Дополнительных инстансов пока нет.', 'hint'));
        const select = $('instance-account'), selected = select.value;
        select.replaceChildren(); const fresh = el('option', 'Новый аккаунт'); fresh.value = ''; select.append(fresh);
        const used = new Set(data.instances.map(row => row.account_name)); used.add(data.main.account_name);
        data.accounts.forEach(name => { const option = el('option', name); option.value = name; option.disabled = used.has(name); select.append(option); });
        select.value = selected;
        if (select.selectedIndex < 0 || (select.selectedOptions[0] && select.selectedOptions[0].disabled)) select.value = '';
        $('instance-new-account-field').hidden = Boolean(select.value);
    }
    async function refresh() {
        if (fetching || document.hidden || !$('instances-modal').classList.contains('open')) return;
        fetching = true;
        try { const data = await App.get('/api/instances'); if (data) render(data); }
        finally { fetching = false; }
    }
    function open() {
        App.openModal('instances-modal'); refresh();
        if (!timer) timer = setInterval(refresh, 2000);
    }
    document.addEventListener('DOMContentLoaded', () => {
        if (!$('instances-modal')) return;
        $('instances-open').addEventListener('click', open);
        $('instance-account').addEventListener('change', () => { $('instance-new-account-field').hidden = Boolean($('instance-account').value); });
        $('instance-create-form').addEventListener('submit', async event => {
            event.preventDefault(); const button = $('instance-create-submit'); button.disabled = true;
            try {
                const data = await App.postJSON('/api/instances/create', Object.fromEntries(new FormData(event.target)));
                if (data) { event.target.reset(); await refresh(); }
            } finally { button.disabled = false; }
        });
        if (new URLSearchParams(location.search).get('instances') === '1') open();
    });
    window.addEventListener('pagehide', () => { clearInterval(timer); timer = null; });
    window.addEventListener('pageshow', () => { if ($('instances-modal') && $('instances-modal').classList.contains('open') && !timer) timer = setInterval(refresh, 2000); });
})();
