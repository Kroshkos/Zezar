/* Модуль «Проектная работа»: карточки проектов (сетка .cards как в прототипе), данные с сервера.
   Форма проекта по образцу projectFormModal() из sipm_lite_v3.html:
   код проекта, руководитель (из участников подразделения), даты начала/окончания,
   связанный грант, показатель Приоритет-2030, маршрут (workflow) — шаблон или индивидуальный;
   для каждого этапа маршрута создаётся автозадача (ответвления и условия — на холсте маршрута).
   Поле «бюджет/источник» удалено по ТЗ.

   ВАЖНО (фикс кнопки «＋ Проект»): имена верхнего уровня объявляются через window.*,
   а не const/function. На странице проектов вместе с projects.js загружается profile.js,
   в котором есть const pfmt; дубликат let/const-объявления в одном глобальном лексическом
   окружении вызывает SyntaxError ("Identifier 'pfmt' has already been declared") — весь
   projects.js не исполняется, projFormModal остаётся неопределённой и кнопка «＋ Проект»
   молча не работает (onclick выбрасывает ReferenceError). window-свойства таких конфликтов
   не боятся. */
"use strict";
window.PRJ = JSON.parse(document.getElementById('projData').textContent);
window.PS = new Set();
window.pstOf = k => PRJ.statuses.find(s=>s.key===k)||{label:k||'—',color:'b-gray'};
window.prioOf = k => PRJ.priorities.find(p=>p.key===k)||{label:k||'—',color:'b-gray'};
window.pfmt = d => d ? String(d).split('-').reverse().join('.') : '—';

window.tglP = function(el){const k=el.dataset.k;
  if(PS.has(k)){PS.delete(k);el.style.outline=''}else{PS.add(k);el.style.outline='2px solid #818cf8'}
  renderProjCards();}

/* ---------- Маршрут (workflow): блок формы как wfFormBlock() из прототипа ---------- */
function wfFormBlock(){
  const tpls=PRJ.wf_templates||[];
  return '<label>Маршрут (workflow)</label>'
    +'<div style="font-size:12px;color:var(--muted);margin-bottom:4px">Для каждого этапа будет создана автозадача. '
    +'Ответвления и условия создаются на холсте маршрута.</div>'
    +'<div class="radio-row" style="display:flex;gap:14px;margin-bottom:6px">'
    +'<label style="text-transform:none;font-size:13px"><input type="radio" name="wfMode" value="template" checked '
    +'onclick="document.getElementById(\'wfTpl\').style.display=\'block\';document.getElementById(\'wfCus\').style.display=\'none\'"> По шаблону</label>'
    +'<label style="text-transform:none;font-size:13px"><input type="radio" name="wfMode" value="custom" '
    +'onclick="document.getElementById(\'wfTpl\').style.display=\'none\';document.getElementById(\'wfCus\').style.display=\'block\'"> Индивидуальный</label>'
    +'</div>'
    +'<div id="wfTpl" style="display:'+(tpls.length?'block':'none')+'">'
    +(tpls.length?'<select name="templateId">'+tpls.map((t,i)=>'<option value="'+t.id+'" '+(i===0?'selected':'')+'>'+esc(t.name)+' ('+esc((t.steps||[]).join(' → '))+')</option>').join('')+'</select>'
      :'<span style="color:var(--muted);font-size:12.5px">шаблонов маршрутов нет — используйте индивидуальный список</span>')
    +'</div>'
    +'<div id="wfCus" style="display:'+(tpls.length?'none':'block')+'">'
    +'<textarea name="stepsText" rows="4" placeholder="Этапы маршрута, каждый с новой строки.&#10;Дедлайн этапа через «|»:&#10;Подготовка | 2026-11-01&#10;Согласование | 2026-12-01"></textarea></div>';
}
// парсинг индивидуального списка этапов: "Название | ГГГГ-ММ-ДД" (как buildStepsFromForm)
function parseStepsText(txt){
  return (txt||'').split('\n').map(s=>s.trim()).filter(Boolean).map(line=>{
    const parts=line.split('|');
    const name=(parts[0]||'').trim();
    let deadline='';
    if(parts[1]){const v=parts[1].trim();if(/^\d{4}-\d{2}-\d{2}$/.test(v))deadline=v;}
    return {name:name,deadline:deadline};
  }).filter(x=>x.name);
}
function collectStages(f){
  // редактирование существующих этапов (строки .pstage) — приоритет над wf-блоком
  const rows=Array.from(document.querySelectorAll('.pstage'));
  if(rows.length)return rows.map(r=>({
    name:r.querySelector('input:not([type=date])').value.trim(),
    due:r.querySelector('input[type=date]').value,
    done:r.querySelector('.pdone').checked
  })).filter(s=>s.name);
  return [];
}

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
    +'<input type="date" name="d" value="'+esc(s.deadline||s.due||'')+'">'
    +'<label style="display:flex;gap:4px;align-items:center;margin:0;font-size:12px;cursor:pointer">'
    +'<input type="checkbox" class="pdone" '+((s.done||s.state==='done')?'checked':'')+' style="width:auto">готов</label></div>';
}
function addStage(){document.getElementById('pstagebox').insertAdjacentHTML('beforeend',stageRow({}));}

/* ---------- Холст маршрута ----------
   Полноценный интерактивный холст (Obsidian-like) живёт в wf_canvas.js,
   который подключается на странице ДО projects.js и задаёт window.wfCanvasHTML
   (просмотр с координатами/связями) и window.wfCanvasEditor (редактор).
   Здесь — только резервный рендерер на случай, если wf_canvas.js не загрузился. */
if (!window.wfCanvasHTML) {
  window.wfCanvasHTML = function(p){
    const st=p.stages||[];
    if(!st.length)return '<div style="font-size:13px;color:var(--muted);padding:14px;border:1px dashed #cfd8e8;border-radius:12px;margin-top:12px">Маршрут пуст — добавьте этапы при редактировании.</div>';
    const STEP={pending:['Ожидает','b-gray'],active:['В работе','b-blue'],done:['Готово','b-green'],rejected:['Отклонён','b-red']};
    let h='<label>🧭 Холст маршрута</label><div style="overflow-x:auto"><div style="display:flex;gap:8px;align-items:stretch;padding:6px 2px">';
    st.forEach((s,i)=>{
      const w=STEP[s.state]||['—','b-gray'];
      h+='<div style="min-width:170px;max-width:210px;border:1px solid #cfd8e8;border-radius:12px;padding:9px;background:#fff">'
        +'<div style="font-weight:600;font-size:12.5px">'+esc(s.name)+'</div>'
        +'<div style="margin-top:5px"><span class="badge '+w[1]+'">'+w[0]+'</span></div>'
        +(s.deadline?'<div style="font-size:11.5px;color:var(--muted);margin-top:5px">⏰ '+pfmt(s.deadline)+'</div>':'')
        +(s.taskId?'<div style="font-size:11px;color:var(--muted);margin-top:3px">автозадача ✓</div>':'')
        +'</div>';
      if(i<st.length-1)h+='<div style="align-self:center;color:#94a3b8">→</div>';
    });
    h+='</div></div>';
    return h;
  };
}

function renderProjCards(){
  const q=(document.getElementById('pq').value||'').toLowerCase();
  let list=PRJ.projects.filter(p=>{
    if(q && !(p.title+' '+(p.customer||'')+' '+(p.code||'')+' '+(p.leader_name||'')).toLowerCase().includes(q))return false;
    if(PS.size && !PS.has(p.status_key))return false;
    return true;
  });
  document.getElementById('pempty').hidden=list.length>0;
  document.getElementById('pcards').innerHTML=list.map(p=>{
    const st=pstOf(p.status_key), pr=prioOf(p.priority_key);
    const bar='<div style="background:#eef1f7;border-radius:99px;height:8px;margin-top:9px">'
      +'<div style="background:var(--acc);height:8px;border-radius:99px;width:'+p.progress+'%"></div></div>';
    const dates=(p.start_date||p.end_date)?(' 📅 '+pfmt(p.start_date)+' → '+pfmt(p.end_date)):'';
    return '<div class="card pad">'
      +'<div style="display:flex;justify-content:space-between;align-items:center">'
      +'<span class="badge '+st.color+'">'+st.label+'</span>'
      +'<span class="badge '+pr.color+'">'+pr.label+'</span></div>'
      +'<h3 style="margin:11px 0 5px">'+(p.code?'<span class="badge b-gray">'+esc(p.code)+'</span> ':'')+'<a href="#" class="entlink" title="Открыть профиль проекта" onclick="openProfile(\''+p.uid+'\');return false">'+esc(p.title)+'</a></h3>'
      +'<div style="color:var(--muted);font-size:12.5px">🎯 Заказчик: '+esc(p.customer||'—')
      +(p.leader_name?' · 👤 рук.: '+esc(p.leader_name):'')+dates+'</div>'
      +'<div style="margin-top:7px;font-size:12.5px;color:var(--muted)">Этапы: '+p.stage_done+'/'+p.stage_total
      +(p.executor_names&&p.executor_names.length||p.responsibles.length?' · 👥 '+esc((p.executor_names&&p.executor_names.length?p.executor_names:p.responsibles).join(', ')):'')+'</div>'+bar
      +'<div class="link-chips" style="margin-top:11px">'
      +(p.parent_title?'<span class="lchip">🗂 часть проекта: <b>'+esc(p.parent_title)+'</b></span>':'')
      +(p.grant_title?'<span class="lchip">💰 <b>'+esc(p.grant_title)+'</b></span>':'')
      +(p.priority_indicator_title?'<span class="lchip">🚀 Приоритет-2030: <b>'+esc(p.priority_indicator_title)+'</b></span>':'')
      +(p.cross_dept?'<span class="lchip">🔗 межпространственный</span>':'')
      +(p.perm==='read'?'<span class="badge b-amber">только чтение</span>':'')
      +'</div>'
      +(p.can_edit?'<button class="btn sm" style="margin-top:11px" onclick="openProjectCard(\''+p.uid+'\')">🧭 Открыть</button>':'')
      +'<div style="display:flex;gap:7px;margin-top:14px;flex-wrap:wrap">'
      +(p.can_edit?'<button class="btn sm primary" onclick="projFormModal(\''+p.uid+'\')">✏️ Изменить</button>':'')
      +(p.can_share?'<button class="icobtn" title="Привлечь подразделение" onclick="shareProj(\''+p.uid+'\')">🔗</button>':'')
      +(p.can_edit?'<button class="icobtn" onclick="delProj(\''+p.uid+'\')">🗑</button>':'')
      +'</div></div>';
  }).join('');
}

function openProjectCard(uid){
  const p=PRJ.projects.find(x=>x.uid===uid);if(!p)return;
  const body='<div style="display:flex;justify-content:space-between;align-items:center">'
    +'<span class="badge b-gray">'+esc(p.code||'б/н')+'</span>'
    +'<span class="badge '+pstOf(p.status_key).color+'">'+pstOf(p.status_key).label+'</span></div>'
    +'<h3 style="margin:10px 0 4px">'+esc(p.title)+'</h3>'
    +'<div style="font-size:12.5px;color:var(--muted)">👤 Руководитель: '+esc(p.leader_name||'—')
    +' · 📅 '+pfmt(p.start_date)+' → '+pfmt(p.end_date)+'</div>'
    +(p.grant_title?'<div style="font-size:12.5px;color:var(--muted);margin-top:3px">💰 Грант: '+esc(p.grant_title)+'</div>':'')
    +(p.priority_indicator_title?'<div style="font-size:12.5px;color:var(--muted);margin-top:3px">🚀 Приоритет-2030: '+esc(p.priority_indicator_title)+'</div>':'')
    +(p.description?'<div style="margin-top:8px;font-size:13px">'+esc(p.description)+'</div>':'')
    +wfCanvasHTML(p);
  openModal('Проект: '+esc(p.title),body,'Закрыть',()=>closeModal(),true);
}

async function delProj(uid){
  const p=PRJ.projects.find(x=>x.uid===uid);
  askConfirm('Удалить проект «'+p.title+'»? Этапы, автозадачи и ссылки будут потеряны.',async()=>{
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
  // Руководитель проекта — из участников подразделения (select по образцу people-полей прототипа)
  let leaderSel='';
  if(PRJ.people&&PRJ.people.length){
    leaderSel='<div><label>Руководитель проекта (из участников подразделения)</label><select name="leader_user_id">'
      +'<option value="">— не назначен —</option>'
      +PRJ.people.map(x=>'<option value="'+x.id+'" '+(p&&p.leader_user_id===x.id?'selected':(!p&&x.id===PRJ.me?'selected':''))+'>'+esc(x.name)+'</option>').join('')
      +'</select></div>';
  }
  // Связанный грант и показатель Приоритет-2030 (как grantId/prId в прототипе)
  const grantOpts=(PRJ.grants||[]).map(g=>'<option value="'+g.uid+'" '+(p&&p.grant_entity_uid===g.uid?'selected':'')+'>'+esc(g.title)+'</option>').join('');
  const prOpts=(PRJ.indicators||[]).map(i=>'<option value="'+i.uid+'" '+(p&&p.priority_indicator_uid===i.uid?'selected':'')+'>'+esc(i.title)+'</option>').join('');
  const grantSel='<div><label>💰 Связанный грант</label><select name="grant_entity_uid"><option value="">— нет —</option>'+grantOpts+'</select></div>';
  const prSel='<div><label>🚀 Показатель Приоритет-2030</label><select name="priority_indicator_uid"><option value="">— нет —</option>'+prOpts+'</select></div>';
  // Иерархия: проект может быть частью другого проекта (родитель выбирается из доступных,
  // исключая самого себя и своих потомков — сервер дополнительно проверяет ацикличность)
  const descs=new Set();
  if(p){
    const stack=[p.uid];
    while(stack.length){const cur=stack.pop();
      PRJ.projects.forEach(x=>{if(x.parent_uid===cur&&!descs.has(x.uid)){descs.add(x.uid);stack.push(x.uid)}});}
  }
  const cand=PRJ.projects.filter(x=>x.uid!==(p?p.uid:'')&&!descs.has(x.uid));
  const parentSel='<div><label>Часть проекта (родитель)</label><select name="parent_uid">'
    +'<option value="">— не является частью —</option>'
    +cand.map(x=>'<option value="'+x.uid+'" '+(p&&p.parent_uid===x.uid?'selected':'')+'>'+esc(x.title)+'</option>').join('')
    +'</select></div>';
  // Исполнители — несколько (мульти-select)
  let execSel='';
  if(PRJ.people&&PRJ.people.length){
    const cur=new Set(((p&&p.executors)||[]).map(e=>String(e.id)));
    execSel='<div style="grid-column:1/-1"><label>Исполнители (можно выбрать несколько, Ctrl/Cmd+клик)</label>'
      +'<select name="executor_user_ids" id="pexec" multiple size="4" style="width:100%">'
      +PRJ.people.map(pr=>'<option value="'+pr.id+'" '+(cur.has(String(pr.id))?'selected':'')+'>'+esc(pr.name)+'</option>').join('')
      +'</select></div>';
  }
  const body='<div class="frow">'
    +'<div><label>Код проекта</label><input name="code" value="'+esc(p&&p.code||'')+'" placeholder="П-01"></div>'
    +'<div><label>Статус</label><select name="status_key">'+statSel+'</select></div></div>'
    +'<label>Название проекта *</label><input name="title" required value="'+esc(p?p.title:'')+'">'
    +'<div class="frow">'+leaderSel+'<div><label>Заказчик</label><input name="customer" value="'+esc(p&&p.customer||'')+'"></div></div>'
    +'<div class="frow">'
    +'<div><label>Дата начала</label><input type="date" name="start_date" value="'+(p&&p.start_date||'')+'"></div>'
    +'<div><label>Дата окончания</label><input type="date" name="end_date" value="'+(p&&p.end_date||'')+'"></div>'
    +'</div><div class="frow">'
    +grantSel+prSel+'</div>'
    +'<div class="frow">'
    +'<div><label>Приоритет</label><select name="priority_key">'+prioSel+'</select></div>'+deptSel
    +parentSel
    +'</div>'
    +execSel
    +(p?'':wfFormBlock())
    +'<label>Описание</label><textarea name="description" rows="2">'+esc(p?p.description:'')+'</textarea>';
  openModal(p?'Редактирование проекта':'Новый проект',body,async(fd,f)=>{
    if(!fd.title.trim()){toast('Укажите название проекта');return}
    if(fd.start_date&&fd.end_date&&fd.end_date<fd.start_date){toast('Дата окончания раньше даты начала');return}
    const pl={title:fd.title.trim(),description:fd.description,customer:fd.customer,
              code:(fd.code||'').trim(),
              start_date:fd.start_date||null,end_date:fd.end_date||null,due_date:fd.end_date||null,
              status_key:fd.status_key};
    if(f.leader_user_id)pl.leader_user_id=f.leader_user_id.value?+f.leader_user_id.value:null;
    if(f.grant_entity_uid)pl.grant_entity_uid=f.grant_entity_uid.value||null;
    if(f.priority_indicator_uid)pl.priority_indicator_uid=f.priority_indicator_uid.value||null;
    if(f.parent_uid)pl.parent_uid=f.parent_uid.value||null;
    const sel=f.querySelector('#pexec');
    if(sel)pl.executor_user_ids=Array.from(sel.selectedOptions).map(o=>+o.value).filter(v=>v>0);
    if(!p){
      // маршрут: по шаблону или индивидуальный (для каждого этапа сервер создаст автозадачу)
      pl.priority_key=fd.priority_key;
      if(fd.wfMode==='custom'){
        const arr=parseStepsText(fd.stepsText);
        pl.stages_json=JSON.stringify(arr.length?arr:[{name:'Подготовка'},{name:'Утверждение'}]);
      }else if(f.templateId&&f.templateId.value){pl.wf_template_id=+f.templateId.value;}
      if(f.department_id)pl.department_id=+f.department_id.value;
    }else{
      const stagesEls=f.querySelectorAll('.pstage');
      if(stagesEls.length)pl.stages=collectStages(f);
    }
    if(p){
      await api('/api/projects/'+p.uid,{method:'PATCH',body:JSON.stringify(pl)});
      Object.assign(p,pl);
      if(pl.leader_user_id!==undefined)p.leader_name=((PRJ.people||[]).find(x=>x.id===pl.leader_user_id)||{}).name||'';
      if(pl.grant_entity_uid!==undefined)p.grant_title=pl.grant_entity_uid?((PRJ.grants||[]).find(g=>g.uid===pl.grant_entity_uid)||{}).title||'':'';
      if(pl.priority_indicator_uid!==undefined)p.priority_indicator_title=pl.priority_indicator_uid?((PRJ.indicators||[]).find(i=>i.uid===pl.priority_indicator_uid)||{}).title||'':'';
      if(pl.executor_user_ids)p.executor_names=pl.executor_user_ids.map(id=>(PRJ.people.find(x=>x.id===id)||{}).name||'').filter(Boolean);
      if(pl.parent_uid!==undefined)p.parent_title=pl.parent_uid?((PRJ.projects.find(x=>x.uid===pl.parent_uid)||{}).title||''):'';
      if(pl.stages){p.stages=pl.stages;p.stage_total=pl.stages.length;
        p.stage_done=pl.stages.filter(s=>s.done).length;p.progress=p.stage_total?Math.round(p.stage_done/p.stage_total*100):0;}
      toast('Сохранено · веха обновится в календарях');
    }else{
      const r=await api('/api/projects',{method:'POST',body:JSON.stringify(pl)});
      PRJ.projects.unshift(Object.assign({uid:r.uid,can_edit:true,can_share:true,responsibles:[],
        executor_names:(pl.executor_user_ids||[]).map(id=>(PRJ.people.find(x=>x.id===id)||{}).name||'').filter(Boolean),
        leader_name:pl.leader_user_id?((PRJ.people||[]).find(x=>x.id===pl.leader_user_id)||{}).name||'':'',
        grant_title:pl.grant_entity_uid?((PRJ.grants||[]).find(g=>g.uid===pl.grant_entity_uid)||{}).title||'':'',
        priority_indicator_title:pl.priority_indicator_uid?((PRJ.indicators||[]).find(i=>i.uid===pl.priority_indicator_uid)||{}).title||'':'',
        cross_dept:false,progress:0,stage_done:0,stage_total:0,stages:[],links:[],code:''},pl));
      toast('Проект создан · автозадачи этапов сформированы');
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
