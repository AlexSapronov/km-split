import base64
import json
from uuid import uuid4
from fastapi.testclient import TestClient
from main import create_app
from km_split.config import Settings
from tests.live_fixture import LiveFixture, SOURCE
from tests.test_setup import organization

SIGNATURE = base64.b64encode(b'fixture-CMS-'*20).decode()


def test_live_setup_auth_issue_label_report_and_residual(tmp_path):
    fixture=LiveFixture()
    app=create_app(Settings.from_root(tmp_path),gateway_factory=fixture.gateway)
    with TestClient(app,headers={'X-Requested-With':'km-split'}) as client:
        cfg=client.get('/api/settings').json()
        org=organization()
        assert client.post('/api/settings',json={'mode':'live','organizations':[org],'revision':cfg['revision']}).status_code==200
        assert client.get('/api/state').json()['mode']=='live'
        for kind in ('true','oms'):
            job=client.post('/api/auth/'+org['inn']+'/'+kind,json={}).json()
            assert job['detached'] is False
            assert base64.b64decode(job['payload_base64'])==b'SIGN-THIS-UTF8-CHALLENGE'
            result=client.post('/api/jobs/'+job['id']+'/submit',json={'signature':SIGNATURE})
            assert result.json()['state']=='done',result.text
        scan=client.post('/api/scan',json={'code':SOURCE})
        assert scan.status_code==200,scan.text
        assert scan.json()['pn']=='PILOT-PN-100' and scan.json()['max_quantity']==99
        op=client.post('/api/operations',json={'code':SOURCE,'quantity':17,'request_id':str(uuid4())}).json()
        jobs=client.get('/api/state').json()['jobs']
        job=next(j for j in jobs if j['kind']=='order')
        signable=client.get('/api/jobs/'+job['id']).json()
        assert signable['detached'] is True
        body=base64.b64decode(signable['payload_base64'])
        result=client.post('/api/jobs/'+job['id']+'/submit',json={'signature':SIGNATURE})
        assert result.json()['state']=='accepted',result.text
        sent=next(r for r in fixture.calls if r.url.path=='/api/v3/order')
        assert sent.content==body
        result=client.post('/api/operations/'+op['id']+'/poll',json={})
        assert result.json()['state']=='ready',result.text
        pdf=client.get('/api/operations/'+op['id']+'/label.pdf')
        assert pdf.status_code==200 and pdf.content.startswith(b'%PDF')
        # There must be no DEMO watermark on a live label.
        from pypdf import PdfReader
        from io import BytesIO
        assert 'DEMO' not in PdfReader(BytesIO(pdf.content)).pages[0].extract_text()
        job=client.post('/api/reports',json={'operation_ids':[op['id']],'confirmed_applied':True}).json()[0]
        assert client.post('/api/jobs/'+job['id']+'/submit',json={'signature':SIGNATURE}).json()['state']=='accepted'
        assert client.post('/api/reports/'+job['id']+'/poll',json={}).json()['state']=='done'
        assert not client.get('/api/state').json()['operations']
        assert client.get('/api/state?history=true').json()['operations'][0]['state']=='done'
        assert client.post('/api/scan',json={'code':SOURCE}).json()['available']==83
        # No external re-issue on a duplicate browser submit.
        client.post('/api/jobs/'+signable['id']+'/submit',json={'signature':SIGNATURE})
        assert len([r for r in fixture.calls if r.url.path=='/api/v3/order'])==1
