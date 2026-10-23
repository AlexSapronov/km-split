import asyncio
import base64
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from uuid import uuid4
import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader
import zxingcpp
from main import create_app
from km_split.codes import parse, with_quantity, division_entry, GS, RS
from km_split.config import Settings
from km_split.db import Store
from km_split.clients import Gateway, RemoteError
from km_split.service import Service
from km_split.labels import render_label

CODE = '0104640638345218215DEMO000000000000000' + GS + '93dGVz'
LONG_CODE = '0104640638345218215"lCk.Q/h\'>EU!cDt(Ua' + GS + '91EE12' + GS + '92' + 'A' * 43 + '='


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path, 'demo', 'warehouse', 'a-long-test-password',
                    [dict(inn='7700000000', name='Демо · склад', template_id=36)])


@pytest.fixture
def svc(settings):
    db = Store(settings.data / 'test.sqlite3')
    return Service(settings, db, Gateway(settings, db))


async def issue(svc, quantity=30):
    op = await svc.create(CODE, quantity, str(uuid4()))
    job = svc.store.one('SELECT id FROM jobs WHERE target=?', (op['id'],))
    await svc.submit(job['id'], 'demo')
    return await svc.poll_order(op['id'])


async def applied(svc, op):
    jobs = await svc.prepare_reports([op['id']], True)
    await svc.submit(jobs[0]['id'], 'demo')
    await svc.poll_report(jobs[0]['id'])
    return jobs[0]['id']


@pytest.mark.parametrize('code', [CODE, LONG_CODE, CODE.replace(GS, ''), LONG_CODE.replace(GS, ''), ']d2'+CODE, CODE.replace(GS, '{GS}')])
def test_parse_preserves_identity_and_crypto(code):
    parsed = parse(code)
    assert len(parsed.cis) == 38
    assert parsed.gtin == '04640638345218'
    assert GS in parsed.full
    assert parse(parsed.full) == parsed


@pytest.mark.parametrize('value', ['', 'abc', CODE[:38], CODE + RS, CODE + GS + '3103001500', CODE + '\nBAD', CODE.replace('04640638345218', '04640638345219')])
def test_parse_rejects_invalid(value):
    with pytest.raises(ValueError): parse(value)


def test_russian_layout_restores_scanner():
    from km_split.codes import RU, EN, SHIFT
    source = CODE
    inverse = {**dict(zip(EN, RU)), **dict(zip(SHIFT, RU.upper()))}
    russian = ''.join(inverse.get(c, c) for c in source)
    assert parse(russian) == parse(source)


@pytest.mark.parametrize('quantity', [0, -1, True, 1.5, '30', 100000000])
def test_invalid_quantity(quantity):
    with pytest.raises(ValueError): with_quantity(CODE, quantity)


def test_report_entry():
    new = with_quantity(LONG_CODE, 30)
    entry = division_entry(new, CODE)
    assert entry == new + RS + CODE
    with pytest.raises(ValueError): division_entry(with_quantity(CODE, 30), CODE)


async def test_complete_cycle(svc):
    op = await issue(svc)
    assert op['state'] == 'ready'
    assert parse(op['new_code']).quantity == 30
    first = svc.label(op['id']).read_bytes()
    assert first == svc.label(op['id']).read_bytes()
    before = await svc.scan(CODE)
    assert (before['quantity'], before['reserved'], before['max_quantity']) == (100, 30, 69)
    report_id = await applied(svc, op)
    assert svc.operation(op['id'])['state'] == 'done'
    assert not svc.state()['operations']
    assert svc.state(True)['operations'][0]['id'] == op['id']
    assert (svc.settings.data / 'receipts' / (report_id + '.json')).exists()
    assert (await svc.scan(CODE))['available'] == 70


async def test_reservations_prevent_overspend(svc):
    await issue(svc, 30)
    with pytest.raises(ValueError, match='69'): await svc.create(CODE, 70, str(uuid4()))
    await issue(svc, 69)
    assert (await svc.scan(CODE))['max_quantity'] == 0
    with pytest.raises(ValueError): await issue(svc, 1)


async def test_request_idempotency(svc):
    request = str(uuid4())
    one = await svc.create(CODE, 30, request)
    two = await svc.create(CODE, 30, request)
    assert one['id'] == two['id']
    assert len(svc.store.all('SELECT id FROM jobs')) == 1
    with pytest.raises(ValueError): await svc.create(CODE, 31, request)


async def test_simultaneous_reservations(svc):
    results = await asyncio.gather(svc.create(CODE, 60, str(uuid4())), svc.create(CODE, 60, str(uuid4())), return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, ValueError) for r in results) == 1


async def test_duplicate_submit_is_safe(svc):
    op = await svc.create(CODE, 30, str(uuid4()))
    job = svc.store.one('SELECT id FROM jobs WHERE target=?', (op['id'],))
    calls = []
    original = svc.gateway.oms
    async def oms(*args, **kwargs):
        calls.append(args[1]); await asyncio.sleep(.03)
        return await original(*args, **kwargs)
    svc.gateway.oms = oms
    await asyncio.gather(svc.submit(job['id'], 'demo'), svc.submit(job['id'], 'demo'))
    await svc.submit(job['id'], 'demo')
    assert calls == ['/api/v3/order']


async def test_unknown_post_keeps_reservation_and_blocks_retry(svc):
    op = await svc.create(CODE, 30, str(uuid4()))
    job = svc.store.one('SELECT id FROM jobs WHERE target=?', (op['id'],))
    calls = []
    async def failure(*args, **kwargs):
        calls.append(args[1]); raise RemoteError('Timeout', uncertain=True)
    svc.gateway.oms = failure
    await svc.submit(job['id'], 'demo')
    assert svc.operation(op['id'])['state'] == 'order_unknown'
    await svc.submit(job['id'], 'demo')
    assert len(calls) == 1
    assert (await svc.scan(CODE))['reserved'] == 30
    with pytest.raises(ValueError): svc.cancel(job['id'])


async def test_cancel_unsubmitted_job_releases_quantity(svc):
    op = await svc.create(CODE, 30, str(uuid4()))
    job = svc.store.one('SELECT id FROM jobs WHERE target=?', (op['id'],))
    svc.cancel(job['id'])
    assert (await svc.scan(CODE))['available'] == 100
    assert svc.operation(op['id'])['state'] == 'cancelled'


async def test_report_requires_explicit_confirmation(svc):
    op = await issue(svc)
    with pytest.raises(ValueError): await svc.prepare_reports([op['id']], False)
    with pytest.raises(ValueError): await svc.prepare_reports([op['id'], op['id']], True)


async def test_report_and_new_split_cannot_overlap(svc):
    op = await issue(svc)
    report = (await svc.prepare_reports([op['id']], True))[0]
    with pytest.raises(ValueError): await svc.prepare_reports([op['id']], True)
    with pytest.raises(ValueError): await issue(svc, 5)
    svc.cancel(report['id'])
    assert svc.operation(op['id'])['state'] == 'ready'


async def test_partial_receipt_does_not_hide_operation(svc):
    op = await issue(svc)
    job = (await svc.prepare_reports([op['id']], True))[0]
    await svc.submit(job['id'], 'demo')
    original = svc.gateway.oms
    async def oms(inn, path, *args, **kwargs):
        if path.endswith('/receipt'):
            return {'results': [{'sourceDocId': svc.job(job['id'])['remote_id'], 'state': 'PARTIALLY_SUCCESS', 'code': 1}]}
        return await original(inn, path, *args, **kwargs)
    svc.gateway.oms = oms
    await svc.poll_report(job['id'])
    assert svc.operation(op['id'])['state'] == 'report_pending'
    assert svc.job(job['id'])['state'] == 'accepted'


async def test_external_balance_change_blocks_report(svc):
    op = await issue(svc)
    original = svc.gateway.info
    async def changed(*args):
        info = await original(*args); info['quantity'] = 80; return info
    svc.gateway.info = changed
    with pytest.raises(ValueError, match='отличается'): await svc.prepare_reports([op['id']], True)


async def test_rejected_report_can_be_corrected_without_emission(svc):
    op = await issue(svc)
    job = (await svc.prepare_reports([op['id']], True))[0]
    await svc.submit(job['id'], 'demo')
    original = svc.gateway.oms
    async def rejected(inn, path, *args, **kwargs):
        if path.endswith('/report/info'): return {'reportStatus':'REJECTED'}
        return await original(inn, path, *args, **kwargs)
    svc.gateway.oms = rejected
    await svc.poll_report(job['id'])
    assert svc.operation(op['id'])['state'] == 'ready'
    assert svc.operation(op['id'])['new_code'] == op['new_code']
    await svc.prepare_reports([op['id']], True)


async def test_restart_preserves_pending_and_history(svc):
    op = await issue(svc)
    replacement = Service(svc.settings, Store(svc.store.path), svc.gateway)
    assert replacement.operation(op['id'])['new_code'] == op['new_code']
    assert (await replacement.scan(CODE))['reserved'] == 30


@pytest.mark.parametrize('raw', [CODE, LONG_CODE, CODE.replace('DEMO000000000000000', 'Ab[Cd(Ef)Gh]JkLm.N0')])
def test_pdf_round_trip(tmp_path, raw):
    if not shutil.which('pdftoppm'): pytest.skip('Poppler required')
    full = with_quantity(raw, 354)
    payload = render_label(full, 'ST01-0505XT', 354)
    page = PdfReader(BytesIO(payload)).pages[0]
    assert float(page.mediabox.width) == pytest.approx(58*72/25.4, abs=.001)
    assert float(page.mediabox.height) == pytest.approx(40*72/25.4, abs=.001)
    assert 'ST01-0505XT' in page.extract_text()
    pdf = tmp_path / 'label.pdf'; pdf.write_bytes(payload)
    subprocess.run(['pdftoppm','-r','203','-singlefile','-png',str(pdf),str(tmp_path/'label')], check=True, capture_output=True)
    decoded = zxingcpp.read_barcode(Image.open(tmp_path/'label.png'))
    assert decoded is not None
    assert decoded.symbology_identifier == ']d2'
    assert decoded.bytes == full.encode('ascii')


def test_auth_csrf_and_secrets(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get('/health').status_code == 200
        assert client.get('/').status_code == 401
        client.auth = (settings.user, settings.password)
        assert client.get('/').status_code == 200
        assert client.post('/api/scan', json={'code':CODE}).status_code == 403
        client.headers['X-Requested-With'] = 'km-split'
        assert client.post('/api/scan', json={'code':CODE}, headers={'Origin':'https://evil.invalid'}).status_code == 403
        assert client.post('/api/scan', json={'code':CODE}).status_code == 200
        assert client.post('/api/operations', json={'code':CODE,'quantity':True,'request_id':str(uuid4())}).status_code == 422
        assert client.post('/api/operations', json={'code':CODE,'quantity':1.5,'request_id':str(uuid4())}).status_code == 422
        payload = client.get('/api/state').text
        assert settings.password not in payload
        assert 'demo-token' not in payload


def test_modes_cannot_share_database(settings):
    create_app(settings)
    settings.mode='live'
    with pytest.raises(ValueError, match='смешивание'): create_app(settings)


async def test_live_protocol_exact_body_and_tokens(settings):
    settings.mode='live'
    org=settings.organizations[0]
    org.update(oms_id=str(uuid4()),oms_connection=str(uuid4()),true_api_url='https://true.example',oms_url='https://oms.example')
    db=Store(settings.data/'live.sqlite3')
    with db.transaction() as tx:
        for kind in ('true','oms'): tx.execute('INSERT INTO tokens VALUES(?,?,?,?)',(org['inn'],kind,kind+'-secret','now'))
    seen=[]
    def transport(request):
        seen.append(request)
        if request.url.path.endswith('/cises/info'):
            assert json.loads(request.content)==[parse(CODE).cis]
            return httpx.Response(200,json=[{'cisInfo':{'cis':parse(CODE).cis,'ownerInn':org['inn'],'status':'INTRODUCED','quantityInPack':100}}])
        if request.url.path.endswith('/product/info'):
            assert json.loads(request.content)=={'gtins':['04640638345218']}
            return httpx.Response(200,json={'results':[{'gtin':'04640638345218','model':'ST01-0505XT'}]})
        if request.url.path=='/api/v3/order':
            assert request.headers['clientToken']=='oms-secret'
            assert request.headers['X-Signature']=='cms'
            assert request.content==b'{"exact":"body"}'
            return httpx.Response(200,json={'orderId':str(uuid4())})
        if request.url.path.endswith('/auth/simpleSignIn/'+org['oms_connection']):
            assert json.loads(request.content)=={'uuid':'challenge-id','data':'attached-cms','inn':org['inn']}
            return httpx.Response(200,json={'token':'session-token'})
        raise AssertionError(str(request.url))
    gateway=Gateway(settings,db,httpx.MockTransport(transport))
    info=await gateway.info(CODE)
    assert info['pn']=='ST01-0505XT'  # Source label without AI 30 is supported.
    await gateway.oms(org['inn'],'/api/v3/order',body='{"exact":"body"}',signature='cms')
    token=await gateway.sign_in({'inn':org['inn'],'kind':'auth_oms','body':'{"uuid":"challenge-id","data":"payload"}'},'attached-cms')
    assert token=='session-token'


async def test_mutating_http_timeout_is_unknown(settings):
    def transport(request): raise httpx.ReadTimeout('test')
    gateway=Gateway(settings,Store(settings.data/'timeout.sqlite3'),httpx.MockTransport(transport))
    with pytest.raises(RemoteError) as exc:
        await gateway.request('POST','https://example.invalid',mutating=True)
    assert exc.value.uncertain
