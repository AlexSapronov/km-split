import base64
import hashlib
import json
from uuid import uuid4
from .codes import parse, with_quantity, division_entry
from .db import now
from .clients import RemoteError
from .labels import render_label, display_pn


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


class Service:
    def __init__(self, settings, store, gateway):
        self.settings, self.store, self.gateway = settings, store, gateway

    def operation(self, operation_id):
        op = self.store.one('SELECT o.*,s.gtin,s.full source_code FROM operations o JOIN sources s ON s.cis=o.source WHERE o.id=?', (operation_id,))
        if not op:
            raise ValueError('Операция не найдена')
        return {**op, 'pn': display_pn(op['pn'])}

    def job(self, job_id):
        job = self.store.one('SELECT * FROM jobs WHERE id=?', (job_id,))
        if not job:
            raise ValueError('Задание не найдено')
        return job

    def _available(self, db, info):
        source = db.execute('SELECT * FROM sources WHERE cis=?', (info['cis'],)).fetchone()
        if source is None:
            return info['quantity']
        if source['inn'] != info['inn'] or source['pn'] != info['pn']:
            raise ValueError('Изменились владелец или PN исходного КМ; требуется разбор')
        sums = db.execute("SELECT COALESCE(SUM(CASE WHEN state='done' THEN quantity ELSE 0 END),0) done, COALESCE(SUM(CASE WHEN state NOT IN ('done','cancelled') THEN quantity ELSE 0 END),0) pending FROM operations WHERE source=?", (info['cis'],)).fetchone()
        expected = source['baseline'] - sums['done']
        if info['quantity'] != expected:
            raise ValueError(f'Остаток ЧЗ ({info["quantity"]}) отличается от ожидаемого ({expected}). Дождитесь обновления ЧЗ или разберите изменение через ЛК')
        return expected - sums['pending']

    async def scan(self, raw):
        if self.settings.mode == 'live' and not self.settings.organizations:
            raise ValueError('Сначала добавьте вашу организацию в «Настройки» и подключите ЧЗ по ЭЦП')
        info = await self.gateway.info(raw)
        with self.store.transaction() as db:
            available = self._available(db, info)
        return {**info, 'pn': display_pn(info['pn']), 'available': available, 'reserved': info['quantity'] - available,
                'max_quantity': max(0, available - 1),
                'organization': self.gateway.org(info['inn'])['name']}

    def _insert_job(self, db, kind, inn, target, body):
        job_id = str(uuid4())
        db.execute('INSERT INTO jobs(id,kind,inn,target,body,state,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                   (job_id, kind, inn, target, body, 'prepared', now(), now()))
        self.store.event(db, job_id, 'prepared', kind)
        return job_id

    async def prepare_auth(self, inn, kind):
        if kind not in ('true', 'oms'):
            raise ValueError('Некорректный вид доступа')
        challenge = await self.gateway.challenge(inn)
        with self.store.transaction() as db:
            job_id = self._insert_job(db, 'auth_' + kind, inn, None, canonical(challenge))
        return self.job_view(job_id)

    def job_view(self, job_id):
        job = self.job(job_id)
        auth = job['kind'].startswith('auth_')
        payload = json.loads(job['body'])['data'] if auth else job['body']
        return {**job, 'payload_base64': base64.b64encode(payload.encode('utf-8')).decode('ascii'),
                'detached': not auth, 'sha256': hashlib.sha256(payload.encode('utf-8')).hexdigest(),
                'organization': self.gateway.org(job['inn'])['name']}

    async def create(self, raw, quantity, request_id):
        if type(quantity) is not int or quantity < 1:
            raise ValueError('Введите положительное целое количество')
        code = parse(raw)
        if self.settings.mode == 'live' and code.gtin.startswith('029'):
            raise ValueError('Эмиссия REAPPLY недоступна для технического GTIN диапазона 029')
        fingerprint = hashlib.sha256(canonical([code.full, quantity]).encode()).hexdigest()
        existing = self.store.one('SELECT id,fingerprint FROM operations WHERE request_id=?', (request_id,))
        if existing:
            if existing['fingerprint'] != fingerprint:
                raise ValueError('Идентификатор запроса уже использован с другими данными')
            return self.operation(existing['id'])
        info = await self.gateway.info(raw)
        # Validate the printable PN/quantity BEFORE any paid emission starts.
        dummy = '01' + code.gtin + '215' + 'X' * 19 + '\x1d93dGVz'
        render_label(with_quantity(dummy, quantity), info['pn'], quantity, self.settings.mode == 'demo')
        org = self.gateway.org(info['inn'])
        if self.settings.mode == 'live':
            self.gateway.token(info['inn'], 'oms')
        op_id = str(uuid4())
        order = {'productGroup': 'radio', 'products': [{'gtin': code.gtin, 'quantity': 1,
                 'serialNumberType': 'OPERATOR', 'templateId': org['template_id'], 'cisType': 'UNIT'}],
                 'attributes': {'releaseMethodType': 'REAPPLY', 'paymentType': 1, 'productionOrderId': op_id}}
        # Optional for global-range GTINs whose cards require owner identification.
        if org.get('producer_inn'):
            order['products'][0]['attributes'] = {'producer': org['producer_inn']}
        with self.store.transaction() as db:
            existing = db.execute('SELECT id,fingerprint FROM operations WHERE request_id=?', (request_id,)).fetchone()
            if existing:
                if existing['fingerprint'] != fingerprint:
                    raise ValueError('Идентификатор запроса уже использован')
                op_id = existing['id']
            else:
                available = self._available(db, info)
                if quantity >= available:
                    raise ValueError(f'Можно отделить от 1 до {max(0, available-1)} шт. с учётом ожидающих операций')
                if db.execute("SELECT 1 FROM operations WHERE source=? AND state IN ('report_prepared','report_sending','report_unknown','report_pending')", (code.cis,)).fetchone():
                    raise ValueError('По исходному КМ уже готовится или обрабатывается отчёт; дождитесь результата')
                db.execute('INSERT OR IGNORE INTO sources(cis,full,gtin,inn,pn,baseline) VALUES(?,?,?,?,?,?)',
                           (code.cis, code.full, code.gtin, info['inn'], info['pn'], info['quantity']))
                db.execute('INSERT INTO operations(id,request_id,fingerprint,source,inn,quantity,pn,state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (op_id, request_id, fingerprint, code.cis, info['inn'], quantity, info['pn'], 'awaiting_signature', now(), now()))
                self._insert_job(db, 'order', info['inn'], op_id, canonical(order))
                self.store.event(db, op_id, 'reserved', str(quantity))
        return self.operation(op_id)

    async def _validate_sources(self, ops):
        checked = set()
        for op in ops:
            if op['source'] in checked:
                continue
            checked.add(op['source'])
            info = await self.gateway.info(op['source_code'], op['inn'])
            with self.store.transaction() as db:
                available = self._available(db, info)
                if available < 1:
                    raise ValueError('Недостаточный остаток после разделений')

    async def prepare_reports(self, ids, confirmed):
        if confirmed is not True or not ids or len(ids) != len(set(ids)) or len(ids) > 1000:
            raise ValueError('Выберите до 1000 разных операций и подтвердите нанесение этикеток')
        ops = [self.operation(i) for i in ids]
        if any(o['state'] != 'ready' for o in ops):
            raise ValueError('В отчёт можно включить только операции с готовой этикеткой')
        await self._validate_sources(ops)
        groups = {}
        for op in ops:
            groups.setdefault(op['inn'], []).append(op)
        job_ids = []
        with self.store.transaction() as db:
            for op in ops:
                actual = db.execute('SELECT state FROM operations WHERE id=?', (op['id'],)).fetchone()
                if actual['state'] != 'ready':
                    raise ValueError('Операцию уже включили в другой отчёт; обновите список')
                if db.execute("SELECT 1 FROM operations WHERE source=? AND state IN ('report_prepared','report_sending','report_unknown','report_pending')", (op['source'],)).fetchone():
                    raise ValueError('По исходному КМ есть незавершённый отчёт')
            for inn, group in groups.items():
                body = canonical({'productGroup': 'radio', 'utilisationType': 'DIVISIONMARK',
                                  'sntins': [division_entry(o['new_code'], o['source_code']) for o in group],
                                  'attributes': {'participantId': inn}})
                job_id = self._insert_job(db, 'report', inn, None, body)
                for op in group:
                    db.execute('INSERT INTO report_items(job_id,operation_id) VALUES(?,?)', (job_id, op['id']))
                    db.execute("UPDATE operations SET state='report_prepared',error=NULL,updated=? WHERE id=?", (now(), op['id']))
                job_ids.append(job_id)
        return [self.job_view(i) for i in job_ids]

    def _job_ops(self, job):
        if job['kind'] == 'order':
            return [self.operation(job['target'])]
        if job['kind'] == 'report':
            return [self.operation(r['operation_id']) for r in self.store.all('SELECT operation_id FROM report_items WHERE job_id=?', (job['id'],))]
        return []

    async def submit(self, job_id, signature):
        job = self.job(job_id)
        if job['state'] != 'prepared':
            # Idempotent UI retries never perform a second external POST.
            return self.job_view(job_id)
        if self.settings.mode == 'live':
            try:
                decoded = base64.b64decode(''.join(signature.split()), validate=True)
                if len(decoded) < 64:
                    raise ValueError()
            except Exception:
                raise ValueError('Некорректная CMS-подпись в Base64')
        ops = self._job_ops(job)
        if ops:
            await self._validate_sources(ops)
        with self.store.transaction() as db:
            changed = db.execute("UPDATE jobs SET state='sending',updated=? WHERE id=? AND state='prepared'", (now(), job_id)).rowcount
            if not changed:
                return self.job_view(job_id)
            for op in ops:
                db.execute('UPDATE operations SET state=?,updated=? WHERE id=?',
                           ('order_sending' if job['kind'] == 'order' else 'report_sending', now(), op['id']))
            self.store.event(db, job_id, 'sending')
        try:
            if job['kind'].startswith('auth_'):
                token = await self.gateway.sign_in(job, signature)
                with self.store.transaction() as db:
                    db.execute('INSERT OR REPLACE INTO tokens(inn,kind,token,updated) VALUES(?,?,?,?)',
                               (job['inn'], job['kind'][5:], token, now()))
                    db.execute("UPDATE jobs SET state='done',updated=? WHERE id=?", (now(), job_id))
            else:
                result = await self.gateway.oms(job['inn'], '/api/v3/order' if job['kind'] == 'order' else '/api/v3/utilisation',
                                                body=job['body'], signature=''.join(signature.split()))
                remote_id = result.get('orderId' if job['kind'] == 'order' else 'reportId')
                if not remote_id:
                    raise RemoteError('ЧЗ не вернул идентификатор; автоматический повтор запрещён', uncertain=True)
                self._accepted(job, remote_id)
        except (RemoteError, ValueError) as exc:
            # ValueError after dispatch is uncertain too (e.g. malformed response).
            uncertain = not isinstance(exc, RemoteError) or exc.uncertain
            state = 'unknown' if uncertain else 'rejected'
            with self.store.transaction() as db:
                db.execute('UPDATE jobs SET state=?,error=?,updated=? WHERE id=?', (state, str(exc), now(), job_id))
                for op in ops:
                    op_state = ('order_unknown' if uncertain else 'order_rejected') if job['kind'] == 'order' else ('report_unknown' if uncertain else 'ready')
                    db.execute('UPDATE operations SET state=?,error=?,updated=? WHERE id=?', (op_state, str(exc), now(), op['id']))
                self.store.event(db, job_id, state, str(exc))
        return self.job_view(job_id)

    def _accepted(self, job, remote_id):
        from uuid import UUID
        try:
            UUID(remote_id)
        except (ValueError, TypeError):
            raise RemoteError('Некорректный идентификатор ЧЗ; проверьте ЛК', uncertain=True)
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET state='accepted',remote_id=?,error=NULL,updated=? WHERE id=?", (remote_id, now(), job['id']))
            for op in self._job_ops(job):
                if job['kind'] == 'order':
                    db.execute("UPDATE operations SET state='order_pending',order_id=?,error=NULL,updated=? WHERE id=?", (remote_id, now(), op['id']))
                else:
                    db.execute("UPDATE operations SET state='report_pending',report_id=?,error=NULL,updated=? WHERE id=?", (remote_id, now(), op['id']))
            self.store.event(db, job['id'], 'accepted', remote_id)

    async def recover(self, job_id, remote_id):
        job = self.job(job_id)
        if job['state'] not in ('unknown', 'sending') or job['kind'] not in ('order', 'report'):
            raise ValueError('Восстановление доступно только при неизвестном результате отправки')
        from uuid import UUID
        UUID(remote_id)
        if job['kind'] == 'order':
            op = self.operation(job['target'])
            result = await self.gateway.oms(job['inn'], '/api/v3/order/status', {'orderId': remote_id, 'gtin': op['gtin']})
            if not isinstance(result, list) or not result or any(r.get('gtin', op['gtin']) != op['gtin'] for r in result):
                raise ValueError('Заказ не найден для этого GTIN')
        else:
            # A report ID alone cannot prove it belongs to this signed payload.
            # Require manual administrator reconciliation rather than hiding data.
            raise ValueError('Неизвестный результат отчёта: нужен разбор квитанции в ЛК; повторная отправка заблокирована')
        self._accepted(job, remote_id)
        return self.job_view(job_id)

    async def poll_order(self, op_id):
        op = self.operation(op_id)
        if op['state'] != 'order_pending':
            return op
        result = await self.gateway.oms(op['inn'], '/api/v3/order/status', {'orderId': op['order_id'], 'gtin': op['gtin']})
        if not isinstance(result, list) or len(result) != 1:
            raise ValueError('Неожиданный ответ статуса заказа')
        if result[0].get('bufferStatus') == 'REJECTED':
            with self.store.transaction() as db:
                db.execute("UPDATE operations SET state='order_rejected',error=?,updated=? WHERE id=? AND state='order_pending'", ('СУЗ отклонила заказ. Проверьте ЛК', now(), op_id))
            return self.operation(op_id)
        if result[0].get('bufferStatus') != 'ACTIVE' or result[0].get('availableCodes', 0) < 1:
            return op
        # GET /codes consumes the buffer. Claim it durably; never retry blindly.
        with self.store.transaction() as db:
            if not db.execute("UPDATE operations SET state='codes_fetching',updated=? WHERE id=? AND state='order_pending'", (now(), op_id)).rowcount:
                return self.operation(op_id)
        try:
            result = await self.gateway.oms(op['inn'], '/api/v3/codes', {'orderId': op['order_id'], 'gtin': op['gtin'], 'quantity': 1})
            codes = result.get('codes', [])
            if len(codes) != 1 or parse(codes[0]).gtin != op['gtin'] or not result.get('blockId'):
                raise ValueError('Неожиданные данные полученного КМ; требуется разбор')
            new_code = with_quantity(codes[0], op['quantity'])
            # Persist raw issued code even if a later PDF generation fails.
            with self.store.transaction() as db:
                db.execute("UPDATE operations SET state='ready',new_code=?,block_id=?,error=NULL,updated=? WHERE id=?", (new_code, result['blockId'], now(), op_id))
                self.store.event(db, op_id, 'code_received', result['blockId'])
        except (RemoteError, ValueError) as exc:
            with self.store.transaction() as db:
                db.execute("UPDATE operations SET state='codes_unknown',error=?,updated=? WHERE id=?", (str(exc), now(), op_id))
            raise
        return self.operation(op_id)

    def label(self, op_id):
        op = self.operation(op_id)
        if not op['new_code']:
            raise ValueError('Новый КМ ещё не получен')
        labels = self.settings.data / 'labels'
        labels.mkdir(mode=0o700, exist_ok=True)
        # Bump the PDF cache version when barcode encodation changes: never
        # return a previously generated v3 label with FNC1 separators.
        path = labels / (op_id + '-v4.pdf')
        if not path.exists():
            payload = render_label(op['new_code'], op['pn'], op['quantity'], self.settings.mode == 'demo')
            temporary = path.with_suffix('.tmp-' + uuid4().hex)
            temporary.write_bytes(payload)
            temporary.chmod(0o600)
            temporary.replace(path)
        with self.store.transaction() as db:
            self.store.event(db, op_id, 'pdf_download')
        return path

    async def poll_report(self, job_id):
        job = self.job(job_id)
        if job['kind'] != 'report' or job['state'] != 'accepted':
            return self.job_view(job_id)
        result = await self.gateway.oms(job['inn'], '/api/v3/report/info', {'reportId': job['remote_id']})
        status = result.get('reportStatus')
        if status not in ('SUCCESS', 'REJECTED'):
            return self.job_view(job_id)
        if status == 'SUCCESS':
            receipts = await self.gateway.oms(job['inn'], '/api/v3/receipts/receipt', {'resultDocId': job['remote_id']})
            # Receipting 2.0: resultDocId identifies the requested receipt;
            # sourceDocId identifies its separate transaction start document.
            relevant = [r for r in receipts.get('results', []) if r.get('resultDocId') == job['remote_id']]
            if not relevant or any(r.get('state') != 'SUCCESS' or r.get('code') != 0 for r in relevant):
                # Includes partial completion and conflicting receipts: keep visible.
                with self.store.transaction() as db:
                    db.execute('UPDATE jobs SET error=?,updated=? WHERE id=?', ('Проверьте квитанцию: полное успешное нанесение не подтверждено', now(), job_id))
                return self.job_view(job_id)
            folder = self.settings.data / 'receipts'
            folder.mkdir(mode=0o700, exist_ok=True)
            path = folder / (job_id + '.json')
            path.write_text(canonical(receipts), encoding='utf-8'); path.chmod(0o600)
        with self.store.transaction() as db:
            if not db.execute('UPDATE jobs SET state=?,error=?,updated=? WHERE id=? AND state=\'accepted\'',
                ('done' if status == 'SUCCESS' else 'rejected', None if status == 'SUCCESS' else 'Отчёт отклонён ЧЗ. Проверьте квитанцию в ЛК', now(), job_id)).rowcount:
                return self.job_view(job_id)
            for op in self._job_ops(job):
                db.execute('UPDATE operations SET state=?,error=?,updated=? WHERE id=?',
                    ('done' if status == 'SUCCESS' else 'ready', None if status == 'SUCCESS' else 'Отчёт отклонён ЧЗ', now(), op['id']))
            self.store.event(db, job_id, 'report_' + status.lower())
        return self.job_view(job_id)

    def cancel(self, job_id):
        job = self.job(job_id)
        if job['state'] != 'prepared':
            raise ValueError('Отменить можно только ещё не отправленное задание')
        with self.store.transaction() as db:
            if not db.execute("UPDATE jobs SET state='cancelled',updated=? WHERE id=? AND state='prepared'", (now(), job_id)).rowcount:
                raise ValueError('Задание уже отправляется')
            for op in self._job_ops(job):
                db.execute('UPDATE operations SET state=?,updated=? WHERE id=?',
                           ('cancelled' if job['kind'] == 'order' else 'ready', now(), op['id']))
            self.store.event(db, job_id, 'cancelled')
        return self.job_view(job_id)

    def state(self, history=False):
        organizations = []
        for org in self.settings.organizations:
            tokens = self.store.all('SELECT kind,updated FROM tokens WHERE inn=?', (org['inn'],))
            organizations.append({'inn': org['inn'], 'name': org['name'], 'tokens': tokens})
        condition = "o.state IN ('done','cancelled')" if history else "o.state NOT IN ('done','cancelled')"
        operations = self.store.all('SELECT o.*,s.gtin FROM operations o JOIN sources s ON s.cis=o.source WHERE ' + condition + ' ORDER BY o.created DESC LIMIT 500')
        operations = [{**op, 'pn': display_pn(op['pn'])} for op in operations]
        jobs = self.store.all("SELECT id,kind,inn,target,state,remote_id,error,created FROM jobs WHERE state NOT IN ('done','cancelled') ORDER BY created DESC LIMIT 500")
        return {'mode': self.settings.mode, 'file_signing_enabled': self.settings.file_signing_enabled,
                'organizations': organizations, 'operations': operations, 'jobs': jobs,
                'demo_code': '0104640638345218215DEMO000000000000000\u001d93dGVz\u001d30100' if self.settings.mode == 'demo' else None}
