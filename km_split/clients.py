"""Only the server talks to True API/OMS. No mutating request is retried."""
import json
import re
from uuid import uuid4
import httpx
from .codes import parse


class RemoteError(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def error_detail(payload, sensitive=()):
    """Only documented diagnostic fields; never dump response or signed request."""
    parts = []
    def collect(value, depth=0):
        if depth > 2 or not isinstance(value, dict):
            return
        for key in ('code', 'errorCode', 'error_code', 'error_message', 'errorMessage', 'message', 'description'):
            item = value.get(key)
            if isinstance(item, (str, int)) and not isinstance(item, bool) and str(item).strip():
                text = str(item).strip()
                if text not in parts:
                    parts.append(text)
        error = value.get('error')
        if isinstance(error, str) and error.strip() and error not in parts:
            parts.append(error.strip())
        elif isinstance(error, dict):
            collect(error, depth + 1)
        if isinstance(value.get('errors'), list):
            for item in value['errors'][:5]:
                collect(item, depth + 1)
    collect(payload)
    text = '; '.join(parts)
    for secret in sorted((s for s in sensitive if isinstance(s, str) and len(s) >= 8), key=len, reverse=True):
        text = text.replace(secret, '[скрыто]')
        text = text.replace(''.join(secret.split()), '[скрыто]')
        text = text.replace(json.dumps(secret)[1:-1], '[скрыто]')
    # Catch otherwise unknown CMS, certificates, JWTs, or tokens echoed in prose.
    text = re.sub(r'\beyJ[A-Za-z0-9_.=-]{30,}', '[скрыто]', text)
    text = re.sub(r'[A-Za-z0-9+/=_-]{80,}', '[скрыто]', text)
    text = ''.join(c if c >= ' ' else ' ' for c in text)
    return text[:1200]


def request_secrets(body, headers):
    sensitive = []
    for key, value in (headers or {}).items():
        if key.lower() in ('authorization', 'clienttoken', 'x-signature'):
            sensitive.extend([value, value.removeprefix('Bearer ').removeprefix('token ')])
    try:
        data = json.loads(body) if body else {}
        if isinstance(data, dict):
            sensitive.extend(data.get(key) for key in ('uuid', 'data', 'signature', 'token', 'uuidToken'))
    except (ValueError, TypeError):
        pass
    return sensitive


class Gateway:
    def __init__(self, settings, store, transport=None):
        self.settings, self.store, self.transport = settings, store, transport

    def org(self, inn):
        for org in self.settings.organizations:
            if org['inn'] == inn:
                return org
        raise ValueError('Организация не настроена')

    def token(self, inn, kind):
        row = self.store.one('SELECT token FROM tokens WHERE inn=? AND kind=?', (inn, kind))
        if not row:
            raise ValueError(f'Обновите доступ {"СУЗ" if kind == "oms" else "ГИС МТ"} для ИНН {inn}')
        return row['token']

    async def request(self, method, url, headers=None, body=None, params=None, mutating=False, context='ЧЗ'):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(40, connect=10), transport=self.transport,
                                         follow_redirects=False) as client:
                response = await client.request(method, url, headers=headers, content=body, params=params)
        except httpx.HTTPError:
            raise RemoteError('Нет подтверждения от ЧЗ. Проверьте состояние операции перед повтором.' if mutating
                              else 'ЧЗ недоступен. Повторите проверку позже.', uncertain=mutating)
        if response.status_code not in (200, 201):
            try:
                diagnostic = error_detail(response.json(), request_secrets(body, headers))
            except ValueError:
                diagnostic = ''
            message = f'{context}: HTTP {response.status_code}.'
            message += (' ' + diagnostic) if diagnostic else ' ЧЗ не передал описание в распознанном формате.'
            raise RemoteError(message,
                              uncertain=mutating and (response.status_code >= 500 or response.status_code in (408, 429)))
        try:
            result = response.json()
        except ValueError:
            raise RemoteError('Неожиданный формат ответа ЧЗ', uncertain=mutating)
        return result

    async def info(self, raw, inn=None):
        code = parse(raw)
        if self.settings.mode == 'demo':
            # Simulate the registry from persisted successful reports, not transient memory.
            done = self.store.one("SELECT COALESCE(SUM(quantity),0) n FROM operations WHERE source=? AND state='done'", (code.cis,))['n']
            return dict(cis=code.cis, full=code.full, gtin=code.gtin, inn='7700000000',
                        pn='DEMO-PN-100', quantity=100-done, status='INTRODUCED')
        orgs = [self.org(inn)] if inn else self.settings.organizations
        errors = []
        for org in orgs:
            try:
                result = await self.request('POST', org['true_api_url'] + '/api/v3/true-api/cises/info',
                    headers={'Authorization': 'Bearer ' + self.token(org['inn'], 'true'), 'Content-Type': 'application/json'},
                    body=json.dumps([code.cis]).encode())
            except (RemoteError, ValueError) as e:
                errors.append(str(e)); continue
            if not isinstance(result, list) or len(result) != 1 or result[0].get('errorCode') or result[0].get('errorMessage'):
                continue
            ci = result[0].get('cisInfo', {})
            owner = ci.get('ownerInn')
            self.org(owner)  # Unknown owners fail closed.
            if owner != org['inn']:
                if inn is not None:
                    raise ValueError('Владелец КМ не соответствует выбранной организации')
                # Re-query with the actual owner's credentials before any emission.
                return await self.info(raw, owner)
            if ci.get('status') != 'INTRODUCED':
                raise ValueError('Разделять можно только исходный КМ со статусом «В обороте»')
            if ci.get('requestedCis', ci.get('cis')) != code.cis:
                raise ValueError('ЧЗ вернул сведения о другом КМ')
            quantity = ci.get('quantityInPack')
            if isinstance(quantity, str) and quantity.isascii() and quantity.isdigit():
                quantity = int(quantity)
            if type(quantity) is not int or not 2 <= quantity <= 99999999:
                raise ValueError('Нет достоверного целого количества больше 1 в ЧЗ')
            pn = ci.get('model')
            if not pn:
                products = await self.request('POST', org['true_api_url'] + '/api/v4/true-api/product/info',
                    headers={'Authorization': 'Bearer ' + self.token(owner, 'true'), 'Content-Type': 'application/json'},
                    body=json.dumps({'gtins': [code.gtin]}).encode())
                if isinstance(products, dict) and isinstance(products.get('results'), list):
                    for product in products['results']:
                        data = product.get('productInfo') or product
                        if str(data.get('gtin')) == code.gtin:
                            pn = data.get('model')
            if not isinstance(pn, str) or not pn.strip() or len(pn) > 120:
                raise ValueError('Не найден достоверный PN (model) в ЧЗ; эмиссия не начата')
            return dict(cis=code.cis, full=code.full, gtin=code.gtin, inn=owner,
                        pn=pn.strip(), quantity=quantity, status=ci['status'])
        raise ValueError(errors[0] if errors else 'КМ не найден или нет прав на сведения о нём')

    async def challenge(self, inn):
        org = self.org(inn)
        if self.settings.mode == 'demo':
            return {'uuid': str(uuid4()), 'data': 'DEMO-CHALLENGE'}
        value = await self.request('GET', org['true_api_url'] + '/api/v3/true-api/auth/key')
        if not isinstance(value, dict) or not value.get('uuid') or not value.get('data'):
            raise ValueError('ЧЗ не вернул данные авторизации')
        return value

    async def sign_in(self, job, signature):
        if self.settings.mode == 'demo':
            return 'demo-token'
        org = self.org(job['inn'])
        challenge = json.loads(job['body'])
        path = '/api/v3/true-api/auth/simpleSignIn'
        if job['kind'] == 'auth_oms':
            path += '/' + org['oms_connection']
        body = {'uuid': challenge['uuid'], 'data': signature, 'inn': org['inn']}
        if job['kind'] == 'auth_true':
            body['unitedToken'] = True
        result = await self.request('POST', org['true_api_url'] + path,
                                    headers={'Content-Type': 'application/json'}, body=json.dumps(body).encode(),
                                    context='Авторизация СУЗ' if job['kind'] == 'auth_oms' else 'Авторизация ГИС МТ')
        if not isinstance(result, dict):
            raise RemoteError('ЧЗ вернул некорректный ответ авторизации. Получите новый запрос и повторите вход')
        token = result.get('token') if job['kind'] == 'auth_oms' else result.get('uuidToken')
        if not token:
            diagnostic = error_detail(result, (signature, challenge['uuid']))
            raise RemoteError(('Авторизация СУЗ' if job['kind'] == 'auth_oms' else 'Авторизация ГИС МТ') +
                              ': ' + (diagnostic or 'ЧЗ не вернул токен авторизации. Получите новый запрос и повторите вход'))
        return token

    async def oms(self, inn, path, params=None, body=None, signature=None):
        org = self.org(inn)
        if self.settings.mode == 'demo':
            if path == '/api/v3/order':
                return {'orderId': str(uuid4())}
            if path == '/api/v3/order/status':
                return [{'bufferStatus': 'ACTIVE', 'availableCodes': 1}]
            if path == '/api/v3/codes':
                serial = '5' + uuid4().hex[:19]
                return {'codes': ['01' + params['gtin'] + '21' + serial + '\x1d93dGVz'], 'blockId': str(uuid4())}
            if path == '/api/v3/utilisation':
                return {'reportId': str(uuid4())}
            if path == '/api/v3/report/info':
                return {'reportStatus': 'SUCCESS'}
            if path == '/api/v3/receipts/receipt':
                return {'results': [{'resultDocId': params['resultDocId'], 'sourceDocId': str(uuid4()), 'state': 'SUCCESS', 'code': 0}]}
            raise ValueError('Unknown demo endpoint')
        headers = {'clientToken': self.token(inn, 'oms'), 'Accept': 'application/json'}
        if body is not None:
            headers['Content-Type'] = 'application/json;charset=UTF-8'
            headers['X-Signature'] = signature
        return await self.request('POST' if body is not None else 'GET', org['oms_url'] + path,
            headers=headers, body=body.encode('utf-8') if body is not None else None,
            params={'omsId': org['oms_id'], **(params or {})},
            mutating=body is not None or path == '/api/v3/codes')
