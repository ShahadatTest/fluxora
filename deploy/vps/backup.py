"""Host-side SQLite backup for the fixed Fluxora demo directory only."""
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

os.umask(0o077)
base=Path('/opt/fluxora').resolve()
source=base/'data/state.sqlite';folder=base/'backups'
folder.mkdir(mode=0o700,parents=True,exist_ok=True)
if folder.is_symlink() or not folder.resolve().is_relative_to(base):raise RuntimeError('Unexpected backup destination')
name=datetime.now(timezone.utc).strftime('fluxora-%Y%m%dT%H%M%S%fZ')
target=folder/(name+'.sqlite')
with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as src:
    with sqlite3.connect(target) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('Backup failed integrity check')
manifest={'file':target.name,'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'integrity':'ok'}
(folder/(name+'.json')).write_text(json.dumps(manifest,indent=2),encoding='utf-8')
items=sorted(p for p in folder.iterdir() if re.fullmatch(r'fluxora-\d{8}T\d{12}Z\.sqlite',p.name) and not p.is_symlink())
for old in items[:-14]:
    if old.resolve().parent!=folder.resolve():raise RuntimeError('Unexpected backup path')
    old.unlink();old.with_suffix('.json').unlink(missing_ok=True)
print(json.dumps(manifest))
