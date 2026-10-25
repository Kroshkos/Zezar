/* Ядро фронтенда: toast/модалка/fetch — поведение как openModal/toast в прототипе */
"use strict";
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// Универсальный вызов API. Тело можно передать строкой (JSON) или объектом — как в core.js, так и в admin_dept.
async function api(path,opts={},body){
  // Поддержка двух стилей вызова: api(path,{method,body}) и api(path,'POST',{..}).
  if(typeof opts==='string')opts={method:opts,body:body};
  if(opts&&typeof opts.body==='object'&&opts.body!==null){opts={...opts,body:JSON.stringify(opts.body)};}
  const r=await fetch(path,{headers:{'Content-Type':'application/json'},credentials:'same-origin',...opts});
  if(r.status===401){location.href='/login';throw new Error('auth');}
  if(!r.ok){
    let d;
    try{
      const j=await r.json();d=j.detail;
      // FastAPI при 422 возвращает массив ошибок валидации — делаем текст читаемым.
      if(Array.isArray(d))d=d.map(e=>(e.loc||[]).slice(1).join('.')+': '+(e.msg||'')).join('; ');
    }catch(e){d=r.statusText}
    toast('⚠ '+d,true);throw new Error(typeof d==='string'?d:JSON.stringify(d));
  }
  return r.status===204?null:r.json();
}
function toast(msg,err){
  const t=document.createElement('div');
  t.style.cssText='position:fixed;bottom:22px;left:50%;transform:translateX(-50%);z-index:99;background:'+(err?'#dc2626':'#0f172a')+';color:#fff;padding:10px 18px;border-radius:12px;font-size:13px;box-shadow:0 10px 30px rgba(0,0,0,.25)';
  t.textContent=msg;document.body.appendChild(t);setTimeout(()=>t.remove(),3200);
}
function openModal(title,bodyHtml,onSave,saveLabel='Сохранить'){
  // Если на странице нет контейнера #modalRoot (или он перекрыт/недоступен),
  // вставляем оверлей прямо в <body> — иначе модалка «не открывается».
  let root=document.getElementById('modalRoot');
  if(!root){ root=document.createElement('div'); root.id='modalRoot'; document.body.appendChild(root); }
  // wide-режим (5-й аргумент или onSave===true): широкая модалка под холст маршрута
  const wide=(saveLabel===true)||arguments[4]===true;
  if(saveLabel===true)saveLabel='Сохранить';
  root.innerHTML='<div style="position:fixed;inset:0;background:rgba(15,23,42,.45);z-index:9999;display:flex;align-items:center;justify-content:center" onclick="if(event.target===this)closeModal()">'
   +'<div class="card" style="width:min('+(wide?'1080px':'560px')+',94vw);max-height:88vh;overflow:auto;padding:20px"><h3 style="margin-bottom:12px">'+esc(title)+'</h3>'
   +'<form id="mform">'+bodyHtml
   +'<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button type="button" class="btn ghost" onclick="closeModal()">Отмена</button>'
   +'<button type="submit" class="btn primary">'+esc(saveLabel)+'</button></div></form></div></div>';
  const form=document.getElementById('mform');
  const first=form.querySelector('input,select,textarea'); if(first) first.focus();
  form.addEventListener('submit',async e=>{
    e.preventDefault();
    // fd — прокси-объект: поля доступны И как значения (fd.title → строка),
    // И как DOM-элементы (fd.login.value). Обратная совместимость со всеми
    // старыми обработчиками (tasks/docs/meetings/... и админ-панелью).
    const fd=new Proxy(form.elements,{
      get(el,k){
        if(typeof k!=='string') return el[k];
        const c=el[k];
        if(!c) return undefined;
        // Элементы форм (HTMLInputElement и т.п.) всегда имеют .value —
        // возвращаем строковое значение поля (fd.title → «текст»).
        if(typeof c==='object'&&'value'in c) return c.value;
        // Служебные свойства HTMLFormControlsCollection (length, item, ...)
        return c;
      },
      has:(el,k)=>k in el,
    });
    try{ await onSave(fd,form); }
    catch(err){ console.error('[modal]',err); /* ошибку api() уже показала toast */ }
  });
}
function closeModal(){document.getElementById('modalRoot').innerHTML=''}
function askConfirm(msg,onYes){
  openModal('Подтверждение','<p>'+esc(msg)+'</p>',async()=>{closeModal();await onYes()},'Да');
}
async function logout(){await api('/auth/logout',{method:'POST'});location.href='/login'}
/* «Мои интеграции»: только Telegram (Яндекс.Календарь из интерфейса удалён).
   Привязка без кодов: пользователь пишет боту логин, затем пароль (бот чистит диалог). */
async function openSettings(){
  const me=await api('/auth/me');
  const st=await api('/api/profile/integrations').catch(()=>({}));
  let body='<p style="font-size:13px;color:var(--muted)">Бот <b>ЦЕЗАРЬ</b> присылает уведомления о задачах, совещаниях и проектах.</p>';
  if(st.telegram_linked){
    body+='<p>Статус Telegram: <span class="badge b-green">привязан</span>'
      +(st.telegram_muted?' <span class="badge b-gray">уведомления отключены (/unmute в боте)</span>':'')
      +'</p><div style="display:flex;gap:8px;flex-wrap:wrap">'
      +'<button type="button" class="btn sm ghost" onclick="tgShowSteps(this)">📋 Показать инструкцию</button>'
      +'<button type="button" class="btn sm ghost" onclick="tgUnbind(this)">Отвязать Telegram</button></div>'
      +'<div id="tgBindBox"></div>';
  }else{
    body+='<div id="tgBindBox"><button type="button" class="btn primary sm" onclick="tgShowSteps(this)">Как привязать Telegram</button></div>';
  }
  openModal('Мои интеграции · '+esc(me.name),body,async()=>{closeModal();},'Готово');
}
async function tgShowSteps(btn){
  try{
    const r=await api('/api/profile/telegram/steps');
    const box=document.getElementById('tgBindBox')||btn.parentElement;
    box.innerHTML='<ol style="font-size:13px;line-height:1.7;margin:6px 0 6px 18px;padding:0">'
     +r.steps.map(s=>'<li>'+esc(s)+'</li>').join('')+'</ol>'
     +'<code id="tgLoginCode" style="display:block;background:#0f172a;color:#fff;padding:10px;border-radius:10px;margin:8px 0;font-size:15px">'+esc(r.login)+'</code>'
     +'<button class="btn sm" type="button" onclick="tgCopyLogin()">📋 Скопировать логин</button>'
     +'<p style="font-size:12px;color:var(--muted);margin-top:8px">'+esc(r.note)+'</p>';
  }catch(e){}
}
function tgCopyLogin(){
  const t=(document.getElementById('tgLoginCode')||{}).textContent||'';
  if(navigator.clipboard&&t){navigator.clipboard.writeText(t).then(()=>toast('Логин скопирован'));}
}
async function tgUnbind(btn){
  askConfirm('Отвязать Telegram от учётной записи? Уведомления приходить перестанут.',async()=>{
    await api('/api/profile/telegram/unbind',{method:'POST'});
    toast('Telegram отвязан');openSettings();
  });
}
