/* ============================================================
   ЦЕЗАРЬ — вкладка «🔔 Уведомления» в админ-панели (глобальный админ).
   Все данные хранятся в БД через REST API (/api/core/admin/notify/*),
   localStorage не используется.
   Сегменты: статус доставки / базовые сценарии / шаблоны рассылок /
   рассылка (все|пользователи|подразделение|руководители) / история / outbox.
   ============================================================ */
(function () {
  'use strict';

  const NA = { scens: [], tpls: [], bcs: [], outbox: [], status: null,
               depts: [], users: [], evKeys: [], enKinds: [] };

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  async function jget(url) {
    const r = await fetch(url, { credentials: 'same-origin' });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || ('HTTP ' + r.status));
    return r.json();
  }
  async function jsend(url, method, body) {
    const r = await fetch(url, {
      method, credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
    return data;
  }
  function toast(msg, ok) {
    if (typeof window.notify === 'function') { window.notify(msg, ok === false ? 'error' : 'success'); return; }
    alert(msg);
  }
  function fmtDT(iso) {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }); }
    catch (e) { return iso; }
  }
  const EV_RU = { assigned: 'Назначение ответственным', executor_added: 'Назначение исполнителем',
                  status_changed: 'Смена статуса', due_soon: 'Приближается срок', overdue: 'Просрочено',
                  created: 'Создание сущности', comment: 'Новый комментарий', shared: 'Открыт доступ',
                  done: 'Завершение', custom: 'Пользовательское событие' };
  const KIND_RU = { project: 'Проекты', task: 'Задачи', meeting: 'Совещания', doc: 'Документы',
                    soft: 'ПО', grant: 'Гранты', schol: 'Стипендии', priority: 'Приоритеты' };
  function evLabel(k) { return EV_RU[k] || k; }
  function kindLabel(k) { return KIND_RU[k] || k; }

  /* ---------------- Модалка ---------------- */
  function showModal(title, html, onOk) {
    const old = document.getElementById('naModal');
    if (old) old.remove();
    const wrap = document.createElement('div');
    wrap.id = 'naModal';
    wrap.style.cssText = 'position:fixed;inset:0;background:rgba(15,23,42,.55);z-index:9999;display:flex;align-items:center;justify-content:center;padding:16px';
    wrap.innerHTML = `<div class="card" style="max-width:680px;width:100%;max-height:88vh;overflow:auto;padding:18px">
      <h3 style="margin:0 0 12px">${esc(title)}</h3>${html}
      <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:14px">
        <button class="btn ghost" id="naMCancel">Отмена</button>
        <button class="btn primary" id="naMok">💾 Сохранить</button>
      </div></div>`;
    document.body.appendChild(wrap);
    wrap.querySelector('#naMCancel').onclick = () => wrap.remove();
    wrap.addEventListener('click', (e) => { if (e.target === wrap) wrap.remove(); });
    wrap.querySelector('#naMok').onclick = async () => {
      const btn = wrap.querySelector('#naMok');
      btn.disabled = true;
      let res;
      try { res = await onOk(); }
      catch (e) { toast('⛔ ' + e.message, false); res = false; }
      btn.disabled = false;
      if (res !== false) wrap.remove();
    };
  }

  /* ---------------- Статус конфигурации доставки ---------------- */
  async function loadStatus() {
    const el = document.getElementById('ntgStatus');
    if (!el) return;
    try {
      const st = await jget('/api/core/admin/notify/status');
      NA.status = st;
      const bot = st.bot_configured
        ? `<b style="color:#0a7d32">✅ Telegram-бот настроен (${esc(st.bot_username || '')})</b>`
        : '<b style="color:#c62828">⛔ TELEGRAM_BOT_TOKEN не задан</b> — фактическая доставка невозможна';
      const flag = st.notify_enabled ? '' : ' · <span style="color:#c62828">рассылка событий отключена (TELEGRAM_NOTIFY_ENABLED=false)</span>';
      el.innerHTML =
        `<div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center">
          <span>${bot}${flag}</span>
          <span class="muted">База: ${st.pending ?? 0} ⏳</span>
          <span style="color:#0a7d32">✅ Доставлено: ${st.sent_ok ?? 0}</span>
          <span style="color:#c62828">⛔ Ошибок: ${st.errors ?? 0}</span>
          <span class="muted">Telegram привязан: ${st.users_telegram_linked ?? 0}/${st.users_total ?? 0} польз.</span>
          <span class="muted">Ссылки в уведомлениях: ${esc(st.public_base_url || '(не задан PUBLIC_BASE_URL)')}</span>
          <button class="btn sm ghost" onclick="naTestSend()">📡 Тестовая отправка себе</button>
        </div>`;
    } catch (e) {
      el.innerHTML = `<span style="color:#c62828">Не удалось получить статус: ${esc(e.message)}</span>`;
    }
  }
  window.naTestSend = async function () {
    try {
      const r = await jsend('/api/core/notify/test', 'POST', {});
      toast(r.note || '📨 Тест поставлен в очередь (~5 секунд)', true);
      setTimeout(() => { loadOutbox(); loadStatus(); }, 6000);
    } catch (e) { toast('⛔ ' + e.message, false); }
  };

  /* ---------------- Базовые сценарии (только админ, для всех пользователей) ---------------- */
  async function loadScenarios() {
    const el = document.getElementById('nscList');
    if (!el) return;
    el.innerHTML = 'Загрузка…';
    try {
      const d = await jget('/api/core/admin/notify/base-scenarios');
      NA.scens = d.scenarios || [];
      NA.evKeys = d.event_keys || Object.keys(EV_RU);
      NA.enKinds = d.entity_kinds || Object.keys(KIND_RU);
      if (!NA.scens.length) { el.innerHTML = '<p class="muted">Базовых сценариев нет — нажмите «➕ Создать».</p>'; return; }
      el.innerHTML = '<table class="tbl"><thead><tr>' +
        '<th>Название</th><th>Событие</th><th>Виды сущностей</th><th>Шаблон текста</th><th>Статус</th><th style="width:120px"></th></tr></thead><tbody>' +
        NA.scens.map(s => `<tr>
          <td><b>${esc(s.name)}</b>${s.description ? `<div class="sub">${esc(s.description)}</div>` : ''}</td>
          <td>${esc(evLabel(s.event_key))}</td>
          <td>${(s.entity_kinds && s.entity_kinds.length) ? s.entity_kinds.map(k => esc(kindLabel(k))).join(', ') : '<i>все</i>'}</td>
          <td class="sub">${esc((s.template || '').slice(0, 90))}${(s.template || '').length > 90 ? '…' : ''}</td>
          <td>${s.is_active ? '<span style="color:#0a7d32">● активен</span>' : '<span class="muted">○ выключен</span>'}</td>
          <td style="white-space:nowrap">
            <button class="btn sm ghost" title="Редактировать" onclick="naScenForm(${s.id})">✏️</button>
            <button class="btn sm ghost" title="${s.is_active ? 'Выключить' : 'Включить'}" onclick="naScenToggle(${s.id},${!s.is_active})">${s.is_active ? '⏸' : '▶️'}</button>
            <button class="btn sm ghost" title="Удалить" onclick="naScenDelete(${s.id})">🗑</button>
          </td></tr>`).join('') + '</tbody></table>';
    } catch (e) { el.innerHTML = `<span style="color:#c62828">${esc(e.message)}</span>`; }
  }

  window.naScenForm = function (id) {
    const s = id ? NA.scens.find(x => x.id === id) : null;
    const evSel = NA.evKeys.map(v => `<option value="${esc(v)}" ${s && s.event_key === v ? 'selected' : ''}>${esc(evLabel(v))}</option>`).join('');
    const kindsChk = NA.enKinds.map(v =>
      `<label style="display:inline-flex;gap:4px;align-items:center;margin:0 12px 4px 0">
        <input type="checkbox" class="naKind" value="${v}" ${s ? (s.entity_kinds || []).includes(v) : true ? (s ? (s.entity_kinds || []).includes(v) : true) : true}> ${esc(kindLabel(v))}</label>`).join('');
    showModal(s ? 'Редактировать базовый сценарий' : 'Новый базовый сценарий', `
      <div style="display:grid;gap:10px">
        <label class="sub">Название<input id="nsName" class="adm-inp" value="${esc(s ? s.name : '')}" maxlength="200"></label>
        <label class="sub">Описание<input id="nsDesc" class="adm-inp" value="${esc(s ? s.description : '')}" maxlength="500"></label>
        <label class="sub">Событие-триггер<select id="nsEv" class="adm-inp">${evSel}</select></label>
        <div class="sub">Виды сущностей (пусто = все):<div id="nsKinds" style="margin-top:6px">${kindsChk}</div></div>
        <label class="sub">Шаблон текста уведомления
          <textarea id="nsTpl" rows="3" class="adm-inp">${esc(s ? s.template : '')}</textarea></label>
        <p class="muted" style="margin:0">Доступные плейсхолдеры: {title} {kind} {module} {action} {changed} {due_date} {actor} {link}.
          Ссылка {link} ведёт на сущность в системе. Базовые сценарии получают все пользователи; отключить их может только администратор здесь.</p>
        <label class="sub" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="nsActive" ${!s || s.is_active ? 'checked' : ''}> Сценарий активен</label>
      </div>`,
      async () => {
        const name = document.getElementById('nsName').value.trim();
        if (!name || name.length < 2) { toast('Укажите название (мин. 2 символа)', false); return false; }
        const body = {
          name,
          description: document.getElementById('nsDesc').value.trim() || null,
          event_key: document.getElementById('nsEv').value,
          entity_kinds: [...document.querySelectorAll('.naKind:checked')].map(i => i.value),
          module_keys: [],
          template: document.getElementById('nsTpl').value.trim(),
          visibility: 'global',
          department_id: null,
          is_active: document.getElementById('nsActive').checked,
        };
        if (s) await jsend('/api/core/admin/notify/base-scenarios/' + s.id, 'PATCH', body);
        else   await jsend('/api/core/admin/notify/base-scenarios', 'POST', body);
        toast('💾 Базовый сценарий сохранён', true);
        loadScenarios(); loadStatus();
      });
  };
  window.naScenToggle = async function (id, active) {
    const s = NA.scens.find(x => x.id === id);
    if (!s) return;
    try {
      await jsend('/api/core/admin/notify/base-scenarios/' + id, 'PATCH',
        { name: s.name, description: s.description, event_key: s.event_key,
          entity_kinds: s.entity_kinds || [], module_keys: s.module_keys || [],
          template: s.template || '', visibility: 'global', department_id: null, is_active: active });
      toast(active ? '▶️ Сценарий включён' : '⏸ Сценарий выключен', true);
      loadScenarios();
    } catch (e) { toast('⛔ ' + e.message, false); }
  };
  window.naScenDelete = async function (id) {
    if (!confirm('Удалить базовый сценарий? Он перестанет применяться ко всем пользователям.')) return;
    try { await jsend('/api/core/admin/notify/base-scenarios/' + id, 'DELETE', {}); toast('🗑 Удалено', true); loadScenarios(); }
    catch (e) { toast('⛔ ' + e.message, false); }
  };

  /* ---------------- Шаблоны рассылок ---------------- */
  async function loadTemplates() {
    const el = document.getElementById('ntplList');
    if (!el) return;
    el.innerHTML = 'Загрузка…';
    try {
      const d = await jget('/api/core/admin/notify/templates');
      NA.tpls = d.templates || [];
      if (!NA.tpls.length) { el.innerHTML = '<p class="muted">Шаблонов нет — нажмите «➕ Создать».</p>'; return; }
      el.innerHTML = '<table class="tbl"><thead><tr><th>Название</th><th>Заголовок</th><th>Текст</th><th style="width:90px"></th></tr></thead><tbody>' +
        NA.tpls.map(t => `<tr>
          <td><b>${esc(t.name)}</b></td><td>${esc(t.title || '')}</td>
          <td class="sub">${esc((t.body || '').slice(0, 80))}${(t.body || '').length > 80 ? '…' : ''}</td>
          <td style="white-space:nowrap">
            <button class="btn sm ghost" onclick="naTplForm(${t.id})">✏️</button>
            <button class="btn sm ghost" onclick="naTplDelete(${t.id})">🗑</button>
          </td></tr>`).join('') + '</tbody></table>';
      naRefreshTplSelect();
    } catch (e) { el.innerHTML = `<span style="color:#c62828">${esc(e.message)}</span>`; }
  }
  function naRefreshTplSelect() {
    const sel = document.querySelector('#bcFrm select[name=template_id]');
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = '<option value="">— без шаблона —</option>' +
      NA.tpls.map(t => `<option value="${t.id}" ${String(t.id) === cur ? 'selected' : ''}>${esc(t.name)}</option>`).join('');
  }
  window.naTplForm = function (id) {
    const t = id ? NA.tpls.find(x => x.id === id) : null;
    showModal(t ? 'Редактировать шаблон рассылки' : 'Новый шаблон рассылки', `
      <div style="display:grid;gap:10px">
        <label class="sub">Название шаблона<input id="tpName" class="adm-inp" value="${esc(t ? t.name : '')}" maxlength="200"></label>
        <label class="sub">Заголовок уведомления<input id="tpTitle" class="adm-inp" value="${esc(t ? t.title : '')}" maxlength="300"></label>
        <label class="sub">Текст<textarea id="tpBody" rows="4" class="adm-inp">${esc(t ? t.body : '')}</textarea></label>
        <p class="muted" style="margin:0">Плейсхолдеры: {name} — имя получателя, {login} — логин.</p>
      </div>`,
      async () => {
        const name = document.getElementById('tpName').value.trim();
        const title = document.getElementById('tpTitle').value.trim();
        const bodyTxt = document.getElementById('tpBody').value.trim();
        if (!name || name.length < 2 || !title) { toast('Заполните название и заголовок', false); return false; }
        const payload = { name, title, body: bodyTxt };
        if (t) await jsend('/api/core/admin/notify/templates/' + t.id, 'PATCH', payload);
        else   await jsend('/api/core/admin/notify/templates', 'POST', payload);
        toast('💾 Шаблон сохранён', true);
        loadTemplates();
      });
  };
  window.naTplDelete = async function (id) {
    if (!confirm('Удалить шаблон?')) return;
    try { await jsend('/api/core/admin/notify/templates/' + id, 'DELETE', {}); toast('🗑 Удалено', true); loadTemplates(); }
    catch (e) { toast('⛔ ' + e.message, false); }
  };
  window.naApplyTpl = function (id) {
    const t = NA.tpls.find(x => String(x.id) === String(id));
    const frm = document.getElementById('bcFrm');
    if (!frm || !t) return;
    frm.title.value = t.title || t.name;
    frm.body.value = (t.body || '').replace(/\{name\}/g, 'Иван Иванов').replace(/\{login\}/g, 'ivanov');
  };

  /* ---------------- Данные аудиторий ---------------- */
  async function ensureAudienceData() {
    if (!NA.depts.length) {
      try {
        const d = await jget('/api/core/departments');   // GET список активных подразделений
        NA.depts = Array.isArray(d) ? d : (d.items || d.departments || []);
      } catch (e) { NA.depts = []; }
    }
    if (!NA.users.length) {
      try {
        const d = await jget('/api/core/admin/users');   // админ видит все УЗ
        NA.users = Array.isArray(d) ? d : (d.items || d.users || []);
      } catch (e) { NA.users = []; }
    }
    const ds = document.getElementById('bcDeptSel');
    if (ds && !ds.options.length) {
      ds.innerHTML = NA.depts.map(x => `<option value="${x.id}">${esc(x.name)}</option>`).join('') ||
        '<option value="">— подразделений нет —</option>';
    }
    const us = document.getElementById('bcUserSel');
    if (us && !us.options.length) {
      us.innerHTML = NA.users.filter(u => u.active !== false)
        .map(u => `<option value="${u.id}">${esc(u.name || u.display_name || u.login)} (${esc(u.login || '')})${u.telegram ? ' ✈' : ''}</option>`).join('') ||
        '<option value="">— пользователей нет —</option>';
    }
  }

  window.naAudChange = function () {
    const frm = document.getElementById('bcFrm');
    if (!frm) return;
    const mode = frm.mode.value;
    document.getElementById('bcAudUsers').style.display = mode === 'users' ? '' : 'none';
    document.getElementById('bcAudDept').style.display = mode === 'department' ? '' : 'none';
    naUpdatePreview();
  };

  async function naUpdatePreview() {
    const pv = document.getElementById('bcPreview');
    const frm = document.getElementById('bcFrm');
    if (!pv || !frm) return;
    const mode = frm.mode.value;
    try {
      if (mode === 'users') {
        const n = [...document.getElementById('bcUserSel').selectedOptions].length;
        pv.textContent = n ? `Получателей: ${n}` : 'Выберите пользователей';
        return;
      }
      let url = '/api/core/admin/notify/audience-preview?mode=' + encodeURIComponent(mode);
      if (mode === 'department') {
        const did = document.getElementById('bcDeptSel').value;
        if (!did) { pv.textContent = ''; return; }
        url += '&department_id=' + encodeURIComponent(did);
      }
      const r = await jget(url);
      pv.textContent = `Получателей: ${r.total} (с Telegram: ${r.telegram_linked})`;
    } catch (e) { pv.textContent = ''; }
  }

  /* ---------------- Форма рассылки ---------------- */
  function initBroadcastForm() {
    const frm = document.getElementById('bcFrm');
    if (!frm || frm.dataset.nabound) return;
    frm.dataset.nabound = '1';
    // значение по умолчанию: сейчас + 10 минут
    const dt = frm.querySelector('input[name=send_at]');
    if (dt && !dt.value) {
      const d = new Date(Date.now() + 10 * 60000);
      const pad = (x) => String(x).padStart(2, '0');
      dt.value = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }
    frm.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const mode = frm.mode.value;
      const title = frm.title.value.trim();
      const bodyTxt = frm.body.value.trim();
      if (!title || title.length < 2) { toast('Укажите наименование уведомления', false); return; }
      if (!bodyTxt) { toast('Укажите содержание', false); return; }
      if (!frm.send_at.value) { toast('Укажите дату и время отправки', false); return; }
      const aud = { mode, user_ids: [], department_id: null, include_head: !!frm.include_head.checked };
      if (mode === 'users') {
        aud.user_ids = [...document.getElementById('bcUserSel').selectedOptions].map(o => Number(o.value));
        if (!aud.user_ids.length) { toast('Выберите пользователей', false); return; }
      }
      if (mode === 'department') {
        aud.department_id = Number(document.getElementById('bcDeptSel').value) || null;
        if (!aud.department_id) { toast('Выберите подразделение', false); return; }
      }
      const payload = {
        title, body: bodyTxt,
        send_at: new Date(frm.send_at.value).toISOString(),
        audience: aud,
      };
      try {
        const r = await jsend('/api/core/admin/notify/broadcast', 'POST', payload);
        toast(`📨 Рассылка №${r.id}: ${r.recipients} получателей${r.immediate ? ' — отправляется сразу' : ' — запланирована'}`, true);
        frm.title.value = ''; frm.body.value = '';
        naLoadHistory(); loadOutbox(); loadStatus();
      } catch (e) { toast('⛔ ' + e.message, false); }
    });
    ['change'].forEach(evt => frm.addEventListener(evt, (e) => {
      if (['bcUserSel', 'bcDeptSel', 'include_head', 'mode'].includes(e.target.id) ||
          ['include_head', 'mode'].includes(e.target.name)) naUpdatePreview();
    }));
    document.getElementById('bcUserSel')?.addEventListener('change', naUpdatePreview);
  }

  async function naLoadHistory() {
    const el = document.getElementById('bcList');
    if (!el) return;
    try {
      const d = await jget('/api/core/admin/notify/broadcast');
      NA.bcs = d.items || [];
      if (!NA.bcs.length) { el.innerHTML = '<p class="muted">Рассылок ещё не было.</p>'; return; }
      const ST = { scheduled: '⏳ запланирована', sent: '✅ отправлена', canceled: '🚫 отменена' };
      const MD = { all: 'Всем пользователям', users: 'Конкретным пользователям',
                   department: 'Подразделению', heads: 'Всем руководителям' };
      el.innerHTML = '<table class="tbl"><thead><tr><th>#</th><th>Наименование</th><th>Отправка</th><th>Аудитория</th><th>Получ./Доставл.</th><th>Статус</th><th style="width:110px"></th></tr></thead><tbody>' +
        NA.bcs.map(b => {
          const aud = b.audience || {};
          let audTxt = MD[aud.mode] || b.status;
          if (aud.mode === 'department') {
            const dep = NA.depts.find(x => String(x.id) === String(aud.department_id));
            audTxt = 'Подразделение: ' + (dep ? dep.name : '#' + aud.department_id) +
              (aud.include_head === false ? ' (без руководителя)' : '');
          }
          if (aud.mode === 'users') audTxt = `Пользователи (${(aud.user_ids || []).length})`;
          return `<tr>
            <td>${b.id}</td>
            <td><b>${esc(b.title)}</b><div class="sub">${esc((b.body || '').slice(0, 70))}${(b.body || '').length > 70 ? '…' : ''}</div></td>
            <td>${fmtDT(b.send_at)}</td><td class="sub">${esc(audTxt)}</td>
            <td>${b.delivered ?? 0}/${b.recipients ?? 0}</td>
            <td>${ST[b.status] || esc(b.status)}${b.sent_at ? `<div class="sub">${fmtDT(b.sent_at)}</div>` : ''}</td>
            <td>${b.status === 'scheduled' ? `<button class="btn sm ghost" onclick="naBcCancel(${b.id})">🚫 Отменить</button>` : ''}</td>
          </tr>`;
        }).join('') + '</tbody></table>';
    } catch (e) { el.innerHTML = `<span style="color:#c62828">${esc(e.message)}</span>`; }
  }
  window.naBcCancel = async function (id) {
    if (!confirm('Отменить рассылку?')) return;
    try { await jsend('/api/core/admin/notify/broadcast/' + id + '/cancel', 'POST', {}); toast('🚫 Отменено', true); naLoadHistory(); }
    catch (e) { toast('⛔ ' + e.message, false); }
  };

  /* ---------------- Очередь доставки ---------------- */
  window.naLoadOutbox = async function () {
    const el = document.getElementById('ntgOutbox');
    if (!el) return;
    try {
      const d = await jget('/api/core/admin/notify/outbox?limit=50');
      NA.outbox = d.items || [];
      if (!NA.outbox.length) { el.innerHTML = '<p class="muted">Очередь пуста — событий telegram.* нет.</p>'; return; }
      const TOPIC_RU = { 'telegram.notify': 'Событие-сценарий', 'telegram.due': 'Приближение срока',
                         'telegram.overdue': 'Просрочка', 'telegram.manual': 'Рассылка',
                         'telegram.test': 'Тестовое сообщение' };
      const ST = { pending: '⏳ в очереди', done: '✅ доставлено', error: '⛔ ошибка' };
      el.innerHTML = '<table class="tbl"><thead><tr><th>#</th><th>Когда</th><th>Тип</th><th>Содержание</th><th>Поп.</th><th>Статус</th></tr></thead><tbody>' +
        NA.outbox.map(o => {
          const p = o.payload || {};
          const txt = p.title ? esc(p.title) : (p.user_id ? ('пользователь #' + p.user_id) : '');
          const extra = [p.action ? esc(p.action) : '', p.kind ? esc(kindLabel(p.kind)) : '',
                         p.link ? `<a href="${esc(p.link)}" target="_blank" rel="noopener">ссылка</a>` : '']
                        .filter(Boolean).join(' · ');
          return `<tr>
            <td>${o.id}</td><td class="sub">${fmtDT(o.at)}</td>
            <td>${esc(TOPIC_RU[o.topic] || o.topic)}</td>
            <td class="sub">${txt}${extra ? `<div>${extra}</div>` : ''}${o.error ? `<div style="color:#c62828">${esc(o.error)}</div>` : ''}</td>
            <td>${o.attempts ?? 0}</td>
            <td>${ST[o.status] || esc(o.status)}</td>
          </tr>`;
        }).join('') + '</tbody></table>';
    } catch (e) { el.innerHTML = `<span style="color:#c62828">${esc(e.message)}</span>`; }
  };

  /* ---------------- Инициализация вкладки ---------------- */
  window.initNotifyPane = async function () {
    loadStatus();
    loadScenarios();
    loadTemplates();
    window.naLoadOutbox();
    try {
      await ensureAudienceData();
      initBroadcastForm();
      window.bcAudChange();
      naLoadHistory();
    } catch (e) { /* аудитория подгрузится при открытии формы */ }
  };
})();
