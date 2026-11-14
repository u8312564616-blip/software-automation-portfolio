"""Applicazione locale, solo libreria standard Python. Avvio: python server.py."""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from contextlib import contextmanager
import datetime as dt
import json
import os
import re
import sqlite3
import uuid
import webbrowser
import threading

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('INTERVENTI_DATA', str(ROOT.parent / 'data')))
CHECKS = ['Controllo visivo', 'Verifica funzionamento', 'Controllo collegamenti', 'Pulizia e riordino']

@contextmanager
def connection():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATA / 'interventi.sqlite3', timeout=10)
    try:
        con.execute('CREATE TABLE IF NOT EXISTS interventi (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        with con:
            yield con
    finally:
        con.close()

def validate(value):
    if not isinstance(value, dict): raise ValueError('Formato intervento non valido.')
    result = {}
    limits = {'cliente':160, 'sede':240, 'tecnico':160, 'impianto':200, 'attivita':4000, 'note':4000, 'prossima_data':10, 'data':10, 'stato':20}
    for key, limit in limits.items():
        text = value.get(key, '')
        if not isinstance(text, str) or len(text) > limit: raise ValueError(f'Campo {key} non valido o troppo lungo.')
        result[key] = text.strip()
    for key in ['cliente','tecnico','data','impianto','attivita']:
        if not result[key]: raise ValueError(f'Compila il campo {key}.')
    for key in ['data','prossima_data']:
        if result[key]:
            try: dt.date.fromisoformat(result[key])
            except ValueError: raise ValueError(f'Data non valida: {key}.')
    if result['prossima_data'] and result['prossima_data'] < result['data']:
        raise ValueError('La prossima scadenza non può precedere l’intervento.')
    if result['stato'] not in ['bozza','completato']: raise ValueError('Stato non valido.')
    checks = value.get('controlli')
    if not isinstance(checks, list) or len(checks) != len(CHECKS): raise ValueError('Checklist incompleta.')
    clean = []
    for label, check in zip(CHECKS, checks):
        if not isinstance(check, dict): raise ValueError('Controllo non valido.')
        status, note = check.get('esito',''), check.get('nota','')
        if status not in ['', 'ok','anomalia','non_applicabile']: raise ValueError('Esito non valido.')
        if not isinstance(note,str) or len(note)>1500: raise ValueError('Nota controllo non valida.')
        note=note.strip()
        if result['stato']=='completato' and not status: raise ValueError(f'Indica un esito per: {label}.')
        if status=='anomalia' and not note: raise ValueError(f'Descrivi l’anomalia: {label}.')
        clean.append({'nome':label,'esito':status,'nota':note})
    result['controlli']=clean
    return result

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f'{self.log_date_time_string()} {fmt % args}', flush=True)

    def respond(self, code, value):
        raw=json.dumps(value,ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers(); self.wfile.write(raw)

    def allowed(self):
        expected=f'127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host') != expected:
            self.respond(403,{'errore':'Usa l’indirizzo locale 127.0.0.1 mostrato all’avvio.'}); return False
        origin=self.headers.get('Origin')
        if origin and origin != 'http://'+expected:
            self.respond(403,{'errore':'Origine non autorizzata.'}); return False
        return True

    def do_GET(self):
        if not self.allowed(): return
        match=re.fullmatch(r'/api/rapporti/([0-9a-f-]{36})\.pdf',self.path)
        if match:
            with connection() as con:
                row=con.execute('SELECT payload FROM interventi WHERE id=?',(match.group(1),)).fetchone()
            if not row:
                self.respond(404,{'errore':'Intervento non trovato.'});return
            try:
                from rapporti import create_pdf
                raw=create_pdf(json.loads(row[0]))
            except ImportError:
                self.respond(503,{'errore':'Componente PDF non disponibile. Avvia tramite Avvia-interventi.cmd oppure usa la stampa del browser.'});return
            self.send_response(200);self.send_header('Content-Type','application/pdf')
            self.send_header('Content-Disposition',f'attachment; filename="rapportino-{match.group(1)}.pdf"')
            self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(raw);return
        if self.path == '/api/interventi':
            with connection() as con:
                records=[json.loads(row[0]) for row in con.execute('SELECT payload FROM interventi')]
            records.sort(key=lambda x:(x['data'],x['aggiornato']),reverse=True)
            self.respond(200,{'interventi':records}); return
        if self.path=='/api/status':
            self.respond(200,{'app':'interventi-locali','versione':2}); return
        static={'/':('index.html','text/html'),'/app.js':('app.js','text/javascript'),'/style.css':('style.css','text/css')}
        if self.path not in static: self.respond(404,{'errore':'Pagina non trovata.'}); return
        name,mime=static[self.path];raw=(ROOT/name).read_bytes()
        self.send_response(200);self.send_header('Content-Type',mime+'; charset=utf-8')
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'")
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers();self.wfile.write(raw)

    def do_POST(self):
        if not self.allowed(): return
        try:
            if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                self.respond(415,{'errore':'Richiesto formato JSON.'}); return
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=2_000_000: raise ValueError('File vuoto o superiore a 2 MB.')
            body=json.loads(self.rfile.read(length))
            if self.path=='/api/ferma':
                self.respond(200,{'stato':'Arresto dell’app in corso. Dati conservati.'})
                threading.Thread(target=self.server.shutdown,daemon=True).start();return
            if self.path=='/api/interventi':
                record=validate(body)
                supplied=body.get('id')
                if supplied:
                    try: record['id']=str(uuid.UUID(supplied))
                    except (ValueError,TypeError,AttributeError): raise ValueError('Identificativo non valido.')
                else: record['id']=str(uuid.uuid4())
                record['aggiornato']=dt.datetime.now(dt.timezone.utc).isoformat()
                with connection() as con:
                    if supplied and not con.execute('SELECT 1 FROM interventi WHERE id=?',(record['id'],)).fetchone():
                        self.respond(404,{'errore':'Intervento non trovato. Ricarica l’archivio.'});return
                    con.execute('INSERT INTO interventi VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',(record['id'],json.dumps(record,ensure_ascii=False)))
                self.respond(200,{'intervento':record});return
            if self.path=='/api/importa':
                if not isinstance(body,dict) or body.get('formato')!='interventi-locali-v1' or not isinstance(body.get('interventi'),list): raise ValueError('Backup non riconosciuto.')
                if len(body['interventi'])>2000: raise ValueError('Massimo 2000 interventi per importazione.')
                records=[];seen=set()
                for original in body['interventi']:
                    r=validate(original)
                    try:r['id']=str(uuid.UUID(original.get('id','')))
                    except (ValueError,TypeError,AttributeError):raise ValueError('Identificativo nel backup non valido.')
                    if r['id'] in seen:raise ValueError('Identificativi duplicati nel file.')
                    seen.add(r['id']);r['aggiornato']=dt.datetime.now(dt.timezone.utc).isoformat();records.append(r)
                inserted=0
                with connection() as con:
                    for r in records:
                        inserted+=con.execute('INSERT OR IGNORE INTO interventi VALUES (?,?)',(r['id'],json.dumps(r,ensure_ascii=False))).rowcount
                self.respond(200,{'aggiunti':inserted,'gia_presenti':len(records)-inserted});return
            self.respond(404,{'errore':'Operazione non trovata.'})
        except (ValueError,TypeError,json.JSONDecodeError) as e:self.respond(400,{'errore':str(e)})
        except sqlite3.Error:
            self.respond(500,{'errore':'Archivio non disponibile. Nessun salvataggio confermato.'})

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8766);parser.add_argument('--no-browser',action='store_true');args=parser.parse_args()
    with connection():pass
    try:server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    except OSError:
        print('Porta occupata: chiudi la precedente finestra dell’app oppure scegli --port 8767.');raise SystemExit(1)
    url=f'http://127.0.0.1:{server.server_port}'
    print(f'Interventi locali: {url}\nDati: {DATA}\nPer fermare: Ctrl+C in questa finestra.',flush=True)
    if not args.no_browser:webbrowser.open(url)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
