import json
import os
import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv


@dataclass
class Settings:
    data: Path
    mode: str
    user: str
    password: str
    organizations: list[dict]
    root: Path | None = None

    @staticmethod
    def validate_organizations(orgs):
        if not isinstance(orgs, list) or len(orgs) > 30 or len({o['inn'] for o in orgs}) != len(orgs):
            raise ValueError('Укажите до 30 организаций без повторов ИНН')
        for o in orgs:
            if not isinstance(o['inn'], str) or not o['inn'].isascii() or not o['inn'].isdigit() or len(o['inn']) not in (10, 12):
                raise ValueError('ИНН должен содержать 10 или 12 цифр')
            if not isinstance(o['name'], str) or not o['name'].strip() or len(o['name']) > 120:
                raise ValueError('Заполните наименование организации')
            for name in ('true_api_url', 'oms_url'):
                parsed = urlparse(o[name])
                if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
                    raise ValueError(f'{name}: укажите базовый HTTPS-адрес без пути')
                o[name] = o[name].rstrip('/')
            from uuid import UUID
            for field in ('oms_id', 'oms_connection'):
                try:
                    value = UUID(o[field])
                    if value.int == 0:
                        raise ValueError()
                    o[field] = str(value)
                except (ValueError, TypeError, AttributeError):
                    raise ValueError(f'{o["name"]}: заполните действительный {field} из ЛК СУЗ')
            if o['template_id'] not in (36, 37):
                raise ValueError('Поддерживаются шаблоны радиоэлектроники 36 и 37')

    def configuration(self):
        root = self.root or self.data
        path = root / 'configuration.json'
        payload = path.read_bytes() if path.exists() else b''
        saved = json.loads(payload) if payload else {}
        orgs = saved.get('organizations')
        if orgs is None:
            legacy = root / 'organizations.json'
            if legacy.exists():
                orgs = json.loads(legacy.read_text())
            elif self.mode == 'live':
                orgs = self.organizations
        return {'mode': self.mode, 'organizations': orgs or [],
                'revision': hashlib.sha256(payload).hexdigest()}

    @classmethod
    def from_root(cls, root, fallback_mode='live'):
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        config = root / 'configuration.json'
        saved = json.loads(config.read_text()) if config.exists() else {}
        mode = saved.get('mode', fallback_mode)
        if mode not in ('live', 'demo'):
            raise ValueError('SPLIT_MODE: live или demo')
        # Preserve old databases in place. All other modes get their own folder.
        legacy_marker = root / 'mode'
        data = root if legacy_marker.exists() and legacy_marker.read_text() == mode else root / mode
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        if mode == 'live':
            legacy = root / 'organizations.json'
            orgs = saved.get('organizations')
            if orgs is None:
                orgs = json.loads(legacy.read_text()) if legacy.exists() else []
            cls.validate_organizations(orgs)
        else:
            orgs = [dict(inn='7700000000', name='Демо · склад', template_id=36)]
        # user/password are retained only for source compatibility with v0.1.
        return cls(data, mode, '', '', orgs, root)

    @classmethod
    def load(cls):
        load_dotenv()
        return cls.from_root(os.getenv('SPLIT_DATA', 'data'), os.getenv('SPLIT_MODE', 'live'))
