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



def test_field_errors_are_extracted_from_nested_payloads():
    detail = error_detail({
        'fieldErrors': [
            {'field': 'sntins[0]', 'errorCode': '7360', 'errorMessage': 'Некорректный формат КМ'},
            {'fieldName': 'attributes.participantId', 'message': 'ИНН участника не совпадает'}
        ]
    })
    assert 'sntins[0]' in detail
    assert '7360' in detail
    assert 'Некорректный формат КМ' in detail
    assert 'attributes.participantId' in detail
    assert 'ИНН участника не совпадает' in detail


def test_top_level_error_array_is_supported():
    detail = error_detail([
        {'field': 'sntins[1]', 'message': 'Код не найден'},
        {'code': 'rights', 'description': 'Недостаточно прав'}
    ])
    assert 'sntins[1]: Код не найден' in detail
    assert 'rights' in detail
    assert 'Недостаточно прав' in detail


def test_request_codes_are_redacted_from_remote_diagnostics():
    code = '0104640638345218215DEMO000000000000000'
    secrets = request_secrets(json.dumps({'sntins': [code]}).encode(), {})
    detail = error_detail({'fieldErrors': [{'field': 'sntins[0]', 'message': f'Ошибка для {code}'}]}, secrets)
    assert 'sntins[0]' in detail
    assert code not in detail
    assert '[скрыто]' in detail


async def test_gateway_surfaces_field_errors_from_oms(tmp_path):
    payload = {'fieldErrors': [{'field': 'sntins[0]', 'errorCode': '7360',
                                'message': 'Некорректная структура элемента'}]}
    gateway = Gateway(None, None, httpx.MockTransport(
        lambda request: httpx.Response(400, json=payload)))
    with pytest.raises(RemoteError) as exc:
        await gateway.request('POST', 'https://example.invalid/api/v3/utilisation',
                              body=json.dumps({'sntins': ['0104640638345218215DEMO000000000000000']}).encode(),
                              headers={'X-Signature': 'signed-cms-material'})
    message = str(exc.value)
    assert 'HTTP 400' in message
    assert 'sntins[0]' in message
    assert '7360' in message
    assert 'Некорректная структура элемента' in message
    assert '0104640638345218215DEMO000000000000000' not in message
    assert 'signed-cms-material' not in message


async def test_non_json_http_error_reports_content_type_and_safe_text():
    gateway = Gateway(None, None, httpx.MockTransport(
        lambda request: httpx.Response(400, text='upstream validation failed',
                                       headers={'content-type': 'text/plain; charset=utf-8'})))
    with pytest.raises(RemoteError) as exc:
        await gateway.request('POST', 'https://example.invalid/api/v3/utilisation', mutating=True)
    message = str(exc.value)
    assert 'Content-Type: text/plain' in message
    assert 'upstream validation failed' in message
    assert not exc.value.uncertain


async def test_unknown_json_error_reports_shape_without_dumping_values():
    gateway = Gateway(None, None, httpx.MockTransport(
        lambda request: httpx.Response(400, json={'traceId': 'do-not-leak-this', 'unexpected': {'secret': 'value'}})))
    with pytest.raises(RemoteError) as exc:
        await gateway.request('POST', 'https://example.invalid/api/v3/utilisation')
    message = str(exc.value)
    assert 'ключи: traceId, unexpected' in message
    assert 'do-not-leak-this' not in message
    assert 'value' not in message
