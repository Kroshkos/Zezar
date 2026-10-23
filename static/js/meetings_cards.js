/* Раздел «Карточки» модуля «Совещания»: печатные карточки всех сущностей.
   Дизайн — как pcardHTML()/renderPrintSheet() из прототипа ИС_Цезарь.html.
   Два типа: entity (чек-лист этапов проекта/ПО/документа) и task (место под
   рукописные заметки). Внизу — срок, ответственный, участники и QR на профиль.
   Права: подразделения видят только свои карточки; админы/координаторы — все. */
"use strict";

const CD = { cards: [], departments: [], is_global: false, loaded: false, loading: false };
const CSEL = new Set();                       // отмеченные uid (печатаются первыми)
const PRINT_PER_PAGE = { a4: 6, a3: 12 };     // А4 3×2, А3 4×3 (альбомные)
const PRI_HEX = { "b-red": "#dc2626", "b-amber": "#d97706", "b-blue": "#2563eb",
                  "b-gray": "#64748b", "b-green": "#16a34a", "b-purple": "#7c3aed" };

/* ---------- переключение представлений (список / карточки) ---------- */
function mView(v) {
  document.querySelectorAll('#mViews button').forEach(b => b.classList.toggle('on', b.dataset.v === v));
  document.getElementById('v-list').hidden = v !== 'list';
  document.getElementById('v-cards').hidden = v !== 'cards';
  if (v === 'cards' && !CD.loaded) loadCards();
}

async function loadCards() {
  if (CD.loading) return;
  CD.loading = true;
  try {
    const d = await api('/api/meetings/cards/api-list');
    CD.cards = d.cards || []; CD.departments = d.departments || []; CD.is_global = !!d.is_global;
    CD.loaded = true;
    // Фильтр подразделений — только те, что реально есть у карточек в выдаче
    // (для MEMBER сервер уже вернул лишь свои подразделения).
    const present = new Set(CD.cards.map(c => c.department_id));
    const sel = document.getElementById('c-dept');
    sel.innerHTML = '<option value="">Все подразделения</option>'
      + CD.departments.filter(d => present.has(d.id))
          .map(d => `<option value="${d.id}">${esc(d.name)}</option>`).join('');
    renderCardSheets();
  } catch (e) { /* toast уже показан в api() */ }
  finally { CD.loading = false; }
}

/* ---------- вспомогательные ---------- */
const cfmt = d => d ? String(d).split('-').reverse().join('.') : '—';
const ccolor = key => PRI_HEX[key] || '#01696F';
const cdeptName = id => (CD.departments.find(d => d.id === id) || {}).name || '';
function chunk(arr, n) { const out = []; for (let i = 0; i < arr.length; i += n) out.push(arr.slice(i, i + n)); return out; }

// QR в dataURL — как qrDataURL() из прототипа
function qrDataURL(text, size = 4) {
  const qr = qrcode(0, 'M');
  qr.addData(text); qr.make();
  const count = qr.getModuleCount();
  const canvas = document.createElement('canvas');
  canvas.width = count * size; canvas.height = count * size;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = '#000';
  for (let r = 0; r < count; r++) for (let cc = 0; cc < count; cc++)
    if (qr.isDark(r, cc)) ctx.fillRect(cc * size, r * size, size, size);
  return canvas.toDataURL('image/png');
}

/* ---------- рендер одной карточки (дизайн оригинала) ---------- */
function cardHTML(c) {
  const color = ccolor(c.priority_color);
  let qrBlock;
  try {
    qrBlock = `<img class="pqr" src="${qrDataURL(location.origin + c.profile_url, 4)}" width="72" height="72" alt="QR">`;
  } catch (e) {
    qrBlock = `<div class="pqr-missing" style="font-size:10px;color:#999;text-align:center">QR недоступен</div>`;
  }
  const parts = [];
  if (c.responsible) parts.push(`<b>Отв.:</b> ${esc(c.responsible)}`);
  const others = (c.participants || []).filter(p => p !== c.responsible);
  if (others.length) parts.push(`<b>Участники:</b> ${esc(others.join(', '))}`);
  const shortId = (c.uid || '').slice(0, 8);

  // содержание: чек-лист этапов (entity) либо зона заметок (task)
  let body;
  if (c.type === 'entity') {
    const rows = (c.stages || []).map(s =>
      `<div class="pk"><span class="bx"></span><span class="tx">`
      + `<span class="nm">${esc(s.name) || '—'}</span>`
      + `<span class="sm"> · ${cfmt(s.deadline)} · ${esc(s.responsible) || '—'}</span>`
      + `</span></div>`).join('');
    body = `<div class="pchecklist">${rows || '<div class="sm">Нет этапов</div>'}</div>`;
  } else {
    body = `<div class="pnotes" title="Место для рукописных заметок"></div>`;
  }

  return `<label class="pcardwrap" data-uid="${esc(c.uid)}" style="display:flex;overflow:hidden">
    <input type="checkbox" class="csel" style="position:absolute;top:6px;left:6px;z-index:2;margin:0"
           ${CSEL.has(c.uid) ? 'checked' : ''} onchange="tglCardSel('${esc(c.uid)}',this.checked)">
    <div class="pcard" style="flex:1;border-left:8px solid ${color}">
      <div>
        <div class="ptop">
          <span class="ptag" style="background:${color}">${esc(c.priority_label) || 'Без приоритета'}</span>
          <span class="pid">${esc(shortId)}</span>
        </div>
        <div class="pdir" style="color:${color}">${esc(c.tag)}${c.cross_dept ? ' · межпространственная' : ''}</div>
        <div class="ptitle">${esc(c.title)}</div>
        ${body}
      </div>
      <div>
        <div class="pmeta"><b>Срок:</b> ${cfmt(c.due_date)}</div>
        <div class="pmeta">${parts.join(' · ') || '<b>Отв.:</b> —'}</div>
        <div class="pbottom"><span class="pid">${esc(cdeptName(c.department_id))}</span>${qrBlock}</div>
      </div>
    </div>
  </label>`;
}

/* ---------- выбор карточек для печати ---------- */
function tglCardSel(uid, on) {
  if (on) CSEL.add(uid); else CSEL.delete(uid);
  updatePrintSrcInfo(filteredCards());
}
function selAllCards(on) {
  filteredCards().forEach(c => on ? CSEL.add(c.uid) : CSEL.delete(c.uid));
  renderCardSheets();
}

function filteredCards() {
  const type = document.getElementById('c-type').value;
  const dept = document.getElementById('c-dept').value;
  const q = (document.getElementById('cq').value || '').toLowerCase();
  return CD.cards.filter(c => {
    if (type && c.type !== type) return false;
    if (dept && String(c.department_id) !== dept) return false;
    if (q && !(c.title + ' ' + c.tag).toLowerCase().includes(q)) return false;
    return true;
  });
}

function updatePrintSrcInfo(data) {
  const info = document.getElementById('printSrcInfo');
  if (!info) return;
  info.textContent = CSEL.size
    ? `Печатаются отмеченные: ${CD.cards.filter(c => CSEL.has(c.uid)).length} шт.`
    : `Печатаются по фильтру (ничего не отмечено): ${data.length} шт.`;
}

/* ---------- разбивка на печатные листы ---------- */
function renderCardSheets() {
  if (!CD.loaded) return;
  const fmt = document.getElementById('c-format').value;
  const perPage = PRINT_PER_PAGE[fmt] || 6;
  let data;
  if (CSEL.size > 0) {
    data = CD.cards.filter(c => CSEL.has(c.uid));       // отмеченные — независимо от фильтра
  } else {
    data = filteredCards();
  }
  updatePrintSrcInfo(data);
  document.getElementById('cempty').hidden = data.length > 0;
  const pages = chunk(data, perPage);
  document.getElementById('cardPages').innerHTML = pages.map(page =>
    `<div class="printpage fmt-${fmt}">`
    + page.map(cardHTML).join('')
    + Array(perPage - page.length).fill('<div></div>').join('')
    + `</div>`).join('');
}

document.addEventListener('DOMContentLoaded', () => {
  // если страница сразу открыта на вкладке карточек — грузим данные
  if (!document.getElementById('v-cards').hidden) loadCards();
});
