/* Модуль «Задачи»: поведение как renderTasks()/cycleTask()/taskFormModal() из прототипа,
   но данные с сервера (Jinja -> #taskData), мутации через fetch (/api/tasks). */
"use strict";
const D = JSON.parse(document.getElementById('taskData').textContent);
const S = {status:new Set(), priority:new Set()};
const stOf = k => D.statuses.find(s=>s.key===k)||{label:k||'—',color:'b-gray'};
const prOf = k => D.priorities.find(p=>p.key===k)||{label:k||'—',color:'b-gray'};
const todayISO = () => new Date().toISOString().slice(0,10);
const fmt = d => d ? d.split('-').reverse().join('.') : '—';
const deptName = id => (D.departments.find(d=>d.id===id)||{}).name || '';

function tglF(el){const k=el.dataset.k;const set=(el.parentElement.querySelector('.badge')===el)?S.status:S.priority;
  // различаем наборы по наличию ключей
  if(D.statuses.some(s=>s.key===k)){set.clear();toggle(S.status,k,el)}else{toggle(S.priority,k,el)}
  renderTaskTable();}
function toggle(set,k,el){if(set.has(k)){set.delete(k);el.style.outline=''}else{set.add(k);el.style.outline='2px solid #818cf8'}}

function filtered(){
  const q=(document.getElementById('fq').value||'').toLowerCase();
  const ov=document.getElementById('fover').checked;
  return D.tasks.filter(t=>{
    if(q && !(t.title+' '+t.description+' '+t.responsibles.join(' ')).toLowerCase().includes(q))return false;
    if(S.status.size && !S.status.has(t.status_key))return false;
    if(S.priority.size && !S.priority.has(t.priority_key))return false;
    if(ov && !(t.due_date && t.due_date<todayISO() && t.status_key!=='done'))return false;
    return true;
  });
}

function renderTaskTable(){
  const list=filtered();const tb=document.getElementById('ttb');
  document.getElementById('tempty').hidden=list.length>0;
  tb.innerHTML=list.map(t=>{
    const st=stOf(t.status_key),pr=prOf(t.priority_key);
    const od=t.due_date&&t.due_date<todayISO()&&t.status_key!=='done';
    const acc=t.perm==='read'?'<span class="badge b-amber">только чтение</span>':
              (t.perm!=='full'&&t.shared_to!==false?'':'');
    return '<tr><td style="min-width:230px"><div class="nm"><a href="#" class="entlink" title="Открыть профиль задачи" onclick="openProfile(\''+t.uid+'\');return false">'+esc(t.title)+'</a></div>'
      +'<div class="sub">'+esc(t.description||'')+'</div></td>'
      +'<td style="font-size:12.5px">'+esc((t.executor_names&&t.executor_names.length?t.executor_names:t.responsibles).join(', ')||'—')+'</td>'
      +'<td><span class="badge '+pr.color+'">'+pr.label+'</span></td>'
      +'<td style="font-size:12.5px;white-space:nowrap">'+fmt(t.due_date)+(od?' <span class="badge b-red">просрочено</span>':'')+'</td>'
      +'<td style="font-size:12px">'+esc(deptName(t.department_id))+(t.can_share?' <button class="icobtn" title="Расшарить" onclick="shareTask(\''+t.uid+'\')">🔗</button>':'')+'</td>'
      +'<td><button class="badge '+st.color+'" style="border:0;cursor:pointer;font-family:inherit" title="Нажмите, чтобы сменить статус"'
      +(t.can_edit?'':' disabled')+' onclick="cycleTask(\''+t.uid+'\')">'+st.label+'</button></td>'
      +'<td style="white-space:nowrap">'+(t.can_edit?'<button class="icobtn" onclick="taskFormModal(\''+t.uid+'\')">✏️</button>':'')
      +(t.can_edit?'<button class="icobtn" onclick="delTask(\''+t.uid+'\')">🗑</button>':'')+'</td></tr>';
  }).join('');
}

async function cycleTask(uid){
  const t=D.tasks.find(x=>x.uid===uid);if(!t||!t.can_edit)return;
  const arr=D.statuses,i=arr.findIndex(s=>s.key===t.status_key);
  const next=arr[(i+1)%arr.length].key;
  await api('/api/tasks/'+uid,{method:'PATCH',body:JSON.stringify({status_key:next})});
  t.status_key=next;toast('Статус: '+stOf(next).label);notifyChanged();renderTaskTable();
}
async function delTask(uid){
  const t=D.tasks.find(x=>x.uid===uid);
  askConfirm('Удалить задачу «'+t.title+'»? Событие будет удалено из календарей.',async()=>{
    await api('/api/tasks/'+uid,{method:'DELETE'});
    D.tasks=D.tasks.filter(x=>x.uid!==uid);renderTaskTable();toast('Удалено');});
}

function taskFormModal(uid){
  const t=uid?D.tasks.find(x=>x.uid===uid):null;
  const prioSel=D.priorities.map(p=>'<option value="'+p.key+'" '+(t&&t.priority_key===p.key?'selected':'')+'>'+esc(p.label)+'</option>').join('');
  const statSel=D.statuses.map(s=>'<option value="'+s.key+'" '+(t&&t.status_key===s.key?'selected':'')+'>'+esc(s.label)+'</option>').join('');
  // Исполнители — может быть несколько (мульти-select, как «Добавить сотрудника/исполнителя» в прототипе)
  let execSel='';
  if(D.people.length){
    const cur=new Set((t&&t.executors||[]).map(e=>String(e.id)));
    execSel='<div style="grid-column:1/-1"><label>Исполнитель(и) — из участников подразделения (можно выбрать несколько, Ctrl/Cmd+клик)</label>'
      +'<select name="executor_user_ids" id="texec" multiple size="4" style="width:100%">'
      +(cur.size?'':'<option disabled value="">— никого (будете вы) —</option>')
      +D.people.map(p=>'<option value="'+p.id+'" '+(cur.has(String(p.id))?'selected':'')+'>'+esc(p.name)+'</option>').join('')
      +'</select></div>';
  }
  // Ответственный — один из участников подразделения (как selectPeople в прототипе)
  let respSel='';
  if(D.people.length){respSel='<div><label>Ответственный — из участников подразделения</label><select name="responsible_user_id">'
    +'<option value="">— не назначен (будете вы) —</option>'
    +D.people.map(p=>'<option value="'+p.id+'" '+(t&&t.responsibles.includes(p.name)?'selected':(!t&&p.id===D.me?'selected':''))+'>'+esc(p.name)+'</option>').join('')+'</select></div>';}
  let deptSel='';
  if(D.departments.length&&!t){deptSel='<div><label>Подразделение</label><select name="department_id">'
    +D.departments.map(d=>'<option value="'+d.id+'">'+esc(d.name)+'</option>').join('')+'</select></div>';}
  const body='<label>Текст задачи *</label><input name="title" required value="'+esc(t?t.title:'')+'">'
    +'<div class="frow">'
    +'<div><label>Приоритет</label><select name="priority_key">'+prioSel+'</select></div>'
    +'<div><label>Срок</label><input type="date" name="due_date" value="'+(t&&t.due_date||'')+'"></div>'
    +'<div><label>Статус</label><select name="status_key">'+statSel+'</select></div>'
    +respSel+deptSel+'</div>'
    +execSel
    +'<label>Описание</label><textarea name="description" rows="2">'+esc(t?t.description:'')+'</textarea>';
  openModal(t?'Редактирование задачи':'Новая задача',body,async(fd,f)=>{
    if(!fd.title.trim()){toast('Укажите текст задачи');return}
    const pl={title:fd.title.trim(),description:fd.description,priority_key:fd.priority_key,
              due_date:fd.due_date||null,status_key:fd.status_key};
    const sel=f.querySelector('#texec');
    if(sel)pl.executor_user_ids=Array.from(sel.selectedOptions).map(o=>+o.value).filter(v=>v>0);
    if(f.responsible_user_id&&f.responsible_user_id.value)pl.responsible_user_id=+f.responsible_user_id.value;
    if(f.department_id)pl.department_id=+f.department_id.value;
    if(t){
      await api('/api/tasks/'+t.uid,{method:'PATCH',body:JSON.stringify(pl)});
      Object.assign(t,pl);
      if(pl.executor_user_ids)t.executor_names=pl.executor_user_ids.map(id=>(D.people.find(p=>p.id===id)||{}).name||'').filter(Boolean);
      toast('Сохранено · уведомим исполнителей');
    }else{
      const r=await api('/api/tasks',{method:'POST',body:JSON.stringify(pl)});
      D.tasks.unshift(Object.assign({uid:r.uid,can_edit:true,can_share:true,responsibles:[],
        executor_names:(pl.executor_user_ids||[]).map(id=>(D.people.find(p=>p.id===id)||{}).name||'').filter(Boolean),
        department_id:pl.department_id||(D.people[0]&&0)},pl));
      toast('Задача создана');
    }
    closeModal();notifyChanged();renderTaskTable();
  });
}

async function shareTask(uid){
  if(!D.departments.length){toast('Нет подразделений');return}
  const body='<label>Подразделение</label><select name="target_department_id">'
    +D.departments.map(d=>'<option value="'+d.id+'">'+esc(d.name)+'</option>').join('')+'</select>'
    +'<label>Права принимающей стороны</label><select name="permission">'
    +'<option value="read">Только чтение</option><option value="edit">Редактирование</option></select>';
  openModal('Шеринг задачи',body,async(fd)=>{
    await api('/api/core/entities/'+uid+'/shares',{method:'POST',
      body:JSON.stringify({target_department_id:+fd.target_department_id,permission:fd.permission})});
    closeModal();toast('Расшарено · у принимающего открыт слот ответственного');
  });
}

// мягкий автообнов счётчика в навигации
function notifyChanged(){/* можно дернуть /api/tasks/api-list для синхронизации */}
document.addEventListener('DOMContentLoaded',renderTaskTable);
