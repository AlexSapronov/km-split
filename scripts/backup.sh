#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Include WAL through sqlite's online backup, not a copy of the main db file.
.venv/bin/python - <<'PY'
from pathlib import Path
from datetime import datetime, timezone
from km_split.config import Settings
import os, sqlite3, tarfile, tempfile
os.umask(0o077)
settings = Settings.load()
folder = settings.data / 'backups'
folder.mkdir(mode=0o700, exist_ok=True)
name = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
destination = folder / ('km-split-' + name + '.tar.gz')
with tempfile.TemporaryDirectory() as temp:
    db_copy = Path(temp) / 'split.sqlite3'
    with sqlite3.connect(settings.data / 'split.sqlite3') as source, sqlite3.connect(db_copy) as target:
        source.backup(target)
    with tarfile.open(destination, 'w:gz') as archive:
        archive.add(db_copy, arcname='split.sqlite3')
        for relative in ['organizations.json', 'mode', 'labels', 'receipts']:
            path = settings.data / relative
            if path.exists(): archive.add(path, arcname=relative)
        configuration = (settings.root or settings.data) / 'configuration.json'
        if configuration.exists(): archive.add(configuration, arcname='configuration.json')
print(destination)
PY
