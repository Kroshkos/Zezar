/* Модуль «Стипендии»: карточки как renderSchols() из прототипа, данные с сервера. */
"use strict";
const XD = JSON.parse(document.getElementById('scholData').textContent);
const XS = new Set();
const xstOf = k => XD.statuses.find(s=>s.key===k)||{label:k||'—',color:'b-gray'};
const xfmt = d => d ? d.split('-').reverse().join('.') : '—';

function tglX(el){const k=el.dataset.k;
  if(XS.has(k)){XS.delete(k);el.style.outline=''}else{XS.add(k);el.style.outline='2px solid #818cf8'}
  renderScholCards();}

function renderScholCards(){
  const q=(document.getElementById('xq').value||'').toLowerCase();
  let list=XD.schols.filter(x=>{
    if(q && !(x.title+' '+(x.person_name||'')+' '+(x.schol_type||'')).toLowerCase().includes(q))return false;
    if(XS.size && !XS.has(x.status_key))return false;
    return true;
  });
  document.getElementById('xempty').hidden=list.length>0;
  document.getElementById('xcards').innerHTML=list.map(x=>{
    const st=xstOf(x.status_key);
    return '<div class="card pad">'
      +'<div style="display:flex;justify-content:space-between;align-items:center"><span>'
      +'<span class="badge '+st.color+'">'+st.label+'</span></span>'
      +'<span style="color:var(--muted);font-size:12px">'+esc(x.amount||'')+'</span></div>'
      +'<h3 style="margin:11px 0 5px">'+'<a href="#" class="entlink" title="Открыть профиль" onclick="openProfile(\''+x.uid+'\');return false">'+esc(x.title||'Стипендия')+'</a></h3>'
      +'<div style="color:var(--muted);font-size:12.5px">👨‍🎓 '+esc(x.person_name||'—')
      +' · 🏅 '+esc(x.schol_type||'—')+' · 📅 '+xfmt(x.due_date)+'</div>'
      +(x.executor_names&&x.executor_names.length?'<div style="margin-top:7px;font-size:12.5px;color:var(--muted)">👥 '+esc(x.executor_names.join(', '))+'</div>':'')
      +(!x.executor_names&&x.responsibles.length?'<div style="margin-top:7px;font-size:12.5px;color:var(--muted)">👤 '+esc(x.responsibles.join(', '))+'</div>':'')
      +'<div class="link-chips" style="margin-top:11px">'
      +(x.project_title?'<span class="lchip">📁 '+esc(x.project_title)+'</span>':'')
      +(x.cross_dept?'<span class="lchip">🔗 межпространственная</span>':'')
      +(x.perm==='read'?'<span class="badge b-amber">только чтение</span>':'')
      +'</div>'
      +'<div style="display:flex;gap:7px;margin-top:14px;flex-wrap:wrap">'
      +(x.can_edit?'<button class="btn sm primary" onclick="scholFormModal(\''+x.uid+'\')">✏️ Изменить</button>':'')
      +(x.can_share?'<button class="icobtn" title="Расшарить" onclick="shareSchol(\''+x.uid+'\')">🔗</button>':'')
      +(x.can_edit?'<button class="icobtn" onclick="delSchol(\''+x.uid+'\')">🗑</button>':'')
      +'</div></div>';
  }).join('');
}

async function delSchol(uid){
  const x=XD.schols.find(e=>e.uid===uid);
  askConfirm('Удалить назначение «'+x.title+'»? Событие будет удалено из календарей.',async()=>{
    await api('/api/schols/'+uid,{method:'DELETE'});
    XD.schols=XD.schols.filter(e=>e.uid!==uid);renderScholCards();toast('Удалено');});
}

function scholFormModal(uid){
  const x=uid?XD.schols.find(e=>e.uid===uid):null;
  const statSel=XD.statuses.map(s=>'<option value="'+s.key+'" '+(x&&x.status_key===s.key?'selected':(!x&&s.key==='nomination'?'selected':''))+'>'+esc(s.label)+'</option>').join('');
  let deptSel='';
  if(XD.departments.length&&!x){deptSel='<div><label>Подразделение</label><select name="department_id">'
    +XD.departments.map(d=>'<option value="'+d.id+'">'+esc(d.name)+'</option>').join('')+'</select></div>';}
  // Вид стипендии — из иерархического справочника (админ + подразделение)
  let typeField='';
  if(XD.schol_types&&XD.schol_types.length){
    typeField='<div><label>Вид стипендии</label><select name="schol_type">'
      +'<option value="">— не указан —</option>'
      +XD.schol_types.map(t=>'<option value="'+esc(t.label)+'" '+((x&&x.schol_type===t.label)?'selected':'')+'>'+esc(t.label)+'</option>').join('')
      +'</select></div>';
  } else {
    typeField='<div><label>Вид стипендии</label><input name="schol_type" value="'+esc(x&&x.schol_type||'')+'"></div>';
  }
  // Привязка к проекту (связанность категорий в рамках подключённых модулей)
  let projSel='';
  if(XD.projects&&XD.projects.length){
    projSel='<div><label>Проект (привязка)</label><select name="project_entity_uid">'
      +'<option value="">— без привязки —</option>'
      +XD.projects.map(p=>'<option value="'+p.uid+'" '+(x&&x.project_entity_uid===p.uid?'selected':'')+'>'+esc(p.title)+'</option>').join('')
      +'</select></div>';}
  else if(x&&x.project_title){projSel='<input type="hidden" name="project_entity_uid" value="'+esc(x.project_entity_uid||'')+'">';}
  // Исполнители — может быть несколько (мульти-select, как в задачах/проектах)
  let execSel='';
  if(XD.people&&XD.people.length){
    const cur=new Set(((x&&x.executors)||[]).map(e=>String(e.id)));
    execSel='<div style="grid-column:1/-1"><label>Исполнители (можно выбрать несколько, Ctrl/Cmd+клик)</label>'
      +'<select name="executor_user_ids" id="xexec" multiple size="4" style="width:100%">'
      +XD.people.map(p=>'<option value="'+p.id+'" '+(cur.has(String(p.id))?'selected':'')+'>'+esc(p.name)+'</option>').join('')
      +'</select></div>';}
  const body='<label>Основание / название *</label><input name="title" required value="'+esc(x?x.title:'')+'">'
    +'<div class="frow">'
    +'<div><label>ФИО стипендиата</label><input name="person_name" value="'+esc(x&&x.person_name||'')+'"></div>'
    +typeField
    +'<div><label>Размер</label><input name="amount" value="'+esc(x&&x.amount||'')+'"></div>'
    +'</div><div class="frow">'
    +'<div><label>Дата (календарь)</label><input type="date" name="due_date" value="'+(x&&x.due_date||'')+'"></div>'
    +'<div><label>Статус</label><select name="status_key">'+statSel+'</select></div>'
    +'</div><div class="frow">'+deptSel+projSel+'</div>'
    +execSel
    +'<label>Комментарий</label><textarea name="description" rows="2">'+esc(x?x.description:'')+'</textarea>';
  openModal(x?'Редактирование назначения':'Новое назначение',body,async(fd,f)=>{
    if(!fd.title.trim()){toast('Укажите основание');return}
    const pl={title:fd.title.trim(),description:fd.description,due_date:fd.due_date||null,
              status_key:fd.status_key,person_name:fd.person_name,schol_type:fd.schol_type,
              amount:fd.amount};
    if(f.project_entity_uid)pl.project_entity_uid=f.project_entity_uid.value||null;
    const sel=f.querySelector('#xexec');
    if(sel)pl.executor_user_ids=Array.from(sel.selectedOptions).map(o=>+o.value);
    if(f.department_id)pl.department_id=+f.department_id.value;
    const enames=(pl.executor_user_ids||[]).map(id=>(XD.people.find(p=>p.id===id)||{}).name||'').filter(Boolean);
    const ptitle=pl.project_entity_uid?((XD.projects.find(p=>p.uid===pl.project_entity_uid)||{}).title||''):'';
    if(x){
      await api('/api/schols/'+x.uid,{method:'PATCH',body:JSON.stringify(pl)});
      Object.assign(x,pl);
      if(pl.executor_user_ids)x.executor_names=enames;
      if(pl.project_entity_uid!==undefined)x.project_title=ptitle;
      toast('Сохранено · событие обновится в календарях');
    }else{
      const r=await api('/api/schols',{method:'POST',body:JSON.stringify(pl)});
      XD.schols.unshift(Object.assign({uid:r.uid,can_edit:true,can_share:true,responsibles:[],
        executor_names:enames,project_title:ptitle},pl));
      toast('Назначение создано');
    }
    closeModal();renderScholCards();
  });
}

async function shareSchol(uid){
  if(!XD.departments.length){toast('Нет подразделений');return}
  const body='<label>Подразделение</label><select name="target_department_id">'
    +XD.departments.map(d=>'<option value="'+d.id+'">'+esc(d.name)+'</option>').join('')+'</select>'
    +'<label>Права принимающей стороны</label><select name="permission">'
    +'<option value="read">Только чтение</option><option value="edit">Редактирование</option></select>';
  openModal('Шеринг назначения',body,async(fd)=>{
    await api('/api/core/entities/'+uid+'/shares',{method:'POST',
      body:JSON.stringify({target_department_id:+fd.target_department_id,permission:fd.permission})});
    closeModal();toast('Расшарено · у принимающего открыт слот ответственного');
  });
}

document.addEventListener('DOMContentLoaded',renderScholCards);
