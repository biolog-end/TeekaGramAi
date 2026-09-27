/* Код и пароль передаются ожидающему входу, не сохраняются в браузере. */
(function () {
    'use strict';
    const $ = id => document.getElementById(id);
    let challenge = null, fetching = false, timer = null;
    async function refresh() {
        if (fetching || document.hidden) return;
        fetching = true;
        try {
            const data = await App.get('/api/login'); if (!data) return;
            if (data.stage === 'ready') { location.replace('/'); return; }
            $('instance-login-message').textContent = data.message;
            const entering = ['phone', 'code', 'password'].includes(data.stage);
            $('instance-login-form').hidden = !entering;
            if (entering && challenge !== data.challenge) {
                challenge = data.challenge;
                const input = $('instance-login-value'); input.value = '';
                input.type = data.stage === 'password' ? 'password' : data.stage === 'phone' ? 'tel' : 'text';
                input.inputMode = data.stage === 'phone' ? 'tel' : data.stage === 'code' ? 'numeric' : 'text';
                input.autocomplete = data.stage === 'code' ? 'one-time-code' : 'off';
                $('instance-login-label').textContent = {phone: 'Номер телефона', code: 'Код Telegram', password: 'Пароль 2FA'}[data.stage];
                $('instance-login-submit').disabled = false; input.focus();
            }
        } finally { fetching = false; }
    }
    document.addEventListener('DOMContentLoaded', () => {
        $('instance-login-form').addEventListener('submit', async event => {
            event.preventDefault(); const input = $('instance-login-value'), value = input.value;
            input.value = ''; $('instance-login-submit').disabled = true;
            await App.postJSON('/api/login/submit', {challenge, value}, {successToast: false});
            challenge = null; refresh();
        });
        refresh(); timer = setInterval(refresh, 1000);
    });
    window.addEventListener('pagehide', () => { clearInterval(timer); timer = null; });
    window.addEventListener('pageshow', event => { if (event.persisted && !timer) { refresh(); timer = setInterval(refresh, 1000); } });
})();
