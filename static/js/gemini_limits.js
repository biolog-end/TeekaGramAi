/* Общая таблица Gemini. Запросы только к локальному серверу, без вызовов Google. */
const GeminiLimits = (function () {
    'use strict';
    const $ = function (id) { return document.getElementById(id); };
    let keys = [];
    let loading = false;
    let timer = null;
    let editing = null;
    let selectedKey = '';
    let hiddenPage = false;

    function element(tag, text, className) {
        const node = document.createElement(tag);
        if (text !== undefined) node.textContent = text;
        if (className) node.className = className;
        return node;
    }

    function button(text, action) {
        const node = element('button', text, 'btn btn-sm');
        node.type = 'button';
        node.addEventListener('click', action);
        return node;
    }

    function numeric(name, value, caption) {
        const label = element('label', caption, 'quota-edit-field');
        const input = document.createElement('input');
        input.type = 'number'; input.min = '0'; input.step = '1';
        input.value = value;
        input.dataset.field = name;
        label.appendChild(input);
        return label;
    }

    function meter(used, limit, remaining) {
        const box = element('div', undefined, 'quota-meter');
        const numbers = element('div', String(used) + ' / ' + (limit === null ? '?' : String(limit)));
        const bar = element('div', undefined, 'quota-bar');
        const fill = element('span');
        fill.style.width = (limit > 0 ? Math.min(100, used / limit * 100) : 100) + '%';
        bar.appendChild(fill);
        box.appendChild(numbers); box.appendChild(bar);
        if (limit !== null) box.appendChild(element('small', 'осталось ' + remaining));
        return box;
    }

    function activeKey() { return keys.find(function (key) { return key.id === selectedKey; }); }

    function render() {
        if (!$('gemini-limits-rows')) return;
        const key = activeKey();
        const body = $('gemini-limits-rows');
        body.replaceChildren();
        if (!key) {
            const row = element('tr'); const cell = element('td', 'Добавьте ключ Gemini в «Ключах API».');
            cell.colSpan = 5; row.appendChild(cell); body.appendChild(row);
            return;
        }
        const rows = key.gemini_usage || [];
        const first = rows[0];
        $('gemini-limits-reset').textContent = first
            ? 'Сброс дня: ' + new Date(first.reset_at * 1000).toLocaleString('ru-RU') + ' (местное время).'
            : '';
        rows.forEach(function (data) {
            if (data.limits.rpd === null) return;
            const row = element('tr');
            const name = element('td');
            name.appendChild(element('div', data.model, 'quota-model'));
            if (data.reason) name.appendChild(element('small', data.reason, 'warn'));
            if (data.in_flight) name.appendChild(element('small', 'Выполняется запросов: ' + data.in_flight));
            row.appendChild(name);
            const isEditing = editing === data.model;
            const metrics = [
                ['requests_last_minute', 'rpm', data.requests_last_minute, data.remaining_minute],
                ['input_tokens_last_minute', 'tpm', data.input_tokens_last_minute, data.remaining_tokens_minute],
                ['requests_today', 'rpd', data.requests_today, data.remaining_day]
            ];
            metrics.forEach(function (metric) {
                const cell = element('td');
                if (isEditing) {
                    cell.appendChild(numeric(metric[0], metric[2], 'Расход'));
                    cell.appendChild(numeric(metric[1], data.limits[metric[1]], 'Лимит'));
                } else cell.appendChild(meter(metric[2], data.limits[metric[1]], metric[3]));
                row.appendChild(cell);
            });
            const actions = element('td');
            const group = element('div', undefined, 'quota-actions');
            if (isEditing) {
                group.appendChild(button('Сохранить', async function () {
                    const values = {};
                    for (const input of row.querySelectorAll('input[data-field]')) {
                        if (!input.checkValidity() || input.value === '') { input.reportValidity(); return; }
                        values[input.dataset.field] = input.value;
                    }
                    group.querySelectorAll('button').forEach(function (node) { node.disabled = true; });
                    const result = await App.postJSON('/api/gemini/usage/correct', {
                        key_id: key.id, model: data.model, values: values
                    });
                    if (result) { editing = null; await refresh(); render(); }
                    else group.querySelectorAll('button').forEach(function (node) { node.disabled = false; });
                }));
                group.appendChild(button('Отмена', function () { editing = null; render(); }));
            } else group.appendChild(button('Изменить', function () { editing = data.model; render(); }));
            actions.appendChild(group); row.appendChild(actions); body.appendChild(row);
        });
    }

    function receive(status) {
        if (!status || !Array.isArray(status.keys)) return;
        keys = status.keys.filter(function (key) { return key.provider === 'gemini'; });
        const select = $('gemini-limits-key');
        if (!select) return;
        const signature = keys.map(function (key) { return key.id + key.label + key.masked + key.enabled; }).join('|');
        if (select.dataset.signature !== signature) {
            select.dataset.signature = signature;
            select.replaceChildren();
            keys.forEach(function (key) {
                const option = element('option', key.label + ' · ' + key.masked + (key.enabled ? '' : ' · выключен'));
                option.value = key.id; select.appendChild(option);
            });
            if (!activeKey()) { selectedKey = keys.length ? keys[0].id : ''; editing = null; }
            select.value = selectedKey;
        }
        if (!editing) render();
        document.dispatchEvent(new CustomEvent('gemini-usage'));
    }

    async function refresh() {
        if (loading) return;
        loading = true;
        try {
            const response = await fetch('/api/gemini/usage', { cache: 'no-store', signal: AbortSignal.timeout(10000) });
            const data = await response.json();
            if (!response.ok || data.status === 'error') throw new Error(data.message || 'Ошибка учёта Gemini');
            receive(data);
            $('gemini-limits-note').textContent = 'Обновлено ' + new Date().toLocaleTimeString('ru-RU');
        } catch (error) {
            if ($('gemini-limits-note')) $('gemini-limits-note').textContent = error.message;
        } finally { loading = false; }
    }

    function remaining(model) {
        model = model.replace(/^models\//, '');
        return keys.map(function (key) {
            const row = (key.gemini_usage || []).find(function (data) { return data.model === model; });
            if (!row || row.remaining_day === null) return '';
            return key.label + ': осталось ' + row.remaining_day + '/' + row.limits.rpd + ' в день, '
                + row.remaining_minute + '/' + row.limits.rpm + ' в минуту' + (key.enabled ? '' : ' (выключен)');
        }).filter(Boolean).join(' · ');
    }

    function quotaInfo(model) {
        model = model.replace(/^models\//, '');
        const rows = keys.filter(function (key) { return key.enabled; }).map(function (key) {
            return (key.gemini_usage || []).find(function (row) { return row.model === model; });
        }).filter(Boolean);
        const tracked = rows.length > 0;
        const blocked = tracked && rows.every(function (row) { return row.blocked; });
        return { tracked: tracked, blocked: blocked ? rows.reduce(function (a, b) { return a.left_s < b.left_s ? a : b; }) : null };
    }

    function restartTimer() {
        clearInterval(timer); timer = null;
        if (!hiddenPage && !document.hidden && $('gemini-limits-modal')) {
            refresh(); timer = setInterval(refresh, 5000);
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        if (!$('gemini-limits-modal')) return;
        document.querySelectorAll('[data-open-gemini-limits]').forEach(function (node) {
            node.addEventListener('click', function () {
                App.closeModal('keys-modal'); App.openModal('gemini-limits-modal'); refresh();
            });
        });
        $('gemini-limits-key').addEventListener('change', function () {
            selectedKey = this.value; editing = null; render();
        });
        $('gemini-limits-refresh').addEventListener('click', refresh);
        document.addEventListener('visibilitychange', restartTimer);
        window.addEventListener('pagehide', function () { hiddenPage = true; restartTimer(); });
        window.addEventListener('pageshow', function () { hiddenPage = false; restartTimer(); });
        restartTimer();
    });
    return { refresh: refresh, receive: receive, remaining: remaining, quotaInfo: quotaInfo };
})();
