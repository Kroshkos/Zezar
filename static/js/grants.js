/* Модуль «Гранты»: карточки как renderGrants() из прототипа, данные с сервера. */
"use strict";
const GD = JSON.parse(document.getElementById('grantData').textContent);
const GS = new Set();
const gstOf = k => GD.statuses.find(s=>s.key===k)||{label:k||'—',color:'b-gray'};
const gfmt = d => d ? d.split('-').reverse().join('.') : '—';

function tglG(el){const k=el.dataset.k;
  if(GS.has(k)){GS.delete(k);el.style.outline=''}else{GS.add(k);el.style.outline='2px solid #818cf8'}
  renderGrantCards();}

function renderGrantCards(){
  const q=(document.getElementById('gq').value||'').toLowerCase();
  let list=GD.grants.filter(g=>{
    if(q && !(g.title+' '+(g.program||'')).toLowerCase().includes(q))return false;
    if(GS.size && !GS.has(g.status_key))return false;
    return true;
  });
  document.getElementById('gempty').hidden=list.length>0;
  document.getElementById('gcards').innerHTML=list.map(g=>{
    const st=gstOf(g.status_key);
    return '<div class="card pad">'
      +'<div style="display:flex;justify-content:space-between;align-items:center"><span>'
      +'<span class="badge '+st.color+'">'+st.label+'</span></span>'
      +'<span style="color:var(--muted);font-size:12px">'+esc(g.amount||'')+'</span></div>'
      +'<h3 style="margin:11px 0 5px">'+'<a href="#" class="entlink" title="Открыть профиль" onclick="openProfile(\''+g.uid+'\');return false">'+esc(g.title||'Грант')+'</a></h3>'
      +'<div style="color:var(--muted);font-size:12.5px">🏦 '+esc(g.program||'—')
      +' · 📅 дедлайн '+gfmt(g.due_date)
      +(g.submitted_at?' · подан '+gfmt(g.submitted_at):'')+'</div>'
      +(g.executor_names&&g.executor_names.length?'<div style="margin-top:7px;font-size:12.5px;color:var(--muted)">👥 '+esc(g.executor_names.join(', '))+'</div>':'')
      +(g.responsibles.length&&!g.executor_names?'<div style="margin-top:7px;font-size:12.5px;color:var(--muted)">👤 '+esc(g.responsibles.join(', '))+'</div>':'')
      +'<div class="link-chips" style="margin-top:11px">'
      +(g.project_title?'<span class="lchip">🗂 проект: <b>'+esc(g.project_title)+'</b></span>':'')
      +(g.cross_dept?'<span class="lchip">🔗 межпространственный</span>':'')
      +(g.perm==='read'?'<span class="badge b-amber">только чтение</span>':'')
      +'</div>'
      +'<div style="display:flex;gap:7px;margin-top:14px;flex-wrap:wrap">'
      +(g.can_edit?'<button class="btn sm primary" onclick="grantFormModal(\''+g.uid+'\')">✏️ Изменить</button>':'')
      +(g.can_share?'<button class="icobtn" title="Расшарить" onclick="shareGrant(\''+g.uid+'\')">🔗</button>':'')
      +(g.can_edit?'<button class="icobtn" onclick="delGrant(\''+g.uid+'\')">🗑</button>':'')
      +'</div></div>';
  }).join('');
}

async function delGrant(uid){
  const g=GD.grants.find(x=>x.uid===uid);
  askConfirm('Удалить грант «'+g.title+'»? Событие будет удалено из календарей.',async()=>{
    await api('/api/grants/'+uid,{method:'DELETE'});
    GD.grants=GD.grants.filter(x=>x.uid!==uid);renderGrantCards();toast('Удалено');});
}

function grantFormModal(uid){
  const g=uid?GD.grants.find(x=>x.uid===uid):null;
  const statSel=GD.statuses.map(s=>'<option value="'+s.key+'" '+(g&&g.status_key===s.key?'selected':(!g&&s.key==='prep'?'selected':''))+'>'+esc(s.label)+'</option>').join('');
  let deptSel='';
  if(GD.departments.length&&!g){deptSel='<div><label>Подразделение</label><select name="department_id">'
    +GD.departments.map(d=>'<option value="'+d.id+'">'+esc(d.name)+'</option>').join('')+'</select></div>';}
  // Привязка к проекту (связанность категорий); варианты — доступные проекты
  let projSel='';
  if(GD.projects&&GD.projects.length){
    projSel='<div><label>Проект (привязка)</label><select name="project_entity_uid">'
      +'<option value="">— без привязки —</option>'
      +GD.projects.map(p=>'<option value="'+p.uid+'" '+(g&&g.project_entity_uid===p.uid?'selected':'')+'>'+esc(p.title)+'</option>').join('')
      +'</select></div>';}
  else if(g&&g.project_title){projSel='<input type="hidden" name="project_entity_uid" value="'+esc(g.project_entity_uid||'')+'">';}
  // Исполнители — может быть несколько (мульти-select, как в задачах/проектах)
  let execSel='';
  if(GD.people&&GD.people.length){
    const cur=new Set(((g&&g.executors)||[]).map(e=>String(e.id)));
    execSel='<div style="grid-column:1/-1"><label>Исполнители (можно выбрать несколько, Ctrl/Cmd+клик)</label>'
      +'<select name="executor_user_ids" id="gexec" multiple size="4" style="width:100%">'
      +GD.people.map(p=>'<option value="'+p.id+'" '+(cur.has(String(p.id))?'selected':'')+'>'+esc(p.name)+'</option>').join('')
      +'</select></div>';}
  const body='<label>Тема заявки / проекта *</label><input name="title" required value="'+esc(g?g.title:'')+'">'
    +'<div class="frow">'
    +'<div><label>Программа / фонд</label><input name="program" value="'+esc(g&&g.program||'')+'"></div>'
    +'<div><label>Сумма</label><input name="amount" value="'+esc(g&&g.amount||'')+'"></div>'
    +'<div><label>Статус</label><select name="status_key">'+statSel+'</select></div>'
    +'</div><div class="frow">'
    +'<div><label>Дедлайн (календарь)</label><input type="date" name="due_date" value="'+(g&&g.due_date||'')+'"></div>'
    +'<div><label>Дата подачи</label><input type="date" name="submitted_at" value="'+esc(g&&g.submitted_at||'')+'"></div>'
    +'</div><div class="frow">'+deptSel+projSel+'</div>'
    +execSel
    +'<label>Описание</label><textarea name="description" rows="2">'+esc(g?g.description:'')+'</textarea>';
  openModal(g?'Редактирование гранта':'Новый грант',body,async(fd,f)=>{
    if(!fd.title.trim()){toast('Укажите тему');return}
    const pl={title:fd.title.trim(),description:fd.description,due_date:fd.due_date||null,
              status_key:fd.status_key,program:fd.program,amount:fd.amount,
              submitted_at:fd.submitted_at||null};
    if(f.project_entity_uid)pl.project_entity_uid=f.project_entity_uid.value||null;
    const sel=f.querySelector('#gexec');
    if(sel)pl.executor_user_ids=Array.from(sel.selectedOptions).map(o=>+o.value);
    if(f.department_id)pl.department_id=+f.department_id.value;
    const enames=(pl.executor_user_ids||[]).map(id=>(GD.people.find(p=>p.id===id)||{}).name||'').filter(Boolean);
    const ptitle=pl.project_entity_uid?((GD.projects.find(p=>p.uid===pl.project_entity_uid)||{}).title||''):'';
    if(g){
      await api('/api/grants/'+g.uid,{method:'PATCH',body:JSON.stringify(pl)});
      Object.assign(g,pl);
      if(pl.executor_user_ids)g.executor_names=enames;
      if(pl.project_entity_uid!==undefined)g.project_title=ptitle;
      toast('Сохранено · событие обновится в календарях');
    }else{
      const r=await api('/api/grants',{method:'POST',body:JSON.stringify(pl)});
      GD.grants.unshift(Object.assign({uid:r.uid,can_edit:true,can_share:true,responsibles:[],
        executor_names:enames,project_title:ptitle},pl));
      toast('Грант создан');
    }
    closeModal();renderGrantCards();
  });
}

async function shareGrant(uid){
  if(!GD.departments.length){toast('Нет подразделений');return}
  const body='<label>Подразделение</label><select name="target_department_id">'
    +GD.departments.map(d=>'<option value="'+d.id+'">'+esc(d.name)+'</option>').join('')+'</select>'
    +'<label>Права принимающей стороны</label><select name="permission">'
    +'<option value="read">Только чтение</option><option value="edit">Редактирование</option></select>';
  openModal('Шеринг гранта',body,async(fd)=>{
    await api('/api/core/entities/'+uid+'/shares',{method:'POST',
      body:JSON.stringify({target_department_id:+fd.target_department_id,permission:fd.permission})});
    closeModal();toast('Расшарено · у принимающего открыт слот ответственного');
  });
}

document.addEventListener('DOMContentLoaded',renderGrantCards);
