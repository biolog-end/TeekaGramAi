/* Логика страницы чата: живые обновления, выбор модели, генерация, настройки.
   Данные о чате приходят из window.CHAT (задаётся в шаблоне). */

(function () {
    'use strict';

    const C = window.CHAT;
    const $ = function (id) { return document.getElementById(id); };

    let models = [];
    let modelFilter = '';
    let budgetStatus = null;
    let generating = false;
    let autoStatusVersion = 0;

    /* ==================== Прокрутка ==================== */

    function scrollToBottom(smooth) {
        const box = $('chat-scroll');
        if (!box) return;
        // 'auto' унаследовал бы scroll-behavior: smooth из CSS — при открытии страницы чат
        // должен стоять внизу сразу, без прокрутки на глазах.
        box.scrollTo({ top: box.scrollHeight, behavior: smooth ? 'smooth' : 'instant' });
    }

    function isNearBottom() {
        const box = $('chat-scroll');
        if (!box) return true;
        return box.scrollHeight - box.scrollTop - box.clientHeight < 150;
    }

    /* ==================== Медиа ==================== */

    async function loadMediaNode(node) {
        if (!node.isConnected || node.dataset.done) return;
        node.dataset.done = '1';
        try {
            const response = await fetch(C.urls.media + node.dataset.messageId);
            const data = await response.json();
            if (data.status === 'success' && data.loading) {
                // Одна задача скачивания уже работает в Telethon; проверяем кэш позже.
                setTimeout(function () {
                    delete node.dataset.done;
                    if (node.isConnected && !pagePaused && !document.hidden) loadMediaNode(node);
                }, 3000);
                return;
            }
            if (data.status !== 'success' || !data.parts) throw new Error(data.message || 'нет данных');
            renderMedia(node, data.parts);
        } catch (err) {
            node.textContent = 'Не удалось загрузить вложение';
            node.style.fontSize = '.75rem';
            node.style.color = 'var(--text-dim)';
        }
    }

    /* Несколько независимых файлов не блокируют друг друга; Telegram-очередь ограничена сервером. */
    async function loadMedia() {
        const placeholders = Array.from(document.querySelectorAll('.media-placeholder:not([data-done])'));
        let next = 0;
        const workers = Array.from({ length: Math.min(4, placeholders.length) }, async function () {
            while (next < placeholders.length) {
                await loadMediaNode(placeholders[next++]);
            }
        });
        await Promise.all(workers);
    }

    function renderMedia(node, parts) {
        node.innerHTML = '';
        node.classList.remove('media-placeholder');
        parts.forEach(function (part) {
            let el = null;
            if (part.image_base64) {
                el = document.createElement('img');
                el.src = 'data:' + part.mime_type + ';base64,' + part.image_base64;
                el.loading = 'lazy';
                el.alt = 'изображение';
            } else if (part.video_base64) {
                el = document.createElement('video');
                el.controls = true; el.loop = true; el.muted = true; el.playsInline = true;
                el.src = 'data:' + part.mime_type + ';base64,' + part.video_base64;
            } else if (part.audio_base64) {
                el = document.createElement('audio');
                el.controls = true;
                el.src = 'data:' + part.mime_type + ';base64,' + part.audio_base64;
            } else if (part.file_base64) {
                el = document.createElement('a');
                el.className = 'pdf-attachment';
                el.target = '_blank';
                el.rel = 'noopener';
                el.href = 'data:' + part.mime_type + ';base64,' + part.file_base64;
                el.textContent = '📄 Открыть PDF';
            } else if (part.text) {
                el = document.createElement('pre');
                el.textContent = part.text;
            }
            if (el) node.appendChild(el);
        });
    }

    /* ==================== История ==================== */

    let reloadTimer = null;
    let historyLoading = false, historyPending = false, pendingForce = false, lastHistoryHTML = null;
    let historyPoll = null, pagePaused = false;

    async function reloadHistory(options) {
        options = options || {};
        if (pagePaused || (document.hidden && !options.force)) return;
        if (historyLoading) {
            historyPending = true; pendingForce = pendingForce || !!options.force; return;
        }
        historyLoading = true;
        const limit = $('limit-input').value;
        const stick = options.force || isNearBottom();
        try {
            const result = await fetch(C.urls.history + '?limit=' + encodeURIComponent(limit), {
                cache: 'no-store', signal: AbortSignal.timeout(65000)
            });
            const data = await result.json();
            if (!result.ok || data.status === 'error') throw new Error(data.message || 'Ошибка обновления истории');
            if (!data || !data.html || pagePaused) return;
            if (lastHistoryHTML === data.html) {
                if (options.force) scrollToBottom(true);
                return;
            }
            lastHistoryHTML = data.html;
            const scroll = $('chat-scroll');
            const old = $('messages');
            // HTML от Jinja с автоэкранированием. Не заменяем неизменившуюся историю:
            // иначе каждые пять секунд сбрасывались бы изображения и проигрывание медиа.
            if (old) old.outerHTML = data.html;
            else scroll.insertAdjacentHTML('beforeend', data.html);
            loadMedia();
            if (stick) scrollToBottom(true);
        } catch (err) {
            if (options.force) App.toast(err.message || 'Не удалось обновить историю.', 'error');
            else console.warn('История временно недоступна:', err);
        } finally {
            historyLoading = false;
            if (historyPending) {
                const force = pendingForce; historyPending = false; pendingForce = false;
                clearTimeout(reloadTimer);
                reloadTimer = setTimeout(function () { reloadHistory({ force: force }); }, 700);
            }
        }
    }

    /* Несколько сообщений подряд не должны вызывать несколько перезагрузок. */
    function scheduleReload() {
        if (document.hidden || pagePaused) return;
        clearTimeout(reloadTimer);
        reloadTimer = setTimeout(function () { reloadHistory(); }, 700);
    }

    /* ==================== Живые обновления (SSE) ==================== */

    let source = null;
    let retryDelay = 1000;
    let reconnectTimer = null;

    function setConnection(online) {
        const badge = $('connection-badge');
        const text = $('connection-text');
        if (!badge) return;
        badge.className = 'conn ' + (online ? 'ok' : 'err');
        text.textContent = online ? 'на связи' : 'нет связи';
    }

    function connect() {
        if (pagePaused) return;
        clearTimeout(reconnectTimer);
        if (source) source.close();
        source = new EventSource(C.urls.events);
        const stream = source;

        source.onopen = function () {
            if (source !== stream || pagePaused) return;
            setConnection(true);
            retryDelay = 1000;
        };

        source.onerror = function () {
            if (source !== stream || pagePaused) return;
            setConnection(false);
            source.close();
            // Переподключаемся с нарастающей паузой, максимум раз в 15 секунд.
            reconnectTimer = setTimeout(connect, retryDelay);
            retryDelay = Math.min(retryDelay * 2, 15000);
        };

        source.onmessage = function (event) {
            if (source !== stream || pagePaused) return;
            let payload;
            try { payload = JSON.parse(event.data); } catch (e) { return; }
            handleEvent(payload.type, payload.data || {});
        };
    }

    function handleEvent(type, data) {
        if (type === 'connected') {
            setConnection(true);
            scheduleReload();
            AutoActivity.refresh();
            // Восстановление статуса после пропущенных событий при потере связи.
            const version = autoStatusVersion;
            App.get(C.urls.autoStatus).then(function (state) {
                if (state && version === autoStatusVersion) setAutoStatus(state.auto_mode_status);
            });
            return;
        }
        if (type === 'auto_activity') {
            AutoActivity.receive(data);
            return;
        }
        if (type === 'cluster_active_chat') {
            const root = Number(data.root_chat_id);
            const active = Number(data.active_chat_id);
            const followedRoot = Number(new URLSearchParams(window.location.search).get('cluster_root'));
            if ((Number(C.id) === root || followedRoot === root) && Number(C.id) !== active) {
                const target = new URL(C.urls.chatPageTemplate.replace('987654321', String(active)), window.location.origin);
                target.searchParams.set('cluster_root', String(root));
                window.location.assign(target.toString());
            }
            return;
        }
        if (type === 'new_message' || type === 'history_changed') {
            scheduleReload();
            return;
        }
        if (type === 'auto_mode') {
            setAutoStatus(data.status);
            return;
        }
        if (type === 'keys') {
            KeyManager.applyStatus(data);
            return;
        }
        if (type === 'stickers') {
            // Модель отказалась из-за стикеров-картинок: они ушли в карантин, история
            // теперь показывает их текстом — как и получит модель.
            const n = (data.quarantined || []).length;
            if (n) App.toast('Модель ответила отказом. В карантин ушло стикеров: ' + n + '. Повторяю без картинок.', 'warning');
            scheduleReload();
            loadQuarantine();
            return;
        }
        if (type === 'generation') {
            if (data.state === 'done') scheduleReload();
            const busy = data.state === 'started' || data.state === 'sending';
            $('typing-row').classList.toggle('on', busy);
            if (busy) scrollToBottom(true);
            if (data.state === 'error' && data.source === 'auto') {
                App.toast('Авто-режим: ' + (data.message || 'ошибка генерации'), 'error');
            }
        }
    }

    /* ==================== Авто-режим ==================== */

    const AUTO_LABELS = {
        active:   { badge: 'ok',   text: 'авто-режим включён', button: 'Остановить' },
        stopping: { badge: 'warn', text: 'останавливается…',   button: 'Останавливается' },
        inactive: { badge: '',     text: 'авто-режим выключен', button: 'Включить авто' }
    };

    function setAutoStatus(status) {
        autoStatusVersion++;
        status = AUTO_LABELS[status] ? status : 'inactive';
        C.autoStatus = status;
        const info = AUTO_LABELS[status];

        const badge = $('auto-badge');
        badge.className = 'conn ' + info.badge;
        badge.innerHTML = '<span class="dot"></span>' + info.text;
        badge.dataset.status = status;

        const button = $('auto-toggle');
        button.textContent = info.button;
        button.disabled = status === 'stopping';
        button.classList.toggle('btn-danger', status === 'active');
        AutoActivity.setStatus(status);
        if (status === 'inactive' && !generating) $('typing-row').classList.remove('on');
    }

    async function toggleAuto() {
        if (C.autoStatus === 'active') {
            const data = await App.post(C.urls.stopAuto, {});
            if (data) setAutoStatus(data.auto_mode_status || 'inactive');
            return;
        }
        if (!C.hasCharacter) {
            App.toast('Сначала выберите персонажа.', 'warning');
            return;
        }
        const data = await App.post(C.urls.startAuto, {});
        if (data) setAutoStatus(data.auto_mode_status || 'active');
    }

    /* ==================== Выбор модели ==================== */

    let retiredModels = {};
    let savedModel = '';
    let providerDefaults = {};
    let currentProvider = 'gemini';

    /* Зеркало providers.provider_for_model: провайдер выводится из имени модели. */
    const OPENAI_PREFIXES = ['gpt-', 'o1', 'o3', 'o4', 'o5', 'chatgpt-'];
    function providerFor(id) {
        const name = (id || '').trim().toLowerCase();
        return OPENAI_PREFIXES.some(function (p) { return name.startsWith(p); }) ? 'openai' : 'gemini';
    }

    function setProvider(provider, opts) {
        opts = opts || {};
        currentProvider = provider;
        document.querySelectorAll('#provider-seg button').forEach(function (b) {
            b.classList.toggle('active', b.dataset.provider === provider);
        });
        KeyManager.setBadgeProvider(provider);
        renderBudget();
        if (!opts.silent) {
            const input = $('model-input');
            if (providerFor(input.value) !== provider) {
                input.value = providerDefaults[provider] || '';
                persistModel();
            }
            updatePriceHint();
            renderModelList('');
        }
    }

    function priceLabel(id) {
        const found = models.find(function (m) { return m.id === id; });
        return found ? found.price : 'нет в каталоге цен — цена неизвестна';
    }

    function blockedInfo(id) {
        const model = models.find(function (item) { return item.id === id; });
        if (model && model.free_tier === false) return null;
        const shared = GeminiLimits.quotaInfo(id);
        if (shared.tracked) return shared.blocked;
        return KeyManager.blockedModels()[id] || null;
    }

    function renderBudget() {
        const box = $('openai-budget');
        if (!box) return;
        box.hidden = currentProvider !== 'openai';
        if (box.hidden) return;
        if (!budgetStatus) {
            box.textContent = 'Остаток бесплатных токенов OpenAI: загружаю…';
            return;
        }
        if (!budgetStatus.available) {
            box.textContent = 'Остаток неизвестен: библиотека openai_budget не установлена.';
            return;
        }
        const groups = budgetStatus.groups || {};
        const fmt = function (value) { return Math.max(0, Number(value) || 0).toLocaleString('ru-RU'); };
        const large = groups.large;
        const small = groups.small;
        if (!large || !small) {
            box.textContent = 'Остаток бесплатных токенов пока недоступен.';
            return;
        }
        const source = budgetStatus.source === 'org+local'
            ? 'отчёт организации + новые вызовы'
            : 'только вызовы на этом устройстве';
        box.textContent = 'Примерно осталось сегодня: умные ' + fmt(large.left) + ' из ' + fmt(large.limit) +
            ' · лёгкие ' + fmt(small.left) + ' из ' + fmt(small.limit) + ' токенов, с запасом (' + source + ').';
    }

    async function refreshBudget() {
        const data = await App.get(C.urls.budget);
        if (data) budgetStatus = data;
        renderBudget();
    }

    function toneForModel(model) {
        const rank = Math.max(0, Math.min(Number(model.rank) || 0, 12));
        if (model.tier === 'strong') return 'hsl(' + (31 - rank) + ' 91% ' + (48 - rank * 1.5) + '%)';
        if (model.tier === 'light') return 'hsl(' + (48 - rank * .6) + ' 89% ' + (48 - rank * 1.4) + '%)';
        if (model.tier === 'paid') return 'hsl(3 75% ' + (47 - rank * 1.4) + '%)';
        return 'var(--text-dim)';
    }

    function groupLabel(model) {
        if (model.tier === 'strong') return model.provider === 'openai' ? 'Бесплатные · 250 тыс./день' : 'Есть Free Tier · сильные';
        if (model.tier === 'light') return model.provider === 'openai' ? 'Бесплатные · 2,5 млн/день' : 'Есть Free Tier · лёгкие';
        if (model.tier === 'paid') return model.provider === 'openai' ? 'Платные · вне предложения аккаунта' : 'Без бесплатного тарифа';
        return 'Тариф неизвестен';
    }

    function updatePriceHint() {
        const hint = $('model-price');
        const value = $('model-input').value.trim();
        hint.classList.remove('warn');
        // Пока список не пришёл, судить о цене нечем — не пугаем «нет в каталоге».
        if (!value || !models.length) { hint.textContent = ''; return; }
        if (retiredModels[value]) {
            hint.textContent = retiredModels[value];
            hint.classList.add('warn');
            return;
        }
        const blocked = blockedInfo(value);
        const found = models.find(function (m) { return m.id === value; });
        const labels = found && found.availability_labels || [];
        const remaining = GeminiLimits.remaining(value);
        const description = priceLabel(value) + (labels.length ? ' · ' + labels.join(' · ') : '')
            + (remaining ? ' · ' + remaining : '');
        if (blocked) {
            hint.textContent = description + ' · ' + blocked.reason
                + (blocked.left_s > 0 ? ' · повторная проверка через ' + App.fmtLeft(blocked.left_s) : '');
            hint.classList.add('warn');
            return;
        }
        hint.textContent = description;
    }

    /* Выбор модели сохраняется для чата сразу: одна точка правды и для ручной
       генерации, и для авто-режима. Скрытое поле в настройках едет вместе с формой. */
    function syncHiddenModel() {
        const hidden = $('model_name_advanced');
        if (hidden) hidden.value = $('model-input').value.trim();
    }

    async function persistModel() {
        const value = $('model-input').value.trim();
        syncHiddenModel();
        if (value === savedModel || !C.hasCharacter) return;
        const data = await App.post(C.urls.setModel, { model_name: value });
        if (data) savedModel = value;
    }

    function renderModelList(filter) {
        const list = $('model-list');
        const previousFilter = modelFilter;
        if (typeof filter === 'string') modelFilter = filter;
        const previousScroll = list.scrollTop;
        const query = modelFilter.trim().toLowerCase();
        const matched = models.filter(function (m) {
            if ((m.provider || 'gemini') !== currentProvider) return false;
            return !query || m.id.toLowerCase().includes(query) || m.label.toLowerCase().includes(query);
        });

        list.innerHTML = '';
        if (currentProvider === 'gemini') {
            const help = document.createElement('div');
            help.className = 'combo-provider-note';
            help.textContent = 'Бесплатные модели и квоты — из вашей таблицы AI Studio. Остатки общие для всех проектов с библиотекой gemini_budget; их можно исправить в «Лимитах Gemini». limit: 0 означает нулевую квоту. Доступ также зависит от проекта.';
            list.appendChild(help);
        }
        if (!matched.length) {
            const empty = document.createElement('div');
            empty.className = 'combo-empty';
            empty.textContent = 'Ничего не найдено. Можно вписать имя модели вручную.';
            list.appendChild(empty);
            return;
        }

        let lastGroup = null;
        matched.forEach(function (model) {
            const group = groupLabel(model);
            if (group !== lastGroup) {
                lastGroup = group;
                const label = document.createElement('div');
                label.className = 'combo-group-label';
                label.textContent = group;
                list.appendChild(label);
            }

            const option = document.createElement('div');
            option.className = 'combo-option tier-' + (model.tier || 'unknown');
            option.dataset.id = model.id;
            option.style.setProperty('--model-tone', toneForModel(model));

            const blocked = blockedInfo(model.id);
            if (blocked) option.classList.add('blocked');

            const top = document.createElement('div');
            top.className = 'opt-top';
            const name = document.createElement('span');
            name.className = 'opt-name';
            name.textContent = model.label;
            const price = document.createElement('span');
            price.className = 'opt-price';
            price.textContent = model.price;
            top.appendChild(name);
            top.appendChild(price);
            option.appendChild(top);

            if (model.availability_labels && model.availability_labels.length) {
                const labels = document.createElement('div');
                labels.className = 'opt-labels';
                model.availability_labels.forEach(function (text) {
                    const label = document.createElement('span');
                    label.className = 'opt-label';
                    label.textContent = text;
                    labels.appendChild(label);
                });
                option.appendChild(labels);
            }

            const remaining = GeminiLimits.remaining(model.id);
            if (remaining) option.appendChild(Object.assign(document.createElement('div'), {
                className: 'opt-note quota-remaining', textContent: remaining
            }));

            const noteText = model.note;
            if (noteText) {
                const note = document.createElement('div');
                note.className = 'opt-note';
                note.textContent = noteText;
                option.appendChild(note);
            }
            if (blocked) {
                const warning = document.createElement('div');
                warning.className = 'opt-note warn';
                warning.textContent = blocked.reason
                    + (blocked.left_s > 0 ? ' · повторная проверка через ' + App.fmtLeft(blocked.left_s) : '');
                option.appendChild(warning);
            }

            option.addEventListener('click', function () {
                $('model-input').value = model.id;
                list.classList.remove('open');
                updatePriceHint();
                persistModel();
            });
            list.appendChild(option);
        });
        placeModelList();
        list.scrollTop = previousFilter === modelFilter ? previousScroll : 0;
    }

    function placeModelList() {
        const rect = $('model-combo').getBoundingClientRect();
        const list = $('model-list');
        const below = window.innerHeight - rect.bottom - 12;
        const above = rect.top - 12;
        const up = below < 300 && above > below;
        list.classList.toggle('open-up', up);
        list.style.maxHeight = Math.max(120, Math.min(420, up ? above : below)) + 'px';
        list.style.left = Math.min(0, window.innerWidth - 16 - rect.left - Math.min(560, window.innerWidth - 32)) + 'px';
    }

    function refreshModelUi() {
        updatePriceHint();
        const list = $('model-list');
        if (list.classList.contains('open')) renderModelList();
        if (currentProvider === 'openai') refreshBudget();
    }

    async function initModels() {
        savedModel = $('model-input').value.trim();
        syncHiddenModel();
        setProvider(providerFor(savedModel), { silent: true });
        const data = await App.get(C.urls.models);
        if (!data) return;
        models = data.models || [];
        const fallbackCatalog = $('fallback-model-catalog');
        if (fallbackCatalog) models.forEach(function (model) {
            const option = document.createElement('option'); option.value = model.id;
            option.label = model.label + ' · ' + model.price; fallbackCatalog.appendChild(option);
        });
        retiredModels = data.retired || {};
        providerDefaults = data.defaults || {};
        renderModelList('');
        updatePriceHint();
        refreshBudget();
    }

    function initModelCombo() {
        const input = $('model-input');
        const list = $('model-list');

        input.addEventListener('focus', function () { renderModelList(''); list.classList.add('open'); });
        input.addEventListener('input', function () {
            // Вписали модель другого провайдера — переключаем вкладку сами.
            const provider = providerFor(input.value);
            if (provider !== currentProvider) setProvider(provider, { silent: true });
            renderModelList(input.value);
            list.classList.add('open');
            updatePriceHint();
        });
        document.querySelectorAll('#provider-seg button').forEach(function (button) {
            button.addEventListener('click', function () { setProvider(button.dataset.provider); });
        });
        $('model-toggle').addEventListener('click', function () {
            const open = list.classList.toggle('open');
            if (open) renderModelList('');
        });
        document.addEventListener('click', function (e) {
            if (!e.target.closest('#model-combo')) list.classList.remove('open');
        });
        input.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') list.classList.remove('open');
            if (e.key === 'Enter') { e.preventDefault(); list.classList.remove('open'); persistModel(); }
        });
        input.addEventListener('change', persistModel);
        window.addEventListener('resize', placeModelList);
        window.addEventListener('scroll', function () {
            if (list.classList.contains('open')) placeModelList();
        }, { passive: true });
        document.addEventListener('keys-status', refreshModelUi);
        document.addEventListener('gemini-usage', function () {
            updatePriceHint();
            if (list.classList.contains('open')) renderModelList();
        });
    }

    /* ==================== Генерация и отправка ==================== */

    async function generate() {
        if (generating) return;
        if (!C.hasCharacter) { App.toast('Сначала выберите персонажа.', 'warning'); return; }

        generating = true;
        const button = $('generate-btn');
        button.disabled = true;
        $('generate-label').textContent = 'Генерирую…';
        $('typing-row').classList.add('on');
        scrollToBottom(true);

        const data = await App.post(C.urls.generate, {
            model_name: $('model-input').value.trim()
        }, { successToast: false });

        generating = false;
        button.disabled = false;
        $('generate-label').textContent = 'Сгенерировать';
        $('typing-row').classList.remove('on');

        if (!data) return;
        const box = $('reply-text');
        box.value = data.reply || '';
        $('send-btn').disabled = !box.value.trim();
        box.focus();
        if (!data.reply) App.toast('Модель вернула пустой ответ.', 'warning');
    }

    async function send() {
        const box = $('reply-text');
        const text = box.value.trim();
        if (!text) return;

        const button = $('send-btn');
        button.disabled = true;
        button.textContent = 'Отправляю…';

        const data = await App.post(C.urls.send, { message_to_send: text });

        button.textContent = 'Отправить ➤';
        if (data) {
            box.value = '';
            button.disabled = true;
            reloadHistory({ force: true });
        } else {
            button.disabled = false;
        }
    }

    /* ==================== Настройки ==================== */

    function initFallbackEditor() {
        const field = $('auto_fallback_models');
        if (!field) return;
        let chain = field.value.split(/[\s,;]+/).filter(Boolean);
        function render() {
            field.value = chain.join('\n');
            const rows = $('fallback-rows');
            rows.replaceChildren();
            chain.forEach(function (model, index) {
                const row = document.createElement('div'); row.className = 'fallback-row';
                const number = document.createElement('span'); number.textContent = index + 1;
                const input = document.createElement('input'); input.type = 'text'; input.value = model;
                input.placeholder = 'Имя модели'; input.setAttribute('list', 'fallback-model-catalog');
                input.setAttribute('aria-label', 'Модель ' + (index + 1));
                input.addEventListener('input', function () { chain[index] = this.value.trim(); field.value = chain.join('\n'); });
                row.appendChild(number); row.appendChild(input);
                [['↑', -1, 'Переместить выше'], ['↓', 1, 'Переместить ниже'], ['×', 0, 'Удалить модель']].forEach(function (action) {
                    const button = document.createElement('button'); button.type = 'button';
                    button.className = 'btn btn-icon'; button.textContent = action[0];
                    button.title = action[2]; button.setAttribute('aria-label', action[2] + ' ' + model);
                    button.disabled = action[1] && (index + action[1] < 0 || index + action[1] >= chain.length);
                    button.addEventListener('click', function () {
                        if (action[1]) {
                            const target = index + action[1]; const previous = chain[target];
                            chain[target] = chain[index]; chain[index] = previous;
                        } else chain.splice(index, 1);
                        render();
                    });
                    row.appendChild(button);
                });
                rows.appendChild(row);
            });
        }
        $('fallback-add').addEventListener('click', function () { chain.push(''); render(); $('fallback-rows').lastChild.querySelector('input').focus(); });
        $('fallback-preset').addEventListener('click', function () {
            chain = ['gemini-3.8-flash', 'gemini-3.7-flash', 'gemini-3.5-flash-lite', 'gpt-5.4', 'gpt-5.4-mini'];
            $('auto_fallback_enabled').checked = true; render();
        });
        render();
    }

    function initLexiconEditor() {
        const field = $('lexicon-rules');
        const rows = $('lexicon-rows');
        if (!field || !rows) return;
        let rules = JSON.parse(field.value || '[]');

        function sync() { field.value = JSON.stringify(rules); }

        function render() {
            rows.replaceChildren();
            sync();
            if (!rules.length) {
                const empty = document.createElement('div');
                empty.className = 'field-hint lexicon-empty';
                empty.textContent = 'Замен пока нет.';
                rows.appendChild(empty);
            }
            rules.forEach(function (rule, index) {
                const row = document.createElement('div');
                row.className = 'lexicon-row';
                const number = document.createElement('span');
                number.className = 'lexicon-number'; number.textContent = String(index + 1);
                row.appendChild(number);
                [['find', 'Найти'], ['replace', 'Заменить на']].forEach(function (pair) {
                    const label = document.createElement('label');
                    label.className = 'field lexicon-text'; label.appendChild(document.createTextNode(pair[1]));
                    const input = document.createElement('textarea');
                    input.rows = 2; input.value = rule[pair[0]] || '';
                    input.setAttribute('aria-label', pair[1] + ' · замена ' + (index + 1));
                    input.spellcheck = false;
                    input.addEventListener('input', function () { rule[pair[0]] = input.value; sync(); });
                    label.appendChild(input); row.appendChild(label);
                });
                const probability = document.createElement('label');
                probability.className = 'field lexicon-chance';
                probability.appendChild(document.createTextNode('Шанс (0–1)'));
                const chance = document.createElement('input');
                chance.type = 'number'; chance.min = '0'; chance.max = '1'; chance.step = 'any';
                chance.value = rule.chance === undefined ? 1 : rule.chance;
                chance.setAttribute('aria-label', 'Шанс · замена ' + (index + 1));
                const percent = document.createElement('span'); percent.className = 'field-hint';
                function showPercent() {
                    const value = Number(chance.value);
                    percent.textContent = chance.value !== '' && Number.isFinite(value) && value >= 0 && value <= 1
                        ? String(Math.round(value * 10000) / 100) + '%' : 'От 0 до 1';
                }
                chance.addEventListener('input', function () {
                    rule.chance = chance.value; showPercent(); sync();
                });
                showPercent(); probability.appendChild(chance); probability.appendChild(percent);
                row.appendChild(probability);
                const actions = document.createElement('div'); actions.className = 'lexicon-actions';
                [['↑', -1, 'Переместить выше'], ['↓', 1, 'Переместить ниже'], ['×', 0, 'Удалить замену']].forEach(function (action) {
                    const button = document.createElement('button');
                    button.type = 'button'; button.className = 'btn btn-icon'; button.textContent = action[0];
                    button.title = action[2]; button.setAttribute('aria-label', action[2] + ' · замена ' + (index + 1));
                    button.disabled = !!action[1] && (index + action[1] < 0 || index + action[1] >= rules.length);
                    button.addEventListener('click', function () {
                        if (action[1]) {
                            const target = index + action[1]; const previous = rules[target];
                            rules[target] = rules[index]; rules[index] = previous;
                        } else rules.splice(index, 1);
                        render();
                    });
                    actions.appendChild(button);
                });
                row.appendChild(actions); rows.appendChild(row);
            });
        }
        $('lexicon-add').addEventListener('click', function () {
            rules.push({ find: '', replace: '', chance: 1 }); render();
            rows.lastChild.querySelector('textarea').focus();
        });
        render();
    }

    function collectSettings() {
        const form = $('settings-form');
        const payload = {};
        form.querySelectorAll('input, textarea, select').forEach(function (field) {
            if (!field.name) return;
            payload[field.name] = field.type === 'checkbox' ? field.checked : field.value;
        });
        return payload;
    }

    async function saveSettings(action) {
        const payload = collectSettings();
        payload.save_action = action;
        const data = await App.post(C.urls.saveSettings, payload);
        if (!data) return;
        App.closeModal('settings-modal');
        // История показывает ровно то, что увидит модель, а настройки это меняют.
        reloadHistory({ force: true });
    }

    async function applyPreset(presetId) {
        const data = await App.post(C.urls.applyPreset, { preset_id: presetId });
        if (!data) return;
        // Настройки изменились на сервере — перечитываем страницу настроек.
        setTimeout(function () { window.location.reload(); }, 600);
    }

    /* ==================== Персонаж ==================== */

    async function saveCharacter() {
        if (!C.urls.saveCharacter) return;
        const form = $('character-form');
        const payload = {};
        form.querySelectorAll('input, textarea, select').forEach(function (field) {
            if (field.name) payload[field.name] = field.value;
        });
        const data = await App.post(C.urls.saveCharacter, payload);
        if (data) App.closeModal('character-modal');
    }

    async function createCharacter() {
        const name = $('new_character_name').value.trim();
        if (!name) { App.toast('Введите имя персонажа.', 'warning'); return; }
        const data = await App.post(C.urls.createCharacter, { new_character_name: name });
        if (!data) return;
        // Сразу делаем нового персонажа активным в этом чате.
        await App.post(C.urls.setCharacter, { character_id: data.character_id }, { successToast: false });
        window.location.reload();
    }

    async function selectCharacter(characterId) {
        const data = await App.post(C.urls.setCharacter, { character_id: characterId });
        if (data) setTimeout(function () { window.location.reload(); }, 400);
    }

    /* ==================== Стикеры ==================== */

    async function loadQuarantine() {
        const grid = $('quarantine-grid');
        if (!grid) return;
        const data = await App.get(C.urls.quarantine);
        if (!data) return;
        const items = data.items || [];
        $('quarantine-count').textContent = items.length;
        if (!items.length) {
            grid.innerHTML = '<div class="empty">Карантин пуст — модель ни разу не отказывалась из-за стикеров.</div>';
            return;
        }
        grid.innerHTML = '';
        items.forEach(function (item) {
            // Карточка собирается DOM-методами: codename и описание приходят из базы,
            // а туда их пишет и модель — в innerHTML им не место.
            const sid = Number(item.sticker_id);
            const cn = String(item.codename || '');
            const suggested = cn.startsWith('raw_') ? '' : cn;

            const card = document.createElement('div');
            card.className = 'qcard';
            card.dataset.stickerId = String(sid);

            const img = document.createElement('img');
            img.className = 'qthumb';
            img.src = '/sticker/' + sid;
            img.alt = '';
            img.loading = 'lazy';
            img.addEventListener('error', function () { img.replaceWith(document.createTextNode('нет превью')); });

            const name = document.createElement('input');
            name.type = 'text';
            name.className = 'qname';
            name.placeholder = 'codename_snake_case';
            name.value = suggested;

            const desc = document.createElement('textarea');
            desc.className = 'qdesc';
            desc.rows = 2;
            desc.placeholder = 'описание (опционально)';
            desc.value = String(item.description || '');

            const btns = document.createElement('div');
            btns.className = 'qbtns';
            [['release', 'Освободить', ''], ['describe', 'Описать', 'btn-primary'], ['ignore', 'Игнор', '']]
                .forEach(function (spec) {
                    const b = document.createElement('button');
                    b.type = 'button';
                    b.className = 'btn btn-sm qact ' + spec[2];
                    b.dataset.act = spec[0];
                    b.textContent = spec[1];
                    btns.appendChild(b);
                });

            card.appendChild(img);
            card.appendChild(name);
            card.appendChild(desc);
            card.appendChild(btns);
            grid.appendChild(card);
        });
    }

    async function quarantineAction(card, act) {
        const sid = card.dataset.stickerId;
        const payload = { sticker_id: Number(sid), action: act };
        if (act === 'describe') {
            const cn = card.querySelector('.qname').value.trim();
            const desc = card.querySelector('.qdesc').value.trim();
            if (!cn) { App.toast('Введите codename.', 'warning'); return; }
            payload.codename = cn;
            payload.description = desc;
        }
        const data = await App.postJSON(C.urls.quarantineDecide, payload);
        if (data) card.remove();
        const remaining = $('quarantine-grid').querySelectorAll('.qcard').length;
        $('quarantine-count').textContent = remaining;
        if (!remaining) loadQuarantine();
    }

    function initStickers() {
        const form = $('sticker-form');
        if (!form) return;

        function syncMaster(setNode) {
            const master = setNode.querySelector('.set-master');
            const boxes = Array.from(setNode.querySelectorAll('.sticker-cb'));
            if (!master || !boxes.length) return;
            const checked = boxes.filter(function (b) { return b.checked; }).length;
            master.checked = checked === boxes.length;
            master.indeterminate = checked > 0 && checked < boxes.length;
        }

        function syncAllMasters() {
            form.querySelectorAll('.sticker-set').forEach(syncMaster);
        }

        form.addEventListener('change', function (e) {
            if (e.target.classList.contains('set-master')) {
                const setNode = e.target.closest('.sticker-set');
                setNode.querySelectorAll('.sticker-cb').forEach(function (b) { b.checked = e.target.checked; });
                syncMaster(setNode);
            } else if (e.target.classList.contains('sticker-cb')) {
                syncMaster(e.target.closest('.sticker-set'));
            }
        });

        const filter = $('sticker-filter');
        if (filter) {
            filter.addEventListener('input', function () {
                const q = this.value.trim().toLowerCase();
                form.querySelectorAll('.sticker-set').forEach(function (setNode) {
                    let visible = 0;
                    setNode.querySelectorAll('.sticker-item').forEach(function (item) {
                        const match = !q || item.dataset.search.includes(q);
                        item.style.display = match ? '' : 'none';
                        if (match) visible++;
                    });
                    const setMatch = !q || setNode.dataset.set.includes(q);
                    setNode.style.display = (visible || setMatch) ? '' : 'none';
                });
            });
        }

        $('stickers-all').addEventListener('click', function () {
            form.querySelectorAll('.sticker-cb').forEach(function (b) { b.checked = true; });
            syncAllMasters();
        });
        $('stickers-none').addEventListener('click', function () {
            form.querySelectorAll('.sticker-cb').forEach(function (b) { b.checked = false; });
            syncAllMasters();
        });
        $('save-stickers').addEventListener('click', async function () {
            const codenames = Array.from(form.querySelectorAll('.sticker-cb:checked'))
                .map(function (b) { return b.value; });
            const data = await App.post(C.urls.stickers, { sticker_enabled: codenames });
            if (data) App.closeModal('sticker-modal');
        });

        // «Скопировать промпт» — тот же текст, что уходит модели.
        const copyPromptBtn = $('stickers-copy-prompt');
        if (copyPromptBtn) {
            copyPromptBtn.addEventListener('click', async function () {
                const data = await App.get(C.urls.stickerPrompt);
                copyPromptToClipboard(data && data.text, 'Промпт стикеров скопирован.');
            });
        }

        const grid = $('quarantine-grid');
        if (grid) {
            grid.addEventListener('click', function (e) {
                const btn = e.target.closest('.qact');
                if (!btn) return;
                quarantineAction(btn.closest('.qcard'), btn.dataset.act);
            });
        }

        const describeFindBtn = $('describe-find');
        const describeBtn = $('describe-run');
        const describerPrompt = $('describe-system-prompt');
        const saveDescriberPrompt = $('describe-prompt-save');
        const resetDescriberPrompt = $('describe-prompt-reset');
        const candidateSection = $('describe-candidates-section');
        const candidateGrid = $('describe-candidates-grid');
        const resultSection = $('describe-result-section');
        const resultGrid = $('describe-result-grid');
        const resultPacks = $('describe-result-packs');
        let foundStickerIds = [];

        function addStickerMeta(card, text, className) {
            const node = document.createElement('div');
            node.className = className || 'sticker-reference-meta';
            node.textContent = text;
            card.appendChild(node);
        }

        function telegramPackLabel(item) {
            let label = item.set_title || item.telegram_pack_title || 'название неизвестно';
            const shortName = item.set_short_name || item.telegram_pack_short_name;
            if (shortName) label += ' (@' + shortName + ')';
            return label;
        }

        async function renderFoundStickers(items) {
            candidateGrid.replaceChildren();
            candidateSection.hidden = false;
            describeBtn.hidden = true;
            foundStickerIds = [];
            const loads = [];

            (items || []).forEach(function (item) {
                const card = document.createElement('div');
                card.className = 'sticker-reference-card sticker-candidate-card';
                const image = document.createElement('img');
                image.alt = 'Стикер ' + item.id;
                if (item.media_available) {
                    loads.push(new Promise(function (resolve) {
                        image.addEventListener('load', function () { resolve(String(item.id)); }, { once: true });
                        image.addEventListener('error', function () {
                            image.classList.add('missing');
                            resolve(null);
                        }, { once: true });
                        image.src = '/sticker/' + encodeURIComponent(String(item.id));
                    }));
                } else {
                    image.classList.add('missing');
                    addStickerMeta(card, 'Медиа не удалось загрузить', 'sticker-reference-desc sticker-media-error');
                }
                card.prepend(image);
                addStickerMeta(card, (item.emoji || '◻') + ' Telegram: ' + telegramPackLabel(item),
                    'sticker-reference-code');
                addStickerMeta(card, 'sticker_id: ' + item.id);
                addStickerMeta(card, 'set_id: ' + (item.set_id || 'неизвестен'));
                candidateGrid.appendChild(card);
            });

            foundStickerIds = (await Promise.all(loads)).filter(Boolean);
            describeBtn.hidden = foundStickerIds.length === 0;
        }

        function renderDescriptionResult(data) {
            resultGrid.replaceChildren();
            resultPacks.replaceChildren();
            const stickers = data.added_stickers || [];
            const packs = data.added_packs || [];
            resultSection.hidden = !(stickers.length || packs.length);

            packs.forEach(function (pack) {
                const row = document.createElement('div');
                row.className = 'sticker-result-pack';
                const action = pack.created ? 'Создан пак программы' : 'Обновлён пак программы';
                row.textContent = action + ' «' + pack.name + '»' +
                    (pack.description ? ': ' + pack.description : '');
                resultPacks.appendChild(row);
            });

            stickers.forEach(function (item) {
                const card = document.createElement('div');
                card.className = 'sticker-reference-card sticker-result-card';
                const image = document.createElement('img');
                image.src = '/sticker/' + encodeURIComponent(String(item.sticker_id));
                image.alt = item.codename;
                image.addEventListener('error', function () { image.classList.add('missing'); }, { once: true });
                card.appendChild(image);
                addStickerMeta(card, item.codename, 'sticker-reference-code');
                addStickerMeta(card, 'пак программы: ' + item.program_pack);
                addStickerMeta(card, (item.emoji || '◻') + ' Telegram: ' + telegramPackLabel(item));
                addStickerMeta(card, 'sticker_id: ' + item.sticker_id);
                addStickerMeta(card, 'set_id: ' + (item.telegram_set_id || 'неизвестен'));
                if (item.description) addStickerMeta(card, item.description, 'sticker-reference-desc');
                resultGrid.appendChild(card);
            });
        }
        if (saveDescriberPrompt) {
            saveDescriberPrompt.addEventListener('click', async function () {
                const data = await App.postJSON(C.urls.stickerDescriberPrompt,
                    { system_prompt: describerPrompt.value });
                if (data) $('describe-status').textContent = data.message;
            });
        }
        if (resetDescriberPrompt) {
            resetDescriberPrompt.addEventListener('click', async function () {
                const data = await App.postJSON(C.urls.stickerDescriberPrompt, { reset: true });
                if (data) {
                    describerPrompt.value = data.system_prompt;
                    $('describe-status').textContent = 'Системный текст восстановлен.';
                }
            });
        }
        if (describeFindBtn) {
            describeFindBtn.addEventListener('click', async function () {
                describeFindBtn.disabled = true;
                describeBtn.hidden = true;
                resultSection.hidden = true;
                candidateSection.hidden = true;
                candidateGrid.replaceChildren();
                foundStickerIds = [];
                const limit = Number($('describe-limit').value) || 100;
                $('describe-status').textContent = 'Ищу стикеры и загружаю медиа…';
                const data = await App.postJSON(C.urls.findStickers, { limit: limit });
                if (!data) {
                    $('describe-status').textContent = '';
                    describeFindBtn.disabled = false;
                    return;
                }
                $('describe-status').textContent = data.message;
                if (data.candidates && data.candidates.length) {
                    await renderFoundStickers(data.candidates);
                    if (!foundStickerIds.length) {
                        $('describe-status').textContent += ' В браузере не загрузилось ни одного превью.';
                    }
                }
                describeFindBtn.disabled = false;
            });
        }
        if (describeBtn) {
            describeBtn.addEventListener('click', async function () {
                describeBtn.disabled = true;
                const limit = Number($('describe-limit').value) || 100;
                const referenceIds = Array.from(document.querySelectorAll(
                    '#describe-reference-grid [data-sticker-id]'))
                    .map(function (card) { return card.dataset.stickerId; });
                $('describe-status').textContent = 'Модель смотрит стикеры…';
                const data = await App.postJSON(C.urls.describeFromChat,
                    { limit: limit, system_prompt: describerPrompt.value,
                      reference_ids: referenceIds, candidate_ids: foundStickerIds });
                describeBtn.disabled = false;
                if (!data) { $('describe-status').textContent = ''; return; }
                $('describe-status').textContent = data.message ||
                    ('Готово. Новых стикеров: ' + data.found + ', описано: ' + data.described + '.');
                renderDescriptionResult(data);
                if (data.described || data.quarantined) describeBtn.hidden = true;
                loadQuarantine();
            });
        }

        syncAllMasters();
        // Список карантина читаем при открытии модалки и при каждом заходе на вкладку.
        const openBtn = $('open-stickers');
        if (openBtn) openBtn.addEventListener('click', loadQuarantine);
        const quarantineTab = document.querySelector('.tab[data-tab="tab-stickers-quarantine"]');
        if (quarantineTab) quarantineTab.addEventListener('click', loadQuarantine);
    }

    /* ==================== Картинки ==================== */

    async function copyPromptToClipboard(text, successMsg) {
        if (!text) { App.toast('Промпт пуст — включите хотя бы один пункт.', 'warning'); return; }
        try {
            await navigator.clipboard.writeText(text);
            App.toast(successMsg || 'Скопировано.', 'success');
        } catch (e) {
            // Клипборд-API требует https/localhost и user gesture — тут второе есть, а первое обычно да.
            App.toast('Не удалось скопировать: браузер запретил доступ к буферу.', 'error');
        }
    }

    function initImages() {
        const form = document.getElementById('image-form');
        if (!form) return;

        const filter = document.getElementById('image-filter');
        if (filter) {
            filter.addEventListener('input', function () {
                const q = this.value.trim().toLowerCase();
                form.querySelectorAll('.image-card').forEach(function (card) {
                    const match = !q || card.dataset.search.includes(q);
                    card.style.display = match ? '' : 'none';
                });
            });
        }

        document.getElementById('images-all').addEventListener('click', function () {
            form.querySelectorAll('.image-cb:not(:disabled)').forEach(function (b) { b.checked = true; });
        });
        document.getElementById('images-none').addEventListener('click', function () {
            form.querySelectorAll('.image-cb').forEach(function (b) { b.checked = false; });
        });

        // Описания сохраняются точечно при потере фокуса — иначе одна кнопка «Сохранить»
        // потребовала бы посылать десятки полей текстов вместе со списком чекбоксов.
        form.addEventListener('change', function (e) {
            if (!e.target.classList.contains('image-desc')) return;
            const card = e.target.closest('.image-card');
            App.postJSON(C.urls.imageDescribe, {
                codename: card.dataset.codename,
                description: e.target.value,
            }, { successToast: false });
        });

        document.getElementById('save-images').addEventListener('click', async function () {
            const codenames = Array.from(form.querySelectorAll('.image-cb:checked'))
                .map(function (b) { return b.value; });
            const data = await App.post(C.urls.images, { image_enabled: codenames });
            if (data) App.closeModal('image-modal');
        });

        document.getElementById('images-copy-prompt').addEventListener('click', async function () {
            const data = await App.get(C.urls.imagePrompt);
            copyPromptToClipboard(data && data.text, 'Промпт картинок скопирован.');
        });
    }

    /* ==================== Экспорт ==================== */

    /* Формат datetime-local: "YYYY-MM-DDTHH:MM" в локальном времени. */
    function fmtLocal(date) {
        var pad = function (n) { return String(n).padStart(2, '0'); };
        return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate())
             + 'T' + pad(date.getHours()) + ':' + pad(date.getMinutes());
    }

    function initExport() {
        var fromInput = $('export-from');
        var toInput = $('export-to');
        if (!fromInput || !toInput) return;

        function fillDefaults() {
            var now = new Date();
            var dayAgo = new Date(now.getTime() - 24 * 60 * 60 * 1000);
            fromInput.value = fromInput.value || fmtLocal(dayAgo);
            toInput.value = toInput.value || fmtLocal(now);
        }

        $('open-export').addEventListener('click', function () {
            fillDefaults();
            App.openModal('export-modal');
        });

        // Быстрые пресеты диапазонов.
        document.querySelectorAll('#export-modal [data-range]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var now = new Date();
                var from, to;
                switch (btn.dataset.range) {
                    case '1h':
                        from = new Date(now.getTime() - 60 * 60 * 1000);
                        to = now;
                        break;
                    case 'today':
                        from = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0);
                        to = now;
                        break;
                    case 'yesterday':
                        var yStart = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1, 0, 0);
                        var yEnd = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1, 23, 59);
                        from = yStart; to = yEnd;
                        break;
                    case '7d':
                        from = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);
                        to = now;
                        break;
                    case '30d':
                        from = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000);
                        to = now;
                        break;
                }
                fromInput.value = fmtLocal(from);
                toInput.value = fmtLocal(to);
            });
        });

        $('export-run').addEventListener('click', async function () {
            var from = fromInput.value;
            var to = toInput.value;
            if (!from || !to) { App.toast('Заполните обе даты.', 'warning'); return; }
            if (from > to) { App.toast('Начало позже конца — поменяйте местами.', 'warning'); return; }

            var btn = this;
            btn.disabled = true;
            btn.textContent = 'Выгружаю…';

            var url = C.urls.export + '?from=' + encodeURIComponent(from) + '&to=' + encodeURIComponent(to);
            try {
                var response = await fetch(url);
                if (!response.ok) {
                    // Ошибка от нашего API — тело JSON, тип application/json.
                    var errBody = null;
                    try { errBody = await response.json(); } catch (e) {}
                    App.toast((errBody && errBody.message) || ('Ошибка ' + response.status), 'error');
                    return;
                }

                // Скачиваем как файл, имя берём из Content-Disposition.
                var blob = await response.blob();
                var dispo = response.headers.get('Content-Disposition') || '';
                var m = dispo.match(/filename="?([^"]+)"?/);
                var filename = m ? m[1] : ('chat_' + C.id + '.json');

                var href = URL.createObjectURL(blob);
                var link = document.createElement('a');
                link.href = href;
                link.download = filename;
                document.body.appendChild(link);
                link.click();
                document.body.removeChild(link);
                URL.revokeObjectURL(href);

                App.toast('Готово: ' + filename, 'success');
                App.closeModal('export-modal');
            } catch (err) {
                console.error(err);
                App.toast('Не удалось выгрузить: связь с сервером потеряна.', 'error');
            } finally {
                btn.disabled = false;
                btn.textContent = 'Скачать JSON';
            }
        });
    }

    /* ==================== Запуск ==================== */

    document.addEventListener('DOMContentLoaded', function () {
        AutoActivity.init(C);
        setAutoStatus(C.autoStatus);
        scrollToBottom(false);
        loadMedia();
        initModels();
        initModelCombo();
        initStickers();
        initImages();
        initExport();
        KeyManager.init();
        connect();
        historyPoll = setInterval(function () { reloadHistory(); }, 5000);
        document.addEventListener('visibilitychange', function () {
            if (!document.hidden) { reloadHistory(); loadMedia(); AutoActivity.refresh(); }
            else clearTimeout(reloadTimer);
        });
        window.addEventListener('pagehide', function () {
            pagePaused = true; clearInterval(historyPoll); clearTimeout(reloadTimer); clearTimeout(reconnectTimer);
            if (source) source.close();
        });
        window.addEventListener('pageshow', function () {
            if (!pagePaused) return;
            pagePaused = false; connect(); reloadHistory(); loadMedia();
            historyPoll = setInterval(function () { reloadHistory(); }, 5000);
        });

        $('reload-history').addEventListener('click', function () { reloadHistory({ force: true }); });
        $('limit-input').addEventListener('change', function () { reloadHistory({ force: true }); });
        $('auto-toggle').addEventListener('click', toggleAuto);
        $('generate-btn').addEventListener('click', generate);
        $('send-btn').addEventListener('click', send);

        const reply = $('reply-text');
        reply.addEventListener('input', function () {
            $('send-btn').disabled = !this.value.trim();
        });
        reply.addEventListener('keydown', function (e) {
            // Ctrl+Enter отправляет — привычно и не мешает переносам строк.
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); }
        });

        $('character-select').addEventListener('change', function () { selectCharacter(this.value); });

        const openCharacter = $('open-character');
        if (openCharacter) openCharacter.addEventListener('click', function () { App.openModal('character-modal'); });
        $('open-new-character').addEventListener('click', function () { App.openModal('new-character-modal'); });
        const saveCharacterBtn = $('save-character');
        if (saveCharacterBtn) saveCharacterBtn.addEventListener('click', saveCharacter);
        $('create-character').addEventListener('click', createCharacter);

        $('open-settings').addEventListener('click', function () { App.openModal('settings-modal'); });
        initFallbackEditor();
        initLexiconEditor();
        $('open-stickers').addEventListener('click', function () { App.openModal('sticker-modal'); });
        const openImages = $('open-images');
        if (openImages) openImages.addEventListener('click', function () { App.openModal('image-modal'); });
        $('open-keys').addEventListener('click', function () { KeyManager.open(); });

        $('save-settings-chat').addEventListener('click', function () { saveSettings('save_for_chat'); });
        $('save-settings-default').addEventListener('click', function () { saveSettings('save_for_chat_and_default'); });
        $('reset-settings').addEventListener('click', async function () {
            if (!confirm('Сбросить настройки этого чата к значениям персонажа по умолчанию?')) return;
            const data = await App.post(C.urls.resetSettings, {});
            if (data) setTimeout(function () { window.location.reload(); }, 600);
        });

        document.querySelectorAll('.preset').forEach(function (button) {
            button.addEventListener('click', function () { applyPreset(this.dataset.preset); });
        });

        $('update-memory').addEventListener('click', async function () {
            if (!C.hasCharacter) { App.toast('Сначала выберите персонажа.', 'warning'); return; }
            if (!confirm('Проанализировать недавние сообщения и дописать их суть в память персонажа?')) return;
            this.disabled = true;
            const data = await App.post(C.urls.updateMemory, {});
            this.disabled = false;
            // Модалка персонажа отрисована при загрузке — подставляем свежую память сами.
            const memoryBox = $('memory_prompt');
            if (data && data.memory_prompt && memoryBox) memoryBox.value = data.memory_prompt;
        });

        // Ползунок «игнорировать всё медиа» гасит переключатели типов.
        const ignoreAll = $('ignore_all_media');
        if (ignoreAll) {
            const sync = function () {
                const types = $('media-types');
                types.style.opacity = ignoreAll.checked ? '.45' : '1';
                types.querySelectorAll('input').forEach(function (i) { i.disabled = ignoreAll.checked; });
            };
            ignoreAll.addEventListener('change', sync);
            sync();
        }
    });
})();
