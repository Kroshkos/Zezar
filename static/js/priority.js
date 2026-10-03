/* Модуль «Приоритетные показатели»: карточки как renderPc() из прототипа, данные с сервера. */
"use strict";
const PD = JSON.parse(document.getElementById('prioData').textContent);
const PS = new Set();
const pstOf = k => PD.statuses.find(s=>s.key===k)||{label:k||'—',color:'b-gray'};
const pfmt = d => d ? d.split('-').reverse().join('.') : '—';

function tglP(el){const k=el.dataset.k;
  if(PS.has(k)){PS.delete(k);el.style.outline=''}else{PS.add(k);el.style.outline='2px solid #818cf8'}
  renderPrioCards();}

function renderPrioCards(){
  const q=(document.getElementById('pq').value||'').toLowerCase();
  let list=PD.indicators.filter(p=>{
    if(q && !(p.title+' '+(p.direction||'')+' '+(p.metric||'')).toLowerCase().includes(q))return false;
    if(PS.size && !PS.has(p.status_key))return false;
    return true;
  });
  document.getElementById('pempty').hidden=list.length>0;
  document.getElementById('pcards').innerHTML=list.map(p=>{
    const st=pstOf(p.status_key);
    return '<div class="card pad">'
      +'<div style="display:flex;justify-content:space-between;align-items:center"><span>'
      +'<span class="badge '+st.color+'">'+st.label+'</span></span>'
      +'<span style="color:var(--muted);font-size:12px">'+esc(p.direction||'—')+'</span></div>'
      +'<h3 style="margin:11px 0 5px">'+esc(p.title||'Показатель')+'</h3>'
      +'<div style="color:var(--muted);font-size:12.5px">📈 цель: '+esc(p.target_value||'—')
      +' · факт: '+esc(p.current_value||'—')+' · 📅 '+pfmt(p.due_date)+'</div>'
      +(p.responsibles.length?'<div style="margin-top:7px;font-size:12.5px;color:var(--muted)">👤 '+esc(p.responsibles.join(', '))+'</div>':'')
      +'<div class="link-chips" style="margin-top:11px">'
      +(p.cross_dept?'<span class="lchip">🔗 межпространственный</span>':'')
      +(p.perm==='read'?'<span class="badge b-amber">только чтение</span>':'')
      +'</div>'
      +'<div style="display:flex;gap:7px;margin-top:14px;flex-wrap:wrap">'
      +(p.can_edit?'<button class="btn sm primary" onclick="prioFormModal(\''+p.uid+'\')">✏️ Изменить</button>':'')
      +(p.can_share?'<button class="icobtn" title="Расшарить" onclick="sharePrio(\''+p.uid+'\')">🔗</button>':'')
      +(p.can_edit?'<button class="icobtn" onclick="delPrio(\''+p.uid+'\')">🗑</button>':'')
      +'</div></div>';
  }).join('');
}

async function delPrio(uid){
  const p=PD.indicators.find(x=>x.uid===uid);
  askConfirm('Удалить показатель «'+p.title+'»? Событие будет удалено из календарей.',async()=>{
    await api('/api/priority/'+uid,{method:'DELETE'});
    PD.indicators=PD.indicators.filter(x=>x.uid!==uid);renderPrioCards();toast('Удалено');});
}

function prioFormModal(uid){
  const p=uid?PD.indicators.find(x=>x.uid===uid):null;
  const statSel=PD.statuses.map(s=>'<option value="'+s.key+'" '+(p&&p.status_key===s.key?'selected':(!p&&s.key==='track'?'selected':''))+'>'+esc(s.label)+'</option>').join('');
  let deptSel='';
  if(PD.departments.length&&!p){deptSel='<div><label>Подразделение</label><select name="department_id">'
    +PD.departments.map(d=>'<option value="'+d.id+'">'+esc(d.name)+'</option>').join('')+'</select></div>';}
  const body='<label>Название показателя *</label><input name="title" required value="'+esc(p?p.title:'')+'">'
    +'<div class="frow">'
    +'<div><label>Направление</label><input name="direction" value="'+esc(p&&p.direction||'')+'"></div>'
    +'<div><label>Метрика</label><input name="metric" value="'+esc(p&&p.metric||'')+'"></div>'
    +'<div><label>Контрольная дата</label><input type="date" name="due_date" value="'+(p&&p.due_date||'')+'"></div>'
    +'</div><div class="frow">'
    +'<div><label>Целевое значение</label><input name="target_value" value="'+esc(p&&p.target_value||'')+'"></div>'
    +'<div><label>Текущее значение</label><input name="current_value" value="'+esc(p&&p.current_value||'')+'"></div>'
    +'<div><label>Статус</label><select name="status_key">'+statSel+'</select></div>'
    +'</div>'+deptSel
    +'<label>Комментарий</label><textarea name="description" rows="2">'+esc(p?p.description:'')+'</textarea>';
  openModal(p?'Редактирование показателя':'Новый показатель',body,async(fd,f)=>{
    if(!fd.title.trim()){toast('Укажите название');return}
    const pl={title:fd.title.trim(),description:fd.description,due_date:fd.due_date||null,
              status_key:fd.status_key,direction:fd.direction,metric:fd.metric,
              target_value:fd.target_value,current_value:fd.current_value};
    if(f.department_id)pl.department_id=+f.department_id.value;
    if(p){
      await api('/api/priority/'+p.uid,{method:'PATCH',body:JSON.stringify(pl)});
      Object.assign(p,pl);
      toast('Сохранено · событие обновится в календарях');
    }else{
      const r=await api('/api/priority',{method:'POST',body:JSON.stringify(pl)});
      PD.indicators.unshift(Object.assign({uid:r.uid,can_edit:true,can_share:true,responsibles:[]},pl));
      toast('Показатель создан');
    }
    closeModal();renderPrioCards();
  });
}

async function sharePrio(uid){
  if(!PD.departments.length){toast('Нет подразделений');return}
  const body='<label>Подразделение</label><select name="target_department_id">'
    +PD.departments.map(d=>'<option value="'+d.id+'">'+esc(d.name)+'</option>').join('')+'</select>'
    +'<label>Права принимающей стороны</label><select name="permission">'
    +'<option value="read">Только чтение</option><option value="edit">Редактирование</option></select>';
  openModal('Шеринг показателя',body,async(fd)=>{
    await api('/api/core/entities/'+uid+'/shares',{method:'POST',
      body:JSON.stringify({target_department_id:+fd.target_department_id,permission:fd.permission})});
    closeModal();toast('Расшарено · у принимающего открыт слот ответственного');
  });
}

document.addEventListener('DOMContentLoaded',renderPrioCards);
