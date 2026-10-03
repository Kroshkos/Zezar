/* Модуль «ПО и разработки»: карточки как renderSofts() из прототипа, данные с сервера. */
"use strict";
const SD = JSON.parse(document.getElementById('softData').textContent);
const SS = new Set();
const sstOf = k => SD.statuses.find(s=>s.key===k)||{label:k||'—',color:'b-gray'};
const sfmt = d => d ? d.split('-').reverse().join('.') : '—';

function tglS(el){const k=el.dataset.k;
  if(SS.has(k)){SS.delete(k);el.style.outline=''}else{SS.add(k);el.style.outline='2px solid #818cf8'}
  renderSoftCards();}

function renderSoftCards(){
  const q=(document.getElementById('sq').value||'').toLowerCase();
  let list=SD.softs.filter(s=>{
    if(q && !(s.title+' '+(s.soft_type||'')+' '+(s.env||'')).toLowerCase().includes(q))return false;
    if(SS.size && !SS.has(s.status_key))return false;
    return true;
  });
  document.getElementById('sempty').hidden=list.length>0;
  document.getElementById('scards').innerHTML=list.map(s=>{
    const st=sstOf(s.status_key);
    return '<div class="card pad">'
      +'<div style="display:flex;justify-content:space-between;align-items:center"><span>'
      +'<span class="badge '+st.color+'">'+st.label+'</span></span>'
      +'<span style="color:var(--muted);font-size:12px">'+esc(s.soft_type||'—')+'</span></div>'
      +'<h3 style="margin:11px 0 5px">'+esc(s.title||'Разработка')+'</h3>'
      +'<div style="color:var(--muted);font-size:12.5px">📅 '+sfmt(s.due_date)
      +(s.env?' · 🖥 '+esc(s.env):'')+'</div>'
      +(s.responsibles.length?'<div style="margin-top:7px;font-size:12.5px;color:var(--muted)">👤 '+esc(s.responsibles.join(', '))+'</div>':'')
      +'<div class="link-chips" style="margin-top:11px">'
      +(s.repo_url?'<a class="lchip" href="'+esc(s.repo_url)+'" target="_blank" rel="noopener">🗂 репозиторий</a>':'')
      +(s.cross_dept?'<span class="lchip">🔗 межпространственное</span>':'')
      +(s.perm==='read'?'<span class="badge b-amber">только чтение</span>':'')
      +'</div>'
      +'<div style="display:flex;gap:7px;margin-top:14px;flex-wrap:wrap">'
      +(s.can_edit?'<button class="btn sm primary" onclick="softFormModal(\''+s.uid+'\')">✏️ Изменить</button>':'')
      +(s.can_share?'<button class="icobtn" title="Расшарить" onclick="shareSoft(\''+s.uid+'\')">🔗</button>':'')
      +(s.can_edit?'<button class="icobtn" onclick="delSoft(\''+s.uid+'\')">🗑</button>':'')
      +'</div></div>';
  }).join('');
}

async function delSoft(uid){
  const s=SD.softs.find(x=>x.uid===uid);
  askConfirm('Удалить запись «'+s.title+'»? Событие будет удалено из календарей.',async()=>{
    await api('/api/softs/'+uid,{method:'DELETE'});
    SD.softs=SD.softs.filter(x=>x.uid!==uid);renderSoftCards();toast('Удалено');});
}

function softFormModal(uid){
  const s=uid?SD.softs.find(x=>x.uid===uid):null;
  const statSel=SD.statuses.map(x=>'<option value="'+x.key+'" '+(s&&s.status_key===x.key?'selected':(!s&&x.key==='backlog'?'selected':''))+'>'+esc(x.label)+'</option>').join('');
  let deptSel='';
  if(SD.departments.length&&!s){deptSel='<div><label>Подразделение</label><select name="department_id">'
    +SD.departments.map(d=>'<option value="'+d.id+'">'+esc(d.name)+'</option>').join('')+'</select></div>';}
  const body='<label>Название *</label><input name="title" required value="'+esc(s?s.title:'')+'">'
    +'<div class="frow">'
    +'<div><label>Дата (план/релиз)</label><input type="date" name="due_date" value="'+(s&&s.due_date||'')+'"></div>'
    +'<div><label>Тип ПО</label><input name="soft_type" value="'+esc(s&&s.soft_type||'')+'"></div>'
    +'<div><label>Окружение/стенд</label><input name="env" value="'+esc(s&&s.env||'')+'"></div>'
    +'</div><div class="frow">'
    +'<div><label>Репозиторий</label><input name="repo_url" placeholder="https://…" value="'+esc(s&&s.repo_url||'')+'"></div>'
    +'<div><label>Статус</label><select name="status_key">'+statSel+'</select></div>'
    +'</div>'+deptSel
    +'<label>Описание</label><textarea name="description" rows="2">'+esc(s?s.description:'')+'</textarea>';
  openModal(s?'Редактирование записи':'Новая разработка',body,async(fd,f)=>{
    if(!fd.title.trim()){toast('Укажите название');return}
    const pl={title:fd.title.trim(),description:fd.description,due_date:fd.due_date||null,
              status_key:fd.status_key,soft_type:fd.soft_type,env:fd.env,repo_url:fd.repo_url};
    if(f.department_id)pl.department_id=+f.department_id.value;
    if(s){
      await api('/api/softs/'+s.uid,{method:'PATCH',body:JSON.stringify(pl)});
      Object.assign(s,pl);
      toast('Сохранено · событие обновится в календарях');
    }else{
      const r=await api('/api/softs',{method:'POST',body:JSON.stringify(pl)});
      SD.softs.unshift(Object.assign({uid:r.uid,can_edit:true,can_share:true,responsibles:[],stages:[]},pl));
      toast('Запись создана');
    }
    closeModal();renderSoftCards();
  });
}

async function shareSoft(uid){
  if(!SD.departments.length){toast('Нет подразделений');return}
  const body='<label>Подразделение</label><select name="target_department_id">'
    +SD.departments.map(d=>'<option value="'+d.id+'">'+esc(d.name)+'</option>').join('')+'</select>'
    +'<label>Права принимающей стороны</label><select name="permission">'
    +'<option value="read">Только чтение</option><option value="edit">Редактирование</option></select>';
  openModal('Шеринг записи',body,async(fd)=>{
    await api('/api/core/entities/'+uid+'/shares',{method:'POST',
      body:JSON.stringify({target_department_id:+fd.target_department_id,permission:fd.permission})});
    closeModal();toast('Расшарено · у принимающего открыт слот ответственного');
  });
}

document.addEventListener('DOMContentLoaded',renderSoftCards);
