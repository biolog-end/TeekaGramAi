/* Локальная проверка таймеров, старых снимков и вывода недоверенного текста без браузера. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

class Element {
    constructor() {
        this.children = []; this.hidden = false; this.textContent = '';
        this.scrollHeight = 100; this.scrollTop = 0; this.clientHeight = 100;
    }
    append(...items) { this.children.push(...items); }
    appendChild(item) { this.children.push(item); }
    replaceChildren(...items) { this.children = items; }
    set innerHTML(_value) { throw new Error('Текст модели нельзя выводить через innerHTML'); }
}

async function test() {
    const elements = new Map();
    let clock = 0, tick, resolveInitial;
    const context = vm.createContext({
        window: {},
        document: {
            getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
            createElement() { return new Element(); }
        },
        performance: { now: () => clock },
        setInterval(fn) { tick = fn; },
        fetch: () => new Promise(resolve => { resolveInitial = resolve; })
    });
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/js/auto_activity.js'), 'utf8'), context);
    const ui = context.window.AutoActivity;
    ui.init({ autoStatus: 'active', urls: { activity: '/fake' } });
    const element = id => elements.get(id);
    const entry = (id, phase, message, fields = {}) => ({ id, phase, at: 100, message, ...fields });
    const receive = (item, fields = {}) => ui.receive({
        run_id: 1, server_now: 100, entry: item, current: item, ...fields
    });
    receive(entry(1, 'part', 'Часть 1 из 2: текст', { part: 1, total: 2, text: 'мой текст' }));
    receive(entry(2, 'typing', 'Печатаю', { part: 1, total: 2, text: 'мой тект', duration_s: 10, ends_at: 110 }));
    clock = 2100; tick();
    assert.match(element('activity-timer').textContent, /7,9 с из 10,0 с/);
    assert.equal(element('activity-part').textContent, 'мой тект');
    assert.match(element('activity-current').textContent, /Часть 1 из 2/);
    assert.ok(Math.abs(element('activity-progress').value - .21) < 1e-8);

    receive(entry(3, 'request', 'Жду API'));
    clock += 3200; tick();
    assert.equal(element('activity-timer').textContent, 'Прошло 3,2 с');
    assert.equal(element('activity-progress').hidden, true);
    assert.equal(element('activity-part').hidden, true);
    receive(entry(2, 'typing', 'Старый снимок'), { entries: [entry(1, 'part', 'Старое событие')] });
    assert.equal(element('activity-current').textContent, 'Жду API');

    const attackText = '<img src=x onerror=alert(1)>\n{split}ещё';
    receive(entry(4, 'response', 'Ответ получен'), { response: { id: 4, text: attackText, model: 'gpt-5-mini' } });
    assert.equal(element('activity-response').textContent, attackText);
    assert.equal(element('activity-response-details').hidden, false);
    receive(entry(5, 'started', 'Новый запуск'), { run_id: 5 });
    assert.equal(element('activity-response-details').hidden, true);
    assert.equal(element('activity-log').children.length, 1);

    // Первоначальный HTTP-запрос завершился после событий SSE нового запуска.
    resolveInitial({ ok: true, json: async () => ({ status: 'success', run_id: 1,
        entries: [entry(1, 'typing', 'Устаревший запуск')], current: entry(1, 'typing', 'Устаревший запуск'),
        response: { id: 4, text: attackText } }) });
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(element('activity-current').textContent, 'Новый запуск');
    assert.equal(element('activity-response-details').hidden, true);

    for (let id = 6; id <= 105; id++) receive(entry(id, 'idle', String(id)), { run_id: 5 });
    assert.equal(element('activity-log').children.length, 80);
    receive(entry(106, 'typing', 'Печатаю', { duration_s: 1, ends_at: 101 }), { run_id: 5 });
    clock += 2000; tick();
    assert.match(element('activity-timer').textContent, /0,0 с.*ожидаю завершения/);
    ui.setStatus('inactive');
    assert.equal(element('activity-timer').textContent, '');
    assert.equal(element('auto-activity').hidden, false);
    // Сервер перезапустился: старый журнал исчезает даже при пустом новом снимке.
    ui.receive({ instance_id: 1, run_id: null, entries: [], current: null, response: null, server_now: 200 });
    assert.equal(element('auto-activity').hidden, true);
    assert.equal(element('activity-log').children.length, 0);
    ui.receive({ instance_id: 0, run_id: 5, entry: entry(107, 'idle', 'Из прошлого процесса') });
    assert.equal(element('auto-activity').hidden, true);
    console.log('Журнал: таймер, восстановление, порядок событий, текст и лимит строк — OK');
}
test().catch(error => { console.error(error); process.exitCode = 1; });
