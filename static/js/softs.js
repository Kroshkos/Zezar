/* Модуль «ПО и разработки»: карточки как renderSofts() из прототипа, данные с сервера. */
"use strict";
const SD = JSON.parse(document.getElementById('softData').textContent);
const SS = new Set();
const sstOf = k => SD.statuses.find(s=>s.key===k)||{label:k||'—',color:'b-gray'};
const sfmt = d => d ? d.split('-').reverse().join('.') : '—';

function tglS(el){const k=el.dataset.k;
  if(SS.has(k)){SS.delete(k);el.style.outline=''}else{SS.add(k);el.style.outline='2px solid #818cf8'}
  renderSoftCards();}

function renderSoftCards(){
  const q=(document.getElementById('sq').value||'').toLowerCase();
  let list=SD.softs.filter(s=>{
    if(q && !(s.title+' '+(s.soft_type||'')+' '+(s.env||'')).toLowerCase().includes(q))return false;
    if(SS.size && !SS.has(s.status_key))return false;
    return true;
  });
  document.getElementById('sempty').hidden=list.length>0;
  document.getElementById('scards').innerHTML=list.map(s=>{
    const st=sstOf(s.status_key);
    return '<div class="card pad">'
      +'<div style="display:flex;justify-content:space-between;align-items:center"><span>'
      +'<span class="badge '+st.color+'">'+st.label+'</span></span>'
      +'<span style="color:var(--muted);font-size:12px">'+esc(s.soft_type||'—')+'</span></div>'
      +'<h3 style="margin:11px 0 5px">'+'<a href="#" class="entlink" title="Открыть профиль" onclick="openProfile(\''+s.uid+'\');return false">'+esc(s.title||'Разработка')+'</a></h3>'
      +'<div style="color:var(--muted);font-size:12.5px">📅 '+sfmt(s.due_date)
      +(s.env?' · 🖥 '+esc(s.env):'')+'</div>'
      +(s.executor_names&&s.executor_names.length?'<div style="margin-top:7px;font-size:12.5px;color:var(--muted)">👥 '+esc(s.executor_names.join(', '))+'</div>':'')
      +(!s.executor_names&&s.responsibles.length?'<div style="margin-top:7px;font-size:12.5px;color:var(--muted)">👤 '+esc(s.responsibles.join(', '))+'</div>':'')
      +(s.stage_total?'<div style="margin-top:7px;font-size:12.5px;color:var(--muted)">🧭 Маршрут: '+s.stage_done+'/'+s.stage_total+'</div>':'')
      +'<div class="link-chips" style="margin-top:11px">'
      +(s.project_title?'<span class="lchip">📁 '+esc(s.project_title)+'</span>':'')
      +(s.can_edit?'<button type="button" class="lchip" title="Редактировать маршрут (workflow)" onclick="openWfEditorGeneric(\'softs\',\''+s.uid+'\')">🖐 '+esc('Маршрут')+'</button>':'')
      +(s.repo_url?'<a class="lchip" href="'+esc(s.repo_url)+'" target="_blank" rel="noopener">🗂 репозиторий</a>':'')
      +(s.cross_dept?'<span class="lchip">🔗 межпространственное</span>':'')
      +(s.perm==='read'?'<span class="badge b-amber">только чтение</span>':'')
      +'</div>'
      +'<div style="display:flex;gap:7px;margin-top:14px;flex-wrap:wrap">'
      +(s.can_edit?'<button class="btn sm primary" onclick="softFormModal(\''+s.uid+'\')">✏️ Изменить</button>':'')
      +(s.can_share?'<button class="icobtn" title="Расшарить" onclick="shareSoft(\''+s.uid+'\')">🔗</button>':'')
      +(s.can_edit?'<button class="icobtn" onclick="delSoft(\''+s.uid+'\')">🗑</button>':'')
      +'</div></div>';
  }).join('');
}

async function delSoft(uid){
  const s=SD.softs.find(x=>x.uid===uid);
  askConfirm('Удалить запись «'+s.title+'»? Событие будет удалено из календарей.',async()=>{
    await api('/api/softs/'+uid,{method:'DELETE'});
    SD.softs=SD.softs.filter(x=>x.uid!==uid);renderSoftCards();toast('Удалено');});
}

function softFormModal(uid){
  const s=uid?SD.softs.find(x=>x.uid===uid):null;
  const statSel=SD.statuses.map(x=>'<option value="'+x.key+'" '+(s&&s.status_key===x.key?'selected':(!s&&x.key==='backlog'?'selected':''))+'>'+esc(x.label)+'</option>').join('');
  let deptSel='';
  if(SD.departments.length&&!s){deptSel='<div><label>Подразделение</label><select name="department_id">'
    +SD.departments.map(d=>'<option value="'+d.id+'">'+esc(d.name)+'</option>').join('')+'</select></div>';}
  // Тип ПО — из иерархического справочника (админ + подразделение)
  let typeField='';
  if(SD.soft_types&&SD.soft_types.length){
    typeField='<div><label>Тип ПО</label><select name="soft_type">'
      +'<option value="">— не указан —</option>'
      +SD.soft_types.map(t=>'<option value="'+esc(t.label)+'" '+((s&&s.soft_type===t.label)?'selected':'')+'>'+esc(t.label)+'</option>').join('')
      +'</select></div>';
  } else {
    typeField='<div><label>Тип ПО</label><input name="soft_type" value="'+esc(s&&s.soft_type||'')+'"></div>';
  }
  // Привязка к проекту (связанность категорий в рамках подключённых модулей)
  let projSel='';
  if(SD.projects&&SD.projects.length){
    projSel='<div><label>Проект (привязка)</label><select name="project_entity_uid">'
      +'<option value="">— без привязки —</option>'
      +SD.projects.map(p=>'<option value="'+p.uid+'" '+(s&&s.project_entity_uid===p.uid?'selected':'')+'>'+esc(p.title)+'</option>').join('')
      +'</select></div>';}
  else if(s&&s.project_title){projSel='<input type="hidden" name="project_entity_uid" value="'+esc(s.project_entity_uid||'')+'">';}
  // Ответственный — один сотрудник из справочника пользователей (как в задачах);
  // при создании по умолчанию выбирается текущий пользователь.
  let respSel='';
  if(SD.people&&SD.people.length){
    const curId=s?(s.responsible_user_id||null):(SD.me||null);
    respSel='<div><label>Ответственный</label><select name="responsible_user_id">'
      +SD.people.map(p=>'<option value="'+p.id+'" '+(String(p.id)===String(curId)?'selected':'')+'>'+esc(p.name)+'</option>').join('')
      +'</select></div>';}
  // Исполнители — может быть несколько (мульти-select, как в задачах/проектах)
  let execSel='';
  if(SD.people&&SD.people.length){
    const cur=new Set(((s&&s.executors)||[]).map(e=>String(e.id)));
    execSel='<div style="grid-column:1/-1"><label>Исполнители (можно выбрать несколько, Ctrl/Cmd+клик)</label>'
      +'<select name="executor_user_ids" id="sexec" multiple size="4" style="width:100%">'
      +SD.people.map(p=>'<option value="'+p.id+'" '+(cur.has(String(p.id))?'selected':'')+'>'+esc(p.name)+'</option>').join('')
      +'</select></div>';}
  const body='<label>Название *</label><input name="title" required value="'+esc(s?s.title:'')+'">'
    +'<div class="frow">'
    +'<div><label>Дата (план/релиз)</label><input type="date" name="due_date" value="'+(s&&s.due_date||'')+'"></div>'
    +typeField
    +'<div><label>Окружение/стенд</label><input name="env" value="'+esc(s&&s.env||'')+'"></div>'
    +'</div><div class="frow">'
    +'<div><label>Репозиторий</label><input name="repo_url" placeholder="https://…" value="'+esc(s&&s.repo_url||'')+'"></div>'
    +'<div><label>Статус</label><select name="status_key">'+statSel+'</select></div>'
    +'</div><div class="frow">'+deptSel+projSel+'</div>'
    +(respSel?'<div class="frow">'+respSel+'</div>':'')
    +execSel
    +'<label>Описание</label><textarea name="description" rows="2">'+esc(s?s.description:'')+'</textarea>';
  openModal(s?'Редактирование записи':'Новая разработка',body,async(fd,f)=>{
    if(!fd.title.trim()){toast('Укажите название');return}
    const pl={title:fd.title.trim(),description:fd.description,due_date:fd.due_date||null,
              status_key:fd.status_key,soft_type:fd.soft_type,env:fd.env,repo_url:fd.repo_url};
    if(f.project_entity_uid)pl.project_entity_uid=f.project_entity_uid.value||null;
    const sel=f.querySelector('#sexec');
    if(sel)pl.executor_user_ids=Array.from(sel.selectedOptions).map(o=>+o.value);
    if(f.responsible_user_id&&f.responsible_user_id.value)pl.responsible_user_id=+f.responsible_user_id.value;
    if(f.department_id)pl.department_id=+f.department_id.value;
    const enames=(pl.executor_user_ids||[]).map(id=>(SD.people.find(p=>p.id===id)||{}).name||'').filter(Boolean);
    const rname=pl.responsible_user_id?((SD.people.find(p=>p.id===pl.responsible_user_id)||{}).name||''):'';
    const ptitle=pl.project_entity_uid?((SD.projects.find(p=>p.uid===pl.project_entity_uid)||{}).title||''):'';
    if(s){
      await api('/api/softs/'+s.uid,{method:'PATCH',body:JSON.stringify(pl)});
      Object.assign(s,pl);
      if(pl.executor_user_ids)s.executor_names=enames;
      if(pl.responsible_user_id!==undefined&&rname)s.responsibles=[rname];
      if(pl.project_entity_uid!==undefined)s.project_title=ptitle;
      toast('Сохранено · событие обновится в календарях');
    }else{
      const r=await api('/api/softs',{method:'POST',body:JSON.stringify(pl)});
      SD.softs.unshift(Object.assign({uid:r.uid,can_edit:true,can_share:true,
        responsibles:rname?[rname]:[],stages:[],
        executor_names:enames,project_title:ptitle},pl));
      toast('Запись создана');
    }
    closeModal();renderSoftCards();
  });
}

async function shareSoft(uid){
  if(!SD.departments.length){toast('Нет подразделений');return}
  const body='<label>Подразделение</label><select name="target_department_id">'
    +SD.departments.map(d=>'<option value="'+d.id+'">'+esc(d.name)+'</option>').join('')+'</select>'
    +'<label>Права принимающей стороны</label><select name="permission">'
    +'<option value="read">Только чтение</option><option value="edit">Редактирование</option></select>';
  openModal('Шеринг записи',body,async(fd)=>{
    await api('/api/core/entities/'+uid+'/shares',{method:'POST',
      body:JSON.stringify({target_department_id:+fd.target_department_id,permission:fd.permission})});
    closeModal();toast('Расшарено · у принимающего открыт слот ответственного');
  });
}

document.addEventListener('DOMContentLoaded',renderSoftCards);
