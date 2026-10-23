'use strict';
const $ = id => document.getElementById(id);
let state, history = false, scanned = null, currentJob = null, busy = false;
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
function notice(message, error=false) { $('notice').textContent = message; $('notice').className = error?'error':''; $('notice').hidden=false; }
async function api(path, body) {
  const options = {credentials:'same-origin', headers:{'X-Requested-With':'km-split'}};
  if(body !== undefined) { options.method='POST'; options.headers['Content-Type']='application/json'; options.body=JSON.stringify(body); }
  const r = await fetch(path, options);
  if(r.status===401) throw new Error('Войдите в приложение: обновите страницу.');
  let value; try { value=await r.json(); } catch { throw new Error('Не удалось прочитать ответ сервера'); }
  if(!r.ok) throw new Error(typeof value.detail==='string'?value.detail:'Некорректные данные запроса');
  return value;
}
async function action(fn) {
  if(busy) return;
  busy=true;
  document.body.setAttribute('aria-busy','true');
  try { await fn(); } catch(e) { notice(e.message, true); }
  finally { busy=false; document.body.removeAttribute('aria-busy'); }
}
function button(text, fn, cls='secondary') { const b=el('button',text,cls); b.type='button'; b.onclick=()=>action(fn); return b; }
function badge(key) { const [text, cls]=labels[key]||[key,'neutral']; return el('span',text,'badge '+cls); }
function orgName(inn) { return state.organizations.find(o=>o.inn===inn)?.name || inn; }
async function reload() {
  state=await api('/api/state?history='+history);
  $('mode').textContent=state.mode==='demo'?'ДЕМО · без ЧЗ':'ЧЗ · рабочий режим';
  $('mode').className='badge '+(state.mode==='demo'?'warning':'');
  $('demoScan').hidden=state.mode!=='demo';
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
    if(op.state==='order_pending') row.append(button('Получить КМ',async()=>{await api('/api/operations/'+op.id+'/poll',{}); await reload();}));
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
      const text=(kind==='true'?'ГИС МТ':'СУЗ')+(token?' · обновлён '+new Date(token.updated).toLocaleTimeString('ru-RU'):' · нет доступа');
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
  $('createSplit').disabled=scanned.max_quantity<1;
  $('splitDialog').showModal(); $('quantity').focus();
}
$('scanForm').onsubmit=e=>{e.preventDefault(); action(()=>doScan($('scan').value));};
$('demoScan').onclick=()=>action(()=>doScan(state.demo_code));
$('quantity').oninput=()=>{const q=Number($('quantity').value); $('remain').textContent='После разделения останется: '+(scanned.available-q)+' шт.';};
$('closeSplit').onclick=()=>{$('splitDialog').close(); $('scan').focus();};
$('splitForm').onsubmit=e=>{e.preventDefault(); action(async()=>{
  const quantity=Number($('quantity').value);
  const fingerprint=JSON.stringify([scanned.full,quantity]);
  let draft; try {draft=JSON.parse(sessionStorage.getItem('splitDraft'));} catch {}
  if(!draft||draft.fingerprint!==fingerprint) draft={fingerprint,request_id:crypto.randomUUID()};
  sessionStorage.setItem('splitDraft',JSON.stringify(draft));
  $('createSplit').disabled=true;
  try {
    const op=await api('/api/operations',{code:scanned.full,quantity,request_id:draft.request_id});
    sessionStorage.removeItem('splitDraft'); $('splitDialog').close(); $('scan').value=''; await reload();
    const job=state.jobs.find(j=>j.target===op.id&&j.kind==='order'&&j.state==='prepared'); if(job) await openSign(job.id);
  } finally {$('createSplit').disabled=false;}
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
    script.onload=async()=>{try{await window.cadesplugin;clearTimeout(timer);resolve(window.cadesplugin);}catch{clearTimeout(timer);reject(new Error('Установите КриптоПро ЭЦП Browser plug-in и расширение браузера'));}};
    document.head.append(script);
  }).catch(e=>{cryptoReady=null;throw e;});
  return cryptoReady;
}
$('loadCertificates').onclick=()=>action(async()=>{
  try {
    $('cryptoStatus').textContent='Подключение к КриптоПро…';
    const cp=await loadCrypto(), store=await cp.CreateObjectAsync('CAdESCOM.Store');
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
  const cp=await loadCrypto(), store=await cp.CreateObjectAsync('CAdESCOM.Store'); await store.Open(2,'My',2);
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
    let signature='demo-signature';
    if(state.mode!=='demo') signature=$('signature').value.trim()||await signWithPlugin(currentJob);
    const job=await api('/api/jobs/'+currentJob.id+'/submit',{signature});
    if(['rejected','unknown','sending'].includes(job.state)) {throw new Error(job.error||'Результат неизвестен. Проверьте список заданий; повторная отправка заблокирована.');}
    $('signDialog').close(); notice(job.kind==='order'?'Заказ принят. Нажмите «Получить КМ», когда код будет готов.':job.kind==='report'?'Отчёт отправлен. Нажмите «Проверить результат».':'Доступ обновлён.');
    await reload(); $('scan').focus();
  } catch(e) {$('signError').textContent=e.message||String(e); $('signError').hidden=false; await reload();}
  finally {$('signAndSubmit').disabled=false;}
});
$('cancelJob').onclick=()=>action(async()=>{await api('/api/jobs/'+currentJob.id+'/cancel',{});$('signDialog').close();await reload();});
window.addEventListener('focus',()=>{if(!$('splitDialog').open&&!$('signDialog').open&&document.activeElement===document.body)$('scan').focus();});
action(async()=>{await reload();$('scan').focus();});
