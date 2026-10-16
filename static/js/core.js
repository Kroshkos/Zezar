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
  const root=document.getElementById('modalRoot');
  root.innerHTML='<div style="position:fixed;inset:0;background:rgba(15,23,42,.45);z-index:60;display:flex;align-items:center;justify-content:center" onclick="if(event.target===this)closeModal()">'
   +'<div class="card" style="width:min(560px,94vw);max-height:88vh;overflow:auto;padding:20px"><h3 style="margin-bottom:12px">'+esc(title)+'</h3>'
   +'<form id="mform">'+bodyHtml
   +'<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px"><button type="button" class="btn ghost" onclick="closeModal()">Отмена</button>'
   +'<button type="submit" class="btn primary">'+esc(saveLabel)+'</button></div></form></div></div>';
  document.getElementById('mform').addEventListener('submit',async e=>{e.preventDefault();
    const fd=Object.fromEntries(new FormData(e.target).entries());
    await onSave(fd,e.target);
  });
}
function closeModal(){document.getElementById('modalRoot').innerHTML=''}
function askConfirm(msg,onYes){
  openModal('Подтверждение','<p>'+esc(msg)+'</p>',async()=>{closeModal();await onYes()},'Да');
}
async function logout(){await api('/auth/logout',{method:'POST'});location.href='/login'}
/* «Мои интеграции»: только Telegram (Яндекс.Календарь из интерфейса удалён).
   Логика привязки: пользователь пишет боту /start <код>; код выдаётся кнопкой ниже. */
async function openSettings(){
  const me=await api('/auth/me');
  const st=await api('/api/profile/integrations').catch(()=>({}));
  let body='<p style="font-size:13px;color:var(--muted)">Бот <b>ЦЕЗАРЬ</b> присылает уведомления о задачах, совещаниях и проектах.</p>';
  if(st.telegram_linked){
    body+='<p>Статус Telegram: <span class="badge b-green">привязан</span>'
      +(st.telegram_muted?' <span class="badge b-gray">уведомления отключены (/unmute в боте)</span>':'')
      +'</p><button type="button" class="btn sm ghost" onclick="tgBind(this)">🔄 Привязать заново</button>';
  }else{
    body+='<div id="tgBindBox"><button type="button" class="btn primary sm" onclick="tgBind(this)">Получить код привязки</button></div>';
  }
  openModal('Мои интеграции · '+esc(me.name),body,async()=>{closeModal();},'Готово');
}
async function tgBind(btn){
  try{
    const r=await api('/api/profile/telegram/bind',{method:'POST'});
    const box=document.getElementById('tgBindBox')||btn.parentElement;
    box.innerHTML='<p style="font-size:13px">1️⃣ Откройте бота <b>@CaesarBot</b> в Telegram<br>'
     +'2️⃣ Отправьте команду:<br>'
     +'<code style="display:block;background:#0f172a;color:#fff;padding:10px;border-radius:10px;margin:8px 0;font-size:15px">'+esc(r.code)+'</code>'
     +'3️⃣ Бот сам проверит код, очистит диалог и пришлёт приветствие. Готово ✅</p>'
     +'<button class="btn sm" onclick="navigator.clipboard&&navigator.clipboard.writeText(\''+esc(r.code)+'\').then(()=>toast(\'Код скопирован\'))">📋 Копировать код</button>';
  }catch(e){}
}
