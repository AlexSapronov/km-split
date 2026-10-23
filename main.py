import base64
import binascii
import secrets
from pathlib import Path
from uuid import UUID
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
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

    @app.middleware('http')
    async def protect(request: Request, call_next):
        if request.url.path == '/health':
            return JSONResponse({'status': 'ok'})
        authorized = False
        try:
            scheme, encoded = request.headers.get('authorization', '').split(' ', 1)
            user, password = base64.b64decode(encoded, validate=True).decode().split(':', 1)
            authorized = scheme.lower() == 'basic' and secrets.compare_digest(user.encode(), settings.user.encode()) and secrets.compare_digest(password.encode(), settings.password.encode())
        except (ValueError, binascii.Error, UnicodeDecodeError):
            pass
        if not authorized:
            return Response(status_code=401, headers={'WWW-Authenticate': 'Basic realm="KM Split", charset="UTF-8"'})
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if request.headers.get('X-Requested-With') != 'km-split':
                return JSONResponse({'detail': 'Обновите страницу приложения'}, status_code=403)
            origin = request.headers.get('origin')
            if origin and origin != str(request.base_url).rstrip('/'):
                return JSONResponse({'detail': 'Недопустимый источник запроса'}, status_code=403)
            if int(request.headers.get('content-length', '0')) > 200000:
                return JSONResponse({'detail': 'Слишком большой запрос'}, status_code=413)
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
