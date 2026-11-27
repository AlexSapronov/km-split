import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from uuid import UUID, uuid4, uuid5
from typing import Literal
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict, StrictInt
from km_split.config import Settings
from km_split.db import Store
from km_split.clients import Gateway, RemoteError
from km_split.service import Service
from km_split.codes import GS, parse, with_quantity
from km_split.labels import render_labels

ROOT = Path(__file__).resolve().parent


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Scan(Body):
    code: str = Field(min_length=1, max_length=1024)


class Split(Scan):
    quantity: StrictInt = Field(ge=1, le=99999999)
    request_id: UUID


class Signature(Body):
    signature: str = Field(min_length=1, max_length=100000)


class Report(Body):
    operation_ids: list[UUID] = Field(min_length=1, max_length=1000)
    confirmed_applied: bool


class Recover(Body):
    remote_id: UUID


class Organization(Body):
    inn: str = Field(min_length=10, max_length=12)
    name: str = Field(min_length=1, max_length=120)
    oms_id: str
    oms_connection: str
    true_api_url: str = 'https://markirovka.crpt.ru'
    oms_url: str = 'https://suzgrid.crpt.ru'
    template_id: Literal[36, 37] = 36
    producer_inn: str | None = Field(default=None, max_length=12)


class Configuration(Body):
    mode: Literal['live', 'demo']
    file_signing_enabled: bool = False
    organizations: list[Organization] = Field(max_length=30)
    revision: str = Field(min_length=64, max_length=64)


class InternalSplit(Body):
    request_id: UUID
    code: str = Field(min_length=1, max_length=1024)
    parts: list[StrictInt] = Field(min_length=1, max_length=32)


class InternalSplitConfirm(Body):
    scanned_cis: list[str] = Field(min_length=1, max_length=33)


class SignerResult(Body):
    id: str = Field(min_length=1, max_length=64)
    lease: str = Field(min_length=1, max_length=128)
    sha256: str = Field(min_length=64, max_length=64)
    signature: str = Field(min_length=1, max_length=1024 * 1024)
    thumbprint: str = Field(min_length=40, max_length=64)


class SignerHeartbeat(Body):
    thumbprint: str = Field(min_length=40, max_length=64)
    expires: str = Field(min_length=1, max_length=80)
    status: str = Field(max_length=120)



def create_app(settings=None, gateway_factory=Gateway):
    settings = settings or Settings.load()
    settings.data.mkdir(exist_ok=True, parents=True, mode=0o700)
    marker = settings.data / 'mode'
    if marker.exists() and marker.read_text() != settings.mode:
        raise ValueError('Для live и demo нужны разные каталоги SPLIT_DATA; смешивание запрещено')
    marker.write_text(settings.mode)
    store = Store(settings.data / 'split.sqlite3')
    service = Service(settings, store, gateway_factory(settings, store))
    app = FastAPI(title='Разделение КМ', docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    write_lock = asyncio.Lock()

    def require_internal(request: Request):
        supplied = request.headers.get('Authorization', '').removeprefix('Bearer ')
        if not settings.internal_token or not hmac.compare_digest(supplied, settings.internal_token):
            raise HTTPException(403, 'Внутренний API не авторизован')

    def require_signer(request: Request):
        supplied = request.headers.get('Authorization', '').removeprefix('Bearer ')
        if not settings.signer_token or not hmac.compare_digest(supplied, settings.signer_token):
            raise HTTPException(403, 'Подписант не авторизован')
        if request.url.scheme != 'https' and request.client.host not in ('testclient', '127.0.0.1', '::1'):
            raise HTTPException(403, 'Требуется HTTPS')

    def update_batch(batch_id, **values):
        allowed = {'operation_ids', 'expected_codes', 'report_job_ids', 'state', 'error'}
        if set(values) - allowed:
            raise ValueError('Недопустимое поле состояния разделения')
        assignments, args = [], []
        for key, value in values.items():
            if key in ('operation_ids', 'expected_codes', 'report_job_ids'):
                value = json.dumps(value, ensure_ascii=False)
            assignments.append(key + '=?')
            args.append(value)
        assignments.append('updated=?')
        from km_split.db import now
        args.extend([now(), batch_id])
        with store.transaction() as db:
            db.execute('UPDATE auto_split_batches SET ' + ','.join(assignments) + ' WHERE id=?', args)

    def batch_payload(row):
        expected = json.loads(row['expected_codes'] or '[]')
        return {
            'id': row['id'], 'state': row['state'], 'error': row['error'],
            'parts': json.loads(row['parts']), 'operation_ids': json.loads(row['operation_ids']),
            'expected_codes': [{'cis': item['cis'], 'quantity': item['quantity'], 'role': item['role']}
                               for item in expected],
            'labels_url': '/api/internal/splits/' + row['id'] + '/labels.pdf' if expected else None,
        }

    async def refresh_batch(batch_id):
        row = store.one('SELECT * FROM auto_split_batches WHERE id=?', (batch_id,))
        if not row:
            raise ValueError('Задание разделения не найдено')
        if row['state'] in ('failed', 'done', 'creating'):
            return batch_payload(row)
        operation_ids = json.loads(row['operation_ids'])
        report_job_ids = json.loads(row['report_job_ids'])
        if row['state'] in ('ordering', 'ready_to_rescan'):
            operations = []
            try:
                for op_id in operation_ids:
                    op = service.operation(op_id)
                    if op['state'] == 'order_pending':
                        op = await service.poll_order(op_id)
                    operations.append(op)
            except Exception as exc:
                update_batch(batch_id, state='failed', error=str(exc)[:1000])
                return batch_payload(store.one('SELECT * FROM auto_split_batches WHERE id=?', (batch_id,)))
            if operations and all(op['state'] == 'ready' for op in operations):
                source = parse(row['source_code'])
                source_row = store.one('SELECT baseline FROM sources WHERE cis=?', (row['source_cis'],))
                if not source_row:
                    update_batch(batch_id, state='failed', error='Не найден исходный КМ в журнале разделений')
                else:
                    remainder = source_row['baseline'] - sum(json.loads(row['parts']))
                    if remainder < 1:
                        update_batch(batch_id, state='failed', error='После разделения должен остаться положительный остаток')
                    else:
                        base_code = source.full.rsplit(GS + '30', 1)[0] if source.quantity is not None else source.full
                        updated_source = with_quantity(base_code, remainder)
                        entries = []
                        for op in operations:
                            entries.append({'code': op['new_code'], 'cis': parse(op['new_code']).cis,
                                            'quantity': op['quantity'], 'role': 'new', 'pn': op['pn']})
                        entries.append({'code': updated_source, 'cis': parse(updated_source).cis,
                                        'quantity': remainder, 'role': 'source', 'pn': operations[0]['pn']})
                        update_batch(batch_id, expected_codes=entries, state='ready_to_rescan', error=None)
        if row['state'] == 'reporting':
            jobs = []
            for job_id in report_job_ids:
                job = service.job(job_id)
                if job['state'] == 'accepted':
                    job = await service.poll_report(job_id)
                jobs.append(job)
            if jobs and all(job['state'] == 'done' for job in jobs):
                update_batch(batch_id, state='done', error=None)
            elif any(job['state'] in ('rejected', 'unknown') for job in jobs):
                detail = next((job.get('error') for job in jobs if job.get('error')), '')
                update_batch(batch_id, state='failed', error=detail or 'DIVISIONMARK не подтверждён')
        return batch_payload(store.one('SELECT * FROM auto_split_batches WHERE id=?', (batch_id,)))

    @app.middleware('http')
    async def protect(request: Request, call_next):
        if request.url.path == '/health':
            return JSONResponse({'status': 'ok'})
        service_api = request.url.path.startswith(('/api/internal/', '/api/signer/'))
        if service_api and request.method not in ('GET', 'HEAD', 'OPTIONS'):
            # These routes use independent bearer-token authentication; they are not browser actions.
            length = request.headers.get('content-length', '0')
            maximum = 1500000 if request.url.path == '/api/signer/result' else 200000
            if not length.isdigit() or int(length) > maximum:
                return JSONResponse({'detail': 'Слишком большой запрос'}, status_code=413)
            response = await call_next(request)
        elif request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if request.headers.get('X-Requested-With') != 'km-split':
                return JSONResponse({'detail': 'Обновите страницу приложения'}, status_code=403)
            origin = request.headers.get('origin')
            if origin and origin != str(request.base_url).rstrip('/'):
                return JSONResponse({'detail': 'Недопустимый источник запроса'}, status_code=403)
            length = request.headers.get('content-length', '0')
            if not length.isdigit() or int(length) > 200000:
                return JSONResponse({'detail': 'Слишком большой запрос'}, status_code=413)
            # Configuration cannot change while a signed external request is in flight.
            async with write_lock:
                if request.url.path != '/api/settings' and request.headers.get('X-Split-Mode', settings.mode) != settings.mode:
                    return JSONResponse({'detail': 'Режим работы изменился в другой вкладке. Обновите страницу перед продолжением'}, status_code=409)
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    @app.exception_handler(RemoteError)
    async def remote_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=502)

    @app.post('/api/signer/heartbeat')
    def signer_heartbeat(body: SignerHeartbeat, request: Request):
        require_signer(request)
        if not re.fullmatch(r'[A-F0-9]{40}', body.thumbprint.replace(' ', '').upper()):
            raise HTTPException(422, 'Некорректный отпечаток сертификата')
        from km_split.db import now
        with store.transaction() as db:
            db.execute('INSERT OR REPLACE INTO signer_heartbeats VALUES (1,?,?,?,?)',
                       (body.thumbprint.replace(' ', '').upper(), body.expires, body.status, now()))
        return {'ok': True}

    @app.post('/api/signer/next')
    def signer_next(request: Request):
        require_signer(request)
        if not re.fullmatch(r'[A-F0-9]{40}', settings.signer_thumbprint):
            raise HTTPException(409, 'Настройте SPLIT_SIGNER_THUMBPRINT')
        with store.transaction() as db:
            db.execute('DELETE FROM signer_leases WHERE expires<?', (time.time(),))
            row = db.execute("SELECT j.* FROM jobs j WHERE j.state='prepared' AND NOT EXISTS (SELECT 1 FROM signer_leases l WHERE l.job_id=j.id) ORDER BY j.created LIMIT 1").fetchone()
            if row is None:
                return Response(status_code=204)
            job = service.job_view(row['id'])
            payload = base64.b64decode(job['payload_base64'])
            sha = hashlib.sha256(payload).hexdigest()
            lease = secrets.token_urlsafe(32)
            db.execute('INSERT OR REPLACE INTO signer_leases VALUES (?,?,?,?)',
                       (row['id'], lease, sha, time.time() + 180))
            return {'id': row['id'], 'kind': row['kind'], 'lease': lease,
                    'payload_base64': job['payload_base64'], 'sha256': sha,
                    'detached': job['detached'], 'thumbprint': settings.signer_thumbprint,
                    'inn': row['inn']}

    @app.post('/api/signer/result')
    async def signer_result(body: SignerResult, request: Request):
        require_signer(request)
        try:
            signature = base64.b64decode(body.signature, validate=True)
            if not 64 <= len(signature) <= 750000:
                raise ValueError()
        except Exception:
            raise HTTPException(422, 'Неверный формат подписи')
        if not hmac.compare_digest(body.thumbprint.replace(' ', '').upper(), settings.signer_thumbprint):
            raise HTTPException(403, 'Другой сертификат')
        with store.transaction() as db:
            lease = db.execute('SELECT * FROM signer_leases WHERE job_id=?', (body.id,)).fetchone()
            if not lease or lease['expires'] < time.time() or not hmac.compare_digest(lease['token'], body.lease):
                raise HTTPException(409, 'Истёк срок задания подписи')
            if not hmac.compare_digest(lease['sha256'], body.sha256):
                raise HTTPException(409, 'Подписаны другие байты')
            db.execute('DELETE FROM signer_leases WHERE job_id=?', (body.id,))
        return await service.submit(body.id, body.signature)

    @app.post('/api/internal/splits')
    async def create_internal_split(body: InternalSplit, request: Request):
        require_internal(request)
        code = parse(body.code)
        if any(type(q) is not int or q < 1 or q > 99999999 for q in body.parts):
            raise HTTPException(422, 'Количество каждой части должно быть положительным целым числом')
        fingerprint = hashlib.sha256(json.dumps([code.full, body.parts], ensure_ascii=False,
                                                separators=(',', ':')).encode()).hexdigest()
        existing = store.one('SELECT * FROM auto_split_batches WHERE request_id=?', (str(body.request_id),))
        if existing and existing['fingerprint'] != fingerprint:
            raise HTTPException(409, 'Идентификатор запроса уже использован с другими данными')
        if existing and existing['state'] != 'creating':
            return await refresh_batch(existing['id'])
        if existing:
            batch_id = existing['id']
            operation_ids = json.loads(existing['operation_ids'])
        else:
            info = await service.gateway.info(code.full)
            if info['status'] != 'INTRODUCED' or sum(body.parts) >= info['quantity']:
                raise HTTPException(409, 'Разделение возможно только для КМ в обороте с положительным остатком')
            batch_id = str(uuid4())
            from km_split.db import now
            with store.transaction() as db:
                db.execute('''INSERT INTO auto_split_batches
                    (id,request_id,fingerprint,source_cis,source_code,parts,state,created,updated)
                    VALUES (?,?,?,?,?,?, 'creating',?,?)''',
                    (batch_id, str(body.request_id), fingerprint, code.cis, code.full,
                     json.dumps(body.parts), now(), now()))
            operation_ids = []
        try:
            for index in range(len(operation_ids), len(body.parts)):
                quantity = body.parts[index]
                op = await service.create(code.full, quantity, str(uuid5(body.request_id, str(index))))
                operation_ids.append(op['id'])
                with store.transaction() as db:
                    db.execute('UPDATE auto_split_batches SET operation_ids=?,state=?,updated=? WHERE id=?',
                               (json.dumps(operation_ids), 'ordering',
                                __import__('km_split.db', fromlist=['now']).now(), batch_id))
        except Exception as exc:
            update_batch(batch_id, operation_ids=operation_ids, state='failed',
                         error=('Часть заказов уже создана. Не повторяйте разделение; требуется разбор: ' + str(exc))[:1000])
            return batch_payload(store.one('SELECT * FROM auto_split_batches WHERE id=?', (batch_id,)))
        return await refresh_batch(batch_id)

    @app.get('/api/internal/splits/{batch_id}')
    async def internal_split_status(batch_id: UUID, request: Request):
        require_internal(request)
        return await refresh_batch(str(batch_id))

    @app.get('/api/internal/splits/{batch_id}/labels.pdf')
    async def internal_split_labels(batch_id: UUID, request: Request):
        require_internal(request)
        result = await refresh_batch(str(batch_id))
        if result['state'] not in ('ready_to_rescan', 'reporting', 'done'):
            raise HTTPException(409, 'Этикетки ещё не готовы')
        row = store.one('SELECT * FROM auto_split_batches WHERE id=?', (str(batch_id),))
        entries = json.loads(row['expected_codes'])
        pdf = render_labels(entries, settings.mode == 'demo')
        return Response(pdf, media_type='application/pdf',
                        headers={'Content-Disposition': 'attachment; filename="KM-split-labels.pdf"',
                                 'Cache-Control': 'no-store'})

    @app.post('/api/internal/splits/{batch_id}/confirm')
    async def confirm_internal_split(batch_id: UUID, body: InternalSplitConfirm, request: Request):
        require_internal(request)
        current = await refresh_batch(str(batch_id))
        if current['state'] in ('reporting', 'done'):
            return current
        if current['state'] != 'ready_to_rescan':
            raise HTTPException(409, 'Разделение ещё не готово к подтверждению этикеток')
        row = store.one('SELECT * FROM auto_split_batches WHERE id=?', (str(batch_id),))
        expected = json.loads(row['expected_codes'])
        expected_cis = [item['cis'] for item in expected]
        if len(body.scanned_cis) != len(set(body.scanned_cis)) or set(body.scanned_cis) != set(expected_cis):
            raise HTTPException(409, 'Повторно отсканируйте все этикетки: исходный КМ с новым количеством и каждый новый КМ')
        jobs = await service.prepare_reports(json.loads(row['operation_ids']), True)
        update_batch(str(batch_id), report_job_ids=[job['id'] for job in jobs], state='reporting', error=None)
        return await refresh_batch(str(batch_id))

    @app.get('/')
    async def index():
        return FileResponse(ROOT / 'static/index.html')

    @app.get('/admin')
    async def admin():
        return FileResponse(ROOT / 'static/admin.html')

    @app.get('/api/state')
    async def state(history: bool = False):
        return service.state(history)

    @app.post('/api/request-id')
    async def request_id():
        return {'request_id': str(uuid4())}

    @app.get('/api/settings')
    async def configuration():
        bridge = ROOT / 'static/vendor/cadesplugin_api.js'
        return {**settings.configuration(), 'crypto_bridge_installed': bridge.exists()}

    @app.post('/api/settings')
    async def save_configuration(body: Configuration):
        nonlocal settings, service
        current = settings.configuration()
        if body.revision != current['revision']:
            raise ValueError('Настройки изменились в другой вкладке. Загрузите актуальные настройки')
        organizations = [o.model_dump(exclude_none=True) for o in body.organizations]
        Settings.validate_organizations(organizations)
        if body.mode == 'live' and not organizations:
            raise ValueError('Добавьте хотя бы одну организацию для рабочего режима')
        # Do not strand paid codes or route old signatures through changed settings.
        operational_change = body.mode != settings.mode or organizations != current['organizations']
        if not operational_change and body.file_signing_enabled == settings.file_signing_enabled:
            return await configuration()
        if operational_change and settings.mode == 'live' and service.store.one("SELECT id FROM operations WHERE state NOT IN ('done','cancelled') LIMIT 1"):
            raise ValueError('Есть незавершённые рабочие разделения. Завершите или разберите их перед сменой настроек')
        if operational_change and settings.mode == 'live' and service.store.one("SELECT id FROM jobs WHERE state='sending' LIMIT 1"):
            raise ValueError('Дождитесь завершения отправки в ЧЗ')
        root = settings.root or settings.data
        config = root / 'configuration.json'
        old_bytes = config.read_bytes() if config.exists() else None
        payload = json.dumps({'mode': body.mode, 'organizations': organizations,
                              'file_signing_enabled': body.file_signing_enabled}, ensure_ascii=False, indent=2)
        fd, temporary = tempfile.mkstemp(prefix='configuration-', suffix='.tmp', dir=root)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(payload); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, config)
            if not operational_change:
                settings.file_signing_enabled = body.file_signing_enabled
                return await configuration()
            try:
                updated = Settings.from_root(root)
                updated_store = Store(updated.data / 'split.sqlite3')
                # Expire tokens if endpoint/connection/permissions settings changed.
                old_orgs = {o['inn']: o for o in current['organizations']}
                with updated_store.transaction() as db:
                    for org in organizations:
                        if old_orgs.get(org['inn']) != org:
                            db.execute('DELETE FROM tokens WHERE inn=?', (org['inn'],))
                    db.execute("UPDATE jobs SET state='cancelled',updated=datetime('now') WHERE kind LIKE 'auth_%' AND state='prepared'")
                updated_service = Service(updated, updated_store, gateway_factory(updated, updated_store))
            except Exception:
                if old_bytes is None: config.unlink(missing_ok=True)
                else: config.write_bytes(old_bytes)
                raise
            settings, service = updated, updated_service
            app.state.service = service
        finally:
            Path(temporary).unlink(missing_ok=True)
        return await configuration()

    @app.post('/api/scan')
    async def scan(body: Scan):
        return await service.scan(body.code)

    @app.post('/api/operations')
    async def create(body: Split):
        return await service.create(body.code, body.quantity, str(body.request_id))

    @app.post('/api/auth/{inn}/{kind}')
    async def auth(inn: str, kind: str):
        return await service.prepare_auth(inn, kind)

    @app.get('/api/jobs/{job_id}')
    async def get_job(job_id: UUID):
        return service.job_view(str(job_id))

    @app.post('/api/jobs/{job_id}/submit')
    async def submit(job_id: UUID, body: Signature):
        return await service.submit(str(job_id), body.signature)

    @app.post('/api/jobs/{job_id}/cancel')
    async def cancel(job_id: UUID):
        return service.cancel(str(job_id))

    @app.post('/api/jobs/{job_id}/recover')
    async def recover(job_id: UUID, body: Recover):
        return await service.recover(str(job_id), str(body.remote_id))

    @app.get('/api/jobs/{job_id}/signing.json')
    async def signing_file(job_id: UUID):
        if not settings.file_signing_enabled:
            raise ValueError('Подпись через файл отключена в настройках')
        job = service.job_view(str(job_id))
        return JSONResponse({k: job[k] for k in ('id', 'kind', 'inn', 'organization', 'payload_base64', 'detached', 'sha256')},
                            headers={'Content-Disposition': f'attachment; filename="sign-{job_id}.json"'})

    @app.get('/api/sign-helper.ps1')
    async def helper():
        if not settings.file_signing_enabled:
            raise ValueError('Подпись через файл отключена в настройках')
        return FileResponse(ROOT / 'scripts/sign-job.ps1', filename='sign-job.ps1')

    @app.get('/api/operations/{op_id}/label.pdf')
    async def label(op_id: UUID):
        path = service.label(str(op_id))
        return FileResponse(path, filename=f'KM-{op_id}-58x40.pdf', media_type='application/pdf')

    @app.post('/api/operations/{op_id}/poll')
    async def poll_order(op_id: UUID):
        return await service.poll_order(str(op_id))

    @app.post('/api/reports')
    async def reports(body: Report):
        return await service.prepare_reports([str(i) for i in body.operation_ids], body.confirmed_applied)

    @app.post('/api/reports/{job_id}/poll')
    async def poll_report(job_id: UUID):
        return await service.poll_report(str(job_id))

    @app.get('/api/reports/{job_id}/receipt.json')
    async def receipt(job_id: UUID):
        path = settings.data / 'receipts' / (str(job_id) + '.json')
        if not path.exists():
            raise ValueError('Квитанция ещё не получена')
        return FileResponse(path, filename=f'receipt-{job_id}.json')

    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
    return app


def create_app(settings=None, gateway_factory=Gateway):
    settings = settings or Settings.load()
    settings.data.mkdir(exist_ok=True, parents=True, mode=0o700)
    marker = settings.data / 'mode'
    if marker.exists() and marker.read_text() != settings.mode:
        raise ValueError('Для live и demo нужны разные каталоги SPLIT_DATA; смешивание запрещено')
    marker.write_text(settings.mode)
    store = Store(settings.data / 'split.sqlite3')
    service = Service(settings, store, gateway_factory(settings, store))
    app = FastAPI(title='Разделение КМ', docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    write_lock = asyncio.Lock()

    @app.middleware('http')
    async def protect(request: Request, call_next):
        if request.url.path == '/health':
            return JSONResponse({'status': 'ok'})
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if request.headers.get('X-Requested-With') != 'km-split':
                return JSONResponse({'detail': 'Обновите страницу приложения'}, status_code=403)
            origin = request.headers.get('origin')
            if origin and origin != str(request.base_url).rstrip('/'):
                return JSONResponse({'detail': 'Недопустимый источник запроса'}, status_code=403)
            length = request.headers.get('content-length', '0')
            if not length.isdigit() or int(length) > 200000:
                return JSONResponse({'detail': 'Слишком большой запрос'}, status_code=413)
            # Configuration cannot change while a signed external request is in flight.
            async with write_lock:
                if request.url.path != '/api/settings' and request.headers.get('X-Split-Mode', settings.mode) != settings.mode:
                    return JSONResponse({'detail': 'Режим работы изменился в другой вкладке. Обновите страницу перед продолжением'}, status_code=409)
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    @app.exception_handler(RemoteError)
    async def remote_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=502)

    @app.get('/')
    async def index():
        return FileResponse(ROOT / 'static/index.html')

    @app.get('/admin')
    async def admin():
        return FileResponse(ROOT / 'static/admin.html')

    @app.get('/api/state')
    async def state(history: bool = False):
        return service.state(history)

    @app.post('/api/request-id')
    async def request_id():
        return {'request_id': str(uuid4())}

    @app.get('/api/settings')
    async def configuration():
        bridge = ROOT / 'static/vendor/cadesplugin_api.js'
        return {**settings.configuration(), 'crypto_bridge_installed': bridge.exists()}

    @app.post('/api/settings')
    async def save_configuration(body: Configuration):
        nonlocal settings, service
        current = settings.configuration()
        if body.revision != current['revision']:
            raise ValueError('Настройки изменились в другой вкладке. Загрузите актуальные настройки')
        organizations = [o.model_dump(exclude_none=True) for o in body.organizations]
        Settings.validate_organizations(organizations)
        if body.mode == 'live' and not organizations:
            raise ValueError('Добавьте хотя бы одну организацию для рабочего режима')
        # Do not strand paid codes or route old signatures through changed settings.
        operational_change = body.mode != settings.mode or organizations != current['organizations']
        if not operational_change and body.file_signing_enabled == settings.file_signing_enabled:
            return await configuration()
        if operational_change and settings.mode == 'live' and service.store.one("SELECT id FROM operations WHERE state NOT IN ('done','cancelled') LIMIT 1"):
            raise ValueError('Есть незавершённые рабочие разделения. Завершите или разберите их перед сменой настроек')
        if operational_change and settings.mode == 'live' and service.store.one("SELECT id FROM jobs WHERE state='sending' LIMIT 1"):
            raise ValueError('Дождитесь завершения отправки в ЧЗ')
        root = settings.root or settings.data
        config = root / 'configuration.json'
        old_bytes = config.read_bytes() if config.exists() else None
        payload = json.dumps({'mode': body.mode, 'organizations': organizations,
                              'file_signing_enabled': body.file_signing_enabled}, ensure_ascii=False, indent=2)
        fd, temporary = tempfile.mkstemp(prefix='configuration-', suffix='.tmp', dir=root)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(payload); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, config)
            if not operational_change:
                settings.file_signing_enabled = body.file_signing_enabled
                return await configuration()
            try:
                updated = Settings.from_root(root)
                updated_store = Store(updated.data / 'split.sqlite3')
                # Expire tokens if endpoint/connection/permissions settings changed.
                old_orgs = {o['inn']: o for o in current['organizations']}
                with updated_store.transaction() as db:
                    for org in organizations:
                        if old_orgs.get(org['inn']) != org:
                            db.execute('DELETE FROM tokens WHERE inn=?', (org['inn'],))
                    db.execute("UPDATE jobs SET state='cancelled',updated=datetime('now') WHERE kind LIKE 'auth_%' AND state='prepared'")
                updated_service = Service(updated, updated_store, gateway_factory(updated, updated_store))
            except Exception:
                if old_bytes is None: config.unlink(missing_ok=True)
                else: config.write_bytes(old_bytes)
                raise
            settings, service = updated, updated_service
            app.state.service = service
        finally:
            Path(temporary).unlink(missing_ok=True)
        return await configuration()

    @app.post('/api/scan')
    async def scan(body: Scan):
        return await service.scan(body.code)

    @app.post('/api/operations')
    async def create(body: Split):
        return await service.create(body.code, body.quantity, str(body.request_id))

    @app.post('/api/auth/{inn}/{kind}')
    async def auth(inn: str, kind: str):
        return await service.prepare_auth(inn, kind)

    @app.get('/api/jobs/{job_id}')
    async def get_job(job_id: UUID):
        return service.job_view(str(job_id))

    @app.post('/api/jobs/{job_id}/submit')
    async def submit(job_id: UUID, body: Signature):
        return await service.submit(str(job_id), body.signature)

    @app.post('/api/jobs/{job_id}/cancel')
    async def cancel(job_id: UUID):
        return service.cancel(str(job_id))

    @app.post('/api/jobs/{job_id}/recover')
    async def recover(job_id: UUID, body: Recover):
        return await service.recover(str(job_id), str(body.remote_id))

    @app.get('/api/jobs/{job_id}/signing.json')
    async def signing_file(job_id: UUID):
        if not settings.file_signing_enabled:
            raise ValueError('Подпись через файл отключена в настройках')
        job = service.job_view(str(job_id))
        return JSONResponse({k: job[k] for k in ('id', 'kind', 'inn', 'organization', 'payload_base64', 'detached', 'sha256')},
                            headers={'Content-Disposition': f'attachment; filename="sign-{job_id}.json"'})

    @app.get('/api/sign-helper.ps1')
    async def helper():
        if not settings.file_signing_enabled:
            raise ValueError('Подпись через файл отключена в настройках')
        return FileResponse(ROOT / 'scripts/sign-job.ps1', filename='sign-job.ps1')

    @app.get('/api/operations/{op_id}/label.pdf')
    async def label(op_id: UUID):
        path = service.label(str(op_id))
        return FileResponse(path, filename=f'KM-{op_id}-58x40.pdf', media_type='application/pdf')

    @app.post('/api/operations/{op_id}/poll')
    async def poll_order(op_id: UUID):
        return await service.poll_order(str(op_id))

    @app.post('/api/reports')
    async def reports(body: Report):
        return await service.prepare_reports([str(i) for i in body.operation_ids], body.confirmed_applied)

    @app.post('/api/reports/{job_id}/poll')
    async def poll_report(job_id: UUID):
        return await service.poll_report(str(job_id))

    @app.get('/api/reports/{job_id}/receipt.json')
    async def receipt(job_id: UUID):
        path = settings.data / 'receipts' / (str(job_id) + '.json')
        if not path.exists():
            raise ValueError('Квитанция ещё не получена')
        return FileResponse(path, filename=f'receipt-{job_id}.json')

    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
    return app
