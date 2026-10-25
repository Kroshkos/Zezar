/* Раздел «Карточки» модуля «Совещания»: печатные карточки всех сущностей.
   Дизайн — как pcardHTML()/renderPrintSheet() из прототипа ИС_Цезарь.html.
   Два типа: entity (чек-лист этапов проекта/ПО/документа) и task (место под
   рукописные заметки). Внизу — срок, ответственный, участники и QR на профиль.
   Права: подразделения видят только свои карточки; админы/координаторы — все. */
"use strict";

const CD = { cards: [], departments: [], is_global: false, loaded: false, loading: false };
const CSEL = new Set();                       // отмеченные uid (печатаются первыми)
// А4 альбомная: 6 карточек (3×2); А3 альбомная: 8 карточек (4×2).
// Для А3 при 12 ячейках (4×3) карточки не помещаются на лист и наслаиваются
// друг на друга — поэтому сетка уменьшена до двух рядов.
const PRINT_PER_PAGE = { a4: 6, a3: 8 };      // см. также grid-template в app.css
// Справочник badge-классов системы (static/css/app.css) -> печатный HEX
// (значения подобраны по оригиналу ИС_Цезарь.html: насыщенный цвет плашки/полосы).
const PRI_HEX = { "b-red": "#dc2626", "b-amber": "#d97706", "b-blue": "#2563eb",
                  "b-gray": "#64748b", "b-green": "#16a34a", "b-purple": "#7c3aed",
                  "b-cyan": "#0e7490", "b-gold": "#92700c" };

/* ---------- переключение представлений (список / карточки) ---------- */
function mView(v) {
  document.querySelectorAll('#mViews button').forEach(b => b.classList.toggle('on', b.dataset.v === v));
  document.getElementById('v-list').hidden = v !== 'list';
  document.getElementById('v-cards').hidden = v !== 'cards';
  // Кнопка «Новое совещание» относится только к списку
  const act = document.querySelector('.topbar [onclick="meetFormModal()"]');
  if (act) act.style.display = v === 'cards' ? 'none' : '';
  if (v === 'cards') {
    if (!CD.loaded) loadCards(); else renderCardSheets();
  } else {
    try { localStorage.setItem('meetings_view', v); } catch (e) {}
  }
}

async function loadCards() {
  if (CD.loading) return;
  CD.loading = true;
  const btns = document.querySelectorAll('#mViews button');
  btns.forEach(b => b.disabled = true);
  const info = document.getElementById('printSrcInfo');
  if (info) info.textContent = 'Загрузка карточек…';
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
  } catch (e) {
    if (info) info.textContent = '';
    if (CD.loaded) return;                 // данные уже есть — оставляем их
    // Сервер недоступен: показываем подсказку вместо пустого экрана
    document.getElementById('cardPages').innerHTML =
      '<div class="empty">⚠️<p>Не удалось загрузить карточки с сервера.</p></div>';
  } finally {
    btns.forEach(b => b.disabled = false);
    CD.loading = false;
  }
}

/* ---------- вспомогательные ---------- */
const cfmt = d => d ? String(d).split('-').reverse().join('.') : '—';
const ccolor = key => PRI_HEX[key] || '#01696F';   // ключ badge-класса из справочника
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
// Цвет левой полосы = цвет подразделения (задаётся администратором);
// при отсутствии настроенного цвета — детерминированный подбор из палитры.
const DEPT_PALETTE = ['#01696F', '#7c3aed', '#2563eb', '#d97706', '#dc2626',
                      '#0e7490', '#16a34a', '#92700c', '#be185d', '#4338ca'];
function deptColor(id) {
  const d = CD.departments.find(x => x.id === id);
  if (d && /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(d.color || '')) return d.color;
  return DEPT_PALETTE[Math.abs(Number(id) || 0) % DEPT_PALETTE.length];
}
// Дата печати (формируется один раз за сессию просмотра/печати)
const PRINT_DATE = new Date().toLocaleDateString('ru-RU');

function cardHTML(c) {
  const dcolor = deptColor(c.department_id);          // полоса — по подразделению
  const pcolor = ccolor(c.priority_color);            // плашка — приоритет (как раньше)
  const scolor = ccolor((c.current_stage || {}).color); // плашка — текущий этап маршрута
  // QR ведёт на профиль сущности: полный URL текущего хоста (чтобы сканер
  // с телефона попал в ту же инсталляцию, что открыта в браузере).
  let qrBlock;
  try {
    const url = location.protocol + '//' + location.host + c.profile_url;
    qrBlock = `<img class="pqr" src="${qrDataURL(url, 4)}" width="60" height="60" alt="QR">`;
  } catch (e) {
    qrBlock = `<div class="pqr-missing" style="font-size:10px;color:#999;text-align:center">QR недоступен</div>`;
  }
  const parts = [];
  if (c.responsible) parts.push(`<b>Отв.:</b> ${esc(c.responsible)}`);
  const others = (c.participants || []).filter(p => p !== c.responsible);
  if (others.length) parts.push(`<b>Участники:</b> ${esc(others.join(', '))}`);

  // Содержание карточки: чек-лист этапов из содержания убран (текущий этап
  // показывается отдельной плашкой над заголовком). Сущность (entity) —
  // компактная информационная сводка; автономная задача (task) — как раньше,
  // зона для рукописных заметок.
  let body;
  if (c.type === 'entity') {
    const info = [];
    if (c.status_label) info.push(`<b>Статус:</b> ${esc(c.status_label)}`);
    info.push(`<b>Этапов в маршруте:</b> ${(c.stages || []).length
      || ((c.current_stage || {}).total || 0)}`);
    body = `<div class="pinfo">${info.map(i => `<div class="pmeta">${i}</div>`).join('')}</div>`;
  } else {
    body = `<div class="pnotes" title="Место для рукописных заметок"></div>`;
  }

  const dname = cdeptName(c.department_id);
  const cs = c.current_stage;
  // Обёртка-ячейка сетки: position:relative для чекбокса выбора. Все карточки
  // теперь стандартного размера (чек-лист этапов из содержания убран), поэтому
  // ряды на экране и на бумаге — фиксированной высоты; удлинённые карточки
  // (если появятся) выносятся на отдельный лист (.tall-sheet).
  return `<div class="pcardwrap" data-uid="${esc(c.uid)}">
    <input type="checkbox" class="csel no-print" style="position:absolute;top:6px;left:6px;z-index:2;margin:0"
           ${CSEL.has(c.uid) ? 'checked' : ''} onchange="tglCardSel('${esc(c.uid)}',this.checked)"
           title="Отметить для печати">
    <div class="pcard" style="height:100%;min-height:0;border-left:8px solid ${dcolor}">
      <div style="min-height:0">
        <div class="ptop">
          <span class="ptag" style="background:${pcolor}">${esc(c.priority_label) || 'Без приоритета'}</span>
          <span class="pid pdate">${PRINT_DATE}</span>
        </div>
        ${cs ? `<div class="pstage-tag"><span class="ptag" style="background:${scolor}">🧭 ${esc(cs.name)}</span>`
             + `<span class="pstage-pos">${cs.index + 1}/${cs.total} · ${esc(cs.label)}</span></div>` : ''}
        ${dname ? `<div class="pdept" style="color:${dcolor}">${esc(dname)}</div>` : ''}
        <div class="pdir" style="color:${dcolor}">${esc(c.tag)}${c.cross_dept ? ' · межпространственная' : ''}</div>
        <div class="ptitle">${esc(c.title)}</div>
        ${body}
      </div>
      <div style="min-height:0">
        <div class="pmeta"><b>Срок:</b> ${cfmt(c.due_date)}</div>
        <div class="pmeta">${parts.join(' · ') || '<b>Отв.:</b> —'}</div>
        <div class="pbottom">${qrBlock}</div>
      </div>
    </div>
  </div>`;
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

/* ---------- фильтры: тип, подразделение, изменения/печать, поиск ---------- */
// Значения фильтра «Изменения»: '' — все; '1'/'7' — созданные/изменённые за
// день/неделю (по created_at/updated_at сущности); 'new' — ещё ни разу не
// печатались (по журналу card_print_log).
function recentFilter() {
  const el = document.getElementById('c-recent');
  return el ? el.value : '';
}

function matchesRecent(c) {
  const mode = recentFilter();
  if (!mode) return true;
  if (mode === 'new') return !c.printed_at;
  const days = Number(mode) || 0;
  const since = Date.now() - days * 86400000;
  const ts = Math.max(Date.parse(c.created_at || '') || 0,
                      Date.parse(c.updated_at || '') || 0);
  return ts >= since;
}

function filteredCards() {
  const type = document.getElementById('c-type').value;
  const dept = document.getElementById('c-dept').value;
  const q = (document.getElementById('cq').value || '').toLowerCase();
  return CD.cards.filter(c => {
    if (type && c.type !== type) return false;
    if (dept && String(c.department_id) !== dept) return false;
    if (q && !(c.title + ' ' + c.tag).toLowerCase().includes(q)) return false;
    if (!matchesRecent(c)) return false;
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
// Динамический @page: размер листа должен соответствовать выбранному формату.
// Если @page захардкодить (например, A4), браузер масштабирует листы А3 до A4
// — карточки начинают наслаиваться друг на друга.
function setCardPageSize(fmt) {
  let el = document.getElementById('cardPageStyle');
  if (!el) {
    el = document.createElement('style');
    el.id = 'cardPageStyle';
    document.head.appendChild(el);
  }
  const size = fmt === 'a3' ? 'A3 landscape' : 'A4 landscape';
  el.textContent = '@media print{@page{size:' + size + ';margin:5mm}}';
}

// Разделение карточек на «стандартные» и «удлинённые»: чек-листы из содержания
// убраны, поэтому все карточки теперь одного размера (одна плашка этапа вместо
// списка этапов). Функция оставлена как точка расширения — всегда относит все
// карточки к стандартным.
function splitTallCards(data) {
  return { standard: data, tall: [] };
}

function buildSheetsHtml(data, fmt) {
  const perPage = PRINT_PER_PAGE[fmt] || 6;
  const { standard, tall } = splitTallCards(data);
  let html = '';
  // Общие листы: строго фиксированная сетка (3×2 / 4×2) — одинаковые ячейки.
  html += chunk(standard, perPage).map(page =>
    `<div class="printpage fmt-${fmt}">`
    + page.map(cardHTML).join('')
    + Array(perPage - page.length).fill('<div></div>').join('')
    + `</div>`).join('');
  // Отдельный лист для нестандартных (удлинённых) карточек: адаптивная высота
  // ячеек, чтобы длинный контент не тянул за собой весь ряд и не обрезался.
  if (tall.length) {
    html += chunk(tall, perPage).map((page, i) =>
      `<div class="printpage tall-sheet fmt-${fmt}">`
      + `<div class="tall-sheet-title">Удлинённые карточки — лист ${i + 1}</div>`
      + page.map(cardHTML).join('')
      + `</div>`).join('');
  }
  return html;
}

/* Подгонка карточек под лист при печати: если содержимое какой-либо карточки
   не влезает в ячейку фиксированной высоты — уменьшаем масштаб её содержимого
   (transform не ломает сетку). Чек-листы из содержания убраны, поэтому обычно
   подгонка не требуется; код оставлен как страховка для длинных заголовков. */
function fitSheets(root) {
  root.querySelectorAll('.pcardwrap').forEach(w => {
    const inner = w.querySelector('.pcard');
    if (!inner) return;
    inner.style.transform = '';
    inner.style.transformOrigin = 'top left';
    const cellH = w.clientHeight;
    const contentH = inner.scrollHeight;
    if (cellH > 40 && contentH > cellH + 2) {
      const k = Math.max(0.72, (cellH - 2) / contentH);
      inner.style.transform = 'scale(' + k.toFixed(3) + ')';
      inner.style.width = (100 / k) + '%';
    } else {
      inner.style.width = '';
    }
  });
  return false;
}

function renderCardSheets() {
  if (!CD.loaded) return;
  const fmt = document.getElementById('c-format').value;
  try { localStorage.setItem('cards_fmt', fmt); } catch (e) {}
  setCardPageSize(fmt);
  let data;
  if (CSEL.size > 0) {
    data = CD.cards.filter(c => CSEL.has(c.uid));       // отмеченные — независимо от фильтра
  } else {
    data = filteredCards();
  }
  updatePrintSrcInfo(data);
  document.getElementById('cempty').hidden = data.length > 0;
  const host = document.getElementById('cardPages');
  host.innerHTML = buildSheetsHtml(data, fmt);
}

/* ---------- печать ----------
   ВАЖНО: window.print() блокирует JS-поток до закрытия диалога печати,
   поэтому все изменения DOM (оверлей, класс printing-cards) выполняются ДО
   вызова; снятие — в событии afterprint + страховочный таймер.
   Листы копируются в изолированный оверлей #printCardsOverlay и печатаются
   через @media print (visibility-подход, как в прототипе). Это гарантирует,
   что на бумаге будут карточки, даже если открыто представление «Список
   совещаний» или контейнер скрыт/обрезан экраном. */
async function printCards() {
  if (!CD.loaded) await loadCards();
  const fmt = document.getElementById('c-format').value;
  setCardPageSize(fmt);                 // @page под выбранный формат (А3/А4)
  const data = CSEL.size > 0 ? CD.cards.filter(c => CSEL.has(c.uid)) : filteredCards();
  if (!data.length) { toast('Нет карточек для печати', 'warn'); return; }
  // Печатаемые uid: с них снимаем чекбоксы в оверлее и регистрируем печать
  const printUids = new Set(data.map(c => c.uid));
  const ov = document.getElementById('printCardsOverlay');
  ov.innerHTML = buildSheetsHtml(data, fmt);
  // Чекбоксы выбора — только для экрана (на бумаге их быть не должно)
  ov.querySelectorAll('.csel').forEach(i => i.remove());
  ov.querySelectorAll('.pcardwrap').forEach(w => {
    if (printUids.has(w.dataset.uid)) w.classList.add('is-printed');
  });
  // Дождаться загрузки dataURL-изображений QR — иначе печать может выйти пустой
  const imgs = Array.from(ov.querySelectorAll('img'));
  await Promise.all(imgs.map(i => i.complete ? Promise.resolve()
    : new Promise(res => { i.onload = i.onerror = res; })));
  document.body.classList.add('printing-cards');
  ov.classList.add('on');
  fitSheets(ov);                          // подгонка многоэтапных карточек под лист
  // Страховка на случай, если браузер не поддерживает событие afterprint
  let cleaned = false;
  const cleanup = () => {
    if (cleaned) return; cleaned = true;
    document.body.classList.remove('printing-cards');
    ov.classList.remove('on');
    ov.innerHTML = '';
    window.removeEventListener('afterprint', cleanup);
  };
  window.addEventListener('afterprint', cleanup, { once: true });
  setTimeout(cleanup, 60000);
  // Журнал печати запускаем ДО print(): код после блокирующего window.print()
  // может исполниться слишком поздно, а при unsupported afterprint — никогда.
  try {
    await api('/api/meetings/cards/print-log', 'POST', { uids: [...printUids] });
    const nowIso = new Date().toISOString();
    CD.cards.forEach(c => { if (printUids.has(c.uid)) c.printed_at = nowIso; });
  } catch (e) {}                                  // журнал не критичен для печати
  window.print();
  // Если фильтр «изменения/печать» активен — перечитать выборку (для 'new')
  if (recentFilter()) renderCardSheets();
}

document.addEventListener('DOMContentLoaded', () => {
  // Восстановление выбора пользователя между визитами
  try {
    const f = localStorage.getItem('cards_fmt');
    if (f && document.querySelector(`#c-format option[value="${f}"]`))
      document.getElementById('c-format').value = f;
  } catch (e) {}
  // Прямая ссылка /meetings/?view=cards (например, из QR/закладки) или сохранённая вкладка
  let start = new URLSearchParams(location.search).get('view')
              || localStorage.getItem('meetings_view') || 'list';
  if (start !== 'cards') start = 'list';
  if (start === 'cards') mView('cards');
});
