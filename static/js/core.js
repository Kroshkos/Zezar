/* Ядро фронтенда: toast/модалка/fetch — поведение как openModal/toast в прототипе */
"use strict";
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(path,opts={}){
  const r=await fetch(path,{headers:{'Content-Type':'application/json'},credentials:'same-origin',...opts});
  if(r.status===401){location.href='/login';throw new Error('auth');}
  if(!r.ok){let d;try{d=(await r.json()).detail}catch(e){d=r.statusText}toast('⚠ '+d,true);throw new Error(d)}
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
async function openSettings(){
  const me=await api('/auth/me');
  let body='<p style="font-size:13px;color:var(--muted)">Привязка интеграций. Telegram: напишите боту <code>/start ваш_код</code>.</p>'
   +'<label>Яндекс.Календарь — app-пароль (<a target=_blank href="https://id.yandex.ru/security/app-passwords">создать</a>)</label>'
   +'<input name="cal_password" type="password" placeholder="app-пароль">'
   +'<label>Имя календаря</label><input name="cal_name" value="default">';
  openModal('Мои интеграции · '+esc(me.name),body,async(fd,f)=>{
    if(f.cal_password&&f.cal_password.value.trim()){
      await api('/api/profile/calendar',{method:'PUT',body:JSON.stringify({app_password:f.cal_password.value.trim(),calendar_name:f.cal_name.value||'default'})});
    }
    closeModal();toast('Настройки сохранены');
  });
}
