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
      +'<h3 style="margin:11px 0 5px">'+esc(m.title||'Совещание (без темы)')+'</h3>'
      +'<div style="color:var(--muted);font-size:12.5px">📅 '+mfmt(m.due_date)+' '+esc(m.time||'')+' · 📍 '+esc(m.location||'—')+'</div>'
      +'<div style="margin-top:7px;font-size:12.5px;color:var(--muted)">Председатель: '+esc(m.chair||'—')
      +(m.responsibles.length?' · 👤 '+esc(m.responsibles.join(', ')):'')+'</div>'
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
    +'<div class="frow"><div><label>Статус</label><select name="status_key">'+statSel+'</select></div>'+deptSel+'</div>'
    +'<label>Описание / комментарий</label><textarea name="description" rows="2">'+esc(m?m.description:'')+'</textarea>';
  openModal(m?'Редактирование совещания':'Новое совещание',body,async(fd,f)=>{
    if(!fd.title.trim()){toast('Укажите тему совещания');return}
    const pl={title:fd.title.trim(),description:fd.description,due_date:fd.due_date||null,
              status_key:fd.status_key,num:fd.num,chair:fd.chair,secretary:fd.secretary,
              attendees:fd.attendees,location:fd.location,time:fd.time||''};
    if(f.department_id)pl.department_id=+f.department_id.value;
    if(m){
      await api('/api/meetings/'+m.uid,{method:'PATCH',body:JSON.stringify(pl)});
      Object.assign(m,pl);if(pl.due_date)m.due_date=pl.due_date;
      toast('Сохранено · событие обновится в календарях');
    }else{
      const r=await api('/api/meetings',{method:'POST',body:JSON.stringify(pl)});
      MD.meetings.unshift(Object.assign({uid:r.uid,can_edit:true,can_share:true,responsibles:[],
        agenda:[],decisions:[],linked_task_uids:[]},pl));
      toast('Совещание создано');
    }
    closeModal();renderMeetCards();
  });
}

async function shareMeet(uid){
  if(!MD.departments.length){toast('Нет подразделений');return}
  const body='<label>Подразделение</label><select name="target_department_id">'
    +MD.departments.map(d=>'<option value="'+d.id+'">'+esc(d.name)+'</option>').join('')+'</select>'
    +'<label>Права принимающей стороны</label><select name="permission">'
    +'<option value="read">Только чтение</option><option value="edit">Редактирование</option></select>';
  openModal('Шеринг совещания',body,async(fd)=>{
    await api('/api/core/entities/'+uid+'/shares',{method:'POST',
      body:JSON.stringify({target_department_id:+fd.target_department_id,permission:fd.permission})});
    closeModal();toast('Расшарено · у принимающего открыт слот ответственного');
  });
}

document.addEventListener('DOMContentLoaded',renderMeetCards);
