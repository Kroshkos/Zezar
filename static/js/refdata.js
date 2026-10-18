/* Вкладка «Справочники» админ-панели (по образцу раздела администрирования
   прототипа sipm_lite_v3.html). ОТЛИЧИЕ ОТ ПРОТОТИПА: все данные хранятся в
   базе данных через REST API /api/core/refdata/* — localStorage НЕ используется.
   Зависит от глобалов страницы admin_dept: DEPT, IS_ADMIN, ADM и api/toast/
   openModal/askConfirm/esc из core.js. */
'use strict';

const RD_SETS = {
  task:     {title:'Статусы задач',        module:'tasks'},
  project:  {title:'Статусы проектов',     module:'projects'},
  doc:      {title:'Статусы документов',   module:'docs'},
  soft:     {title:'Статусы ПО',           module:'softs'},
  grant:    {title:'Статусы грантов',      module:'grants'},
  schol:    {title:'Статусы стипендий',    module:'schols'},
  priority: {title:'Статусы приоритетов',  module:'priority'},
  meet:     {title:'Статусы совещаний',    module:'meetings'},
};
const RD_TYPES = {
  doc_type:   {title:'Типы документов', module:'docs'},
  soft_type:  {title:'Типы ПО',         module:'softs'},
  schol_type: {title:'Типы стипендий',  module:'schols'},
};
let RD_CACHE = null;          // последний ответ /sections (кэш в памяти, НЕ localStorage)
let RD_SUB = 'status:task';   // активный подраздел

// Глобальные переменные страницы admin_dept объявлены в inline-скрипте ПОСЛЕ
// загрузки этого файла — читаем их лениво (в функциях), а не на этапе парсинга.
function rdAdm(){ return (typeof ADM!=='undefined' && ADM) ? ADM : {}; }
function rdDept(){ const d=(typeof DEPT!=='undefined')?DEPT:null; return d||null; }
// «Вся система»: администратор/координатор без выбранного пространства.
// IS_ADMIN включает также координатора-руководителя (у него есть scoped-вкладки),
// поэтому глобальный режим определяем по флагу сервера GLOBAL_MODE/ADM.global_mode.
function rdIsGlobal(){ return !!(typeof IS_ADMIN!=='undefined' && IS_ADMIN) && !!rdAdm().global_mode; }
// право ведения справочников: администратор/координатор (глобально) и руководитель
// подразделения; участник — только просмотр (сервер дублирует проверку на API)
function rdCanWrite(){ return (typeof IS_ADMIN!=='undefined' && IS_ADMIN) || !!rdAdm().can_manage; }
function rdDeptQ(){ const d=rdDept(); return d?('department_id='+d):''; }

async function rdLoad(force){
  if(RD_CACHE && !force) return RD_CACHE;
  RD_CACHE = await api('/api/core/refdata/sections?'+rdDeptQ());
  return RD_CACHE;
}

/* ---------- рендер подраздела ---------- */
async function rdShow(sub){
  RD_SUB = sub;
  document.querySelectorAll('.rd-sub').forEach(b=>b.classList.toggle('on', b.dataset.sub===sub));
  const box = document.getElementById('rdBody');
  if(!box) return;
  box.innerHTML = '<span class="muted">Загрузка…</span>';
  let data;
  try{ data = await rdLoad(); }catch(e){ box.innerHTML='<span class="muted">Нет доступа к справочникам.</span>'; return; }
  const [kind, arg] = sub.split(':');
  if(kind==='status')      rdRenderStatus(box, data.status_sets[arg], arg);
  else if(kind==='prio')   rdRenderGeneric(box, data.prio);
  else if(kind==='type')   rdRenderGeneric(box, data.types[arg]);
  else if(kind==='wf')     rdRenderGeneric(box, data.wf);
  else if(kind==='groups') rdRenderGeneric(box, data.groups);
  else box.innerHTML='<span class="muted">Раздел не найден.</span>';
}

function rdBadge(r){
  let b = `<span class="badge ${esc(r.color||'b-gray')}">${esc(r.label||r.name)}</span>`;
  if(r.is_active===false) b += ' <span class="badge b-red">выключен</span>';
  if(r.department_id==null) b += ' <span class="badge b-purple" title="Глобальный шаблон администратора: применяется ко всем подразделениям с подключённым модулем">🌐 общесистемный</span>';
  else if(r.source_global_id) b += ' <span class="badge b-blue" title="Активированная копия шаблона администратора">📎 из шаблона</span>';
  else b += ' <span class="badge b-green" title="Собственная запись подразделения">местный</span>';
  if(typeof r.usage==='number' && r.usage>0) b += ` <span class="sub">используется: ${r.usage}</span>`;
  return b;
}

function rdActions(r, section, arg){
  if(!rdCanWrite()) return '';
  let html = '';
  // админ/координатор: глобальные шаблоны И (в режиме «Вся система») записи подразделений — доступны правка/удаление
  const deptRecEditable = rdIsGlobal() && r.department_id!=null;
  // контекст удаления = подразделение САМОЙ ЗАПИСИ (в режиме «Вся система» rdDept()=null,
  // а сервер требует department_id записи при удалении чужих записей подразделений)
  const delArg = `rdDel(${r.id},'${section}',${r.usage||0},${r.department_id!=null?r.department_id:'null'})`;
  if(rdIsGlobal() && r.department_id==null){
    html += `<button class="btn sm ghost" onclick="rdEdit(${r.id},'${section}','${arg}')">✏️ Изменить</button> `
          + `<button class="btn sm danger" onclick="${delArg}">🗑 Удалить</button>`;
  } else if(deptRecEditable){
    html += `<span class="badge b-amber" title="Запись подразделения ${esc(r.department_name||('№'+r.department_id))}">${esc(r.department_name||('пространство №'+r.department_id))}</span> `
          + `<button class="btn sm ghost" onclick="rdEdit(${r.id},'${section}','${arg}')">✏️ Изменить</button> `
          + `<button class="btn sm danger" onclick="${delArg}">🗑 Удалить</button>`;
  } else {
    if(r.source_global_id){
      html += r.is_active
        ? `<button class="btn sm ghost" onclick="rdDeact(${r.id},'${section}')">⏸ Деактивировать шаблон</button>`
        : `<button class="btn sm" onclick="rdAct(${r.source_global_id})">▶ Включить шаблон обратно</button> `
          + `<button class="btn sm danger" onclick="${delArg}">🗑</button>`;
    } else {
      html += `<button class="btn sm ghost" onclick="rdEdit(${r.id},'${section}','${arg}')">✏️ Изменить</button> `
            + `<button class="btn sm danger" onclick="${delArg}">🗑 Удалить</button>`;
    }
  }
  return html;
}

function rdTableHead(){ return '<table class="tbl"><thead><tr><th>Наименование</th><th>Параметры</th><th style="text-align:right">Действия</th></tr></thead><tbody>'; }

function rdAvailBlock(avail, section){
  // Шаблоны администратора для руководителя: применить / деактивировать / включить обратно
  if(rdIsGlobal() || !avail || !avail.length) return '';
  const items = avail.map(g=>{
    let st = g.activated ? '<span class="badge b-green">применён</span>'
             : (g.deactivated ? '<span class="badge b-gray">деактивирован руководителем</span>'
                              : '<span class="badge b-purple">общесистемный шаблон</span>');
    let btn = g.activated
      ? `<button class="btn sm ghost" onclick="rdDeact(${g.copy_id},'${section}')">⏸ Деактивировать</button>`
      : `<button class="btn sm" onclick="rdAct(${g.id})">▶ Применить у себя</button>`;
    return `<tr><td>${esc(g.label)} ${st}</td><td></td><td style="text-align:right">${btn}</td></tr>`;
  }).join('');
  if(!items) return '';
  return `<p class="sub muted" style="margin:14px 0 4px"><b>Шаблоны администратора</b> — примените к своему пространству или деактивируйте (история сохраняется, можно включить обратно):</p>
          <table class="tbl"><tbody>${items}</tbody></table>`;
}

function rdRenderStatus(box, view, setName){
  if(!view){ box.innerHTML='<span class="muted">Модуль не подключён к подразделению — набор статусов недоступен.</span>'; return; }
  const rows = view.items.map(r=>`<tr>
    <td>${rdBadge(r)}</td><td class="muted"><code>${esc(r.key)}</code> · ${esc((r.color||'b-gray').replace('b-',''))} · порядок ${r.order??0}</td>
    <td style="text-align:right;white-space:nowrap">${rdActions(r,'status',setName)}</td></tr>`).join('');
  box.innerHTML =
    `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
       <h3 style="margin:0">${esc(RD_SETS[setName].title)}</h3>
       ${rdCanWrite()?`<button class="btn sm primary" onclick="rdAdd('status','${setName}')">＋ Добавить статус</button>`:''}
       <span class="sub muted">Применяется к модулю «${RD_SETS[setName].module}». 🌐 — общие шаблоны администратора, 📎 — активированные копии, «местный» — свои записи пространства.</span>
     </div>` + rdTableHead() + (rows||'<tr><td colspan="3" class="muted">Список пуст.</td></tr>') + '</tbody></table>'
     + rdAvailBlock(view.available_templates,'status');
}

function rdRenderGeneric(box, view){
  if(!view || !Object.keys(view).length){ box.innerHTML='<span class="muted">Раздел недоступен: модуль не подключён к подразделению.</span>'; return; }
  const endpoint = view.ref, arg = view.set||'';
  const title = endpoint==='prio' ? 'Приоритеты задач'
              : endpoint==='wf' ? 'Маршруты согласования'
              : endpoint==='groups' ? 'Группы задач (для совещаний)'
              : (RD_TYPES[arg]?RD_TYPES[arg].title:'Справочник');
  const rows = view.items.map(r=>{
    const params = endpoint==='wf' ? esc((r.steps||[]).join(' → '))+(r.kind_scope?' <span class="sub">['+esc(r.kind_scope)+']</span>':'')
               : endpoint==='groups' ? (esc(r.description||rdCritSummary(r.criteria))+' · <b>соответствует задач: '+(r.matches??0)+'</b>')
               : endpoint==='prio' ? ('<code>'+esc(r.key)+'</code> · вес '+(r.weight??0)+' · '+esc((r.color||'').replace('b-','')))
               : endpoint==='type' ? ('<span class="sub">'+esc(r.kind||arg)+'</span>'+(r.usage?` <span class="sub">используется: ${r.usage}</span>`:''))
               : '';
    return `<tr><td>${rdBadge({...r, color:r.color||(endpoint==='wf'?'b-blue':'b-gray')})}</td>
      <td class="muted">${params}</td>
      <td style="text-align:right;white-space:nowrap">${rdActions(r,endpoint,arg)}</td></tr>`;
  }).join('');
  box.innerHTML =
    `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
       <h3 style="margin:0">${esc(title)}</h3>
       ${rdCanWrite()?`<button class="btn sm primary" onclick="rdAdd('${endpoint}','${arg}')">＋ Добавить</button>`:''}
     </div>` + rdTableHead() + (rows||'<tr><td colspan="3" class="muted">Список пуст.</td></tr>') + '</tbody></table>'
     + rdAvailBlock(view.available_templates,endpoint);
}

/* ---------- конструктор групп задач (по образцу sipm_lite_v3.html:
   мультивыбор чипсами — проекты, статусы, приоритеты, сроки + описание) ---- */
const RD_DUE_LABELS={week:'Ближайшая неделя',month:'Ближайший месяц',overdue:'Просроченные'};
let RD_GFORM = null;  // {projectIds:Set,statuses:Set,priorities:Set,dues:Set}

function rdChip(name,val,on,label){
  return `<label class="rd-chip" style="display:inline-flex;align-items:center;gap:5px;border:1px solid #cbd5e1;border-radius:999px;padding:4px 10px;font-size:12.5px;cursor:pointer;margin:0 6px 6px 0">`
    +`<input type="checkbox" name="${name}" value="${esc(String(val))}" ${on?'checked':''}> ${esc(label)}</label>`;
}
function rdChipsBlock(name,opts,sel){
  if(!opts||!opts.length) return '<p class="sub muted" style="margin:2px 0 8px">нет доступных значений</p>';
  return opts.map(o=>{
    const val=o.id!==undefined?o.id:o.key!==undefined?o.key:o.value;
    return rdChip(name,val,sel.has(String(val)),o.label||RD_DUE_LABELS[val]||val);
  }).join('');
}
function rdGroupFields(view,existing){
  const crit=existing?normCrit(existing.criteria):{projectIds:[],statuses:[],priorities:[],dues:[]};
  RD_GFORM={projectIds:new Set(crit.projectIds.map(String)),
            statuses:new Set(crit.statuses.map(String)),
            priorities:new Set(crit.priorities.map(String)),
            dues:new Set(crit.dues.map(String))};
  const o=(view&&view.group_options)||{projects:[],statuses:[],priorities:[],dues:[]};
  return '<label>Название группы</label><input name="gname" required maxlength="120" value="'+esc(existing?existing.name:'')+'">'
    +'<div style="margin-top:10px"><b class="sub">Проекты</b><div id="rdGProjects">'+rdChipsBlock('g_proj',o.projects,RD_GFORM.projectIds)+'</div></div>'
    +'<div style="margin-top:6px"><b class="sub">Статусы задач</b><div id="rdGStatuses">'+rdChipsBlock('g_status',o.statuses,RD_GFORM.statuses)+'</div></div>'
    +'<div style="margin-top:6px"><b class="sub">Приоритеты</b><div id="rdGPrios">'+rdChipsBlock('g_prio',o.priorities,RD_GFORM.priorities)+'</div></div>'
    +'<div style="margin-top:6px"><b class="sub">Сроки исполнения</b><div id="rdGDues">'+rdChipsBlock('g_due',o.dues,RD_GFORM.dues)+'</div></div>'
    +'<p class="sub muted" style="margin:8px 0 0">Описание формируется автоматически по выбранным критериям; пустой набор = все задачи.</p>';
}
function normCrit(c){
  c=c||{};
  const arr=v=>Array.isArray(v)?v:(v==null||v===''?[]:[v]);
  return {projectIds:arr(c.projectIds||c.projects),statuses:arr(c.statuses),
          priorities:arr(c.priorities),dues:arr(c.dues)};
}
function rdCollectCrit(form){
  const pick=(name,set)=>{set.clear();form.querySelectorAll(`input[name=${name}]:checked`).forEach(i=>set.add(i.value));return [...set];};
  return {projectIds:pick('g_proj',RD_GFORM.projectIds),
          statuses:pick('g_status',RD_GFORM.statuses),
          priorities:pick('g_prio',RD_GFORM.priorities),
          dues:pick('g_due',RD_GFORM.dues)};
}
function rdCritSummary(c){
  if(!c) return '';
  c=normCrit(c);
  const parts=[];
  if((c.projectIds||[]).length) parts.push('проектов: '+c.projectIds.length);
  if((c.statuses||[]).length) parts.push('статусы: '+esc(c.statuses.join(', ')));
  if((c.priorities||[]).length) parts.push('приоритеты: '+esc(c.priorities.join(', ')));
  if((c.dues||[]).length) parts.push('срок: '+c.dues.map(d=>RD_DUE_LABELS[d]||d).join(', '));
  return parts.join(' · ')||'все задачи';
}

/* ---------- CRUD-модалки (сохранение — только в БД через API) ---------- */
function rdColorSel(cur){
  const cs=(RD_CACHE&&RD_CACHE.colors)||['b-blue','b-green','b-amber','b-red','b-gray','b-purple','b-orange'];
  return '<label>Цвет метки</label><select name="color">'+cs.map(c=>`<option value="${c}" ${c===cur?'selected':''}>${c.replace('b-','')}</option>`).join('')+'</select>';
}

function rdAdd(section, arg){
  if(!rdCanWrite()) return toast('Справочники ведут администратор и руководитель подразделения','warn');
  let fields='';
  if(section==='status')      fields='<label>Наименование статуса</label><input name="label" required>'+rdColorSel('b-blue')+'<label>Порядок</label><input name="order" type="number" value="0">';
  else if(section==='prio')   fields='<label>Наименование приоритета</label><input name="label" required>'+rdColorSel('b-blue')+'<label>Вес (сортировка)</label><input name="weight" type="number" value="10">';
  else if(section==='type')   fields='<label>Наименование типа</label><input name="label" required>';
  else if(section==='wf')     fields='<label>Название маршрута</label><input name="name" required><label>Шаги (через запятую)</label><input name="steps" placeholder="Подготовка, Согласование, Утверждение"><label>Область применения (project/doc/soft/task/meet — пусто для любой)</label><input name="kind_scope">';
  else if(section==='groups'){
    const view=rdCurrentView();
    openModal('Добавить: группа задач', rdGroupFields(view,null), async(fd,form)=>{
      const body={ department_id: rdIsGlobal() ? (rdDept()||null) : rdDept(),
                   name: form.elements.gname.value.trim(),
                   criteria: rdCollectCrit(form) };
      await api('/api/core/refdata/groups','POST',body);
      closeModal(); toast('Сохранено в базу данных'); RD_CACHE=null; rdShow(RD_SUB);
    });
    return;
  }
  openModal('Добавить: '+sectionLabel(section,arg), fields, async(fd,form)=>{
    const body={ department_id: rdIsGlobal() ? (rdDept()||null) : rdDept() };
    if(section==='status'){ body.set=arg; body.label=fd.label.trim(); body.color=fd.color; body.order=+fd.order||0; }
    else if(section==='prio'){ body.label=fd.label.trim(); body.color=fd.color; body.weight=+fd.weight||0; }
    else if(section==='type'){ body.kind=arg; body.label=fd.label.trim(); }
    else if(section==='wf'){ body.name=fd.name.trim(); body.steps=(form.elements.steps.value||'').split(',').map(s=>s.trim()).filter(Boolean); body.kind_scope=(form.elements.kind_scope.value||'').trim()||null; }
    else if(section==='groups'){ body.name=fd.name.trim(); body.criteria={statuses:(form.elements.statuses.value||'').split(',').map(s=>s.trim()).filter(Boolean), priorities:(form.elements.priorities.value||'').split(',').map(s=>s.trim()).filter(Boolean)}; }  // legacy — не используется (группы открывают ранний return выше)
    await api('/api/core/refdata/'+section,'POST',body);
    closeModal(); toast('Сохранено в базу данных'); RD_CACHE=null; rdShow(RD_SUB);
  });
}

function rdCurrentView(){
  if(!RD_CACHE) return null;
  const [kind,arg]=RD_SUB.split(':');
  if(kind==='status') return RD_CACHE.status_sets[arg];
  if(kind==='prio')   return RD_CACHE.prio;
  if(kind==='type')   return RD_CACHE.types[arg];
  if(kind==='wf')     return RD_CACHE.wf;
  if(kind==='groups') return RD_CACHE.groups;
  return null;
}

function rdEdit(id, section, arg){
  const view = rdCurrentView(); if(!view) return;
  const r = (view.items||[]).find(x=>x.id===id); if(!r) return;
  // контекст правки = подразделение САМОЙ ЗАПИСИ (в режиме «Вся система»
  // rdDept() равно null для глобальных шаблонов и не совпадает с чужими
  // записями подразделений — сервер иначе отклонил бы патч как 403/смену scope)
  const deptPatch = {department_id: (r.department_id!=null ? r.department_id : rdDept())};
  const done = ()=>{ closeModal(); toast('Сохранено'); RD_CACHE=null; rdShow(RD_SUB); };
  if(section==='status') openModal('Изменить статус',
      '<label>Наименование</label><input name="label" value="'+esc(r.label)+'" required>'+rdColorSel(r.color)+'<label>Порядок</label><input name="order" type="number" value="'+(r.order||0)+'">',
      async(fd)=>{ await api('/api/core/refdata/status/'+id,'PATCH',{...deptPatch,label:fd.label.trim(),color:fd.color,order:+fd.order||0}); done(); });
  else if(section==='prio') openModal('Изменить приоритет',
      '<label>Наименование</label><input name="label" value="'+esc(r.label)+'" required>'+rdColorSel(r.color)+'<label>Вес</label><input name="weight" type="number" value="'+(r.weight||0)+'">',
      async(fd)=>{ await api('/api/core/refdata/prio/'+id,'PATCH',{...deptPatch,label:fd.label.trim(),color:fd.color,weight:+fd.weight||0}); done(); });
  else if(section==='type') openModal('Изменить тип',
      '<label>Наименование</label><input name="label" value="'+esc(r.label)+'" required>',
      async(fd)=>{ await api('/api/core/refdata/type/'+id,'PATCH',{...deptPatch,label:fd.label.trim()}); done(); });
  else if(section==='wf') openModal('Изменить маршрут',
      '<label>Название</label><input name="name" value="'+esc(r.name)+'" required><label>Шаги (через запятую)</label><input name="steps" value="'+esc((r.steps||[]).join(', '))+'">',
      async(fd,form)=>{ await api('/api/core/refdata/wf/'+id,'PATCH',{...deptPatch,name:fd.name.trim(),steps:(form.elements.steps.value||'').split(',').map(s=>s.trim()).filter(Boolean)}); done(); });
  else if(section==='groups') openModal('Изменить группу', rdGroupFields(view,r),
      async(fd,form)=>{ await api('/api/core/refdata/groups/'+id,'PATCH',{...deptPatch,name:form.elements.gname.value.trim(),criteria:rdCollectCrit(form)}); done(); });
}

function rdDel(id, section, usage, recDeptId){
  if(usage>0) return toast('Нельзя удалить: значение используется сущностями ('+usage+'). Сначала переведите их на другой элемент справочника.','warn');
  askConfirm('Удалить запись справочника из базы данных?', async()=>{
    // удаляем в контексте подразделения ЗАПИСИ (режим «Вся система»: rdDept()=null)
    const d = (recDeptId!=null ? recDeptId : rdDept());
    await api('/api/core/refdata/'+section+'/'+id+(d?('?department_id='+d):''),'DELETE');
    toast('Удалено'); RD_CACHE=null; rdShow(RD_SUB);
  });
}
async function rdAct(globalId){
  await api('/api/core/refdata/activate','POST',{global_id:globalId, department_id:rdDept()});
  toast('Шаблон администратора применён к подразделению'); RD_CACHE=null; rdShow(RD_SUB);
}
async function rdDeact(copyId, section){
  await api('/api/core/refdata/deactivate/'+section+'/'+copyId+'?'+rdDeptQ(),'POST');
  toast('Шаблон деактивирован (можно включить обратно)'); RD_CACHE=null; rdShow(RD_SUB);
}
function sectionLabel(section,arg){
  if(section==='status') return (RD_SETS[arg]||{}).title||'статус';
  if(section==='type')   return (RD_TYPES[arg]||{}).title||'тип';
  return {prio:'приоритет', wf:'маршрут', groups:'группа задач'}[section]||section;
}

/* ---------- каркас вкладки (вызывается из showTab('refdata')) ---------- */
async function initRefDataPane(){
  // Режим «Вся система» доступен только администратору/координатору;
  // руководитель без выбранного пространства справочники не видит.
  if(!rdDept() && !rdIsGlobal()){
    const box=document.getElementById('rdBody');
    if(box) box.innerHTML='<span class="muted">Выберите подразделение в шапке — справочники доступны руководителю только в его пространстве.</span>';
    return;
  }
  const pane=document.getElementById('pane-refdata'); if(!pane) return;
  const card=pane.querySelector('.card'); if(!card) return;
  let data; try{ data=await rdLoad(true); }catch(e){ card.innerHTML='<span class="muted">Справочники недоступны.</span>'; return; }
  const subs=[];
  for(const s of Object.keys(RD_SETS)) if(data.status_sets[s]) subs.push(['status:'+s, RD_SETS[s].title]);
  if(data.prio&&Object.keys(data.prio).length) subs.push(['prio','Приоритеты задач']);
  for(const t of Object.keys(RD_TYPES)) if(data.types[t]) subs.push(['type:'+t, RD_TYPES[t].title]);
  if(data.wf&&Object.keys(data.wf).length) subs.push(['wf','Маршруты']);
  if(data.groups&&Object.keys(data.groups).length) subs.push(['groups','Группы задач']);
  const writable = (typeof IS_ADMIN!=='undefined' && IS_ADMIN) || !!rdAdm().can_manage;
  card.innerHTML =
     '<p class="adm-scope">Единые справочники ядра: все изменения сохраняются в <b>базе данных</b> (не в браузере). 🌐 общесистемные шаблоны создаёт <b>администратор</b> — они применяются ко всем подразделениям согласно подключённым модулям; <b>руководитель подразделения</b> применяет их у себя, деактивирует/включает обратно и ведёт собственные записи своего пространства.</p>'
    +'<div id="rdSubtabs" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">'
    +subs.map(([k,t])=>`<button class="adm-tab rd-sub" data-sub="${k}" onclick="rdShow('${k}')">${esc(t)}</button>`).join('')+'</div>'
    +'<div id="rdBody" style="margin-top:12px"><span class="muted">Загрузка…</span></div>';
  if(!writable){ /* участник: только просмотр — кнопки добавления скрываются */
    new MutationObserver(()=>{ card.querySelectorAll('[onclick^="rdAdd"]').forEach(b=>b.style.display='none'); })
      .observe(card, {childList:true, subtree:true});
  }
  rdShow(subs.length?subs[0][0]:'status:task');
}
