/* Модуль «Проектная работа»: карточки проектов (сетка .cards как в прототипе), данные с сервера. */
"use strict";
const PRJ = JSON.parse(document.getElementById('projData').textContent);
const PS = new Set();
const pstOf = k => PRJ.statuses.find(s=>s.key===k)||{label:k||'—',color:'b-gray'};
const prioOf = k => PRJ.priorities.find(p=>p.key===k)||{label:k||'—',color:'b-gray'};
const pfmt = d => d ? d.split('-').reverse().join('.') : '—';

function tglP(el){const k=el.dataset.k;
  if(PS.has(k)){PS.delete(k);el.style.outline=''}else{PS.add(k);el.style.outline='2px solid #818cf8'}
  renderProjCards();}

function stageEditor(v){
  const st=(v&&v.stages)||[];
  return '<label>Этапы проекта (вехи уходят в Яндекс.Календарь)</label>'
    +'<div id="pstagebox">'+(st.length?st.map(s=>stageRow(s)).join(''):stageRow({name:'',done:false}))+'</div>'
    +'<button type="button" class="btn sm" onclick="addStage()">＋ этап</button>';
}
function stageRow(s){
  s=s||{};
  return '<div class="frow pstage" style="margin-bottom:6px">'
    +'<input placeholder="Название этапа" value="'+esc(s.name||'')+'" style="flex:1">'
    +'<input type="date" name="d" value="'+esc(s.due||'')+'">'
    +'<label style="display:flex;gap:4px;align-items:center;margin:0;font-size:12px;cursor:pointer">'
    +'<input type="checkbox" class="pdone" '+(s.done?'checked':'')+' style="width:auto">готов</label></div>';
}
function addStage(){document.getElementById('pstagebox').insertAdjacentHTML('beforeend',stageRow({}));}

function collectStages(f){
  return Array.from(document.querySelectorAll('.pstage')).map(r=>({
    name:r.querySelector('input:not([type=date])').value.trim(),
    due:r.querySelector('input[type=date]').value,
    done:r.querySelector('.pdone').checked
  })).filter(s=>s.name);
}

function renderProjCards(){
  const q=(document.getElementById('pq').value||'').toLowerCase();
  let list=PRJ.projects.filter(p=>{
    if(q && !(p.title+' '+(p.customer||'')+' '+(p.budget||'')).toLowerCase().includes(q))return false;
    if(PS.size && !PS.has(p.status_key))return false;
    return true;
  });
  document.getElementById('pempty').hidden=list.length>0;
  document.getElementById('pcards').innerHTML=list.map(p=>{
    const st=pstOf(p.status_key), pr=prioOf(p.priority_key);
    const bar='<div style="background:#eef1f7;border-radius:99px;height:8px;margin-top:9px">'
      +'<div style="background:var(--acc);height:8px;border-radius:99px;width:'+p.progress+'%"></div></div>';
    return '<div class="card pad">'
      +'<div style="display:flex;justify-content:space-between;align-items:center">'
      +'<span class="badge '+st.color+'">'+st.label+'</span>'
      +'<span class="badge '+pr.color+'">'+pr.label+'</span></div>'
      +'<h3 style="margin:11px 0 5px">'+esc(p.title)+'</h3>'
      +'<div style="color:var(--muted);font-size:12.5px">🎯 Заказчик: '+esc(p.customer||'—')
      +' · 💰 '+esc(p.budget||'—')+' · 📅 до '+pfmt(p.due_date)+'</div>'
      +'<div style="margin-top:7px;font-size:12.5px;color:var(--muted)">Этапы: '+p.stage_done+'/'+p.stage_total
      +(p.responsibles.length?' · 👤 '+esc(p.responsibles.join(', ')):'')+'</div>'+bar
      +'<div class="link-chips" style="margin-top:11px">'
      +(p.cross_dept?'<span class="lchip">🔗 межпространственный</span>':'')
      +(p.perm==='read'?'<span class="badge b-amber">только чтение</span>':'')
      +'</div>'
      +'<div style="display:flex;gap:7px;margin-top:14px;flex-wrap:wrap">'
      +(p.can_edit?'<button class="btn sm primary" onclick="projFormModal(\''+p.uid+'\')">✏️ Изменить</button>':'')
      +(p.can_share?'<button class="icobtn" title="Привлечь подразделение" onclick="shareProj(\''+p.uid+'\')">🔗</button>':'')
      +(p.can_edit?'<button class="icobtn" onclick="delProj(\''+p.uid+'\')">🗑</button>':'')
      +'</div></div>';
  }).join('');
}

async function delProj(uid){
  const p=PRJ.projects.find(x=>x.uid===uid);
  askConfirm('Удалить проект «'+p.title+'»? Этапы и ссылки будут потеряны.',async()=>{
    await api('/api/projects/'+uid,{method:'DELETE'});
    PRJ.projects=PRJ.projects.filter(x=>x.uid!==uid);renderProjCards();toast('Удалено');});
}

function projFormModal(uid){
  const p=uid?PRJ.projects.find(x=>x.uid===uid):null;
  const statSel=PRJ.statuses.map(s=>'<option value="'+s.key+'" '+(p&&p.status_key===s.key?'selected':(!p&&s.key==='init'?'selected':''))+'>'+esc(s.label)+'</option>').join('');
  const prioSel=PRJ.priorities.map(x=>'<option value="'+x.key+'" '+((p?p.priority_key:'medium')===x.key?'selected':'')+'>'+esc(x.label)+'</option>').join('');
  let deptSel='';
  if(PRJ.departments.length&&!p){deptSel='<div><label>Подразделение-владелец</label><select name="department_id">'
    +PRJ.departments.map(d=>'<option value="'+d.id+'">'+esc(d.name)+'</option>').join('')+'</select></div>';}
  const body='<label>Название проекта *</label><input name="title" required value="'+esc(p?p.title:'')+'">'
    +'<div class="frow">'
    +'<div><label>Заказчик</label><input name="customer" value="'+esc(p&&p.customer||'')+'"></div>'
    +'<div><label>Бюджет / источник</label><input name="budget" value="'+esc(p&&p.budget||'')+'"></div>'
    +'<div><label>Срок завершения</label><input type="date" name="due_date" value="'+(p&&p.due_date||'')+'"></div>'
    +'</div><div class="frow">'
    +'<div><label>Статус</label><select name="status_key">'+statSel+'</select></div>'
    +'<div><label>Приоритет</label><select name="priority_key">'+prioSel+'</select></div>'+deptSel
    +'</div>'
    +(p?'':stageEditor())
    +'<label>Описание</label><textarea name="description" rows="2">'+esc(p?p.description:'')+'</textarea>';
  openModal(p?'Редактирование проекта':'Новый проект',body,async(fd,f)=>{
    if(!fd.title.trim()){toast('Укажите название проекта');return}
    const pl={title:fd.title.trim(),description:fd.description,due_date:fd.due_date||null,
              status_key:fd.status_key,customer:fd.customer,budget:fd.budget};
    if(!p){pl.priority_key=fd.priority_key;pl.stages_json=JSON.stringify(collectStages(f));
      if(f.department_id)pl.department_id=+f.department_id.value;}
    if(p){
      if(f.querySelector('.pstage'))pl.stages_json=undefined; // payload-stages только при создании (упрощённо — через PATCH ниже)
      const stagesEls=f.querySelectorAll('.pstage');
      if(stagesEls.length)pl.stages=collectStages(f);
      await api('/api/projects/'+p.uid,{method:'PATCH',body:JSON.stringify(pl)});
      Object.assign(p,pl);if(pl.stages){p.stages=pl.stages;p.stage_total=pl.stages.length;
        p.stage_done=pl.stages.filter(s=>s.done).length;p.progress=p.stage_total?Math.round(p.stage_done/p.stage_total*100):0;}
      toast('Сохранено · веха обновится в календарях');
    }else{
      const r=await api('/api/projects',{method:'POST',body:JSON.stringify(pl)});
      PRJ.projects.unshift(Object.assign({uid:r.uid,can_edit:true,can_share:true,responsibles:[],
        cross_dept:false,progress:0,stage_done:0,stage_total:0,stages:[],links:[]},pl));
      toast('Проект создан');
    }
    closeModal();renderProjCards();
  });
}

async function shareProj(uid){
  if(!PRJ.departments.length){toast('Нет подразделений');return}
  const body='<label>Подразделение-участник</label><select name="target_department_id">'
    +PRJ.departments.map(d=>'<option value="'+d.id+'">'+esc(d.name)+'</option>').join('')+'</select>'
    +'<label>Права принимающей стороны</label><select name="permission">'
    +'<option value="edit">Редактирование</option><option value="read">Только чтение</option></select>';
  openModal('Привлечь подразделение к проекту',body,async(fd)=>{
    await api('/api/core/entities/'+uid+'/shares',{method:'POST',
      body:JSON.stringify({target_department_id:+fd.target_department_id,permission:fd.permission})});
    closeModal();toast('Привлечено · у участника открыт слот ответственного');
  });
}

document.addEventListener('DOMContentLoaded',renderProjCards);
