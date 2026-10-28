/* Полноценное окно «Интеграции» (страница /integrations).
   Все данные — из базы через API (/api/core/notify/*), ничего в localStorage. */
const D = JSON.parse(document.getElementById('intgData').textContent);
const EV_LABELS = {assigned:'Назначение ответственным', executor_added:'Назначение исполнителем',
  status_changed:'Смена статуса', due_soon:'Приближение срока', overdue:'Просрочка',
  created:'Создание', updated:'Обновление', shared:'Шеринг подразделению',
  done:'Выполнено', custom:'Произвольное'};
const PH_HINT = 'Доступные плейсхолдеры: {title} {kind_label} {action} {changed} {due_date} {actor} {link}';

/* ---------------- Telegram ---------------- */
async function tgShowSteps(){
  try{
    const r = await api('/api/profile/telegram/steps');
    document.getElementById('tgBindBox').innerHTML =
      '<ol style="font-size:13px;line-height:1.7;margin:8px 0 6px 18px;padding:0">'
      + r.steps.map(s=>'<li>'+esc(s)+'</li>').join('') + '</ol>'
      + '<code style="display:block;background:#0f172a;color:#fff;padding:10px;border-radius:10px;margin:8px 0;font-size:15px">'+esc(r.login)+'</code>'
      + '<button class="btn sm" type="button" onclick="tgCopy(this)" data-login="'+esc(r.login)+'">📋 Скопировать логин</button> '
      + '<a class="btn sm primary" style="text-decoration:none" href="https://t.me/'+esc(D.telegram_bot_username||"sipm_liot_bot")+'?start" target="_blank" rel="noopener">💬 Открыть бота '+esc((r.bot||"@sipm_liot_bot"))+'</a>'
      + (r.bot_configured?'':'<p class="sub" style="color:#b91c1c">Бот не настроен на сервере (TELEGRAM_BOT_TOKEN пуст) — уведомления доставляться не будут.</p>')
      + '<p class="sub" style="margin-top:8px">'+esc(r.note)+'</p>';
  }catch(e){}
}
function tgCopy(btn){ navigator.clipboard && navigator.clipboard.writeText(btn.dataset.login).then(()=>toast('Логин скопирован')); }
async function tgTest(){
  try{ const r=await api('/api/core/notify/test',{method:'POST'}); toast(r.note||'Тест отправлен'); }catch(e){}
}
async function tgUnbind(){
  askConfirm('Отвязать Telegram? Уведомления приходить перестанут.', async()=>{
    await api('/api/profile/telegram/unbind',{method:'POST'}); location.reload();
  });
}

/* ---------------- Яндекс.Календарь ---------------- */
function calForm(){
  document.getElementById('calBox').innerHTML =
    '<form id="calFrm" style="display:grid;gap:8px;margin-top:10px">'
    +'<label class="sub">Логин аккаунта Яндекса (для CalDAV, не email)</label><input name="cal_login" class="inp" placeholder="например ivan.petrov">'
    +'<label class="sub">Пароль приложения (id.yandex.ru → Пароли приложений → «Другие»)</label><input name="app_password" type="password" class="inp" required>'
    +'<label class="sub">Название календаря</label><input name="calendar_name" class="inp" value="'+esc(D.calendar_name||'Цезарь')+'">'
    +'<button class="btn primary sm" type="submit">Сохранить и проверить доступ</button></form>';
  document.getElementById('calFrm').onsubmit = async (ev)=>{
    ev.preventDefault();
    const f = new FormData(ev.target);
    try{
      await api('/api/profile/calendar',{method:'PUT',body:{
        app_password:f.get('app_password'), calendar_name:f.get('calendar_name'),
        cal_login:f.get('cal_login')}});
      toast('Календарь подключён'); setTimeout(()=>location.reload(),600);
    }catch(e){}
  };
}
async function calUnbind(){
  askConfirm('Отключить Яндекс.Календарь?', async()=>{
    await api('/api/profile/calendar/unbind',{method:'POST'}); location.reload();
  });
}

/* ---------------- Сценарии уведомлений ---------------- */
function scnCard(s){
  const kinds = (s.entity_kinds||[]).map(k=>D.kind_labels[k]||k).join(', ') || 'все сущности';
  return '<div class="card pad" style="margin-bottom:10px;border-left:4px solid '+(s.is_base?'#4f46e5':'#10b981')+'">'
    +'<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
    +'<b>'+esc(s.name)+'</b>'
    +(s.is_base?'<span class="badge b-blue">базовый · нельзя отключить</span>':'<span class="badge b-gray">пользовательский</span>')
    +'<span class="badge b-amber">'+esc(EV_LABELS[s.event_key]||s.event_key)+'</span>'
    +'<span class="sub">'+esc(kinds)+'</span>'
    +'<span style="margin-left:auto;display:flex;gap:6px;align-items:center">'
    +(s.switchable?'<label class="sub" style="display:flex;gap:6px;align-items:center;cursor:pointer"><input type="checkbox" '+(s.enabled?'checked':'')+' onchange="scnToggle('+s.id+',this.checked)"> вкл</label>':(s.enabled?'<span class="badge b-green">всегда включён</span>':''))
    +(s.editable?'<button class="btn sm ghost" onclick="scnForm('+s.id+')">✏️</button><button class="btn sm ghost" onclick="scnDel('+s.id+')">🗑</button>':'')
    +'</span></div>'
    +(s.description?'<p class="sub" style="margin:6px 0 0">'+esc(s.description)+'</p>':'')
    +(s.template?'<details style="margin-top:6px"><summary class="sub" style="cursor:pointer">текст уведомления</summary><pre style="white-space:pre-wrap;background:#f4f6fb;border-radius:8px;padding:8px;font-size:12px">'+esc(s.template)+'</pre></details>':'')
    +'</div>';
}
function renderScns(list){
  const base = list.filter(s=>s.is_base), own = list.filter(s=>!s.is_base);
  document.getElementById('scnList').innerHTML =
    '<h4 style="margin:4px 0 8px">🛡 Базовые (администратор, обязательны для всех)</h4>'
    +(base.length?base.map(scnCard).join(''):'<p class="sub">Базовых сценариев пока нет.</p>')
    +'<h4 style="margin:14px 0 8px">⚙️ Мои сценарии и сценарии подразделения</h4>'
    +(own.length?own.map(scnCard).join(''):'<p class="sub">Нет пользовательских сценариев — создайте свой, например «Удался статус „Готово“ у моей задачи».</p>');
}
async function scnReload(){
  try{ const r=await api('/api/core/notify/scenarios'); renderScns(r.scenarios); }catch(e){}
}
async function scnToggle(id,on){
  try{ await api('/api/core/notify/scenarios/'+id+'/enabled',{method:'PUT',body:{enabled:on}});
       toast(on?'Сценарий включён':'Сценарий выключен'); }catch(e){ scnReload(); }
}
async function scnDel(id){
  askConfirm('Удалить пользовательский сценарий?', async()=>{
    await api('/api/core/notify/scenarios/'+id,{method:'DELETE'}); toast('Удалено'); scnReload();
  });
}
function scnForm(id){
  const s = id ? D.__scns.find(x=>x.id===id) : null;
  const opt=(v,c,l)=>`<option value="${v}" ${c===v?'selected':''}>${esc(l)}</option>`;
  openModal(s?'Редактировать сценарий':'Новый сценарий уведомлений',
    '<form id="scnFrm" style="display:grid;gap:9px">'
    +'<label class="sub">Название<input name="name" class="inp" required value="'+esc(s?s.name:'')+'"></label>'
    +'<label class="sub">Описание<input name="description" class="inp" value="'+esc(s?(s.description||''):'')+'"></label>'
    +'<label class="sub">Событие<select name="event_key" class="inp">'
      + D.event_keys.map(k=>opt(k,s?s.event_key:'',EV_LABELS[k]||k)).join('')+'</select></label>'
    +'<label class="sub">Виды сущностей (Ctrl/Cmd+клик — несколько)<select name="entity_kinds" multiple size="4" class="inp">'
      + D.entity_kinds.map(k=>opt(k,(s&&s.entity_kinds||[]).includes(k)?k:'∅',D.kind_labels[k]||k)).join('')+'</select></label>'
    +'<label class="sub">Текст уведомления (HTML Telegram)<textarea name="template" rows="4" class="inp">'+esc(s?(s.template||''):'')+'</textarea><span class="sub">'+PH_HINT+'</span></label>'
    +'<label class="sub">Видимость<select name="visibility" class="inp">'
      + opt('private',(s?s.visibility:'private'),'только я')
      + opt('department',(s?s.visibility:''),'моё подразделение')
      + opt('global',(s?s.visibility:''),'все пользователи')+'</select></label>'
    +'</form>',
    async(fd)=>{
      const kinds=[...fd.entity_kinds.selectedOptions].filter(o=>o.value!=='∅').map(o=>o.value);
      const body={name:fd.name, description:fd.description||null, event_key:fd.event_key,
                  entity_kinds:kinds, module_keys:[], template:fd.template, visibility:fd.visibility};
      if(s) await api('/api/core/notify/scenarios/'+s.id,{method:'PATCH',body});
      else  await api('/api/core/notify/scenarios',{method:'POST',body});
      closeModal(); toast('Сценарий сохранён'); scnReload();
    },'Сохранить');
}
document.addEventListener('DOMContentLoaded', async ()=>{
  const r = await api('/api/core/notify/scenarios').catch(()=>null);
  if(r){ D.__scns=r.scenarios; renderScns(r.scenarios); }
});
