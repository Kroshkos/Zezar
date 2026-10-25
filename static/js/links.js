/* Произвольные связи между сущностями (UI поверх /api/core/links).
   renderEntityLinks(containerId, kind, uid) — чипы существующих связей + кнопка
   «🔗 Связать с…» (модалка с поиском доступных сущностей; для project/soft-целей
   можно указать этап маршрута). Используется в карточках проектов, задач, docs,
   софтов и т.д.; функции глобальны — вызываются из других модульных скриптов. */
"use strict";

const LINK_ICONS = {task: "✅", doc: "📄", soft: "💻", project: "📁",
                    grant: "💰", scholarship: "🎓", priority: "🚀", meeting: "🤝"};

async function renderEntityLinks(boxId, kind, uid) {
  const box = document.getElementById(boxId);
  if (!box || !uid) return;
  let rows = [];
  try { rows = await api("/api/core/entities/" + uid + "/links"); }
  catch (e) { return; }            // нет прав/сервер не отдал — блок молча скрываем
  const chips = rows.map(l => {
    const stage = l.stage_name ? ' · <span style="color:var(--accent)">этап «' + esc(l.stage_name) + '»</span>' : "";
    return '<span class="badge b-gray" style="margin:2px 4px 2px 0;display:inline-flex;align-items:center;gap:4px">'
      + (LINK_ICONS[l.kind] || "🔗") + ' <a href="#" data-lk="' + esc(l.kind) + '" data-lu="' + esc(l.uid) + '"'
      + ' style="color:inherit;text-decoration:none" title="' + esc(l.title) + '">' + esc(l.title.slice(0, 48)) + '</a>'
      + stage
      + ' <button type="button" class="icobtn" style="border:0;background:none;cursor:pointer;padding:0 2px"'
      + ' title="Удалить связь" onclick="removeEntityLink(' + l.id + ',\'' + escJs(uid) + '\',\'' + boxId + '\')">✕</button></span>';
  }).join("");
  box.innerHTML = '<div style="font-size:12px;color:var(--muted);margin-bottom:4px">🔗 Связи</div>'
    + (chips || '<span style="font-size:12px;color:var(--muted)">нет</span> ')
    + '<button type="button" class="btn sm ghost" onclick="openLinkPicker(\'' + escJs(kind) + '\',\'' + escJs(uid) + '\',\'' + boxId + '\')">＋ Связать с…</button>';
  box.querySelectorAll("a[data-lu]").forEach(a => a.onclick = ev => {
    ev.preventDefault(); openLinkedEntity(a.dataset.lk, a.dataset.lu);
  });
}

function escJs(s) { return String(s == null ? "" : s).replace(/\\/g, "\\\\").replace(/'/g, "\\'"); }

async function removeEntityLink(linkId, uid, boxId) {
  try {
    await api("/api/core/links/" + linkId, {method: "DELETE"});
    toast("Связь удалена");
    renderEntityLinks(boxId, "", uid);
  } catch (e) { toast(e.message || "Не удалось удалить связь", true); }
}

// переход к связанной сущности: открываем карточку своего модуля либо ведём на страницу
function openLinkedEntity(kind, uid) {
  const K = String(kind || "");
  if (K === "project" && typeof PRJ !== "undefined" && PRJ.projects) {
    const p = PRJ.projects.find(x => x.uid === uid); if (p) return openProjectCard(uid);
  }
  const page = {doc: "/docs", soft: "/softs", grant: "/grants", scholarship: "/schols",
                priority: "/priority", meeting: "/meetings"}[K];
  if (page) { location.href = page + "?open=" + encodeURIComponent(uid); }
  else { toast("Откройте эту сущность вручную: " + uid, true); }
}

function openLinkPicker(kind, uid, boxId) {
  const body = '<div><label>Поиск сущности (название)</label>'
    + '<input name="q" id="lq" placeholder="например: этап внедрения…" autocomplete="off"></div>'
    + '<div id="lres" style="max-height:300px;overflow:auto;margin-top:8px;border:1px solid var(--line);border-radius:8px"></div>'
    + '<div id="lstage" style="display:none;margin-top:8px"><label>Этап маршрута цели (необязательно)</label>'
    + '<select id="lstagesel"><option value="">— без этапа —</option></select></div>'
    + '<div style="margin-top:8px"><label>Комментарий (необязательно)</label><input name="note" maxlength="500"></div>';
  openModal("🔗 Связать с…", body, async () => {
    if (!PICK.selected) { toast("Выберите сущность для связи", true); return; }
    const stSel = document.getElementById("lstagesel");
    const stageIdx = (PICK.hasStages && stSel && stSel.value !== "") ? parseInt(stSel.value, 10) : null;
    try {
      await api("/api/core/links", {method: "POST", body: JSON.stringify({
        source_uid: uid, target_uid: PICK.selected,
        link_type: "related", note: null, stage_index: stageIdx})});
      toast("Связь создана");
      closeModal(); renderEntityLinks(boxId, kind, uid);
    } catch (e) { toast(e.message || "Не удалось создать связь", true); }
  }, "🔗 Связать");
  const PICK = openLinkPicker._st = {selected: null, hasStages: false};
  const inp = document.getElementById("lq");
  let timer = null;
  const doSearch = async () => {
    const res = document.getElementById("lres");
    try {
      const rows = await api("/api/core/links/search?q=" + encodeURIComponent(inp.value || ""));
      res.innerHTML = rows.length ? rows.map(r =>
        '<label class="linkopt" data-uid="' + esc(r.uid) + '" data-st="' + (r.has_stages ? "1" : "") + '"'
        + ' style="display:flex;gap:8px;align-items:center;padding:7px 10px;border-bottom:1px solid var(--line);cursor:pointer">'
        + '<input type="radio" name="lpick" style="margin:0">'
        + '<span>' + (LINK_ICONS[r.kind] || "🔗") + '</span>'
        + '<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(r.title) + '</span>'
        + '<span class="badge b-gray" style="flex:none">' + esc(r.kind_label || r.kind) + '</span></label>').join("")
        : '<div style="padding:10px;color:var(--muted);font-size:12.5px">Ничего не найдено (или всё уже связано)</div>';
      res.querySelectorAll(".linkopt").forEach(el => el.onclick = () => {
        res.querySelectorAll(".linkopt").forEach(x => x.style.background = "");
        el.style.background = "rgba(37,99,235,.08)";
        el.querySelector("input").checked = true;
        PICK.selected = el.dataset.uid;
        PICK.hasStages = el.dataset.st === "1";
        const stBox = document.getElementById("lstage");
        stBox.style.display = PICK.hasStages ? "" : "none";
        if (PICK.hasStages) fillStageOptions(el.dataset.uid);
      });
    } catch (e) { res.innerHTML = '<div style="padding:10px;color:#dc2626;font-size:12.5px">' + esc(e.message || "Ошибка поиска") + '</div>'; }
  };
  inp.oninput = () => { clearTimeout(timer); timer = setTimeout(doSearch, 300); };
  doSearch();
}

async function fillStageOptions(targetUid) {
  const sel = document.getElementById("lstagesel");
  sel.innerHTML = '<option value="">— без этапа —</option>';
  // этапы берём из карточки цели: проект — из локального кэша, остальное — через профиль
  try {
    let stages = null;
    if (typeof PRJ !== "undefined" && PRJ.projects) {
      const p = PRJ.projects.find(x => x.uid === targetUid); if (p) stages = p.stages;
    }
    if (!stages) {
      const prof = await api("/api/core/entities/" + targetUid + "/profile").catch(() => null);
      if (prof && prof.module && Array.isArray(prof.module.stages)) stages = prof.module.stages;
    }
    (stages || []).forEach((s, i) => {
      const o = document.createElement("option");
      o.value = i; o.textContent = (i + 1) + ". " + (s.name || ("Этап " + (i + 1)));
      sel.appendChild(o);
    });
  } catch (e) {}
}
