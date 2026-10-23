'use strict';
const $ = id => document.getElementById(id);
window.GisTheme?.bind($('theme-btn'));
let state, history = false, scanned = null, currentJob = null, busy = false, configuration = null;
const labels = {
  awaiting_signature: ['Подписать заказ', 'warning'], order_sending: ['Отправка заказа', 'warning'],
  order_unknown: ['Результат заказа неизвестен', 'error'], order_rejected: ['Заказ отклонён', 'error'],
  order_pending: ['КМ готовится в СУЗ', 'neutral'], codes_fetching: ['Получение КМ · при зависании нужен разбор', 'warning'],
  codes_unknown: ['Получение КМ требует разбора', 'error'], ready: ['Ожидает нанесения и отчёта', 'warning'],
  report_prepared: ['Подписать отчёт', 'warning'], report_sending: ['Отправка отчёта', 'warning'],
  report_unknown: ['Результат отчёта неизвестен', 'error'], report_pending: ['Отчёт обрабатывается', 'neutral'],
  done: ['Успешно нанесено', ''], cancelled: ['Отменено', 'neutral'], prepared: ['Ожидает подписи', 'warning'],
  sending: ['Отправляется · повтор запрещён', 'warning'], unknown: ['Нужен разбор в ЛК', 'error'],
  rejected: ['Отклонено', 'error'], accepted: ['Принято, обрабатывается', 'neutral']
};
const kindName = kind => ({order:'Заказ нового КМ', report:'Отчёт о разделении', auth_true:'Авторизация ГИС МТ', auth_oms:'Авторизация СУЗ'})[kind] || kind;
function el(tag, text, cls) { const e = document.createElement(tag); if(text !== undefined) e.textContent = text; if(cls) e.className = cls; return e; }
function notice(message, error=false) {
  $('notice').textContent = message; $('notice').className = error?'error':''; $('notice').hidden=false;
  if(error) {
    const target=$('configurationDialog').open?$('configurationError'):$('signDialog').open?$('signError'):$('splitDialog').open?$('splitError'):null;
    if(target) {target.textContent=message;target.hidden=false;target.scrollIntoView({block:'nearest'});}
    else $('notice').scrollIntoView({block:'nearest'});
  }
}
async function api(path, body) {
  const options = {credentials:'same-origin', headers:{'X-Requested-With':'km-split'}};
  if(state) options.headers['X-Split-Mode']=state.mode;
  if(body !== undefined) { options.method='POST'; options.headers['Content-Type']='application/json'; options.body=JSON.stringify(body); }
  let r;
  try { r = await fetch(path, options); }
  catch(error) { $('connection').dataset.state='offline'; $('connection').textContent='Нет связи с сервером'; throw error; }
  $('connection').dataset.state='online'; $('connection').textContent='Сервер доступен';
  let value; try { value=await r.json(); } catch { throw new Error('Не удалось прочитать ответ сервера'); }
  if(!r.ok) throw new Error(typeof value.detail==='string'?value.detail:(Array.isArray(value.detail)?value.detail.map(e=>e.msg).join('; '):'Некорректные данные запроса'));
  return value;
}
async function action(fn) {
  if(busy) return;
  busy=true; $('busy-state').hidden=false;
  document.body.setAttribute('aria-busy','true');
  try { await fn(); } catch(e) { notice(e.message || String(e), true); }
  finally { busy=false; $('busy-state').hidden=true; document.body.removeAttribute('aria-busy'); }
}
function button(text, fn, cls='secondary') { const b=el('button',text,cls); b.type='button'; b.onclick=()=>action(fn); return b; }
function badge(key) { const [text, cls]=labels[key]||[key,'neutral']; return el('span',text,'badge '+cls); }
function orgName(inn) { return state.organizations.find(o=>o.inn===inn)?.name || inn; }
async function reload() {
  state=await api('/api/state?history='+history);
  $('mode').textContent=state.mode==='demo'?'ДЕМО · без ЧЗ':'ЧЗ · рабочий режим';
  $('mode').className='badge '+(state.mode==='demo'?'warning':'');
  $('demoScan').hidden=state.mode!=='demo';
  const setup=state.mode==='demo'||!state.organizations.length;
  $('setupHint').hidden=!setup;
  $('setupHintText').textContent=state.mode==='demo'?'Сейчас включена демонстрация. Для реального товара переключитесь в рабочий режим.':'Добавьте ваши организации и параметры СУЗ, затем подключите ЧЗ по ЭЦП.';
  $('activeTab').classList.toggle('selected',!history); $('historyTab').classList.toggle('selected',history);
  $('listTitle').textContent=history?'История разделений':'Ожидают завершения';
  $('counts').textContent=state.operations.length+' операций · показаны последние 500';
  $('empty').hidden=state.operations.length>0;
  $('operations').replaceChildren();
  for(const op of state.operations) {
    const tr=el('tr'), choose=el('td'), pn=el('td'), qty=el('td'), status=el('td'), actions=el('td');
    if(op.state==='ready'&&!history) { const checkbox=el('input'); checkbox.type='checkbox'; checkbox.value=op.id; checkbox.setAttribute('aria-label','Включить '+op.pn+' в отчёт'); checkbox.onchange=updateSelected; choose.append(checkbox); }
    pn.append(el('strong',op.pn),el('small',orgName(op.inn)),el('small',new Date(op.created).toLocaleString('ru-RU')));
    qty.append(el('strong',op.quantity+' шт.'));
    status.append(badge(op.state)); if(op.error) status.append(el('small',op.error));
    const row=el('div',undefined,'row');
    if(op.new_code) { const a=el('a','Скачать PDF','button secondary'); a.href='/api/operations/'+op.id+'/label.pdf'; row.append(a); }
    if(op.state==='awaiting_signature') { const job=state.jobs.find(j=>j.target===op.id&&j.kind==='order'&&j.state==='prepared'); if(job) row.append(button('Подписать',()=>openSign(job.id),'primary')); }
    if(op.state==='order_pending') row.append(button('Получить КМ',async()=>{const result=await api('/api/operations/'+op.id+'/poll',{});await reload();notice(result.state==='ready'?'Новый КМ получен. Скачайте PDF, распечатайте и нанесите этикетку.':(result.error||'СУЗ ещё готовит КМ. Повторите проверку готовности позже.'),Boolean(result.error));}));
    actions.append(row); tr.append(choose,pn,qty,status,actions); $('operations').append(tr);
  }
  updateSelected();
  $('jobs').replaceChildren();
  const jobs=state.jobs.filter(j=>j.kind!=='order'||j.state!=='accepted');
  $('jobsSection').hidden=!jobs.length;
  for(const job of jobs) {
    const item=el('div',undefined,'job'), info=el('div'), row=el('div',undefined,'row');
    info.append(el('strong',kindName(job.kind)+' · '+orgName(job.inn)),el('p',job.remote_id||job.id),badge(job.state));
    if(job.error) info.append(el('p',job.error));
    if(job.state==='prepared') row.append(button('Открыть и подписать',()=>openSign(job.id),'primary'));
    if(job.kind==='report'&&job.state==='accepted') row.append(button('Проверить результат',async()=>{const value=await api('/api/reports/'+job.id+'/poll',{}); notice(value.state==='done'?'Нанесение подтверждено. Операции перенесены в историю.':(value.error||'Отчёт ещё обрабатывается')); await reload();}));
    if(job.kind==='order'&&['unknown','sending'].includes(job.state)) row.append(button('Указать ID из ЛК',async()=>{const id=prompt('ID заказа из ЛК СУЗ. Сверьте productionOrderId с ID операции: '+job.target); if(id){await api('/api/jobs/'+job.id+'/recover',{remote_id:id}); await reload();}}));
    item.append(info,row); $('jobs').append(item);
  }
  $('orgs').replaceChildren();
  for(const org of state.organizations) {
    const card=el('div'); card.append(el('strong',org.name+' · '+org.inn));
    const row=el('div',undefined,'row');
    for(const kind of ['true','oms']) {
      const token=org.tokens.find(t=>t.kind===kind);
      const text=(kind==='true'?'Подключить ГИС МТ':'Подключить СУЗ')+(token?' · обновлён '+new Date(token.updated).toLocaleTimeString('ru-RU'):'');
      row.append(button(text,async()=>{const job=await api('/api/auth/'+org.inn+'/'+kind,{}); await reload(); await openSign(job.id);}));
    }
    card.append(row); $('orgs').append(card);
  }
}
function updateSelected() { const n=document.querySelectorAll('#operations input:checked').length; $('prepareReport').disabled=n===0; $('prepareReport').textContent=n?'Подготовить отчёт · '+n:'Подготовить отчёт'; }
async function doScan(raw) {
  $('notice').hidden=true;
  scanned=await api('/api/scan',{code:raw});
  $('splitPN').textContent=scanned.pn; $('splitOrg').textContent=scanned.organization;
  $('originalQuantity').textContent=scanned.quantity; $('reservedQuantity').textContent=scanned.reserved; $('availableQuantity').textContent=scanned.available;
  $('quantity').max=scanned.max_quantity; $('quantity').value=''; $('remain').textContent='Максимум '+scanned.max_quantity+' шт.';
  $('splitError').hidden=true;
  $('createSplit').disabled=scanned.max_quantity<1;
  $('splitDialog').showModal(); $('quantity').focus();
}
$('scanForm').onsubmit=e=>{e.preventDefault(); action(()=>doScan($('scan').value));};
$('demoScan').onclick=()=>action(()=>doScan(state.demo_code));
$('quantity').oninput=()=>{const q=Number($('quantity').value); $('remain').textContent='После разделения останется: '+(scanned.available-q)+' шт.';};
$('closeSplit').onclick=()=>{$('splitDialog').close(); $('scan').focus();};
$('splitForm').onsubmit=e=>{e.preventDefault(); action(async()=>{
  $('splitError').hidden=true;
  const quantity=Number($('quantity').value);
  const fingerprint=JSON.stringify([scanned.full,quantity]);
  let draft; try {draft=JSON.parse(sessionStorage.getItem('splitDraft'));} catch {}
  // randomUUID is secure-context-only: HTTP on a LAN IP does not expose it.
  // Allocate the idempotency key on the server; no crypto API/polyfill required.
  if(!draft||draft.fingerprint!==fingerprint) draft={fingerprint,...await api('/api/request-id',{})};
  try {sessionStorage.setItem('splitDraft',JSON.stringify(draft));} catch {throw new Error('Браузер запретил сохранение запроса. Разрешите данные сайта для повторной отправки без дублей.');}
  $('createSplit').disabled=true;
  try {
    const op=await api('/api/operations',{code:scanned.full,quantity,request_id:draft.request_id});
    sessionStorage.removeItem('splitDraft'); $('splitDialog').close(); $('scan').value=''; await reload();
    const job=state.jobs.find(j=>j.target===op.id&&j.kind==='order'&&j.state==='prepared'); if(job) await openSign(job.id);
  } finally {$('createSplit').disabled=false;}
});};

function editorRow(org={}) {
  const row=el('fieldset',undefined,'organizationEdit');
  row.append(el('legend','Организация'));
  const defaults={true_api_url:'https://markirovka.crpt.ru',oms_url:'https://suzgrid.crpt.ru',template_id:36};
  const fields=[['name','Наименование'],['inn','ИНН'],['oms_id','OMS ID — идентификатор СУЗ'],['oms_connection','OMS Connection — идентификатор подключения']];
  const appendField=(key,label,parent=row)=>{
    const id='org-'+key+'-'+document.querySelectorAll('.organizationEdit').length+'-'+row.querySelectorAll('input').length;
    const input=el('input');input.id=id;input.dataset.field=key;input.value=org[key]??defaults[key]??'';
    input.autocomplete='off';input.spellcheck=false;
    const title=el('label',label);title.htmlFor=id;parent.append(title,input);return input;
  };
  for(const [key,label] of fields) appendField(key,label);
  const label=el('label','Шаблон нового КМ'),select=el('select');select.dataset.field='template_id';
  for(const [id,title] of [[36,'36 · длинный криптохвост 91/92'],[37,'37 · короткий криптохвост 93']]) {const option=el('option',title);option.value=id;select.append(option);}
  select.value=org.template_id??36;label.append(select);row.append(label);
  const advanced=el('details'),summary=el('summary','Адреса API и владелец карточки (при необходимости)');advanced.append(summary);
  appendField('true_api_url','Базовый адрес True API',advanced);appendField('oms_url','Базовый адрес СУЗ',advanced);appendField('producer_inn','ИНН владельца карточки (необязательно)',advanced);row.append(advanced);
  const remove=el('button','Убрать организацию','quiet');remove.type='button';remove.onclick=()=>row.remove();row.append(remove);
  $('organizationEditor').append(row);
}
async function openConfiguration() {
  configuration=await api('/api/settings');
  $('workMode').value=configuration.mode;
  $('organizationEditor').replaceChildren();
  for(const org of configuration.organizations) editorRow(org);
  if(!configuration.organizations.length) editorRow();
  $('bridgeStatus').textContent=configuration.crypto_bridge_installed?'Скрипт КриптоПро установлен на VPS.':'Для подписи в браузере установите мост КриптоПро на VPS: bash /opt/km-split/scripts/install-crypto-bridge.sh';
  $('configurationError').hidden=true;$('configurationDialog').showModal();
}
$('configurationToggle').onclick=()=>action(openConfiguration);$('setupOpen').onclick=()=>action(openConfiguration);
$('closeConfiguration').onclick=()=>{$('configurationDialog').close();$('scan').focus();};
$('addOrganization').onclick=()=>editorRow();
$('configurationForm').onsubmit=e=>{e.preventDefault();action(async()=>{
  $('configurationError').hidden=true;
  const organizations=[];
  for(const row of document.querySelectorAll('.organizationEdit')) {
    const org={};for(const input of row.querySelectorAll('[data-field]'))org[input.dataset.field]=input.value.trim();
    if(!org.name&&!org.inn&&!org.oms_id&&!org.oms_connection)continue;
    org.template_id=Number(org.template_id);if(!org.producer_inn)delete org.producer_inn;organizations.push(org);
  }
  const mode=$('workMode').value;
  if(mode==='live'&&configuration.mode!=='live'&&!confirm('Включить рабочий режим? Заказы и отчёты после подписи будут отправляться в настоящий ЧЗ.'))return;
  configuration=await api('/api/settings',{mode,organizations,revision:configuration.revision});
  $('configurationDialog').close();scanned=null;currentJob=null;history=false;sessionStorage.removeItem('splitDraft');
  $('scan').value='';await reload();$('access').hidden=false;
  notice(mode==='live'?'Рабочий режим включён. Подключите ГИС МТ и СУЗ вашей организации по ЭЦП.':'Демонстрационный режим включён. Рабочие данные сохранены отдельно.');
});};
$('prepareReport').onclick=()=>action(async()=>{
  const ids=Array.from(document.querySelectorAll('#operations input:checked'),e=>e.value);
  if(!confirm('Подтверждаете, что этикетки по выбранным операциям ('+ids.length+') напечатаны и нанесены на отделённый товар?')) return;
  const jobs=await api('/api/reports',{operation_ids:ids,confirmed_applied:true}); await reload();
  if(jobs.length) await openSign(jobs[0].id);
});
$('refresh').onclick=()=>action(reload); $('settingsToggle').onclick=()=>{$('access').hidden=!$('access').hidden;};
$('activeTab').onclick=()=>action(async()=>{history=false;await reload();}); $('historyTab').onclick=()=>action(async()=>{history=true;await reload();});
async function openSign(jobId) {
  currentJob=await api('/api/jobs/'+jobId);
  if(currentJob.state!=='prepared') {notice('Это задание уже отправлено или отменено');return;}
  $('signTitle').textContent=kindName(currentJob.kind); $('signOrg').textContent=currentJob.organization+' · '+currentJob.inn;
  let summary;
  if(currentJob.kind==='order') {const body=JSON.parse(currentJob.body); const op=state.operations.find(o=>o.id===currentJob.target);summary='Новый КМ: 1 шт.\nGTIN: '+body.products[0].gtin+'\nТовар внутри КМ: '+(op?.quantity||'')+' шт.\nPN: '+(op?.pn||'');}
  else if(currentJob.kind==='report') {const body=JSON.parse(currentJob.body);summary='Нанесение при разделении · DIVISIONMARK\nОпераций: '+body.sntins.length+'\nОрганизация: '+body.attributes.participantId;}
  else summary='Вход в '+(currentJob.kind==='auth_oms'?'СУЗ':'ГИС МТ')+' по ЭЦП сотрудника.\nКлюч остаётся на этом ПК.';
  $('signSummary').textContent=summary; $('signature').value=''; $('signatureFile').value=''; $('signError').hidden=true;
  $('demoSignNotice').hidden=state.mode!=='demo'; $('liveSign').hidden=state.mode==='demo';
  $('signingFile').href='/api/jobs/'+jobId+'/signing.json';
  $('signAndSubmit').disabled=false;
  $('signAndSubmit').textContent='Подписать и отправить';$('cancelJob').hidden=false;
  if(!$('signDialog').open) $('signDialog').showModal();
}
$('closeSign').onclick=()=>{$('signDialog').close(); $('scan').focus();};
$('signatureFile').onchange=async()=>{const f=$('signatureFile').files[0]; if(f){if(f.size>100000){$('signError').textContent='Файл слишком большой';$('signError').hidden=false;return;} $('signature').value=(await f.text()).trim();}};
let cryptoReady;
async function loadCrypto() {
  if(cryptoReady) return cryptoReady;
  cryptoReady=new Promise((resolve,reject)=>{
    const script=document.createElement('script'); script.src='/static/vendor/cadesplugin_api.js';
    const timer=setTimeout(()=>reject(new Error('Плагин подписи не ответил. Используйте подпись через файл.')),22000);
    script.onerror=()=>{clearTimeout(timer);reject(new Error('Скрипт КриптоПро не установлен на сервере. Используйте подпись через файл.'));};
    // cadesplugin is itself a Promise decorated with API methods. Resolving a
    // Promise with it unwraps to undefined and loses CreateObjectAsync.
    script.onload=async()=>{try{await window.cadesplugin;clearTimeout(timer);resolve();}catch{clearTimeout(timer);reject(new Error('Установите КриптоПро ЭЦП Browser plug-in и расширение браузера'));}};
    document.head.append(script);
  }).catch(e=>{cryptoReady=null;throw e;});
  return cryptoReady;
}
$('loadCertificates').onclick=()=>action(async()=>{
  try {
    $('cryptoStatus').textContent='Подключение к КриптоПро…';
    await loadCrypto();const cp=window.cadesplugin, store=await cp.CreateObjectAsync('CAdESCOM.Store');
    await store.Open(2,'My',2);
    try {
      const certificates=await store.Certificates, count=await certificates.Count;
      $('certificate').replaceChildren(el('option','Выберите сертификат')); $('certificate').options[0].value='';
      for(let i=1;i<=count;i++) {const cert=await certificates.Item(i);if(!(await cert.HasPrivateKey()))continue;const end=new Date(await cert.ValidToDate);if(end<new Date())continue;const option=el('option',(await cert.SubjectName)+' · до '+end.toLocaleDateString('ru-RU'));option.value=await cert.Thumbprint;$('certificate').append(option);}
      $('cryptoStatus').textContent='Выберите ЭЦП с полномочиями для указанной организации.';
    } finally {await store.Close();}
  } catch(e) {$('cryptoStatus').textContent=e.message;}
});
async function signWithPlugin(job) {
  const thumb=$('certificate').value;
  if(!thumb) throw new Error('Выберите сертификат или загрузите подпись из файла');
  await loadCrypto();const cp=window.cadesplugin, store=await cp.CreateObjectAsync('CAdESCOM.Store'); await store.Open(2,'My',2);
  try {
    const all=await store.Certificates, found=await all.Find(0,thumb);
    if(!(await found.Count)) throw new Error('Сертификат недоступен. Подключите ЭЦП к этому ПК');
    const cert=await found.Item(1), signer=await cp.CreateObjectAsync('CAdESCOM.CPSigner');
    await signer.propset_Certificate(cert); await signer.propset_CheckCertificate(true);
    const signed=await cp.CreateObjectAsync('CAdESCOM.CadesSignedData');
    await signed.propset_ContentEncoding(1); await signed.propset_Content(job.payload_base64);
    return await signed.SignCades(signer,1,job.detached);
  } finally {await store.Close();}
}
$('signAndSubmit').onclick=()=>action(async()=>{
  $('signAndSubmit').disabled=true; $('signError').hidden=true;
  try {
    if(currentJob.kind.startsWith('auth_')&&currentJob.state!=='prepared') {
      const next=await api('/api/auth/'+currentJob.inn+'/'+currentJob.kind.slice(5),{});
      await reload();await openSign(next.id);return;
    }
    let signature='demo-signature';
    if(state.mode!=='demo') signature=$('signature').value.trim()||await signWithPlugin(currentJob);
    const job=await api('/api/jobs/'+currentJob.id+'/submit',{signature});
    currentJob=job;
    if(['rejected','unknown','sending'].includes(job.state)) {throw new Error(job.error||'Результат неизвестен. Проверьте список заданий; повторная отправка заблокирована.');}
    $('signDialog').close(); notice(job.kind==='order'?'Заказ принят. Нажмите «Получить КМ», когда код будет готов.':job.kind==='report'?'Отчёт отправлен. Нажмите «Проверить результат».':'Доступ обновлён.');
    await reload();
    if(job.kind.startsWith('auth_')&&state.organizations.some(org=>org.inn===job.inn&&['true','oms'].every(kind=>org.tokens.some(token=>token.kind===kind))))$('access').hidden=true;
    $('scan').focus();
  } catch(e) {$('signError').textContent=e.message||String(e); $('signError').hidden=false; await reload();}
  finally {
    $('signAndSubmit').disabled=false;
    if(currentJob?.kind.startsWith('auth_')&&currentJob.state!=='prepared'&&$('signDialog').open) {
      $('signAndSubmit').textContent='Получить новый запрос авторизации';$('cancelJob').hidden=true;
    }
  }
});
$('cancelJob').onclick=()=>action(async()=>{await api('/api/jobs/'+currentJob.id+'/cancel',{});$('signDialog').close();await reload();});
window.addEventListener('focus',()=>{if(!$('splitDialog').open&&!$('signDialog').open&&document.activeElement===document.body)$('scan').focus();});
action(async()=>{await reload();$('scan').focus();});
