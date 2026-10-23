"""Real live-client code, simulated CRPT HTTP and simulated local certificate.

No real code orders or real keys are used. The test loads the same PC UI as LAN.
"""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit
from uuid import uuid4
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tests.live_fixture import SOURCE

BRIDGE=r'''
window.testSignatures=[];
const cert={SubjectName:'CN=Fixture Employee',Thumbprint:'FAKE-TEST-CERT',ValidToDate:'2100-01-01',HasPrivateKey:async()=>true};
const certificates={Count:1,Item:async()=>cert,Find:async()=>certificates};
window.cadesplugin=Object.assign(Promise.resolve(),{CreateObjectAsync:async kind=>{
 if(kind==='CAdESCOM.Store')return {Open:async()=>{},Close:async()=>{},Certificates:certificates};
 if(kind==='CAdESCOM.CPSigner')return {propset_Certificate:async()=>{},propset_CheckCertificate:async()=>{}};
 if(kind==='CAdESCOM.CadesSignedData'){
  const obj={};obj.propset_ContentEncoding=async value=>{if(value!==1)throw Error('Wrong encoding');};
  obj.propset_Content=async value=>{obj.content=value;};
  obj.SignCades=async(signer,type,detached)=>{window.testSignatures.push({payload:obj.content,detached});return btoa('fixture-CMS-'.repeat(20));};
  return obj;
 }
 throw Error('Unexpected COM object '+kind);
}});
'''


def main():
    with tempfile.TemporaryDirectory() as temp,socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        env={**os.environ,'SPLIT_DATA':temp,'SPLIT_MODE':'live'}
        server=subprocess.Popen([sys.executable,'-m','uvicorn','tests.live_fixture:create_fixture_app','--factory','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        base=f'http://127.0.0.1:{port}'
        try:
            for _ in range(100):
                try: urllib.request.urlopen(base+'/health',timeout=1).close();break
                except Exception:
                    if server.poll() is not None:raise RuntimeError(server.stderr.read().decode())
                    time.sleep(.1)
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True)
                context=browser.new_context(viewport={'width':1440,'height':1100},accept_downloads=True)
                def forward(route):
                    target=urlsplit(route.request.url)
                    headers=route.request.all_headers();headers['host']=target.netloc
                    route.fulfill(response=route.fetch(url=base+target.path+('?' + target.query if target.query else ''),headers=headers))
                context.route('http://km-split.test:*/**',forward)
                page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                page.route('**/static/vendor/cadesplugin_api.js',lambda route:route.fulfill(content_type='application/javascript',body=BRIDGE))
                page.goto(f'http://km-split.test:{port}')
                assert not page.evaluate('window.isSecureContext')
                try: expect(page.locator('#mode')).to_have_text('ЧЗ · рабочий режим')
                except AssertionError:
                    print('Page errors:',errors,'Notice:',page.locator('#notice').inner_text());raise
                page.locator('#configurationToggle').click()
                page.locator('[data-field=name]').fill('Пилотная организация')
                page.locator('[data-field=inn]').fill('7700000000')
                page.locator('[data-field=oms_id]').fill(str(uuid4()))
                page.locator('[data-field=oms_connection]').fill(str(uuid4()))
                page.locator('#saveConfiguration').click();expect(page.locator('#configurationDialog')).not_to_be_visible()
                page.get_by_role('button',name='Подключить ГИС МТ',exact=True).click()
                page.locator('#loadCertificates').click()
                expect(page.locator('#certificate option')).to_have_count(2)
                page.locator('#certificate').select_option('FAKE-TEST-CERT')
                page.locator('#signAndSubmit').click();expect(page.locator('#signDialog')).not_to_be_visible()
                page.get_by_role('button',name='Подключить СУЗ',exact=True).click()
                page.locator('#signAndSubmit').click();expect(page.locator('#signDialog')).not_to_be_visible()
                page.locator('#scan').fill(SOURCE.replace('\x1d',''));page.locator('#scan').press('Enter')
                expect(page.locator('#splitDialog')).to_be_visible();expect(page.locator('#splitPN')).to_have_text('PILOT-PN-100')
                page.locator('#quantity').fill('17');page.locator('#createSplit').click()
                expect(page.locator('#signDialog')).to_be_visible();page.locator('#signAndSubmit').click()
                expect(page.locator('#signDialog')).not_to_be_visible()
                page.get_by_role('button',name='Получить КМ',exact=True).click()
                expect(page.locator('#operations')).to_contain_text('Ожидает нанесения и отчёта')
                screenshots=os.getenv('SPLIT_SCREENSHOTS')
                if screenshots:
                    folder=Path(screenshots);folder.mkdir(parents=True,exist_ok=True)
                    page.screenshot(path=str(folder/'live-operations.png'),full_page=True)
                with page.expect_download() as download:page.get_by_role('link',name='Скачать PDF',exact=True).click()
                assert download.value.suggested_filename.endswith('58x40.pdf')
                page.locator('#operations input[type=checkbox]').check();page.once('dialog',lambda dialog:dialog.accept())
                page.locator('#prepareReport').click();expect(page.locator('#signTitle')).to_have_text('Отчёт о разделении')
                page.locator('#signAndSubmit').click();expect(page.locator('#signDialog')).not_to_be_visible()
                page.get_by_role('button',name='Проверить результат',exact=True).click()
                expect(page.locator('#operations tr')).to_have_count(0)
                page.locator('#historyTab').click();expect(page.locator('#operations')).to_contain_text('Успешно нанесено')
                signatures=page.evaluate('window.testSignatures')
                assert [s['detached'] for s in signatures]==[False,False,True,True]
                assert not errors,errors
                browser.close()
            print('Live UI acceptance passed: configure → EDS auth (attached) → issue (detached) → PDF → DIVISIONMARK (detached) → receipt → history. CRPT and EDS simulated.')
        finally:
            server.terminate();server.wait(timeout=10)


if __name__=='__main__':main()
