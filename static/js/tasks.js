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
    // Иерархия задач: подзадачи помечаются в списке; их карточки печатаются
    // только в составе родительской задачи (см. printTaskCards / api-list).
    const hier=(t.parent_task_uid?'<div class="sub" style="color:#6b7280">↳ подзадача: '+esc(t.parent_task_title||D.tasks.find(x=>x.uid===t.parent_task_uid)?.title||'родительская задача')+'</div>':'')
      +(t.subtasks&&t.subtasks.length?'<div class="sub" style="color:#6b7280">📋 подзадачи ('+t.subtasks.length+'): '
        +t.subtasks.map(s=>esc(s.title)+' · '+fmt(s.due_date)+' · '+esc(s.status_label)).join(' | ')+'</div>':'');
    return '<tr><td style="min-width:230px"><div class="nm"><a href="#" class="entlink" title="Открыть профиль задачи" onclick="openProfile(\''+t.uid+'\');return false">'+esc(t.title)+'</a></div>'
      +'<div class="sub">'+esc(t.description||'')+'</div>'+hier+'</td>'
      +'<td style="font-size:12.5px">'+esc((t.executor_names&&t.executor_names.length?t.executor_names:t.responsibles).join(', ')||'—')+'</td>'
      +'<td><span class="badge '+pr.color+'">'+pr.label+'</span></td>'
      +'<td style="font-size:12.5px;white-space:nowrap">'+fmt(t.due_date)+(od?' <span class="badge b-red">просрочено</span>':'')+'</td>'
      +'<td style="font-size:12px">'+esc(deptName(t.department_id))+(t.can_share?' <button class="icobtn" title="Расшарить" onclick="shareTask(\''+t.uid+'\')">🔗</button>':'')+'</td>'
      +'<td><button class="badge '+st.color+'" style="border:0;cursor:pointer;font-family:inherit" title="Нажмите, чтобы сменить статус"'
      +(t.can_edit?'':' disabled')+' onclick="cycleTask(\''+t.uid+'\')">'+st.label+'</button></td>'
      +'<td style="white-space:nowrap">'+(t.can_edit?'<button class="icobtn" onclick="taskFormModal(\''+t.uid+'\')">✏️</button>':'')
      +(t.can_edit?'<button class="icobtn" onclick="delTask(\''+t.uid+'\')">🗑</button>':'')
      +'<button class="icobtn" title="QR-код для мобильного приложения" onclick="openQRCode(\''+t.uid+'\')">📱</button>'
      +'</td></tr>';
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
  // Иерархия задач: выбор родительской задачи (см. TaskIn.parent_task_uid).
  // Кандидаты — все доступные задачи, кроме редактируемой и её потомков
  // (защита от циклов; серверную проверку дублирует _check_parent).
  const descSet=new Set();
  if(t){(function walk(uid){(D.tasks.find(x=>x.uid===uid)||{}).subtasks?.forEach(s=>{if(!descSet.has(s.uid)){descSet.add(s.uid);walk(s.uid)}});})(t.uid);}
  const parCands=D.tasks.filter(x=>x.uid!==(t&&t.uid)&&!descSet.has(x.uid));
  const parSel='<div style="grid-column:1/-1"><label>Родительская задача (подзадача будет печататься в её карточке)</label>'
    +'<select name="parent_task_uid"><option value="">— без родителя (самостоятельная задача) —</option>'
    +parCands.map(x=>'<option value="'+x.uid+'" '+(t&&t.parent_task_uid===x.uid?'selected':'')+'>'+esc(x.title)+'</option>').join('')
    +'</select></div>';
  const body='<label>Текст задачи *</label><input name="title" required value="'+esc(t?t.title:'')+'">'
    +'<div class="frow">'
    +'<div><label>Приоритет</label><select name="priority_key">'+prioSel+'</select></div>'
    +'<div><label>Срок</label><input type="date" name="due_date" value="'+(t&&t.due_date||'')+'"></div>'
    +'<div><label>Статус</label><select name="status_key">'+statSel+'</select></div>'
    +respSel+deptSel+'</div>'
    +execSel
    +parSel
    +'<label>Описание</label><textarea name="description" rows="2">'+esc(t?t.description:'')+'</textarea>';
  openModal(t?'Редактирование задачи':'Новая задача',body,async(fd,f)=>{
    if(!fd.title.trim()){toast('Укажите текст задачи');return}
    const pl={title:fd.title.trim(),description:fd.description,priority_key:fd.priority_key,
              due_date:fd.due_date||null,status_key:fd.status_key};
    // Родительская задача: всегда отправляем (пусто = null — сброс родителя).
    pl.parent_task_uid=(fd.parent_task_uid||'').trim()||null;
    const sel=f.querySelector('#texec');
    if(sel)pl.executor_user_ids=Array.from(sel.selectedOptions).map(o=>+o.value).filter(v=>v>0);
    if(f.responsible_user_id&&f.responsible_user_id.value)pl.responsible_user_id=+f.responsible_user_id.value;
    if(f.department_id)pl.department_id=+f.department_id.value;
    if(t){
      await api('/api/tasks/'+t.uid,{method:'PATCH',body:JSON.stringify(pl)});
      Object.assign(t,pl);
      t.parent_task_title=pl.parent_task_uid?((D.tasks.find(x=>x.uid===pl.parent_task_uid)||{}).title||''):'';
      // синхронизируем список подзадач у старого/нового родителя (для формы и таблицы)
      D.tasks.forEach(x=>{if(x.subtasks)x.subtasks=x.subtasks.filter(s=>s.uid!==t.uid)});
      if(pl.parent_task_uid){const p=D.tasks.find(x=>x.uid===pl.parent_task_uid);
        if(p)(p.subtasks=p.subtasks||[]).push({uid:t.uid,title:t.title,due_date:t.due_date||'',status_label:stOf(t.status_key).label});}
      if(pl.executor_user_ids)t.executor_names=pl.executor_user_ids.map(id=>(D.people.find(p=>p.id===id)||{}).name||'').filter(Boolean);
      toast('Сохранено · уведомим исполнителей');
    }else{
      const r=await api('/api/tasks',{method:'POST',body:JSON.stringify(pl)});
      const nt=Object.assign({uid:r.uid,can_edit:true,can_share:true,responsibles:[],subtasks:[],
        parent_task_title:pl.parent_task_uid?((D.tasks.find(x=>x.uid===pl.parent_task_uid)||{}).title||''):'',
        executor_names:(pl.executor_user_ids||[]).map(id=>(D.people.find(p=>p.id===id)||{}).name||'').filter(Boolean),
        department_id:pl.department_id||(D.people[0]&&0)},pl);
      D.tasks.unshift(nt);
      if(pl.parent_task_uid){const p=D.tasks.find(x=>x.uid===pl.parent_task_uid);
        if(p)(p.subtasks=p.subtasks||[]).push({uid:nt.uid,title:nt.title,due_date:nt.due_date||'',status_label:stOf(nt.status_key).label});}
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

/* ---------- Печать карточек задач (кнопка «🖨 Карточки») ----------
   Дизайн карточек — как cardHTML() из static/js/meetings_cards.js.
   Правила иерархии (серверная выборка /api/tasks/cards/api-list,
   см. _printable_task_cards в modules/tasks/router.py):
   - подзадачи отдельные карточки НЕ имеют — печатаются в содержании
     родительской карточки: наименование · дедлайн · статус выполнения;
   - задачи-этапы проектов в выдачу не попадают (их карточка — у проекта). */
const TC={cards:[],loaded:false};
const PRI_HEX={"b-red":"#dc2626","b-amber":"#d97706","b-blue":"#2563eb",
               "b-gray":"#64748b","b-green":"#16a34a","b-purple":"#7c3aed",
               "b-cyan":"#0e7490","b-gold":"#92700c"};
const tcPrintDate=new Date().toLocaleDateString('ru-RU');
const tcColor=key=>PRI_HEX[key]||'#01696F';

async function taskCardsModal(force){
  const wide=document.getElementById('tcWide');
  if(!wide.hidden&&!force){wide.hidden=true;return}       // повторный клик — закрыть
  if(!TC.loaded||force){
    try{const d=await api('/api/tasks/cards/api-list');TC.cards=d.cards||[];TC.loaded=true;}
    catch(e){return}                                      // toast с ошибкой уже показан
  }
  document.getElementById('tcInfo').textContent='Карточек к печати: '+TC.cards.length;
  document.getElementById('tcBody').innerHTML=TC.cards.map(tcCardHTML).join('')
    ||'<div class="empty">✅<p>Нет задач для печати</p></div>';
  wide.hidden=false;
}

function tcCardHTML(c){
  const pc=tcColor(prOf(c.priority_key).color);
  const subs=c.subtasks||[];
  const subBlock='<div style="margin:4px 0 6px;font-size:10.5px;line-height:1.45">'
    +'<b style="font-size:10px;text-transform:uppercase;letter-spacing:.03em">📋 Подзадачи:</b>'
    +(subs.length
      ?'<table style="width:100%;border-collapse:collapse;margin-top:2px">'
        +subs.map(s=>'<tr style="border-bottom:1px dotted #cbd5e1">'
          +'<td style="padding:1px 4px 1px 0">'+esc(s.title)+'</td>'
          +'<td style="padding:1px 4px;white-space:nowrap;color:#475569">'+fmt(s.due_date)+'</td>'
          +'<td style="padding:1px 0;white-space:nowrap;text-align:right"><span class="ptag" style="background:'+pc+'">'+esc(s.status_label)+'</span></td>'
          +'</tr>').join('')+'</table>'
      :'<div style="color:#94a3b8;padding:2px 0">нет подзадач</div>')
    +'</div>';
  return '<div class="pcardwrap" style="page-break-inside:avoid;margin-bottom:4mm">'
    +'<div class="pcard" style="border-left:8px solid '+pc+';display:flex;flex-direction:column">'
    +'<div class="ptop"><span class="ptag" style="background:'+pc+'">'+esc(prOf(c.priority_key).label)+'</span>'
    +'<span class="pid pdate">'+tcPrintDate+'</span></div>'
    +'<div class="ptitle">'+esc(c.title)+'</div>'
    +(c.description?'<div class="pmeta">'+esc(c.description)+'</div>':'')
    +subBlock
    +'<div class="pnotes" title="Место для рукописных заметок"></div>'
    +'<div class="pmeta"><b>Срок:</b> '+fmt(c.due_date)
    +' · <b>Статус:</b> '+esc(c.status_label)+'</div>'
    +((c.responsibles&&c.responsibles.length)||(c.executors&&c.executors.length)
      ?'<div class="pmeta"><b>Отв.:</b> '+esc((c.responsibles||[]).join(', ')||'—')
       +(c.executors&&c.executors.length?' · <b>Исполнители:</b> '+esc(c.executors.join(', ')):'')+'</div>'
      :'')
    +'</div></div>';
}

function printTaskCards(){
  const body=document.getElementById('tcBody');
  if(!body||!body.children.length||!TC.cards.length){toast('Нет карточек для печати');return}
  // Листы копруются в изолированный оверлей #printCardsOverlay и печатаются
  // через @media print (тот же механизм, что в модуле «Совещания»).
  let ov=document.getElementById('printCardsOverlay');
  if(!ov){ov=document.createElement('div');ov.id='printCardsOverlay';document.body.appendChild(ov);}
  ov.innerHTML='<div class="printpage fmt-a4" style="grid-template-columns:1fr;height:auto;width:200mm">'+body.innerHTML+'</div>';
  const cleanup=()=>{document.body.classList.remove('printing-cards');ov.classList.remove('on');};
  window.addEventListener('afterprint',cleanup,{once:true});
  setTimeout(cleanup,60000);
  document.body.classList.add('printing-cards');
  ov.classList.add('on');
  window.print();
}

// QR-код для сущности (HTTPS-шлюз: приложение или сайт)
async function openQRCode(uid){
  let shareUrl='';
  try{const r=await api('/api/core/entities/'+uid+'/share-link');shareUrl=r.share_url||'';}catch(e){}
  const url='/api/core/entities/'+uid+'/qr';
  const body='<div style="text-align:center"><img src="'+url+'" style="max-width:300px;border:2px solid #e5e7eb;border-radius:8px">'
    +(shareUrl?'<br><br><input readonly value="'+esc(shareUrl)+'" onclick="this.select()" style="width:90%;text-align:center;font-size:12px;color:#374151;border:1px solid #d1d5db;border-radius:6px;padding:6px">':'')
    +'<p style="font-size:12px;color:#6b7280;margin-top:12px">Сканируйте QR-код камерой: карточка откроется в приложении Zezar, а если оно не установлено — на сайте.</p></div>';
  openModal('QR-код задачи',body);
}

document.addEventListener('DOMContentLoaded',()=>{
  renderTaskTable();
  // Deep link вида /tasks?focus=<uid> (приходит с QR-шлюза /open/e/{uid}):
  // открываем карточку сразу после загрузки страницы.
  const focus=new URLSearchParams(location.search).get('focus');
  if(focus && D.tasks.some(t=>t.uid===focus)) openProfile(focus);
});
