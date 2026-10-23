
'use strict';
const $=id=>document.getElementById(id);
window.GisTheme?.bind($('theme-btn'));
let configuration, busy=false;
function el(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
function notice(text,error=false){$('notice').textContent=text;$('notice').className=error?'error':'';$('notice').hidden=false;if(error){$('configurationError').textContent=text;$('configurationError').hidden=false;} $('notice').scrollIntoView({block:'nearest'});}
async function api(path,body){
  const options={credentials:'same-origin',headers:{'X-Requested-With':'km-split'}};
  if(body!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
  let response;
  try{response=await fetch(path,options);}catch(error){$('connection').dataset.state='offline';$('connection').textContent='Нет связи с сервером';throw error;}
  $('connection').dataset.state='online';$('connection').textContent='Сервер доступен';
  const value=await response.json();
  if(!response.ok)throw new Error(typeof value.detail==='string'?value.detail:'Не удалось сохранить настройки. Проверьте заполнение полей.');
  return value;
}
async function action(fn){if(busy)return;busy=true;$('saveConfiguration').disabled=true;try{await fn();}catch(error){notice(error.message,true);}finally{busy=false;$('saveConfiguration').disabled=false;}}
function editorRow(org={}) {
  const row=el('fieldset',undefined,'organizationEdit');
  row.append(el('legend','Организация'));
  const defaults={true_api_url:'https://markirovka.crpt.ru',oms_url:'https://suzgrid.crpt.ru',template_id:36};
  const fields=[['name','Наименование'],['inn','ИНН'],['oms_id','OMS ID — идентификатор СУЗ'],['oms_connection','Идентификатор соединения — OMS Connection']];
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
  $('configurationError').hidden=true;
}
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
  notice(mode==='live'?'Настройки сохранены. На складском экране подключите ЧЗ по ЭЦП.':'Демонстрационный режим включён. Рабочие данные сохранены отдельно.');
});};

action(openConfiguration);
