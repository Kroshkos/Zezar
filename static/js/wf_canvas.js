/* Интерактивный холст маршрута (workflow) в стиле Obsidian Canvas:
   узлы-этапы свободно расставляются перетаскиванием, связи рисуются SVG-стрелками,
   из узла можно «вырастить» следующий этап. Координаты (x,y) и связи (links_out)
   хранятся прямо в payload.stages (JSON) — отдельной миграции не нужно.

   Использование:
     wfCanvasEditor(containerEl, stages) -> { getStages() }  — редактор на холсте
     window.wfCanvasHTML(p)             — рендер-просмотр (заменяет статическую
                                          ленту из projects.js/profile.js; если у
                                          этапов есть x/y — свободная раскладка,
                                          иначе — автоукладка по цепочке)
   Совместимость со старыми данными: этапы без координат получают их автоматически
   (автоукладка слева направо), связи восстанавливаются по порядку/branches. */
"use strict";

// esc может не быть, если wf_canvas.js загружен раньше core.js — своя копия
const _wfEsc = (typeof esc === "function") ? esc
  : s => String(s == null ? "" : s).replace(/[&<>"']/g,
      c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

const WF_STEP = {pending: ["Ожидает", "#94a3b8"], active: ["В работе", "#2563eb"],
                 done: ["Готово", "#16a34a"], rejected: ["Отклонён", "#dc2626"]};
const WF_NODE_W = 190, WF_NODE_H = 88, WF_GRID = 24;

let _wfSeq = 0;
const wfUid = () => "n" + (++_wfSeq) + "-" + Math.random().toString(36).slice(2, 7);

function _wfState(s) {
  if (s.state && WF_STEP[s.state]) return s.state;
  if (s.done) return "done";
  return "pending";
}

// ---------- нормализация / автоукладка ----------
function wfNormalize(stages) {
  const st = (stages || []).filter(s => s && typeof s === "object" && String(s.name || "").trim());
  st.forEach(s => { if (!s.id) s.id = wfUid(); });
  const hasPos = st.some(s => Number.isFinite(s.x) && Number.isFinite(s.y));
  if (!hasPos && st.length) {                     // автоукладка: змейкой по сетке
    const perRow = Math.max(1, Math.floor((window.innerWidth - 220) / (WF_NODE_W + 60)));
    st.forEach((s, i) => {
      s.x = 30 + (i % perRow) * (WF_NODE_W + 60);
      s.y = 30 + Math.floor(i / perRow) * (WF_NODE_H + 66);
    });
  } else {
    st.forEach(s => { s.x = Number.isFinite(s.x) ? s.x : 30; s.y = Number.isFinite(s.y) ? s.y : 30; });
  }
  return st;
}

// список рёбер: [{from,to,kind}] — kind: main|branch
function wfEdges(st) {
  const byId = {}; st.forEach(s => byId[s.id] = s);
  const edges = [];
  const seen = new Set();
  const push = (a, b, kind) => {
    if (!a || !b || !byId[a] || !byId[b]) return;
    const k = a + ">" + b; if (seen.has(k)) return; seen.add(k);
    edges.push({from: a, to: b, kind});
  };
  st.forEach(s => {
    (s.links_out || []).forEach(t => push(s.id, t, "main"));
    (s.branches || []).forEach(br => {
      const steps = (br.steps || []).filter(x => x && x.id && byId[x.id]);
      for (let i = 0; i < steps.length; i++)
        if (i === 0) push(s.id, steps[i].id, "branch"); else push(steps[i - 1].id, steps[i].id, "branch");
    });
  });
  // порядок как резервная цепочка, если явных связей нет вообще
  if (!edges.length) for (let i = 0; i < st.length - 1; i++) push(st[i].id, st[i + 1].id, "main");
  return edges;
}

// ---------- геометрия стрелок ----------
function _wfAnchor(n, side) {
  const w = n._w || WF_NODE_W, h = n._h || WF_NODE_H;
  if (side === "r") return [n.x + w, n.y + h / 2];
  if (side === "l") return [n.x, n.y + h / 2];
  if (side === "t") return [n.x + w / 2, n.y];
  return [n.x + w / 2, n.y + h];                       // bottom
}
function _wfEdgePath(a, b, kind) {
  const goRight = a.x + (a._w || WF_NODE_W) <= b.x;
  const p1 = goRight ? _wfAnchor(a, "r") : _wfAnchor(a, "b");
  const p2 = goRight ? _wfAnchor(b, "l") : _wfAnchor(b, "t");
  const dx = Math.max(36, Math.abs(p2[0] - p1[0]) * 0.45);
  return '<path d="M' + p1[0] + ',' + p1[1] + ' C' + (p1[0] + dx) + ',' + p1[1] + ' ' + (p2[0] - dx) + ',' + p2[1] + ' ' + p2[0] + ',' + p2[1] + '" '
       + 'fill="none" stroke="#94a3b8" stroke-width="1.6" marker-end="url(#wfArr)"'
       + (kind === "branch" ? ' stroke-dasharray="5 4"' : '') + '/>';
}

// ---------- общий контейнер-холст (просмотр и редактор) ----------
function _wfShell(st, opts) {
  const maxX = Math.max.apply(null, st.map(n => (n.x || 0) + WF_NODE_W).concat([400])) + 70;
  const maxY = Math.max.apply(null, st.map(n => (n.y || 0) + WF_NODE_H).concat([160])) + 60;
  const byId = {}; st.forEach(n => byId[n.id] = n);
  let svg = '<svg width="' + maxX + '" height="' + maxY + '" style="position:absolute;left:0;top:0;pointer-events:none">'
    + '<defs><marker id="wfArr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
    + '<path d="M0,0 L10,5 L0,10 z" fill="#94a3b8"/></marker></defs>';
  wfEdges(st).forEach(e => {
    const a = byId[e.from], b = byId[e.to];
    if (a && b) svg += _wfEdgePath(a, b, e.kind);
  });
  svg += '</svg>';
  let html = '<div class="wf-canvas" data-maxx="' + maxX + '" style="position:relative;width:' + maxX + 'px;height:' + maxY + 'px">'
    + '<div class="wf-plane" style="position:absolute;inset:0;background-image:radial-gradient(#d7deea 1px,transparent 1px);background-size:' + WF_GRID + 'px ' + WF_GRID + 'px;border-radius:14px"></div>'
    + svg;
  st.forEach(n => {
    const w = WF_STEP[_wfState(n)] || WF_STEP.pending;
    const dl = n.deadline ? '<div class="wf-dl">⏰ ' + String(n.deadline).split("-").reverse().join(".") + '</div>' : "";
    const tk = n.taskId ? '<span class="wf-tk" title="Автозадача создана">🤖</span>' : "";
    html += '<div class="wf-node st-' + _wfState(n) + '" data-id="' + _wfEsc(n.id) + '"'
      + ' style="left:' + n.x + 'px;top:' + n.y + 'px;width:' + WF_NODE_W + 'px">'
      + '<div class="wf-head"><span class="wf-dot" style="background:' + w[1] + '"></span>'
      + '<span class="wf-name">' + _wfEsc(n.name || "") + '</span>' + tk + '</div>'
      + '<div class="wf-state" style="color:' + w[1] + '">' + w[0] + '</div>' + dl
      + (opts.editable ? '<button type="button" class="wf-link" data-id="' + _wfEsc(n.id) + '" title="Связать этот этап с другим">⛓</button>'
        + '<button type="button" class="wf-add" data-id="' + _wfEsc(n.id) + '" title="Добавить этап после этого">＋→</button>'
        + '<button type="button" class="wf-del" data-id="' + _wfEsc(n.id) + '" title="Удалить этап">✕</button>' : "")
      + '</div>';
  });
  html += '</div>';
  return html;
}

// ================= ПРОСМОТР (замена статической ленты) =================
window.wfCanvasHTML = function (p) {
  const st = wfNormalize((p && p.stages) || []);
  if (!st.length) return '<div style="font-size:13px;color:var(--muted);padding:14px;border:1px dashed #cfd8e8;border-radius:12px;margin-top:12px">Маршрут пуст — добавьте этапы при редактировании.</div>';
  return '<label>🧭 Холст маршрута</label><div style="overflow:auto;max-height:420px;margin-top:6px">'
    + _wfShell(st, {editable: false}) + '</div>';
};

// ================= РЕДАКТОР =================
function wfCanvasEditor(container, stages) {
  let st = wfNormalize(JSON.parse(JSON.stringify(stages || [])));
  let dirty = false;

  const render = () => {
    container.innerHTML = '<div style="overflow:auto;max-height:460px;border:1px solid #dbe3f0;border-radius:14px;background:#fbfcfe">'
      + _wfShell(st, {editable: true}) + '</div>'
      + '<div style="display:flex;gap:8px;margin-top:8px;align-items:center;flex-wrap:wrap">'
      + '<button type="button" class="btn sm wf-newroot">＋ корневой этап</button>'
      + (st.length ? '<select class="wf-linksel" style="max-width:220px"></select>'
                   + '<button type="button" class="btn sm wf-dellink" title="Удалить выбранную связь">⛓✕</button>' : "")
      + '<span style="font-size:11.5px;color:var(--muted)">Перетащите узлы мышью · ＋→ создаёт следующий этап · ✕ удаляет · двойной клик — переименовать'
      + (st.length > 1 ? ' · «⛓» связывает выбранный этап с другим' : '') + '</span></div>';
    bind();
  };

  const findNode = (id) => st.find(n => n.id === id);

  function addAfter(srcId) {
    const src = findNode(srcId); if (!src) return;
    const name = prompt("Название нового этапа:", "Этап"); if (!name || !name.trim()) return;
    const node = {id: wfUid(), name: name.trim(), state: "pending", date: "", deadline: "",
                  comment: "", taskId: "", branches: [],
                  x: (src.x || 0) + WF_NODE_W + 60, y: src.y || 30};
    src.links_out = src.links_out || [];
    src.links_out.push(node.id);
    st.push(node);
    dirty = true; render();
  }

  function removeNode(id) {
    st = st.filter(s => s.id !== id);
    st.forEach(s => {
      s.links_out = (s.links_out || []).filter(t => t !== id);
      s.branches = (s.branches || []).map(br => ({...br, steps: (br.steps || []).filter(x => x.id !== id)}))
                    .filter(br => (br.steps || []).length);
    });
    if (linkFrom === id) linkFrom = null;
    dirty = true; render();
  }

  // ---------- ручное связывание этапов (⛓) ----------
  let linkFrom = null;
  function markLinkSrc(canvas) {
    canvas.querySelectorAll(".wf-node").forEach(el =>
      el.classList.toggle("wf-linksrc", el.dataset.id === linkFrom));
  }
  function linkTo(targetId) {
    const src = findNode(linkFrom), tgt = findNode(targetId);
    linkFrom = null;
    if (!src || !tgt || src === tgt) { render(); return; }
    src.links_out = src.links_out || [];
    if (src.links_out.includes(tgt.id)) { alert("Связь уже существует"); render(); return; }
    src.links_out.push(tgt.id);
    dirty = true; render();
  }

  function redrawEdges(canvas) {
    const oldSvg = canvas.querySelector("svg");
    if (oldSvg) oldSvg.remove();
    const frag = document.createElement("div");
    frag.innerHTML = _wfShell(st, {editable: true});
    const svg = frag.querySelector("svg");
    if (svg) canvas.insertAdjacentHTML("afterbegin", svg.outerHTML);
  }

  function bind() {
    const canvas = container.querySelector(".wf-canvas");
    if (!canvas) return;
    // измерить реальные высоты узлов для геометрии стрелок
    canvas.querySelectorAll(".wf-node").forEach(el => {
      const n = findNode(el.dataset.id);
      if (n) { n._w = el.offsetWidth; n._h = el.offsetHeight; }
    });
    redrawEdges(canvas);

    canvas.querySelectorAll(".wf-node").forEach(el => {
      let sx = 0, sy = 0, nx = 0, ny = 0, drag = false, moved = false;
      el.addEventListener("pointerdown", ev => {
        if (ev.target.closest("button")) return;
        drag = true; moved = false;
        try { el.setPointerCapture(ev.pointerId); } catch (e) {}
        const n = findNode(el.dataset.id);
        sx = ev.clientX; sy = ev.clientY; nx = n.x; ny = n.y;
        el.classList.add("wf-drag");
      });
      el.addEventListener("pointermove", ev => {
        if (!drag) return;
        const n = findNode(el.dataset.id);
        const dx = ev.clientX - sx, dy = ev.clientY - sy;
        if (Math.abs(dx) + Math.abs(dy) > 2) moved = true;
        n.x = Math.max(0, Math.round((nx + dx) / 6) * 6);
        n.y = Math.max(0, Math.round((ny + dy) / 6) * 6);
        el.style.left = n.x + "px"; el.style.top = n.y + "px";
        redrawEdges(canvas);
      });
      const stop = () => {
        if (!drag) return;
        drag = false; el.classList.remove("wf-drag");
        if (moved) { dirty = true; syncCanvasSize(canvas); }
      };
      el.addEventListener("pointerup", stop);
      el.addEventListener("pointercancel", stop);
      el.addEventListener("dblclick", () => {
        const n = findNode(el.dataset.id);
        const name = prompt("Название этапа:", n.name);
        if (name && name.trim()) { n.name = name.trim(); dirty = true; render(); }
      });
    });
    canvas.querySelectorAll(".wf-add").forEach(b => b.addEventListener("click", () => addAfter(b.dataset.id)));
    canvas.querySelectorAll(".wf-del").forEach(b => b.addEventListener("click", () => removeNode(b.dataset.id)));
    // связывание этапов: ⛓ на узле -> клик по другому узлу
    canvas.querySelectorAll(".wf-link").forEach(b => b.addEventListener("click", ev => {
      ev.stopPropagation();
      linkFrom = (linkFrom === b.dataset.id) ? null : b.dataset.id;
      markLinkSrc(canvas);
    }));
    canvas.querySelectorAll(".wf-node").forEach(el => el.addEventListener("click", () => {
      if (linkFrom && linkFrom !== el.dataset.id) linkTo(el.dataset.id);
    }));
    markLinkSrc(canvas);
    // выпадающий список связей выбранного исходного этапа + удаление связи
    const sel = container.querySelector(".wf-linksel");
    const delBtn = container.querySelector(".wf-dellink");
    if (sel) {
      sel.innerHTML = '<option value="">— этап-источник связи —</option>'
        + st.map(n => '<option value="' + _wfEsc(n.id) + '">' + _wfEsc(n.name || n.id) + '</option>').join('');
      sel.value = linkFrom || "";
      sel.addEventListener("change", () => {
        const id = sel.value;
        if (!id) { linkFrom = null; markLinkSrc(canvas); return; }
        const others = st.filter(n => n.id !== id && !(findNode(id).links_out || []).includes(n.id));
        if (!others.length) { alert("Все возможные связи этого этапа уже созданы"); return; }
        const names = others.map((n, i) => (i + 1) + ". " + (n.name || n.id)).join("\n");
        const ans = prompt("Связать «" + (findNode(id).name || "") + "» с этапом (введите номер):\n" + names, "1");
        const idx = parseInt(ans, 10) - 1;
        if (idx >= 0 && idx < others.length) { linkFrom = id; linkTo(others[idx].id); }
      });
    }
    if (delBtn) delBtn.addEventListener("click", () => {
      const id = (container.querySelector(".wf-linksel") || {}).value;
      if (!id) { alert("Сначала выберите этап-источник"); return; }
      const src = findNode(id);
      const outs = src.links_out || [];
      if (!outs.length) { alert("У этого этапа нет исходящих связей"); return; }
      const byId = {}; st.forEach(n => byId[n.id] = n);
      const names = outs.map((t, i) => (i + 1) + ". → " + ((byId[t] || {}).name || t)).join("\n");
      const ans = prompt("Какую связь удалить?\n" + names, "1");
      const idx = parseInt(ans, 10) - 1;
      if (idx >= 0 && idx < outs.length) {
        src.links_out = outs.filter((_, i) => i !== idx);
        if (!src.links_out.length) delete src.links_out;
        dirty = true; render();
      }
    });
    const rootBtn = container.querySelector(".wf-newroot");
    if (rootBtn) rootBtn.addEventListener("click", () => {
      const name = prompt("Название корневого этапа:", "Этап"); if (!name || !name.trim()) return;
      st.unshift({id: wfUid(), name: name.trim(), state: "pending", date: "", deadline: "",
                  comment: "", taskId: "", branches: [], x: 30, y: 30});
      dirty = true; render();
    });
  }

  function syncCanvasSize(canvas) {
    const maxX = Math.max.apply(null, st.map(n => (n.x || 0) + WF_NODE_W).concat([400])) + 70;
    const maxY = Math.max.apply(null, st.map(n => (n.y || 0) + WF_NODE_H).concat([160])) + 60;
    canvas.style.width = maxX + "px"; canvas.style.height = maxY + "px";
  }

  render();

  return {
    getStages: () => JSON.parse(JSON.stringify(st)),
    isDirty: () => dirty,
    isEmpty: () => !st.length,
  };
}
window.wfCanvasEditor = wfCanvasEditor;
