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
  // Общие фильтры (дедлайны + подразделение) — из core.js: window.FLS/applyListFilters.
  // Старая галочка «только просроченные» (#fover) заменена на флаг df_overdue.
  return applyListFilters(D.tasks,{
    search:q, status:S.status, priority:S.priority,
    search_cb:(t,q)=>(t.title+' '+t.description+' '+(t.responsibles||[]).join(' ')).toLowerCase().includes(q),
  });
}

/* Древовидная структура списка: родительские задачи идут первыми, а сразу под
   ними — строки их подзадач (с отступом). Подзадачи больше НЕ перечисляются
   текстом в ячейке родителя: показывается только их количество (📋 N). */
function treeRows(list){
  const byUid=new Map(D.tasks.map(t=>[t.uid,t]));
  const inList=new Set(list.map(t=>t.uid));
  const kids=new Map();      // uid родителя -> [строки-подзадачи (из фильтра)]
  list.forEach(t=>{ if(t.parent_task_uid)(kids.get(t.parent_task_uid)||kids.set(t.parent_task_uid,[]).get(t.parent_task_uid)).push(t); });
  const roots=list.filter(t=>!t.parent_task_uid||!inList.has(t.parent_task_uid)&&!byUid.has(t.parent_task_uid)? !t.parent_task_uid : false);
  // корень = задача без родителя; сироты (родитель не виден фильтром) тоже выводятся отдельным блоком сверху вниз
  const out=[];const seen=new Set();
  function push(t,depth){
    if(seen.has(t.uid))return;seen.add(t.uid);
    out.push({t,depth});
    (kids.get(t.uid)||[]).forEach(s=>push(s,depth+1));
  }
  list.filter(t=>!t.parent_task_uid).forEach(t=>push(t,0));
  list.forEach(t=>push(t,0));   // добор «сирот», если их родителей скрыл фильтр
  return out;
}

function renderTaskTable(){
  const list=filtered();const tb=document.getElementById('ttb');
  document.getElementById('tempty').hidden=list.length>0;
  tb.innerHTML=treeRows(list).map(({t,depth})=>{
    const st=stOf(t.status_key),pr=prOf(t.priority_key);
    const od=t.due_date&&t.due_date<todayISO()&&t.status_key!=='done';
    // Иерархия: у родителя — только счётчик подзадач; у подзадачи — метка «↳».
    const subN=(t.subtasks&&t.subtasks.length)||0;
    const hier=(depth?'<div class="sub" style="color:#6b7280">↳ подзадача'+(t.parent_task_title?' · '+esc(t.parent_task_title):'')+'</div>':'')
      +(subN&&!depth?'<div class="sub" style="color:#6b7280">📋 подзадач: '+subN+'</div>':'');
    const indent=depth? 'padding-left:'+(depth*22)+'px;':'';
    return '<tr><td style="min-width:230px"><div class="nm" style="'+indent+'"><a href="#" class="entlink" title="Открыть профиль задачи" onclick="openProfile(\''+t.uid+'\');return false">'+(depth?'└ ':'')+esc(t.title)+'</a></div>'
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
      t.is_subtask=!!pl.parent_task_uid;   // печать: подзадача без своей карточки
      t.parent_task_title=pl.parent_task_uid?((D.tasks.find(x=>x.uid===pl.parent_task_uid)||{}).title||''):'';
      // синхронизируем список подзадач у старого/нового родителя (для формы и таблицы)
      D.tasks.forEach(x=>{if(x.subtasks)x.subtasks=x.subtasks.filter(s=>s.uid!==t.uid)});
      if(pl.parent_task_uid){const p=D.tasks.find(x=>x.uid===pl.parent_task_uid);
        if(p)(p.subtasks=p.subtasks||[]).push({uid:t.uid,title:t.title,due_date:t.due_date||'',status_key:t.status_key,status_label:stOf(t.status_key).label});}
      if(pl.executor_user_ids)t.executor_names=pl.executor_user_ids.map(id=>(D.people.find(p=>p.id===id)||{}).name||'').filter(Boolean);
      TC.loaded=false;                     // карточки устарели — при открытии модалки перезагрузим с сервера
      toast('Сохранено · уведомим исполнителей');
    }else{
      const r=await api('/api/tasks',{method:'POST',body:JSON.stringify(pl)});
      const nt=Object.assign({uid:r.uid,can_edit:true,can_share:true,responsibles:[],subtasks:[],
        is_subtask:!!pl.parent_task_uid,   // печать: подзадача без своей карточки
        parent_task_title:pl.parent_task_uid?((D.tasks.find(x=>x.uid===pl.parent_task_uid)||{}).title||''):'',
        executor_names:(pl.executor_user_ids||[]).map(id=>(D.people.find(p=>p.id===id)||{}).name||'').filter(Boolean),
        department_id:pl.department_id||(D.people[0]&&0)},pl);
      D.tasks.unshift(nt);
      if(pl.parent_task_uid){const p=D.tasks.find(x=>x.uid===pl.parent_task_uid);
        if(p)(p.subtasks=p.subtasks||[]).push({uid:nt.uid,title:nt.title,due_date:nt.due_date||'',status_key:nt.status_key,status_label:stOf(nt.status_key).label});}
      TC.loaded=false;                     // карточки устарели — перезагрузим при открытии модалки
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
// палитра статусов задач берётся из справочника страницы (D.statuses) —
// цвета StatusSet, а не приоритетов (см. stOf/prOf выше); для неизвестного
// ключа — нейтральный серый b-gray.
const tcStatusColor=k=>PRI_HEX[(D.statuses.find(s=>s.key===k)||{}).color]||PRI_HEX["b-gray"];
const tcPrintDate=new Date().toLocaleDateString('ru-RU');
const tcColor=key=>PRI_HEX[key]||'#01696F';

async function taskCardsModal(force){
  const wide=document.getElementById('tcWide');
  if(!wide.hidden&&!force){wide.hidden=true;return}       // повторный клик — закрыть
  // Список карточек всегда берём с сервера (см. _printable_task_cards).
  // cache:'no-store' — fetch по умолчанию может отдать ответ из HTTP-кэша,
  // и после создания/перепривязки подзадачи пользователь продолжал видеть
  // её старую отдельную карточку («не работает» именно так).
  // Сетевой сбой не должен показывать устаревший TC.cards: окно открываем
  // с явным сообщением об ошибке и предложением «↻ Обновить».
  let loadErr=null;
  if(!TC.loaded||force){
    try{const d=await api('/api/tasks/cards/api-list',{cache:'no-store'});
        TC.cards=d.cards||[];
        // Резервная отметка подзадач на клиенте: если сервер по какой-то
        // причине отдал карточку задачи с заполненным parent_task_uid,
        // помечаем её is_subtask — фильтр ниже её исключит.
        TC.cards.forEach(c=>{if(c.parent_task_uid)c.is_subtask=true});
        TC.loaded=true;}
    catch(e){loadErr=e;toast('Не удалось загрузить карточки с сервера',true)}
  }
  // Резервная фильтрация на клиенте (сервер её тоже делает,
  // _printable_task_cards): подзадачи отдельных карточек не имеют —
  // они печатаются только в содержании родительской задачи.
  // Три независимых признака (любой достаточно), чтобы подзадача точно
  // не «проскочила» в отдельные карточки:
  //  1) is_subtask от сервера;
  //  2) uid встречается в чужом списке subtasks (дерево потомков api-list);
  //  3) parent_task_uid заполнен и это НЕ «битая» ссылка на удалённую задачу.
  // Родитель ищется по ВСЕМ задачам страницы (D.tasks — полный список модуля),
  // а не только по карточкам: родитель-этап проекта отдельной карточки не
  // имеет, но его подзадачи всё равно печатаются в его составе.
  const byUid = new Map(TC.cards.map(c => [c.uid, c]));
  const liveTaskUids = new Set(D.tasks.map(t => t.uid));
  const subUids = new Set();
  TC.cards.forEach(c => (c.subtasks || []).forEach(s => subUids.add(s.uid)));
  const cards = TC.cards.filter(c => {
    if (c.is_subtask || subUids.has(c.uid)) return false;
    const pu = c.parent_task_uid;
    if (pu && (byUid.has(pu) || liveTaskUids.has(pu))) return false;
    return true;
  });
  document.getElementById('tcInfo').textContent='Карточек к печати: '+cards.length;
  document.getElementById('tcBody').innerHTML=(loadErr
      ?'<div class="empty">⚠️<p>Сервер карточек недоступен — нажмите «↻ Обновить»</p></div>'
      :cards.map(tcCardHTML).join('')||'<div class="empty">✅<p>Нет задач для печати</p></div>');
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
          +'<td style="padding:1px 0;white-space:nowrap;text-align:right"><span class="ptag" style="background:'+tcStatusColor(s.status_key)+'">'+esc(s.status_label)+'</span></td>'
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
  // Фиксированная сетка листов: карточки разбиваются по листам A4 landscape
  // (3 колонки × 2 ряда = 6 на лист), неполные ряды — пустые ячейки-заглушки.
  // ВАЖНО: без @page{size:A4 landscape} браузер печатает альбомные листы как
  // портретные с автомасштабированием — карточки уходят за границы бумаги
  // («печатается мусор / не то»). Тот же приём, что setCardPageSize() в
  // static/js/meetings_cards.js; очистка стиля — в cleanup().
  let ps=document.getElementById('taskCardPageStyle');
  if(!ps){ps=document.createElement('style');ps.id='taskCardPageStyle';document.head.appendChild(ps);}
  ps.textContent='@media print{@page{size:A4 landscape;margin:5mm}}';
  const PER=6;
  const cardsArr=Array.from(body.children);
  const pages=[];
  for(let i=0;i<cardsArr.length;i+=PER){
    const perPage=Math.min(PER,cardsArr.length-i);
    pages.push('<div class="printpage fmt-a4">'+cardsArr.slice(i,i+perPage).map(n=>n.outerHTML).join('')
      +Array(PER-perPage).fill('<div></div>').join('')+'</div>');
  }
  ov.innerHTML=pages.join('');
  const cleanup=()=>{document.body.classList.remove('printing-cards');ov.classList.remove('on');
                     ps.textContent='';};
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
