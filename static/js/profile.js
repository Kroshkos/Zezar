/* Профили сущностей (модульное окно по клику на название) — по образцу
   детальных модалок sipm_lite_v3.html. Данные — только из базы: профиль
   загружается через GET /api/core/entities/{uid}/profile, изменения — через
   PATCH соответствующего модуля (/api/tasks|projects|docs|softs|grants|schols|priority|meetings/{uid}). */
"use strict";

const PROFILE_KIND_API = {task:"tasks", project:"projects", doc:"docs", soft:"softs",
  grant:"grants", scholarship:"schols", priority:"priority", meeting:"meetings"};
const PROFILE_KIND_TITLE = {task:"Задача", project:"Проект", doc:"Документ", soft:"ПО / проектная работа",
  grant:"Грант", scholarship:"Стипендия", priority:"Показатель Приоритет-2030", meeting:"Совещание"};

function pfmt(d){return d?String(d).split("-").reverse().join("."):"—";}
function pfmtDT(s){if(!s)return "—";try{const d=new Date(s);return d.toLocaleDateString("ru-RU")+" "+d.toLocaleTimeString("ru-RU",{hour:"2-digit",minute:"2-digit"});}catch(e){return String(s);}}

async function openProfile(uid){
  if(!uid)return;
  try{history.replaceState(null,"","?entity="+encodeURIComponent(uid));}catch(e){}
  let P;
  try{ P=await api('/api/core/entities/'+uid+'/profile'); }
  catch(e){ return; } // ошибку показал toast
  const body=profileBodyHTML(P);
  // wide-модалка без кнопки «Сохранить»: действия — кнопки внутри профиля
  openModal((PROFILE_KIND_TITLE[P.kind]||"Сущность")+': '+P.title,body,null,'Закрыть');
  const form=document.getElementById('mform');
  if(form){ // рендерим внутри <form> — убираем лишние кнопки сабмита из openModal
    const btn=form.parentElement.querySelector('div[style*="justify-content:flex-end"]');
    if(btn)btn.innerHTML='<button type="button" class="btn ghost" onclick="closeModal()">Закрыть</button>';
  }
  if(window.renderEntityLinks)renderEntityLinks('entLinksBox',P.kind,P.uid);
}

function profBadge(o){return o?'<span class="badge '+(o.color||'b-gray')+'">'+esc(o.label||'—')+'</span>':'<span class="badge b-gray">—</span>';}

function profileBodyHTML(P){
  const m=P.module||{};
  let h='';
  // ---- шапка: статус/приоритет/сроки/подразделение ----
  h+='<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:8px">'
    +profBadge(P.status)+profBadge(P.priority)
    +(P.due_date?'<span class="badge b-gray">⏰ срок: '+pfmt(P.due_date)+'</span>':'')
    +(P.cross_dept?'<span class="badge b-blue">🔗 межпространственная</span>':'')
    +(P.perm==='read'?'<span class="badge b-amber">только чтение</span>':'')
    +'</div>';
  h+='<h3 style="margin:2px 0 4px">'+esc(P.title)+'</h3>';
  if(m.code!==undefined&&m.code!=='')h+='<div style="font-size:12.5px;color:var(--muted)">Код: <b>'+esc(m.code)+'</b></div>';
  h+='<div style="font-size:12px;color:var(--muted)">Подразделение: '+esc(P.department_name||'—')
    +' · Создано: '+pfmtDT(P.created_at)+' · Изменено: '+pfmtDT(P.updated_at)+'</div>';
  if(P.description)h+='<div style="margin-top:8px;font-size:13px">'+esc(P.description)+'</div>';
  if(P.tags&&P.tags.length)h+='<div style="margin-top:6px">'+P.tags.map(t=>'<span class="badge b-gray">#'+esc(t)+'</span>').join(' ')+'</div>';

  // ---- специфика модуля ----
  h+=moduleBlockHTML(P,m);

  // ---- связанные сущности (связанность и подчинённость категорий) ----
  const lk=m.linked||{};
  const chips=[];
  if(lk.project)chips.push('<a href="#" class="lchip" onclick="openProfile(\''+projUidByTitle(P,lk.project)+'\');return false">🗂 проект: <b>'+esc(lk.project)+'</b></a>');
  if(lk.parent_project)chips.push('<span class="lchip">🗂 часть проекта: <b>'+esc(lk.parent_project)+'</b></span>');
  if(lk.grant)chips.push('<span class="lchip">💰 грант: <b>'+esc(lk.grant)+'</b></span>');
  if(lk.priority_indicator)chips.push('<span class="lchip">🚀 Приоритет-2030: <b>'+esc(lk.priority_indicator)+'</b></span>');
  if(lk.tasks)chips.push('<span class="lchip">🎯 задачи: '+esc(lk.tasks)+'</span>');
  if(chips.length)h+='<div class="link-chips" style="margin-top:12px">'+chips.join('')+'</div>';

  // ---- произвольные связи между сущностями (EntityLink, /api/core/links) ----
  h+='<div id="entLinksBox" style="margin-top:12px"></div>';

  // ---- ответственные и исполнители ----
  h+='<div style="margin-top:12px;font-size:12.5px"><b>Ответственные:</b> '
    +(P.responsibles.length?P.responsibles.map(r=>esc(r.user_name)+(r.is_primary?' (осн.)':'')+' <span style="color:var(--muted)">['+esc(r.department)+']</span>').join('; '):'—')+'</div>';
  h+='<div style="margin-top:4px;font-size:12.5px"><b>Исполнители:</b> '
    +(P.executors.length?P.executors.map(e=>esc(e.name)).join(', '):'—')+'</div>';
  if(P.shares.length){
    h+='<div style="margin-top:4px;font-size:12.5px"><b>Расшарено:</b> '
      +P.shares.map(s=>esc(s.department_name)+' ('+(s.permission==='read'?'чтение':'редактирование')+')').join(', ')+'</div>';
  }

  // ---- быстрые действия (сохраняются в БД) ----
  if(P.can_edit){
    const statSel=P.statuses.map(s=>'<option value="'+esc(s.key)+'" '+(P.status.key===s.key?'selected':'')+'>'+esc(s.label)+'</option>').join('');
    const prioSel='<option value="">— без приоритета —</option>'+P.priorities.map(q=>'<option value="'+esc(q.key)+'" '+((P.priority||{}).key===q.key?'selected':'')+'>'+esc(q.label)+'</option>').join('');
    h+='<div class="frow" style="margin-top:14px">'
      +'<div><label>Статус</label><select id="pfStatus">'+statSel+'</select></div>'
      +'<div><label>Приоритет</label><select id="pfPrio">'+prioSel+'</select></div>'
      +'<div><label>Срок</label><input type="date" id="pfDue" value="'+esc(P.due_date||'')+'"></div>'
      +'</div>';
    if(P.people.length){
      const cur=new Set(P.executors.map(e=>String(e.id)));
      h+='<div style="margin-top:6px"><label>Исполнитель(и) — из участников подразделения (можно выбрать несколько, Ctrl/Cmd+клик)</label>'
        +'<select id="pfExec" multiple size="4" style="width:100%">'
        +P.people.map(p=>'<option value="'+p.id+'" '+(cur.has(String(p.id))?'selected':'')+'>'+esc(p.name)+(p.is_head?' (руководитель)':'')+'</option>').join('')
        +'</select></div>';
      h+='<div style="margin-top:6px"><label>Ответственный — из участников подразделения</label><select id="pfResp">'
        +'<option value="">— не изменять —</option>'
        +P.people.map(p=>'<option value="'+p.id+'">'+esc(p.name)+'</option>').join('')+'</select></div>';
    }
    h+='<div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">'
      +'<button class="btn sm primary" onclick="saveProfile(\''+P.uid+'\',\''+esc(P.module_key)+'\')">💾 Сохранить</button>'
      +'<button class="btn sm" onclick="editProfileEntity(\''+P.uid+'\')">✏️ Открыть форму редактирования</button>'
      +'</div>';
  }

  // ---- история изменений (AuditLog из БД) ----
  if(P.history&&P.history.length){
    h+='<div style="margin-top:14px"><b style="font-size:12.5px">История изменений</b>'
      +'<table class="tbl" style="margin-top:6px;font-size:12px"><tbody>'
      +P.history.map(a=>'<tr><td style="white-space:nowrap">'+pfmtDT(a.at)+'</td>'
        +'<td>'+esc(a.actor)+'</td><td>'+esc(PROFILE_ACTION[a.action]||a.action)+'</td>'
        +'<td style="color:var(--muted)">'+(a.fields&&a.fields.length?esc(a.fields.join(', ')):'')+'</td></tr>').join('')
      +'</tbody></table></div>';
  }
  return h;
}

const PROFILE_ACTION={create:'создана',update:'изменена',status:'смена статуса',reassign:'переназначена',share:'расшарена',delete:'удалена'};

function moduleBlockHTML(P,m){
  const k=P.kind;let h='';
  if(k==='project'){
    h+='<div style="margin-top:8px;font-size:12.5px;color:var(--muted)">'
      +'🎯 Заказчик: '+esc(m.customer||'—')
      +(m.start_date||m.end_date?' · 📅 '+pfmt(m.start_date)+' → '+pfmt(m.end_date):'')+'</div>';
    if(m.stage_total)h+='<div style="margin-top:6px;font-size:12.5px">Этапы: '+m.stage_done+'/'+m.stage_total
      +'<div style="background:#eef1f7;border-radius:99px;height:8px;margin-top:4px"><div style="background:var(--acc);height:8px;border-radius:99px;width:'+m.progress+'%"></div></div></div>';
    if(m.stages&&m.stages.length)h+='<div style="overflow-x:auto;margin-top:8px">'+wfCanvasHTML({stages:m.stages})+'</div>';
  }
  if(k==='task'&&m.checklist&&m.checklist.length){
    h+='<div style="margin-top:8px"><b style="font-size:12.5px">Чек-лист</b><ul style="margin:4px 0 0 18px;font-size:12.5px">'
      +m.checklist.map(c=>'<li>'+(c.done?'✅ ':'⬜ ')+esc(c.text||'')+'</li>').join('')+'</ul></div>';
  }
  if(k==='doc'){
    h+='<div style="margin-top:8px;font-size:12.5px;color:var(--muted)">'
      +(m.doc_type?'📑 '+esc(m.doc_type)+' · ':'')+(m.number?'№ '+esc(m.number)+' · ':'')
      +(m.issued_at?'выдан '+pfmt(m.issued_at):'')+'</div>';
    if(m.file_ref)h+='<div style="margin-top:6px"><a class="lchip" href="'+esc(m.file_ref)+'" target="_blank" rel="noopener">📎 файл</a></div>';
  }
  if(k==='soft'){
    h+='<div style="margin-top:8px;font-size:12.5px;color:var(--muted)">'
      +(m.soft_type?'🧩 '+esc(m.soft_type):'')+(m.env?' · ⚙️ '+esc(m.env):'')+'</div>';
    if(m.repo_url)h+='<div style="margin-top:6px"><a class="lchip" href="'+esc(m.repo_url)+'" target="_blank" rel="noopener">🔗 репозиторий</a></div>';
    if(m.stages&&m.stages.length)h+='<div style="overflow-x:auto;margin-top:8px">'+wfCanvasHTML({stages:m.stages})+'</div>';
  }
  if(k==='grant'){
    h+='<div style="margin-top:8px;font-size:12.5px;color:var(--muted)">'
      +'🏦 '+esc(m.program||'—')+(m.amount?' · 💰 '+esc(m.amount):'')
      +(m.submitted_at?' · подан '+pfmt(m.submitted_at):'')+'</div>';
  }
  if(k==='scholarship'){
    h+='<div style="margin-top:8px;font-size:12.5px;color:var(--muted)">'
      +'👤 '+esc(m.person_name||'—')+(m.schol_type?' · 🎓 '+esc(m.schol_type):'')
      +(m.amount?' · 💰 '+esc(m.amount):'')+'</div>';
  }
  if(k==='priority'){
    h+='<div style="margin-top:8px;font-size:12.5px;color:var(--muted)">'
      +'🧭 '+esc(m.direction||'—')+(m.metric?' · 📏 '+esc(m.metric):'')
      +(m.target_value?' · цель: '+esc(m.target_value):'')+(m.current_value?' · текущее: '+esc(m.current_value):'')+'</div>';
  }
  if(k==='meeting'){
    h+='<div style="margin-top:8px;font-size:12.5px;color:var(--muted)">'
      +(m.num?'№ '+esc(m.num)+' · ':'')+'👤 пред.: '+esc(m.chair||'—')+' · 📝 секр.: '+esc(m.secretary||'—')
      +(m.time?' · 🕒 '+esc(m.time):'')+(m.location?' · 📍 '+esc(m.location):'')+'</div>';
    if(m.attendees)h+='<div style="margin-top:4px;font-size:12.5px;color:var(--muted)">👥 Участники: '+esc(m.attendees)+'</div>';
    if(m.agenda&&m.agenda.length){
      h+='<div style="margin-top:8px"><b style="font-size:12.5px">Повестка</b><ol style="margin:4px 0 0 18px;font-size:12.5px">'
        +m.agenda.map(a=>'<li>'+esc(a.q||'')+(a.who?' <span style="color:var(--muted)">— '+esc(a.who)+'</span>':'')+'</li>').join('')+'</ol></div>';
    }
    if(m.decisions&&m.decisions.length){
      h+='<div style="margin-top:8px"><b style="font-size:12.5px">Решения</b><ul style="margin:4px 0 0 18px;font-size:12.5px">'
        +m.decisions.map(d=>'<li>'+esc(d.text||'')+(d.who?' <span style="color:var(--muted)">— '+esc(d.who)+(d.due?', срок '+pfmt(d.due):'')+'</span>':'')+'</li>').join('')+'</ul></div>';
    }
  }
  return h;
}

// холст маршрута (тот же, что в projects.js; если страница проектов не загружена — своя копия)
function wfCanvasHTML(p){
  if(typeof window.wfCanvasHTML==='function'&&window.wfCanvasHTML!==arguments.callee){
    try{return window.wfCanvasHTML(p);}catch(e){}
  }
  const st=p.stages||[];
  if(!st.length)return '';
  const STEP={pending:['Ожидает','b-gray'],active:['В работе','b-blue'],done:['Готово','b-green'],rejected:['Отклонён','b-red']};
  let h='<label>🧭 Холст маршрута</label><div style="overflow-x:auto"><div style="display:flex;gap:8px;align-items:stretch;padding:6px 2px">';
  st.forEach((s,i)=>{
    const w=STEP[s.state]||[(s.done?'Готово':'—'),(s.done?'b-green':'b-gray')];
    h+='<div style="min-width:150px;max-width:200px;border:1px solid #cfd8e8;border-radius:12px;padding:9px;background:#fff">'
      +'<div style="font-weight:600;font-size:12.5px">'+esc(s.name)+'</div>'
      +'<div style="margin-top:5px"><span class="badge '+w[1]+'">'+w[0]+'</span></div>'
      +(s.deadline?'<div style="font-size:11.5px;color:var(--muted);margin-top:5px">⏰ '+pfmt(s.deadline)+'</div>':'')
      +(s.taskId?'<div style="font-size:11px;color:var(--muted);margin-top:3px">автозадача ✓</div>':'')
      +'</div>';
    if(i<st.length-1)h+='<div style="align-self:center;color:#94a3b8">→</div>';
  });
  h+='</div></div>';
  return h;
}

// uid связанного проекта для перехода по клику (из данных текущей страницы, если есть)
function projUidByTitle(P,title){
  const src=(typeof PRJ!=='undefined'&&PRJ.projects)?PRJ.projects:(typeof TD!=='undefined'&&TD.tasks?null:null);
  if(src){const f=src.find(x=>x.title===title);if(f)return f.uid;}
  return P.uid; // fallback: остаться на текущем профиле
}

async function saveProfile(uid,moduleKey){
  const pl={};
  const st=document.getElementById('pfStatus'),pr=document.getElementById('pfPrio'),du=document.getElementById('pfDue');
  if(st)pl.status_key=st.value;
  if(pr)pl.priority_key=pr.value||null;
  if(du)pl.due_date=du.value||null;
  const ex=document.getElementById('pfExec');
  if(ex)pl.executor_user_ids=Array.from(ex.selectedOptions).map(o=>+o.value).filter(v=>v>0);
  const rs=document.getElementById('pfResp');
  if(rs&&rs.value)pl.responsible_user_id=+rs.value;
  const base=PROFILE_KIND_API[moduleKey]||moduleKey;
  await api('/api/'+base+'/'+uid,{method:'PATCH',body:JSON.stringify(pl)});
  toast('Профиль сохранён');
  openProfile(uid); // перезагрузить профиль из БД
}

function editProfileEntity(uid){
  // открыть существующую форму редактирования модуля (если скрипт страницы загружен)
  const fnMap={task:()=>typeof taskFormModal==='function'&&taskFormModal(uid),
    project:()=>typeof projFormModal==='function'&&projFormModal(uid),
    doc:()=>typeof docFormModal==='function'&&docFormModal(uid),
    soft:()=>typeof softFormModal==='function'&&softFormModal(uid),
    grant:()=>typeof grantFormModal==='function'&&grantFormModal(uid),
    schol:()=>typeof scholFormModal==='function'&&scholFormModal(uid),
    priority:()=>typeof prioFormModal==='function'&&prioFormModal(uid),
    meetings:()=>typeof meetFormModal==='function'&&meetFormModal(uid)};
  closeModal();
  const key=Object.keys(fnMap).find(k=>{
    const el=document.getElementById({'task':'taskData','project':'projData','doc':'docData','soft':'softData',
      'grant':'grantData','schol':'scholData','priority':'prioData','meetings':'meetData'}[k]);
    return !!el;
  });
  if(key&&fnMap[key]())return;
  toast('Форма этого модуля недоступна на данной странице');
}

/* Ссылки из Telegram вида /tasks?entity=<uid> — открываем профиль сущности
   после загрузки данных страницы (DOMContentLoaded уже поставлен скриптом модуля). */
document.addEventListener('DOMContentLoaded',()=>{
  try{
    const uid=new URLSearchParams(location.search).get('entity');
    if(uid)setTimeout(()=>openProfile(uid),150);
  }catch(e){}
});
