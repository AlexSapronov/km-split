import json
import os
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

    @classmethod
    def load(cls):
        load_dotenv()
        data = Path(os.getenv('SPLIT_DATA', 'data')).resolve()
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        mode = os.getenv('SPLIT_MODE', 'demo')
        if mode not in ('live', 'demo'):
            raise ValueError('SPLIT_MODE: live или demo')
        password = os.getenv('SPLIT_PASSWORD', '')
        if len(password) < 16 or password == 'REPLACE_WITH_A_LONG_RANDOM_PASSWORD':
            raise ValueError('Задайте SPLIT_PASSWORD (не менее 16 символов)')
        if mode == 'live':
            orgs = json.loads((data / 'organizations.json').read_text())
            if not orgs or len({o['inn'] for o in orgs}) != len(orgs):
                raise ValueError('Заполните организации без повторов ИНН')
            for o in orgs:
                if not str(o['inn']).isdigit() or len(o['inn']) not in (10, 12):
                    raise ValueError('Некорректный ИНН')
                for name in ('true_api_url', 'oms_url'):
                    parsed = urlparse(o[name])
                    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
                        raise ValueError(f'{name}: укажите базовый HTTPS-адрес без пути')
                from uuid import UUID
                UUID(o['oms_id']); UUID(o['oms_connection'])
                if o['template_id'] not in (36, 37):
                    raise ValueError('Поддерживаются шаблоны радиоэлектроники 36 и 37')
        else:
            orgs = [dict(inn='7700000000', name='Демо · склад', template_id=36)]
        return cls(data, mode, os.getenv('SPLIT_USER', 'warehouse'), password, orgs)
