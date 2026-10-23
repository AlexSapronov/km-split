"""Browser acceptance test on a temporary demo DB. Never contacts CRPT."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory() as temp, socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port=sock.getsockname()[1]; sock.close()
        env={**os.environ, 'SPLIT_DATA':temp, 'SPLIT_MODE':'demo', 'SPLIT_USER':'warehouse', 'SPLIT_PASSWORD':'browser-test-password'}
        server=subprocess.Popen([sys.executable,'-m','uvicorn','main:create_app','--factory','--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        base=f'http://127.0.0.1:{port}'
        try:
            for _ in range(100):
                try:
                    urllib.request.urlopen(base+'/health',timeout=1).close(); break
                except Exception:
                    if server.poll() is not None: raise RuntimeError(server.stderr.read().decode())
                    time.sleep(.1)
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True)
                context=browser.new_context(http_credentials={'username':'warehouse','password':'browser-test-password'},viewport={'width':1440,'height':1000},accept_downloads=True)
                page=context.new_page(); errors=[]
                page.on('pageerror',lambda error: errors.append(str(error)))
                page.goto(base); expect(page.locator('#mode')).to_contain_text('ДЕМО')
                page.locator('#demoScan').click(); expect(page.locator('#splitDialog')).to_be_visible()
                expect(page.locator('#originalQuantity')).to_have_text('100')
                page.locator('#quantity').fill('30'); expect(page.locator('#remain')).to_contain_text('70')
                page.locator('#createSplit').click(); expect(page.locator('#signDialog')).to_be_visible()
                page.locator('#signAndSubmit').click(); expect(page.locator('#signDialog')).not_to_be_visible()
                page.get_by_role('button',name='Получить КМ',exact=True).click()
                expect(page.locator('#operations')).to_contain_text('Ожидает нанесения и отчёта')
                with page.expect_download() as download:
                    page.get_by_role('link',name='Скачать PDF',exact=True).click()
                assert download.value.suggested_filename.endswith('-58x40.pdf')
                page.locator('#operations input[type=checkbox]').check()
                page.once('dialog',lambda dialog:dialog.accept())
                page.locator('#prepareReport').click(); expect(page.locator('#signTitle')).to_have_text('Отчёт о разделении')
                page.locator('#signAndSubmit').click(); expect(page.locator('#signDialog')).not_to_be_visible()
                page.get_by_role('button',name='Проверить результат',exact=True).click()
                expect(page.locator('#operations tr')).to_have_count(0)
                page.locator('#historyTab').click(); expect(page.locator('#operations')).to_contain_text('Успешно нанесено')
                page.reload(); expect(page.locator('#operations tr')).to_have_count(0)
                page.locator('#historyTab').click(); expect(page.locator('#operations')).to_contain_text('30 шт.')
                page.locator('#activeTab').click(); page.locator('#demoScan').click()
                expect(page.locator('#originalQuantity')).to_have_text('70')
                page.locator('#quantity').fill('20')
                screenshots=os.getenv('SPLIT_SCREENSHOTS')
                if screenshots:
                    folder=Path(screenshots);folder.mkdir(parents=True,exist_ok=True)
                    page.screenshot(path=str(folder/'split-dialog.png'),full_page=True)
                page.locator('#closeSplit').click()
                if screenshots: page.screenshot(path=str(folder/'warehouse.png'),full_page=True)
                assert not errors,errors
                browser.close()
            print('Browser acceptance passed: scan → signature → code → PDF → report → receipt → history; residual 70.')
        finally:
            server.terminate(); server.wait(timeout=10)


if __name__=='__main__': main()
