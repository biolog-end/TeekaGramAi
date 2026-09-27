/* Общий слой интерфейса: уведомления, запросы, модальные окна, тема, ключи API.
   Подключается на всех страницах. */

const App = (function () {
    'use strict';

    /* ---------------------- Уведомления ---------------------- */

    const TOAST_LIFETIME = { error: 9000, warning: 6500, success: 3500, info: 4500 };

    function toast(message, type) {
        type = type || 'info';
        const box = document.getElementById('toasts');
        if (!box) { console.log('[' + type + '] ' + message); return; }

        const el = document.createElement('div');
        el.className = 'toast ' + type;

        const text = document.createElement('div');
        text.className = 'toast-text';
        text.textContent = message;

        const close = document.createElement('button');
        close.className = 'toast-close';
        close.type = 'button';
        close.setAttribute('aria-label', 'Закрыть');
        close.textContent = '✕';

        function dismiss() {
            if (!el.isConnected) return;
            el.classList.add('leaving');
            setTimeout(function () { el.remove(); }, 200);
        }
        close.addEventListener('click', dismiss);

        el.appendChild(text);
        el.appendChild(close);
        box.appendChild(el);
        setTimeout(dismiss, TOAST_LIFETIME[type] || 4500);
    }

    /* ---------------------- Запросы ---------------------- */

    function toFormData(payload) {
        if (payload instanceof FormData) return payload;
        const fd = new FormData();
        Object.keys(payload || {}).forEach(function (key) {
            const value = payload[key];
            if (Array.isArray(value)) {
                value.forEach(function (v) { fd.append(key, v); });
            } else if (typeof value === 'boolean') {
                // Бэкенд считает флаг включённым по 'true'/'on'/'1'.
                fd.append(key, value ? 'true' : 'false');
            } else if (value !== undefined && value !== null) {
                fd.append(key, value);
            }
        });
        return fd;
    }

    /* Возвращает разобранный JSON либо null, если запрос не удался.
       Об ошибке сообщает сам, звать toast повторно не нужно. */
    async function request(url, options, opts) {
        opts = opts || {};
        try {
            const response = await fetch(url, options);
            let data = null;
            try {
                data = await response.json();
            } catch (e) {
                if (!response.ok) {
                    toast('Сервер вернул ошибку ' + response.status, 'error');
                    return null;
                }
                throw e;
            }

            if (!response.ok || (data && data.status === 'error')) {
                toast((data && data.message) || ('Ошибка ' + response.status), 'error');
                return null;
            }
            if (opts.successToast !== false && data && data.message) {
                toast(data.message, 'success');
            }
            return data;
        } catch (err) {
            console.error('Запрос не удался:', url, err);
            toast('Не удалось связаться с сервером. Он ещё запущен?', 'error');
            return null;
        }
    }

    function post(url, payload, opts) {
        return request(url, { method: 'POST', body: toFormData(payload) }, opts);
    }

    function postForm(url, payload) {
        return post(url, payload);
    }

    function postJSON(url, payload, opts) {
        return request(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        }, opts);
    }

    function get(url, opts) {
        return request(url, { method: 'GET' }, Object.assign({ successToast: false }, opts || {}));
    }

    /* ---------------------- Модальные окна ---------------------- */

    function openModal(id) {
        const modal = document.getElementById(id);
        if (!modal) return;
        modal.classList.add('open');
        document.body.style.overflow = 'hidden';
    }

    function closeModal(id) {
        const modal = document.getElementById(id);
        if (!modal) return;
        modal.classList.remove('open');
        if (!document.querySelector('.modal.open')) document.body.style.overflow = '';
    }

    function closeAllModals() {
        document.querySelectorAll('.modal.open').forEach(function (m) { m.classList.remove('open'); });
        document.body.style.overflow = '';
    }

    function initModals() {
        document.addEventListener('click', function (e) {
            const closer = e.target.closest('[data-close-modal]');
            if (closer) { closeModal(closer.dataset.closeModal); return; }
            // Клик по подложке (но не по самому окну) закрывает.
            if (e.target.classList && e.target.classList.contains('modal')) {
                closeModal(e.target.id);
            }
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') closeAllModals();
        });
    }

    /* ---------------------- Вкладки ---------------------- */

    function initTabs() {
        document.addEventListener('click', function (e) {
            const tab = e.target.closest('.tab');
            if (!tab || !tab.dataset.tab) return;
            const container = tab.closest('.modal-body') || document;
            container.querySelectorAll('.tab').forEach(function (t) { t.classList.remove('active'); });
            container.querySelectorAll('.tab-panel').forEach(function (p) { p.classList.remove('active'); });
            tab.classList.add('active');
            const panel = container.querySelector('#' + tab.dataset.tab);
            if (panel) panel.classList.add('active');
        });
    }

    /* ---------------------- Тема ---------------------- */

    function currentTheme() {
        return document.documentElement.getAttribute('data-theme') || 'warm';
    }

    function applyTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        try { localStorage.setItem('teeka-theme', theme); } catch (e) { /* приватный режим */ }
        const btn = document.getElementById('theme-toggle');
        if (btn) {
            btn.title = theme === 'dark' ? 'Светлая тема' : 'Тёмная тема';
        }
    }

    function initTheme() {
        applyTheme(currentTheme());
        const btn = document.getElementById('theme-toggle');
        if (btn) {
            btn.addEventListener('click', function () {
                applyTheme(currentTheme() === 'dark' ? 'warm' : 'dark');
            });
        }
        // Плавные переходы включаем после первой отрисовки.
        requestAnimationFrame(function () { document.body.classList.add('theme-ready'); });
    }

    /* ---------------------- Прочее ---------------------- */

    /* «40 с», «12 мин», «5 ч 3 мин» — для кулдаунов ключей. */
    function fmtLeft(seconds) {
        seconds = Math.max(Math.round(seconds || 0), 0);
        if (seconds >= 3600) return Math.floor(seconds / 3600) + ' ч ' + Math.round((seconds % 3600) / 60) + ' мин';
        if (seconds >= 90) return Math.round(seconds / 60) + ' мин';
        return seconds + ' с';
    }

    /* ---------------------- Инициализация ---------------------- */

    document.addEventListener('DOMContentLoaded', function () {
        initTheme();
        initModals();
        initTabs();
    });

    return {
        toast: toast,
        post: post,
        postForm: postForm,
        postJSON: postJSON,
        get: get,
        openModal: openModal,
        closeModal: closeModal,
        toFormData: toFormData,
        fmtLeft: fmtLeft
    };
})();


/* ======================= Менеджер ключей API ======================= */

const KeyManager = (function () {
    'use strict';

    let keys = [];          // {id, label, masked|key, enabled, provider, ...}
    let loaded = false;
    let blockedModels = {}; // model -> {left_s, reason}: квота исчерпана на всех ключах
    let badgeProvider = 'gemini';

    const PROVIDER_LABELS = { gemini: 'Gemini', openai: 'OpenAI' };

    function setStatus(status) {
        blockedModels = (status && status.blocked_models) || {};
        if (typeof GeminiLimits !== 'undefined') GeminiLimits.receive(status);
        document.dispatchEvent(new CustomEvent('keys-status'));
    }

    function setBadgeProvider(provider) {
        badgeProvider = provider;
        updateBadge();
    }

    /* 12 345 → «12 тыс», 2 500 000 → «2,5 млн». */
    function fmtTokens(n) {
        n = Number(n) || 0;
        if (n >= 1e6) return (n / 1e6).toFixed(1).replace('.', ',').replace(',0', '') + ' млн';
        if (n >= 1e3) return Math.round(n / 1e3) + ' тыс';
        return String(n);
    }

    function providerSelect(idx, value) {
        return '<select class="key-provider" data-idx="' + idx + '" aria-label="Поставщик">' +
            Object.keys(PROVIDER_LABELS).map(function (p) {
                return '<option value="' + p + '"' + (p === value ? ' selected' : '') + '>' + PROVIDER_LABELS[p] + '</option>';
            }).join('') + '</select>';
    }

    function freeTierLine(item) {
        if (!item.free_tier) return '';
        const parts = Object.keys(item.free_tier).map(function (group) {
            const g = item.free_tier[group];
            return fmtTokens(g.used) + ' / ' + fmtTokens(g.limit) + (group === 'large' ? ' (большие)' : ' (mini/nano)');
        });
        return '<div class="key-stats">бесплатно сегодня: ' + parts.join(' · ') + '</div>';
    }

    function badgeFor(item) {
        if (!item.enabled) return '<span class="badge">выключен</span>';
        if (item.cooling_down) {
            return '<span class="badge warn" title="' + escapeAttr(item.cooldown_reason) + '">' +
                   'ждёт ' + item.cooldown_left_s + ' с</span>';
        }
        return '<span class="badge ok">готов</span>';
    }

    function escapeAttr(s) {
        return String(s || '').replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }

    function render() {
        const list = document.getElementById('keys-list');
        if (!list) return;

        if (!keys.length) {
            list.innerHTML = '<div class="empty" style="padding:24px;">' +
                'Ключей пока нет. Вставьте их в поле выше.</div>';
        } else {
            list.innerHTML = keys.map(function (item, i) {
                const cls = 'key-row' + (item.cooling_down ? ' cooling' : '') + (item.enabled ? '' : ' off');
                const stats = ((item.requests_today !== undefined)
                    ? '<div class="key-stats">этот инстанс: запросов сегодня: ' + item.requests_today +
                      ' · за минуту: ' + item.requests_last_minute +
                      ' · токенов сегодня: ' + fmtTokens(item.tokens_today || 0) +
                      (item.last_error ? ' · последняя ошибка: ' + escapeAttr(item.last_error) : '') + '</div>'
                    : '') +
                    freeTierLine(item) +
                    (item.cooling_down && item.cooldown_reason
                        ? '<div class="key-stats warn">' + escapeAttr(item.cooldown_reason) + ' · ещё ' + App.fmtLeft(item.cooldown_left_s) + '</div>'
                        : '') +
                    (item.model_cooldowns || []).map(function (mc) {
                        return '<div class="key-stats warn">' + escapeAttr(mc.model) + ': ' +
                               escapeAttr(mc.reason) + (mc.left_s > 0 ? ' · повторная проверка через ' + App.fmtLeft(mc.left_s) : '') + '</div>';
                    }).join('');
                return '' +
                '<div class="key-row ' + cls + '">' +
                    '<label class="switch" style="padding:0;" title="Включить или выключить ключ">' +
                        '<input type="checkbox" data-idx="' + i + '" class="key-enabled"' +
                            (item.enabled ? ' checked' : '') + '>' +
                        '<span class="track"></span>' +
                    '</label>' +
                    '<div class="key-main">' +
                        '<div class="key-head">' +
                            '<input type="text" class="key-label-input" data-idx="' + i + '" ' +
                                   'value="' + escapeAttr(item.label) + '" placeholder="Название" ' +
                                   'style="padding:4px 8px; font-size:.82rem;">' +
                            providerSelect(i, item.provider || 'gemini') +
                        '</div>' +
                        '<input type="text" class="key-value-input" data-idx="' + i + '" ' +
                               'value="' + escapeAttr(item.key !== undefined ? item.key : item.masked) + '" ' +
                               'placeholder="AIza…" style="padding:4px 8px; font-size:.76rem; font-family:ui-monospace,monospace;">' +
                        stats +
                    '</div>' +
                    '<div style="display:flex; flex-direction:column; gap:5px; align-items:flex-end;">' +
                        badgeFor(item) +
                        '<button type="button" class="btn btn-sm key-copy" data-idx="' + i + '">Копировать</button>' +
                        '<button type="button" class="btn btn-sm btn-danger key-remove" data-idx="' + i + '">Удалить</button>' +
                    '</div>' +
                '</div>';
            }).join('');
        }

        const note = document.getElementById('keys-foot-note');
        if (note) {
            const ready = keys.filter(function (k) { return k.enabled && !k.cooling_down; }).length;
            note.textContent = 'Всего: ' + keys.length + ', готовы к работе: ' + ready;
        }
        updateBadge();
    }

    function updateBadge() {
        const badge = document.getElementById('keys-badge');
        if (!badge) return;
        const mine = keys.filter(function (k) { return (k.provider || 'gemini') === badgeProvider; });
        const total = mine.length;
        const ready = mine.filter(function (k) { return k.enabled && !k.cooling_down; }).length;
        const label = PROVIDER_LABELS[badgeProvider] || badgeProvider;
        if (!total) {
            badge.textContent = 'нет ключей ' + label;
        } else if (!ready) {
            badge.textContent = label + ': все ' + total + ' в лимите';
        } else {
            badge.textContent = label + ': ключей ' + ready + ' из ' + total;
        }
    }

    function setAdminKeyField(masked) {
        const field = document.getElementById('openai-admin-key');
        if (field) field.value = masked || '';
    }

    function adminKeyValue() {
        const field = document.getElementById('openai-admin-key');
        return field ? field.value.trim() : null;
    }

    async function load() {
        const data = await App.get('/api/keys');
        if (!data) return;
        keys = (data.keys || []).map(function (k) {
            return Object.assign({}, k, { key: undefined });
        });
        loaded = true;
        render();
        setAdminKeyField(data.openai_admin_key_masked);
        setStatus(data);
    }

    /* Реальное потребление организации OpenAI за сегодня — из usage/costs API. */
    function renderUsage(data) {
        const box = document.getElementById('openai-usage');
        if (!box) return;
        const groups = data.groups || {};
        const groupLines = Object.keys(groups).map(function (g) {
            const it = groups[g];
            const pct = it.pct !== undefined ? it.pct : (it.limit ? Math.min(100, Math.round(it.used / it.limit * 100)) : 0);
            const cls = pct >= 90 ? ' warn' : '';
            return '<div class="key-stats' + cls + '">' +
                (g === 'large' ? 'большие модели' : 'mini / nano') + ': ' +
                fmtTokens(it.used) + ' / ' + fmtTokens(it.limit) + ' (' + pct + '%) · можно ещё ~' + fmtTokens(it.left) +
                (it.margin ? ' (запас ' + fmtTokens(it.margin) + ')' : '') + '</div>';
        }).join('');

        const sourceLine = data.source === 'org+local'
            ? '<div class="key-stats">отчёт организации' +
              (data.synced_ago_s !== null && data.synced_ago_s !== undefined ? ' (синк ' + App.fmtLeft(data.synced_ago_s) + ' назад)' : '') +
              ' + вызовы после него · общий счёт для всех проектов на этой машине</div>'
            : '<div class="key-stats warn">только локальные вызовы — Admin-ключ не задан или отчёт недоступен</div>';

        const rows = (data.models || []).map(function (m) {
            const tier = m.service_tier ? (m.free ? ' · бесплатно' : ' · ' + escapeAttr(m.service_tier))
                                        : (m.local_pending ? ' · ещё не в отчёте' : '');
            return '<tr><td>' + escapeAttr(m.model) + tier + '</td>' +
                '<td class="num">' + m.requests + '</td>' +
                '<td class="num">' + fmtTokens(m.input_tokens) + '</td>' +
                '<td class="num">' + fmtTokens(m.output_tokens) + '</td>' +
                '<td class="num">' + fmtTokens(m.total_tokens) + '</td></tr>';
        }).join('');

        const cost = Number(data.cost_today_usd || 0);
        const costLine = cost > 0
            ? '<div class="key-stats warn">списано сегодня: $' + cost.toFixed(4) +
              (data.cost_lines || []).slice(0, 4).map(function (c) {
                  return ' · ' + escapeAttr(c.line_item) + ' $' + Number(c.usd).toFixed(4);
              }).join('') + '</div>'
            : '<div class="key-stats">списано сегодня: $0 — всё в бесплатном лимите</div>';

        box.innerHTML =
            '<div class="key-stats">сброс через ' + App.fmtLeft(data.reset_in_s) + ' (00:00 UTC)</div>' +
            sourceLine + groupLines + costLine +
            (rows ? '<table class="usage-table"><thead><tr><th>модель</th><th class="num">запр.</th>' +
                    '<th class="num">вход</th><th class="num">выход</th><th class="num">всего</th></tr></thead>' +
                    '<tbody>' + rows + '</tbody></table>'
                  : '<div class="key-stats">сегодня запросов ещё не было</div>');
    }

    async function refreshUsage() {
        const btn = document.getElementById('openai-usage-refresh');
        const box = document.getElementById('openai-usage');
        const typed = adminKeyValue();
        // Новый ключ (не маска) — сначала сохраняем, иначе сервер его не увидит.
        if (typed && typed.indexOf('…') === -1) await save();
        if (btn) btn.disabled = true;
        if (box) box.innerHTML = '<div class="key-stats">запрашиваю отчёт OpenAI…</div>';
        const data = await App.get('/api/openai/usage');
        if (btn) btn.disabled = false;
        if (!data) { if (box) box.innerHTML = ''; return; }
        renderUsage(data);
    }

    /* Обновление состояния из события SSE, без перерисовки полей ввода. */
    function applyStatus(status) {
        if (!status || !status.keys) return;
        const byId = {};
        status.keys.forEach(function (k) { byId[k.id] = k; });
        keys.forEach(function (k) {
            const fresh = byId[k.id];
            if (fresh) {
                k.cooling_down = fresh.cooling_down;
                k.cooldown_left_s = fresh.cooldown_left_s;
                k.cooldown_reason = fresh.cooldown_reason;
                k.model_cooldowns = fresh.model_cooldowns;
                k.last_error = fresh.last_error;
                k.requests_today = fresh.requests_today;
                k.requests_last_minute = fresh.requests_last_minute;
                k.tokens_today = fresh.tokens_today;
                k.free_tier = fresh.free_tier;
                k.enabled = fresh.enabled;
            }
        });
        updateBadge();
        if (document.getElementById('keys-modal').classList.contains('open')) render();
        setStatus(status);
    }

    function collectFromInputs() {
        document.querySelectorAll('.key-label-input').forEach(function (input) {
            keys[input.dataset.idx].label = input.value;
        });
        document.querySelectorAll('.key-value-input').forEach(function (input) {
            keys[input.dataset.idx].key = input.value;
        });
        document.querySelectorAll('.key-enabled').forEach(function (input) {
            keys[input.dataset.idx].enabled = input.checked;
        });
        document.querySelectorAll('.key-provider').forEach(function (select) {
            keys[select.dataset.idx].provider = select.value;
        });
    }


    async function copyKeys(indices) {
        collectFromInputs();
        const chosen = indices.map(function (index) { return keys[index]; }).filter(Boolean);
        if (!chosen.length) { App.toast('Нет ключей для копирования.', 'warning'); return; }
        const saved = chosen.filter(function (key) { return key.masked && (!(key.key || '').trim() || key.key.indexOf('…') !== -1); });
        let values = {};
        if (saved.length) {
            const data = await App.postJSON('/api/keys/copy', { key_ids: saved.map(function (key) { return key.id; }) });
            if (!data) return;
            values = data.values || {};
        }
        const text = chosen.map(function (key) {
            const typed = (key.key || '').trim();
            return typed && typed.indexOf('…') === -1 ? typed : values[key.id];
        }).filter(Boolean).join('\n');
        if (!text) { App.toast('Нет заполненных ключей для копирования.', 'warning'); return; }
        try {
            try {
                if (!navigator.clipboard) throw new Error('Clipboard unavailable');
                await navigator.clipboard.writeText(text);
            } catch (_) {
                const previousFocus = document.activeElement;
                const field = document.createElement('textarea');
                field.value = text; field.style.position = 'fixed'; field.style.opacity = '0';
                document.body.appendChild(field);
                try {
                    field.select();
                    if (!document.execCommand('copy')) throw new Error('Clipboard unavailable');
                } finally {
                    field.remove();
                    if (previousFocus && previousFocus.focus) previousFocus.focus();
                }
            }
            App.toast(chosen.length === 1 ? 'Ключ скопирован.' : 'Ключи скопированы, каждый с новой строки.', 'info');
        } catch (_) {
            App.toast('Браузер не разрешил копирование в буфер обмена.', 'error');
        }
    }

    async function save() {
        collectFromInputs();
        const payload = keys
            .filter(function (k) { return (k.key || '').trim() || k.masked; })
            .map(function (k) {
                return {
                    id: k.id,
                    label: (k.label || '').trim(),
                    // Значение с многоточием — маска, сервер подставит сохранённый ключ.
                    key: (k.key || '').trim(),
                    enabled: !!k.enabled,
                    provider: k.provider || 'gemini'
                };
            });

        const body = { keys: payload };
        const admin = adminKeyValue();
        if (admin !== null) body.openai_admin_key = admin;   // '' убирает ключ, маска — без изменений

        const data = await App.postJSON('/api/keys/save', body);
        if (!data) return;
        keys = (data.keys || []).map(function (k) { return Object.assign({}, k, { key: undefined }); });
        render();
        setAdminKeyField(data.openai_admin_key_masked);
        setStatus(data);
    }

    function bulkProvider() {
        const select = document.getElementById('bulk-provider');
        return select ? select.value : 'gemini';
    }

    function addKeys(values, provider) {
        const existing = new Set(keys.map(function (k) { return (k.masked || k.key || '').trim(); }));
        let added = 0;
        values.forEach(function (value) {
            const clean = value.trim();
            if (!clean || existing.has(clean)) return;
            existing.add(clean);
            added++;
            keys.push({
                id: 'k' + Date.now() + '_' + Math.random().toString(36).slice(2, 7),
                label: (PROVIDER_LABELS[provider] || '') + ' ключ ' + (keys.length + 1),
                key: clean,
                enabled: true,
                provider: provider,
                cooling_down: false,
                requests_today: 0,
                requests_last_minute: 0,
                tokens_today: 0
            });
        });
        render();
        return added;
    }

    function open() {
        App.openModal('keys-modal');
        if (!loaded) load();
    }

    function init() {
        load();

        const list = document.getElementById('keys-list');
        if (list) {
            list.addEventListener('click', function (e) {
                const copy = e.target.closest('.key-copy');
                if (copy) { copyKeys([Number(copy.dataset.idx)]); return; }
                const remove = e.target.closest('.key-remove');
                if (remove) {
                    collectFromInputs();
                    keys.splice(Number(remove.dataset.idx), 1);
                    render();
                }
            });
        }

        const bulk = document.getElementById('bulk-add');
        if (bulk) {
            bulk.addEventListener('click', function () {
                const field = document.getElementById('bulk-keys');
                const parts = field.value.split(/[\s,;\n]+/).filter(Boolean);
                if (!parts.length) { App.toast('Вставьте хотя бы один ключ.', 'warning'); return; }
                collectFromInputs();
                const added = addKeys(parts, bulkProvider());
                field.value = '';
                App.toast(added ? ('Добавлено ключей: ' + added + '. Не забудьте сохранить.')
                                : 'Все эти ключи уже есть в списке.',
                          added ? 'info' : 'warning');
            });
        }

        const addEmpty = document.getElementById('add-empty-key');
        if (addEmpty) {
            addEmpty.addEventListener('click', function () {
                collectFromInputs();
                const provider = bulkProvider();
                keys.push({
                    id: 'k' + Date.now(),
                    label: (PROVIDER_LABELS[provider] || '') + ' ключ ' + (keys.length + 1),
                    key: '',
                    enabled: true,
                    provider: provider,
                    cooling_down: false,
                    requests_today: 0,
                    requests_last_minute: 0,
                    tokens_today: 0
                });
                render();
            });
        }

        const saveBtn = document.getElementById('save-keys');
        if (saveBtn) saveBtn.addEventListener('click', save);
        const copyAll = document.getElementById('copy-all-keys');
        if (copyAll) copyAll.addEventListener('click', function () {
            copyKeys(keys.map(function (_, index) { return index; }));
        });

        const usageBtn = document.getElementById('openai-usage-refresh');
        if (usageBtn) usageBtn.addEventListener('click', refreshUsage);
    }

    return {
        init: init, open: open, applyStatus: applyStatus, updateBadge: updateBadge,
        setBadgeProvider: setBadgeProvider,
        blockedModels: function () { return blockedModels; }
    };
})();
