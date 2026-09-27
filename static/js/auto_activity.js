/* Журнал авто-режима: события SSE, восстановление снимка, таймер по серверному дедлайну. */
(function () {
    'use strict';
    const $ = function (id) { return document.getElementById(id); };
    let config, current = null, response = null, runId = 0, instanceId = 0, status = 'inactive';
    let serverTime = 0, clockStart = 0;
    const entries = new Map();
    const elapsedPhases = new Set(['request', 'memory', 'history', 'send_queue', 'telegram_send', 'correction_send']);

    function seconds(value) { return Math.max(0, value).toFixed(1).replace('.', ','); }
    function now() { return serverTime + (performance.now() - clockStart) / 1000; }

    function tick() {
        if (!current) return;
        const timer = $('activity-timer'), bar = $('activity-progress');
        const running = status === 'active' || status === 'stopping';
        bar.hidden = true;
        timer.textContent = '';
        if (!running) return;
        if (current.ends_at != null && current.duration_s != null) {
            const left = Math.max(0, current.ends_at - now());
            timer.textContent = 'Осталось ' + seconds(left) + ' с из ' + seconds(current.duration_s) + ' с';
            if (left === 0) timer.textContent += ' · ожидаю завершения';
            bar.hidden = false;
            bar.value = current.duration_s > 0 ? Math.min(1, Math.max(0, 1 - left / current.duration_s)) : 1;
        } else if (elapsedPhases.has(current.phase)) {
            // Длительность API неизвестна: показываем прошедшее время, без выдуманного прогноза.
            timer.textContent = 'Прошло ' + seconds(now() - current.at) + ' с';
        }
    }

    function render() {
        $('auto-activity').hidden = status === 'inactive' && entries.size === 0;
        $('activity-mode').textContent = status === 'active' ? 'Включён' : status === 'stopping' ? 'Останавливается…' : 'Выключен';
        $('activity-current').textContent = current
            ? (current.part && current.phase !== 'part' ? 'Часть ' + current.part + ' из ' + current.total + ' · ' : '') + current.message
            : 'Ожидаю события…';
        const part = $('activity-part');
        const lastText = Array.from(entries.values()).reverse().find(function (item) {
            return current && current.part && item.part === current.part && item.text != null;
        });
        part.hidden = !lastText || !current || !current.part;
        part.textContent = part.hidden ? '' : lastText.text;
        $('activity-response-details').hidden = !response;
        $('activity-response').textContent = response ? response.text : '';
        $('activity-response-model').textContent = response && response.model ? '· ' + response.model : '';

        const box = $('activity-log');
        const follow = box.scrollHeight - box.scrollTop - box.clientHeight < 50;
        box.replaceChildren();
        entries.forEach(function (entry) {
            const row = document.createElement('div');
            row.className = 'activity-event' + (entry.level === 'error' ? ' activity-error' : '');
            const time = document.createElement('time');
            time.dateTime = new Date(entry.at * 1000).toISOString();
            time.textContent = new Date(entry.at * 1000).toLocaleTimeString('ru-RU');
            const message = document.createElement('span');
            message.textContent = entry.message + (entry.duration_s != null ? ' (' + seconds(entry.duration_s) + ' с)' : '');
            row.append(time, message);
            box.appendChild(row);
        });
        if (follow) box.scrollTop = box.scrollHeight;
        tick();
    }

    function receive(data) {
        if (!config || !data) return;
        const incomingInstance = data.instance_id || 0;
        if (incomingInstance < instanceId) return;
        if (incomingInstance > instanceId) {
            entries.clear(); current = null; response = null; runId = 0; instanceId = incomingInstance;
        }
        // Старый HTTP-снимок не должен затереть новый запуск или более свежее событие SSE.
        const incomingRun = data.run_id || 0;
        if (incomingRun < runId) return;
        if (incomingRun > runId) {
            entries.clear(); current = null; response = null; runId = incomingRun;
        }
        const incoming = (data.entries || []).concat(data.entry ? [data.entry] : []);
        incoming.forEach(function (entry) { entries.set(entry.id, entry); });
        const sorted = Array.from(entries.values()).sort(function (a, b) { return a.id - b.id; }).slice(-80);
        entries.clear(); sorted.forEach(function (entry) { entries.set(entry.id, entry); });
        const candidate = data.current;
        if (candidate && (!current || candidate.id >= current.id)) {
            current = candidate;
            if (data.server_now != null) { serverTime = data.server_now; clockStart = performance.now(); }
        }
        if (data.response && (!response || data.response.id >= response.id)) response = data.response;
        render();
    }

    async function refresh() {
        if (!config) return;
        try {
            const result = await fetch(config.urls.activity);
            if (!result.ok) throw new Error('HTTP ' + result.status);
            const data = await result.json();
            if (data.status === 'success') receive(data);
        } catch (err) {
            $('activity-mode').textContent = 'Не удалось обновить журнал';
        }
    }

    window.AutoActivity = {
        init: function (options) {
            config = options; status = options.autoStatus || 'inactive';
            render(); refresh(); setInterval(tick, 250);
        },
        receive: receive,
        refresh: refresh,
        setStatus: function (value) { status = value; if (config) render(); }
    };
})();
