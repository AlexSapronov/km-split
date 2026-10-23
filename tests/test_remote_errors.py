import json
import httpx
import pytest
from km_split.clients import Gateway, RemoteError, error_detail
from km_split.config import Settings
from km_split.db import Store


@pytest.mark.parametrize('status', [200, 400])
async def test_oms_auth_preserves_error_code_and_description(tmp_path, status):
    from tests.test_setup import organization
    settings = Settings(tmp_path, 'live', '', '', [organization()])
    gateway = Gateway(settings, Store(tmp_path/'errors.sqlite3'), httpx.MockTransport(lambda request:
        httpx.Response(status, json={'code':'003','error_message':'Идентификатор соединения для указанного УОТ не найден'})))
    job = {'inn':settings.organizations[0]['inn'], 'kind':'auth_oms', 'body':json.dumps({'uuid':'test-uuid','data':'challenge'})}
    with pytest.raises(RemoteError) as exc:
        await gateway.sign_in(job, 'signature-base64')
    assert 'Авторизация СУЗ' in str(exc.value)
    assert '003' in str(exc.value)
    assert 'Идентификатор соединения' in str(exc.value)
    assert not exc.value.uncertain


async def test_remote_error_redacts_signature_and_headers(tmp_path):
    signature = 'PRIVATE-SIGNATURE-' * 20
    token = 'private-bearer-token'
    data = {'code':'failure','description':f'Bad signature {signature}; token {token}', 'data':signature, 'token':token}
    gateway = Gateway(None, None, httpx.MockTransport(lambda request:httpx.Response(400,json=data)))
    with pytest.raises(RemoteError) as exc:
        await gateway.request('POST','https://example.invalid/auth',headers={'Authorization':'Bearer '+token},
                              body=json.dumps({'data':signature}).encode())
    assert 'failure' in str(exc.value)
    assert signature not in str(exc.value) and token not in str(exc.value)
    assert '[скрыто]' in str(exc.value)


def test_nested_diagnostics_do_not_dump_unrecognized_fields():
    detail = error_detail({'errors':[{'code':'rights','message':'Недостаточно прав'}],
                          'data':'DO-NOT-PRINT','token':'DO-NOT-PRINT','request':{'signature':'DO-NOT-PRINT'}})
    assert 'rights' in detail and 'Недостаточно прав' in detail
    assert 'DO-NOT-PRINT' not in detail


async def test_http_error_and_uncertain_status_still_block_retry():
    gateway = Gateway(None, None, httpx.MockTransport(lambda request:httpx.Response(503,json={'message':'Сервис недоступен'})))
    with pytest.raises(RemoteError) as exc:
        await gateway.request('POST','https://example.invalid/order',mutating=True)
    assert exc.value.uncertain and 'Сервис недоступен' in str(exc.value)
