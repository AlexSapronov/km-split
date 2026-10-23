import asyncio
import json
import os
import tempfile
from pathlib import Path
from uuid import UUID, uuid4
from typing import Literal
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict, StrictInt
from km_split.config import Settings
from km_split.db import Store
from km_split.clients import Gateway, RemoteError
from km_split.service import Service

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
    organizations: list[Organization] = Field(max_length=30)
    revision: str = Field(min_length=64, max_length=64)


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
        if body.mode == settings.mode and organizations == current['organizations']:
            return await configuration()
        if settings.mode == 'live' and service.store.one("SELECT id FROM operations WHERE state NOT IN ('done','cancelled') LIMIT 1"):
            raise ValueError('Есть незавершённые рабочие разделения. Завершите или разберите их перед сменой настроек')
        if settings.mode == 'live' and service.store.one("SELECT id FROM jobs WHERE state='sending' LIMIT 1"):
            raise ValueError('Дождитесь завершения отправки в ЧЗ')
        root = settings.root or settings.data
        config = root / 'configuration.json'
        old_bytes = config.read_bytes() if config.exists() else None
        payload = json.dumps({'mode': body.mode, 'organizations': organizations}, ensure_ascii=False, indent=2)
        fd, temporary = tempfile.mkstemp(prefix='configuration-', suffix='.tmp', dir=root)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(payload); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, config)
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
        job = service.job_view(str(job_id))
        return JSONResponse({k: job[k] for k in ('id', 'kind', 'inn', 'organization', 'payload_base64', 'detached', 'sha256')},
                            headers={'Content-Disposition': f'attachment; filename="sign-{job_id}.json"'})

    @app.get('/api/sign-helper.ps1')
    async def helper():
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
