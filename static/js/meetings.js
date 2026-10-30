/* Модуль «Совещания»: карточки как renderMeetings() из прототипа, данные с сервера. */
"use strict";
const MD = JSON.parse(document.getElementById('meetData').textContent);
const MS = new Set();
const mstOf = k => MD.statuses.find(s=>s.key===k)||{label:k||'—',color:'b-gray'};
const mfmt = d => d ? d.split('-').reverse().join('.') : '—';
const mdeptName = id => (MD.departments.find(d=>d.id===id)||{}).name || '';

function tglM(el){const k=el.dataset.k;
  if(MS.has(k)){MS.delete(k);el.style.outline=''}else{MS.add(k);el.style.outline='2px solid #818cf8'}
  renderMeetCards();}

function renderMeetCards(){
  const q=(document.getElementById('mq').value||'').toLowerCase();
  let list=MD.meetings.filter(m=>{
    if(q && !(m.title+' '+(m.num||'')+' '+(m.location||'')+' '+(m.chair||'')).toLowerCase().includes(q))return false;
    if(MS.size && !MS.has(m.status_key))return false;
    return true;
  });
  document.getElementById('mempty').hidden=list.length>0;
  document.getElementById('mcards').innerHTML=list.map(m=>{
    const st=mstOf(m.status_key);
    return '<div class="card pad">'
      +'<div style="display:flex;justify-content:space-between;align-items:center"><span>'
      +'<span class="badge '+st.color+'">'+st.label+'</span></span>'
      +'<span style="color:var(--muted);font-size:12px">'+(m.num?'№ '+esc(m.num):'без номера')+'</span></div>'
      +'<h3 style="margin:11px 0 5px">'+'<a href="#" class="entlink" title="Открыть профиль" onclick="openProfile(\''+m.uid+'\');return false">'+esc(m.title||'Совещание (без темы)')+'</a></h3>'
      +'<div style="color:var(--muted);font-size:12.5px">📅 '+mfmt(m.due_date)+' '+esc(m.time||'')+' · 📍 '+esc(m.location||'—')+'</div>'
      +'<div style="margin-top:7px;font-size:12.5px;color:var(--muted)">Председатель: '+esc(m.chair||'—')
      +(m.responsibles.length?' · 👤 '+esc(m.responsibles.join(', ')):'')+'</div>'
      +(m.executor_names&&m.executor_names.length?'<div style="margin-top:4px;font-size:12.5px;color:var(--muted)">👥 Исполнители: '+esc(m.executor_names.join(', '))+'</div>':'')
      +(m.project_title?'<div style="margin-top:4px;font-size:12.5px">📁 Проект: '+esc(m.project_title)+'</div>':'')
      +'<div class="link-chips" style="margin-top:11px">'
      +'<span class="lchip">📃 повестка: '+m.agenda.length+'</span>'
      +'<span class="lchip">🎯 задач: '+m.linked_task_uids.length+'</span>'
      +'<span class="lchip">📌 решений: '+m.decisions.length+'</span>'
      +(m.cross_dept?'<span class="lchip">🔗 межпространственное</span>':'')
      +(m.perm==='read'?'<span class="badge b-amber">только чтение</span>':'')
      +'</div>'
      +'<div style="display:flex;gap:7px;margin-top:14px;flex-wrap:wrap">'
      +(m.can_edit?'<button class="btn sm primary" onclick="meetFormModal(\''+m.uid+'\')">✏️ Изменить</button>':'')
      +(m.can_share?'<button class="icobtn" title="Расшарить" onclick="shareMeet(\''+m.uid+'\')">🔗</button>':'')
      +(m.can_edit?'<button class="icobtn" onclick="delMeet(\''+m.uid+'\')">🗑</button>':'')
      +'</div></div>';
  }).join('');
}

async function cycleMeet(uid){
  const m=MD.meetings.find(x=>x.uid===uid);if(!m||!m.can_edit)return;
  const arr=MD.statuses,i=arr.findIndex(s=>s.key===m.status_key);
  const next=arr[(i+1)%arr.length].key;
  await api('/api/meetings/'+uid,{method:'PATCH',body:JSON.stringify({status_key:next})});
  m.status_key=next;toast('Статус: '+mstOf(next).label);renderMeetCards();
}

async function delMeet(uid){
  const m=MD.meetings.find(x=>x.uid===uid);
  askConfirm('Удалить совещание «'+m.title+'» вместе с повесткой и решениями? Событие будет удалено из календарей.',async()=>{
    await api('/api/meetings/'+uid,{method:'DELETE'});
    MD.meetings=MD.meetings.filter(x=>x.uid!==uid);renderMeetCards();toast('Удалено');});
}

function meetFormModal(uid){
  const m=uid?MD.meetings.find(x=>x.uid===uid):null;
  const statSel=MD.statuses.map(s=>'<option value="'+s.key+'" '+(m&&m.status_key===s.key?'selected':(!m&&s.key==='planned'?'selected':''))+'>'+esc(s.label)+'</option>').join('');
  let deptSel='';
  if(MD.departments.length&&!m){deptSel='<div><label>Подразделение</label><select name="department_id">'
    +MD.departments.map(d=>'<option value="'+d.id+'">'+esc(d.name)+'</option>').join('')+'</select></div>';}
  // Привязка к проекту (связанность категорий в рамках подключённых модулей)
  let projSel='';
  if(MD.projects&&MD.projects.length){
    projSel='<div><label>Проект (привязка)</label><select name="project_entity_uid">'
      +'<option value="">— без привязки —</option>'
      +MD.projects.map(p=>'<option value="'+p.uid+'" '+(m&&m.project_entity_uid===p.uid?'selected':'')+'>'+esc(p.title)+'</option>').join('')
      +'</select></div>';}
  else if(m&&m.project_title){projSel='<input type="hidden" name="project_entity_uid" value="'+esc(m.project_entity_uid||'')+'">';}
  // Исполнители — может быть несколько (мульти-select, как в задачах/проектах)
  let execSel='';
  if(MD.people&&MD.people.length){
    const cur=new Set(((m&&m.executors)||[]).map(e=>String(e.id)));
    execSel='<div style="grid-column:1/-1"><label>Исполнители / ответственные за поручения (можно выбрать несколько, Ctrl/Cmd+клик)</label>'
      +'<select name="executor_user_ids" id="mexec" multiple size="4" style="width:100%">'
      +MD.people.map(p=>'<option value="'+p.id+'" '+(cur.has(String(p.id))?'selected':'')+'>'+esc(p.name)+'</option>').join('')
      +'</select></div>';}
  const body='<label>Тема совещания *</label><input name="title" required value="'+esc(m?m.title:'')+'">'
    +'<div class="frow">'
    +'<div><label>Дата</label><input type="date" name="due_date" value="'+(m&&m.due_date||'')+'"></div>'
    +'<div><label>Время</label><input type="time" name="time" value="'+esc(m&&m.time||'')+'"></div>'
    +'<div><label>Номер протокола</label><input name="num" value="'+esc(m&&m.num||'')+'"></div>'
    +'</div><div class="frow">'
    +'<div><label>Место</label><input name="location" value="'+esc(m&&m.location||'')+'"></div>'
    +'<div><label>Председатель</label><input name="chair" value="'+esc(m&&m.chair||'')+'"></div>'
    +'<div><label>Секретарь</label><input name="secretary" value="'+esc(m&&m.secretary||'')+'"></div>'
    +'</div>'
    +'<label>Участники</label><textarea name="attendees" rows="2">'+esc(m&&m.attendees||'')+'</textarea>'
    +'<div class="frow"><div><label>Статус</label><select name="status_key">'+statSel+'</select></div>'+deptSel+projSel+'</div>'
    +execSel
    +'<label>Описание / комментарий</label><textarea name="description" rows="2">'+esc(m?m.description:'')+'</textarea>';
  openModal(m?'Редактирование совещания':'Новое совещание',body,async(fd,f)=>{
    if(!fd.title.trim()){toast('Укажите тему совещания');return}
    const pl={title:fd.title.trim(),description:fd.description,due_date:fd.due_date||null,
              status_key:fd.status_key,num:fd.num,chair:fd.chair,secretary:fd.secretary,
              attendees:fd.attendees,location:fd.location,time:fd.time||''};
    if(f.project_entity_uid)pl.project_entity_uid=f.project_entity_uid.value||null;
    const sel=f.querySelector('#mexec');
    if(sel)pl.executor_user_ids=Array.from(sel.selectedOptions).map(o=>+o.value);
    if(f.department_id)pl.department_id=+f.department_id.value;
    const enames=(pl.executor_user_ids||[]).map(id=>(MD.people.find(p=>p.id===id)||{}).name||'').filter(Boolean);
    const ptitle=pl.project_entity_uid?((MD.projects.find(p=>p.uid===pl.project_entity_uid)||{}).title||''):'';
    if(m){
      await api('/api/meetings/'+m.uid,{method:'PATCH',body:JSON.stringify(pl)});
      Object.assign(m,pl);if(pl.due_date)m.due_date=pl.due_date;
      if(pl.executor_user_ids)m.executor_names=enames;
      if(pl.project_entity_uid!==undefined)m.project_title=ptitle;
      toast('Сохранено · событие обновится в календарях');
    }else{
      const r=await api('/api/meetings',{method:'POST',body:JSON.stringify(pl)});
      MD.meetings.unshift(Object.assign({uid:r.uid,can_edit:true,can_share:true,responsibles:[],
        agenda:[],decisions:[],linked_task_uids:[],
        executor_names:enames,project_title:ptitle},pl));
      toast('Совещание создано');
    }
    closeModal();renderMeetCards();
  });
}

async function shareMeet(uid){
  // Цели шеринга — ВСЕ активные подразделения (ядро), а не только пространства пользователя.
  let depts=[];try{depts=await api('/api/core/departments/for-share')}catch(e){depts=MD.departments||[]}
  if(!depts.length){toast('Нет подразделений');return}
  const body='<label>Подразделение</label><select name="target_department_id">'
    +depts.map(d=>'<option value="'+d.id+'">'+esc(d.name)+'</option>').join('')+'</select>'
    +'<label>Права принимающей стороны</label><select name="permission">'
    +'<option value="read">Только чтение</option><option value="edit">Редактирование</option></select>';
  openModal('Шеринг совещания',body,async(fd)=>{
    await api('/api/core/entities/'+uid+'/shares',{method:'POST',
      body:JSON.stringify({target_department_id:+fd.target_department_id,permission:fd.permission})});
    closeModal();toast('Расшарено · у принимающего открыт слот ответственного');
  });
}

document.addEventListener('DOMContentLoaded',renderMeetCards);
