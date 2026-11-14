"""Copia consistente SQLite, senza modificare o eliminare l'archivio sorgente."""
from pathlib import Path
import datetime as dt
import hashlib,json,sqlite3,uuid

def create_backup(source,destination):
    source=Path(source).resolve();destination=Path(destination).resolve()
    if not source.is_file():raise ValueError('Archivio non trovato. Avvia prima l’app e crea una pratica.')
    destination.mkdir(parents=True,exist_ok=True)
    name='noleggio-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-UTC')+'-'+uuid.uuid4().hex[:8]+'.sqlite3'
    target=(destination/name).resolve()
    if not target.is_relative_to(destination):raise ValueError('Destinazione non valida.')
    with target.open('xb'):pass
    original=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True);copy=sqlite3.connect(target)
    try:
        original.backup(copy)
        if copy.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or copy.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('Controllo di integrità della copia non superato.')
        counts={table:copy.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ['beni','prenotazioni','eventi']}
    finally:original.close();copy.close()
    manifest={'formato':'backup-noleggio-v1','file':target.name,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'conteggi':counts,'integrita':'ok','creato_utc':dt.datetime.now(dt.timezone.utc).isoformat()}
    manifest_file=target.with_suffix('.json')
    with manifest_file.open('x',encoding='utf-8') as file:json.dump(manifest,file,ensure_ascii=False,indent=2)
    return target,manifest

if __name__=='__main__':
    root=Path(__file__).resolve().parent
    ecosystem=next((p for p in root.parents if p.name=='AI-BUSINESS-ECOSYSTEM'),None)
    destination=ecosystem/'BACKUPS/apps/44-noleggio' if ecosystem else root/'backups'
    try:
        path,report=create_backup(root/'data/noleggio.sqlite3',destination)
        print('Backup verificato:',path)
        print('Beni:',report['conteggi']['beni'],'Prenotazioni:',report['conteggi']['prenotazioni'],'Eventi:',report['conteggi']['eventi'])
    except (OSError,ValueError,sqlite3.Error) as error:print('Backup non confermato:',error);raise SystemExit(1)
