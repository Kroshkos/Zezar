/* Модуль «Документы»: карточки как renderDocs() из прототипа, данные с сервера. */
"use strict";
const DD = JSON.parse(document.getElementById('docData').textContent);
const DS = new Set();
const dstOf = k => DD.statuses.find(s=>s.key===k)||{label:k||'—',color:'b-gray'};
const dfmt = d => d ? d.split('-').reverse().join('.') : '—';

function tglD(el){const k=el.dataset.k;
  if(DS.has(k)){DS.delete(k);el.style.outline=''}else{DS.add(k);el.style.outline='2px solid #818cf8'}
  renderDocCards();}

function renderDocCards(){
  const q=(document.getElementById('dq').value||'').toLowerCase();
  let list=DD.docs.filter(d=>{
    if(q && !(d.title+' '+(d.number||'')+' '+(d.doc_type||'')).toLowerCase().includes(q))return false;
    if(DS.size && !DS.has(d.status_key))return false;
    return true;
  });
  document.getElementById('dempty').hidden=list.length>0;
  document.getElementById('dcards').innerHTML=list.map(d=>{
    const st=dstOf(d.status_key);
    return '<div class="card pad">'
      +'<div style="display:flex;justify-content:space-between;align-items:center"><span>'
      +'<span class="badge '+st.color+'">'+st.label+'</span></span>'
      +'<span style="color:var(--muted);font-size:12px">'+(d.number?'№ '+esc(d.number):'без номера')+'</span></div>'
      +'<h3 style="margin:11px 0 5px">'+'<a href="#" class="entlink" title="Открыть профиль" onclick="openProfile(\''+d.uid+'\');return false">'+esc(d.title||'Документ')+'</a></h3>'
      +'<div style="color:var(--muted);font-size:12.5px">📅 '+dfmt(d.due_date)
      +(d.doc_type?' · 📑 '+esc(d.doc_type):'')
      +(d.issued_at?' · выдан '+dfmt(d.issued_at):'')+'</div>'
      +(d.executor_names&&d.executor_names.length?'<div style="margin-top:7px;font-size:12.5px;color:var(--muted)">👥 '+esc(d.executor_names.join(', '))+'</div>':'')
      +(d.responsibles.length&&!d.executor_names?'<div style="margin-top:7px;font-size:12.5px;color:var(--muted)">👤 '+esc(d.responsibles.join(', '))+'</div>':'')
      +(d.stage_total?'<div style="margin-top:7px;font-size:12.5px;color:var(--muted)">🧭 Маршрут: '+d.stage_done+'/'+d.stage_total+'</div>':'')
      +'<div class="link-chips" style="margin-top:11px">'
      +(d.project_title?'<span class="lchip">🗂 проект: <b>'+esc(d.project_title)+'</b></span>':'')
      +(d.can_edit?'<button type="button" class="lchip" title="Редактировать маршрут (workflow)" onclick="openWfEditorGeneric(\'docs\',\''+d.uid+'\')">🖐 '+esc('Маршрут')+'</button>':'')
      +(d.file_ref?'<a class="lchip" href="'+esc(d.file_ref)+'" target="_blank" rel="noopener">📎 файл</a>':'')
      +(d.cross_dept?'<span class="lchip">🔗 межпространственный</span>':'')
      +(d.perm==='read'?'<span class="badge b-amber">только чтение</span>':'')
      +'</div>'
      +'<div style="display:flex;gap:7px;margin-top:14px;flex-wrap:wrap">'
      +(d.can_edit?'<button class="btn sm primary" onclick="docFormModal(\''+d.uid+'\')">✏️ Изменить</button>':'')
      +(d.can_share?'<button class="icobtn" title="Расшарить" onclick="shareDoc(\''+d.uid+'\')">🔗</button>':'')
      +(d.can_edit?'<button class="icobtn" onclick="delDoc(\''+d.uid+'\')">🗑</button>':'')
      +'</div></div>';
  }).join('');
}

async function delDoc(uid){
  const d=DD.docs.find(x=>x.uid===uid);
  askConfirm('Удалить документ «'+d.title+'»? Событие будет удалено из календарей.',async()=>{
    await api('/api/docs/'+uid,{method:'DELETE'});
    DD.docs=DD.docs.filter(x=>x.uid!==uid);renderDocCards();toast('Удалено');});
}

function docFormModal(uid){
  const d=uid?DD.docs.find(x=>x.uid===uid):null;
  const statSel=DD.statuses.map(s=>'<option value="'+s.key+'" '+(d&&d.status_key===s.key?'selected':(!d&&s.key==='draft'?'selected':''))+'>'+esc(s.label)+'</option>').join('');
  let deptSel='';
  if(DD.departments.length&&!d){deptSel='<div><label>Подразделение</label><select name="department_id">'
    +DD.departments.map(x=>'<option value="'+x.id+'">'+esc(x.name)+'</option>').join('')+'</select></div>';}
  // Привязка к проекту (связанность категорий); варианты берутся из доступных проектов
  let projSel='';
  if(DD.projects&&DD.projects.length){
    projSel='<div><label>Проект (привязка)</label><select name="project_entity_uid">'
      +'<option value="">— без привязки —</option>'
      +DD.projects.map(p=>'<option value="'+p.uid+'" '+(d&&d.project_entity_uid===p.uid?'selected':'')+'>'+esc(p.title)+'</option>').join('')
      +'</select></div>';}
  else if(d&&d.project_title){projSel='<input type="hidden" name="project_entity_uid" value="'+esc(d.project_entity_uid||'')+'">';}
  // Исполнители — может быть несколько (мульти-select, как в задачах/проектах)
  let execSel='';
  if(DD.people&&DD.people.length){
    const cur=new Set(((d&&d.executors)||[]).map(e=>String(e.id)));
    execSel='<div style="grid-column:1/-1"><label>Исполнители (можно выбрать несколько, Ctrl/Cmd+клик)</label>'
      +'<select name="executor_user_ids" id="dexec" multiple size="4" style="width:100%">'
      +DD.people.map(p=>'<option value="'+p.id+'" '+(cur.has(String(p.id))?'selected':'')+'>'+esc(p.name)+'</option>').join('')
      +'</select></div>';}
  // Ответственный — один сотрудник из выпадающего списка (при создании по умолчанию — текущий пользователь)
  let respSel='';
  if(DD.people&&DD.people.length){
    const curId=d?(d.responsible_user_id||null):(DD.me||null);
    respSel='<div><label>Ответственный</label><select name="responsible_user_id">'
      +DD.people.map(p=>'<option value="'+p.id+'" '+(String(p.id)===String(curId)?'selected':'')+'>'+esc(p.name)+'</option>').join('')
      +'</select></div>';}
  const dtList=(DD.doc_types||[]).length
    ? '<div><label>Вид документа</label><select name="doc_type"><option value="">—</option>'
      +DD.doc_types.map(t=>'<option value="'+esc(t.label)+'" '+((d&&d.doc_type===t.label)?'selected':'')+'>'+esc(t.label)+'</option>').join('')
      +'</select></div>'
    : '<div><label>Вид документа</label><input name="doc_type" value="'+esc(d&&d.doc_type||'')+'"></div>';
  const body='<label>Название документа *</label><input name="title" required value="'+esc(d?d.title:'')+'">'
    +'<div class="frow">'
    +'<div><label>Дедлайн (календарь)</label><input type="date" name="due_date" value="'+(d&&d.due_date||'')+'"></div>'
    +dtList
    +'<div><label>Статус</label><select name="status_key">'+statSel+'</select></div>'
    +'</div><div class="frow">'
    +'<div><label>Ссылка на файл</label><input name="file_ref" placeholder="https://…" value="'+esc(d&&d.file_ref||'')+'"></div>'
    +(respSel||'<div></div>')
    +'</div><div class="frow">'+deptSel+projSel+'</div>'
    +execSel
    +'<label>Описание / комментарий</label><textarea name="description" rows="2">'+esc(d?d.description:'')+'</textarea>';
  openModal(d?'Редактирование документа':'Новый документ',body,async(fd,f)=>{
    if(!fd.title.trim()){toast('Укажите название документа');return}
    const pl={title:fd.title.trim(),description:fd.description,due_date:fd.due_date||null,
              status_key:fd.status_key,doc_type:fd.doc_type,file_ref:fd.file_ref};
    if(f.project_entity_uid)pl.project_entity_uid=f.project_entity_uid.value||null;
    if(f.responsible_user_id&&f.responsible_user_id.value)pl.responsible_user_id=+f.responsible_user_id.value;
    const sel=f.querySelector('#dexec');
    if(sel)pl.executor_user_ids=Array.from(sel.selectedOptions).map(o=>+o.value);
    if(f.department_id)pl.department_id=+f.department_id.value;
    const enames=(pl.executor_user_ids||[]).map(id=>(DD.people.find(p=>p.id===id)||{}).name||'').filter(Boolean);
    const rname=pl.responsible_user_id?((DD.people.find(p=>p.id===pl.responsible_user_id)||{}).name||''):'';
    const ptitle=pl.project_entity_uid?((DD.projects.find(p=>p.uid===pl.project_entity_uid)||{}).title||''):'';
    if(d){
      await api('/api/docs/'+d.uid,{method:'PATCH',body:JSON.stringify(pl)});
      Object.assign(d,pl);
      if(pl.executor_user_ids)d.executor_names=enames;
      if(pl.responsible_user_id&&rname)d.responsibles=[rname];
      if(pl.project_entity_uid!==undefined)d.project_title=ptitle;
      toast('Сохранено · событие обновится в календарях');
    }else{
      const r=await api('/api/docs',{method:'POST',body:JSON.stringify(pl)});
      DD.docs.unshift(Object.assign({uid:r.uid,can_edit:true,can_share:true,
        responsibles:rname?[rname]:[],executor_names:enames,project_title:ptitle},pl));
      toast('Документ создан');
    }
    closeModal();renderDocCards();
  });
}

async function shareDoc(uid){
  if(!DD.departments.length){toast('Нет подразделений');return}
  const body='<label>Подразделение</label><select name="target_department_id">'
    +DD.departments.map(d=>'<option value="'+d.id+'">'+esc(d.name)+'</option>').join('')+'</select>'
    +'<label>Права принимающей стороны</label><select name="permission">'
    +'<option value="read">Только чтение</option><option value="edit">Редактирование</option></select>';
  openModal('Шеринг документа',body,async(fd)=>{
    await api('/api/core/entities/'+uid+'/shares',{method:'POST',
      body:JSON.stringify({target_department_id:+fd.target_department_id,permission:fd.permission})});
    closeModal();toast('Расшарено · у принимающего открыт слот ответственного');
  });
}

document.addEventListener('DOMContentLoaded',renderDocCards);
