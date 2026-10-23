import base64
import json
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from main import create_app
from km_split.config import Settings


def organization():
    return dict(inn='7700000000', name='Пилотная организация', oms_id=str(uuid4()),
                oms_connection=str(uuid4()), template_id=36,
                true_api_url='https://markirovka.crpt.ru', oms_url='https://suzgrid.crpt.ru')


def client(root, mode='demo'):
    app = create_app(Settings.from_root(root, mode))
    return TestClient(app, headers={'X-Requested-With':'km-split'})


def test_default_boot_needs_no_password_or_org_file(tmp_path, monkeypatch):
    monkeypatch.setenv('SPLIT_DATA', str(tmp_path))
    monkeypatch.delenv('SPLIT_MODE', raising=False)
    monkeypatch.delenv('SPLIT_PASSWORD', raising=False)
    settings = Settings.load()
    assert settings.mode == 'live'
    with TestClient(create_app(settings)) as session:
        assert session.get('/').status_code == 200
        state = session.get('/api/state').json()
        assert state['mode'] == 'live' and state['organizations'] == []
        result = session.post('/api/scan', json={'code':'x'}, headers={'X-Requested-With':'km-split'})
        assert result.status_code == 409 and 'Настройки' in result.json()['detail']


def test_legacy_demo_database_is_preserved_when_enabling_live(tmp_path):
    (tmp_path/'mode').write_text('demo')
    with client(tmp_path) as session:
        before = session.app.state.service.store.path
        config = session.get('/api/settings').json()
        result = session.post('/api/settings', json={k:v for k,v in config.items() if k!='crypto_bridge_installed'} | {'mode':'live','organizations':[organization()]})
        assert result.status_code == 200,result.text
        assert session.get('/api/state').json()['mode'] == 'live'
        assert session.app.state.service.store.path.parent == tmp_path/'live'
        assert before.exists() and before.parent == tmp_path
        assert (tmp_path/'configuration.json').stat().st_mode & 0o777 == 0o600
    with client(tmp_path) as restarted:
        assert restarted.get('/api/state').json()['mode'] == 'live'


def test_configuration_conflict_and_invalid_connection(tmp_path):
    with client(tmp_path) as session:
        config = session.get('/api/settings').json()
        body = {'mode':'live','organizations':[organization()],'revision':config['revision']}
        body['organizations'][0]['oms_connection']='00000000-0000-0000-0000-000000000000'
        assert session.post('/api/settings',json=body).status_code == 409
        assert session.get('/api/state').json()['mode']=='demo'
        body['organizations']=[organization()]
        assert session.post('/api/settings',json=body).status_code==200
        assert session.post('/api/settings',json=body).status_code==409


def test_server_generates_durable_request_uuid_and_stale_tabs_blocked(tmp_path):
    with client(tmp_path) as session:
        from uuid import UUID
        one=session.post('/api/request-id',json={}).json()['request_id']
        two=session.post('/api/request-id',json={}).json()['request_id']
        assert UUID(one).version==4 and one!=two
        cfg=session.get('/api/settings').json()
        session.post('/api/settings',json={'mode':'live','organizations':[organization()],'revision':cfg['revision']})
        response=session.post('/api/request-id',json={},headers={'X-Split-Mode':'demo'})
        assert response.status_code==409 and 'другой вкладке' in response.json()['detail']


def test_switch_back_to_demo_does_not_mix_or_delete_live_data(tmp_path):
    with client(tmp_path) as session:
        cfg=session.get('/api/settings').json()
        result=session.post('/api/settings',json={'mode':'live','organizations':[organization()],'revision':cfg['revision']}).json()
        live_db=session.app.state.service.store.path
        session.post('/api/settings',json={k:result[k] for k in ('mode','organizations','revision')}|{'mode':'demo'})
        assert session.get('/api/state').json()['mode']=='demo'
        assert session.app.state.service.store.path!=live_db and live_db.exists()


def test_cannot_change_live_configuration_with_unfinished_operation(tmp_path):
    from km_split.db import now
    with client(tmp_path) as session:
        cfg=session.get('/api/settings').json()
        result=session.post('/api/settings',json={'mode':'live','organizations':[organization()],'revision':cfg['revision']}).json()
        store=session.app.state.service.store
        with store.transaction() as db:
            db.execute('INSERT INTO sources VALUES(?,?,?,?,?,?)',('cis','full','gtin','7700000000','PN',100))
            db.execute('INSERT INTO operations(id,request_id,fingerprint,source,inn,quantity,pn,state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',('op','request','fingerprint','cis','7700000000',30,'PN','ready',now(),now()))
        reply=session.post('/api/settings',json={k:result[k] for k in ('mode','organizations','revision')}|{'mode':'demo'})
        assert reply.status_code==409
        assert session.get('/api/state').json()['mode']=='live'
