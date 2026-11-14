"""Noleggio giornaliero locale. Nessun pagamento o servizio esterno."""
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path
from contextlib import contextmanager
from zoneinfo import ZoneInfo
from collections import defaultdict
import datetime as dt
import json,os,re,sqlite3,threading,uuid,webbrowser

ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('NOLEGGIO_DATA',str(ROOT.parent/'data')))
ROME=ZoneInfo('Europe/Rome')
def today():return dt.datetime.now(ROME).date().isoformat()
def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def dump(x):return json.dumps(x,ensure_ascii=False,separators=(',',':'))
class Conflict(ValueError):pass
def uid(v):
    try:return str(uuid.UUID(v))
    except (ValueError,TypeError,AttributeError):raise ValueError('Identificativo non valido.')
def text(v,label,limit,required=True):
    if not isinstance(v,str) or len(v.strip())>limit or required and not v.strip():raise ValueError(f'{label}: campo obbligatorio o oltre {limit} caratteri.')
    return v.strip()
def number(v,label,low=1,high=1000000):
    if not isinstance(v,int) or isinstance(v,bool) or not low<=v<=high:raise ValueError(f'{label}: intero da {low} a {high}.')
    return v
def sku(v):
    s=text(v,'SKU',64).upper()
    if not re.fullmatch(r'[A-Z0-9][A-Z0-9._-]*',s):raise ValueError('SKU: lettere A-Z, numeri, punto, trattino o underscore.')
    return s
def period(body):
    start,end=body.get('inizio'),body.get('fine')
    for s in [start,end]:
        if not isinstance(s,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',s):raise ValueError('Date non valide.')
        try:dt.date.fromisoformat(s)
        except ValueError:raise ValueError('Data inesistente.')
    if start<today():raise ValueError('Il ritiro non può essere nel passato.')
    if end<=start:raise ValueError('Il rientro deve essere successivo al ritiro.')
    return start,end

@contextmanager
def connection():
    DATA.mkdir(parents=True,exist_ok=True);c=sqlite3.connect(DATA/'noleggio.sqlite3',timeout=15);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON')
    try:
        c.execute('CREATE TABLE IF NOT EXISTS beni(sku TEXT PRIMARY KEY,nome TEXT NOT NULL,categoria TEXT NOT NULL,quantita INTEGER NOT NULL CHECK(quantita>0),creato TEXT NOT NULL)')
        c.execute("CREATE TABLE IF NOT EXISTS prenotazioni(id TEXT PRIMARY KEY,sku TEXT NOT NULL REFERENCES beni(sku),cliente TEXT NOT NULL,riferimento TEXT NOT NULL,inizio TEXT NOT NULL,fine TEXT NOT NULL,quantita INTEGER NOT NULL CHECK(quantita>0),nota TEXT NOT NULL,stato TEXT NOT NULL,versione INTEGER NOT NULL,creato TEXT NOT NULL)")
        c.execute('CREATE TABLE IF NOT EXISTS eventi(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,prenotazione TEXT NOT NULL REFERENCES prenotazioni(id),azione TEXT NOT NULL,motivo TEXT NOT NULL,creato TEXT NOT NULL)')
        yield c;c.commit()
    except Exception:c.rollback();raise
    finally:c.close()

def availability(c,code,start,end):
    asset=c.execute('SELECT * FROM beni WHERE sku=?',(code,)).fetchone()
    if not asset:raise ValueError('Attrezzatura non trovata.')
    events=defaultdict(int);late=0
    for b in c.execute("SELECT * FROM prenotazioni WHERE sku=? AND stato IN ('prenotata','ritirata')",(code,)):
        overdue=b['stato']=='ritirata' and b['fine']<=today()
        if overdue:late+=b['quantita']
        effective_end=end if overdue else b['fine']
        if b['inizio']<end and effective_end>start:
            events[max(start,b['inizio'])]+=b['quantita'];events[min(end,effective_end)]-=b['quantita']
    running=peak=0
    for day in sorted(events):running+=events[day];peak=max(peak,running)
    return {'sku':code,'inizio':start,'fine':end,'totale':asset['quantita'],'massimo_impegnati':peak,'disponibili':max(0,asset['quantita']-peak),'pezzi_in_ritardo':late,'sovraccarico':peak>asset['quantita']}

def create_asset(c,b):
    code=sku(b.get('sku'));name=text(b.get('nome'),'Nome',160);category=text(b.get('categoria',''),'Categoria',100,False);qty=number(b.get('quantita'),'Quantità',1,10000)
    if c.execute('SELECT 1 FROM beni WHERE sku=?',(code,)).fetchone():raise Conflict('SKU già presente.')
    if c.execute('SELECT COUNT(*) FROM beni').fetchone()[0]>=1000:raise ValueError('Limite del prototipo: 1000 attrezzature.')
    c.execute('INSERT INTO beni VALUES (?,?,?,?,?)',(code,name,category,qty,now()));return {'sku':code}

def create_booking(c,b):
    identifier=uid(b.get('id'));code=sku(b.get('sku'));start,end=period(b)
    data={'sku':code,'cliente':text(b.get('cliente'),'Cliente',160),'riferimento':text(b.get('riferimento',''),'Riferimento',100,False),'inizio':start,'fine':end,'quantita':number(b.get('quantita'),'Quantità',1,10000),'nota':text(b.get('nota',''),'Nota',1000,False)}
    previous=c.execute('SELECT * FROM prenotazioni WHERE id=?',(identifier,)).fetchone()
    if previous:
        if any(previous[k]!=v for k,v in data.items()):raise Conflict('Riferimento già usato per un’altra prenotazione.')
        return {'id':identifier,'gia_presente':True,'stato':previous['stato']}
    if c.execute('SELECT COUNT(*) FROM prenotazioni').fetchone()[0]>=20000:raise ValueError('Limite del prototipo: 20000 prenotazioni.')
    available=availability(c,code,start,end)
    if data['quantita']>available['disponibili']:raise Conflict(f"Disponibilità insufficiente nel periodo: {available['disponibili']} pezzi prenotabili.")
    c.execute('INSERT INTO prenotazioni VALUES (?,?,?,?,?,?,?,?,?,?,?)',(identifier,code,data['cliente'],data['riferimento'],start,end,data['quantita'],data['nota'],'prenotata',1,now()))
    c.execute('INSERT INTO eventi(id,prenotazione,azione,motivo,creato) VALUES (?,?,?,?,?)',(str(uuid.uuid4()),identifier,'prenotazione',data['nota'],now()))
    return {'id':identifier,'gia_presente':False,'stato':'prenotata'}

def action(c,b):
    identifier=uid(b.get('id'));request=uid(b.get('richiesta_id'));version=number(b.get('versione'),'Versione');kind=b.get('azione');note=text(b.get('motivo',''),'Nota operazione',1000,False)
    if kind not in ['ritiro','reso','annulla','ripristina']:raise ValueError('Operazione non valida.')
    previous=c.execute('SELECT * FROM eventi WHERE id=?',(request,)).fetchone()
    if previous:
        if previous['prenotazione']!=identifier or previous['azione']!=kind or previous['motivo']!=note:raise Conflict('Riferimento operazione già utilizzato.')
        return {'stato':'Operazione già registrata. Nessun duplicato.'}
    row=c.execute('SELECT * FROM prenotazioni WHERE id=?',(identifier,)).fetchone()
    if not row:raise ValueError('Prenotazione non trovata.')
    if row['versione']!=version:raise Conflict('Prenotazione aggiornata da un’altra finestra. Ricarica prima di continuare.')
    if kind in ['annulla','ripristina'] and not note:raise ValueError('Scrivi il motivo dell’annullamento o del ripristino.')
    if kind=='ritiro':
        if row['stato']!='prenotata':raise Conflict('Il ritiro è possibile solo per una prenotazione attiva.')
        if not row['inizio']<=today()<row['fine']:raise Conflict('Il ritiro è possibile solo durante il periodo prenotato.')
        total=c.execute('SELECT quantita FROM beni WHERE sku=?',(row['sku'],)).fetchone()[0]
        out=c.execute("SELECT COALESCE(SUM(quantita),0) FROM prenotazioni WHERE sku=? AND stato='ritirata'",(row['sku'],)).fetchone()[0]
        if out+row['quantita']>total:raise Conflict('Pezzi fisicamente insufficienti: registra prima gli eventuali resi in ritardo.')
        state='ritirata'
    elif kind=='reso':
        if row['stato']!='ritirata':raise Conflict('Il reso è possibile solo dopo il ritiro.')
        state='restituita'
    elif kind=='annulla':
        if row['stato']!='prenotata':raise Conflict('Puoi annullare solo una prenotazione non ritirata.')
        state='annullata'
    else:
        if row['stato']!='annullata':raise Conflict('Puoi ripristinare solo una prenotazione annullata.')
        if row['inizio']<today():raise Conflict('Il periodo è già iniziato nel passato. Crea una nuova prenotazione.')
        available=availability(c,row['sku'],row['inizio'],row['fine'])
        if row['quantita']>available['disponibili']:raise Conflict('Disponibilità insufficiente per ripristinare la prenotazione.')
        state='prenotata'
    c.execute('UPDATE prenotazioni SET stato=?,versione=versione+1 WHERE id=?',(state,identifier))
    c.execute('INSERT INTO eventi(id,prenotazione,azione,motivo,creato) VALUES (?,?,?,?,?)',(request,identifier,kind,note,now()))
    return {'stato':state}

def snapshot(c):
    assets=[dict(r) for r in c.execute("SELECT b.*,COALESCE(SUM(CASE WHEN p.stato='ritirata' THEN p.quantita ELSE 0 END),0) AS fuori FROM beni b LEFT JOIN prenotazioni p ON p.sku=b.sku GROUP BY b.sku ORDER BY b.sku")]
    for a in assets:a['presenti']=a['quantita']-a['fuori']
    bookings=[dict(r) for r in c.execute('SELECT p.*,b.nome FROM prenotazioni p JOIN beni b ON p.sku=b.sku ORDER BY p.rowid DESC LIMIT 200')]
    for b in bookings:
        b['in_ritardo']=b['stato']=='ritirata' and b['fine']<=today()
        b['periodo_terminato']=b['stato']=='prenotata' and b['fine']<=today()
    events=[dict(r) for r in c.execute('SELECT * FROM eventi ORDER BY seq DESC LIMIT 100')]
    late=c.execute("SELECT COUNT(*) FROM prenotazioni WHERE stato='ritirata' AND fine<=?",(today(),)).fetchone()[0]
    return {'beni':assets,'prenotazioni':bookings,'eventi':events,'prenotazioni_totali':c.execute('SELECT COUNT(*) FROM prenotazioni').fetchone()[0],'ritardi':late,'data_locale':today()}

class Handler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args):print(f'{self.log_date_time_string()} {fmt%args}',flush=True)
    def send_bytes(self,code,raw,mime,filename=None):
        self.send_response(code);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        if filename:self.send_header('Content-Disposition',f'attachment; filename="{filename}"')
        if mime.startswith('text/html'):self.send_header('Content-Security-Policy',"default-src 'self'; object-src 'none'; frame-ancestors 'none'")
        self.end_headers();self.wfile.write(raw)
    def respond(self,code,data):self.send_bytes(code,dump(data).encode(),'application/json; charset=utf-8')
    def allowed(self):
        host=f'127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host')!=host or self.headers.get('Origin','http://'+host)!='http://'+host:self.respond(403,{'errore':'Accesso consentito soltanto dall’app locale.'});return False
        return True
    def do_GET(self):
        if not self.allowed():return
        try:
            if self.path=='/api/status':self.respond(200,{'app':'noleggio-locale','versione':1});return
            if self.path in ['/api/stato','/api/registro']:
                with connection() as c:
                    c.execute('BEGIN')
                    if self.path=='/api/stato':result=snapshot(c)
                    else:result={'formato':'registro-noleggio-v1','esportato':now(),'beni':[dict(r) for r in c.execute('SELECT * FROM beni')],'prenotazioni':[dict(r) for r in c.execute('SELECT * FROM prenotazioni ORDER BY rowid')],'eventi':[dict(r) for r in c.execute('SELECT * FROM eventi ORDER BY seq')]}
                if self.path=='/api/registro':self.send_bytes(200,dump(result).encode(),'application/json; charset=utf-8','registro-noleggio.json')
                else:self.respond(200,result)
                return
            static={'/':('index.html','text/html'),'/app.js':('app.js','text/javascript'),'/style.css':('style.css','text/css')}
            if self.path not in static:self.respond(404,{'errore':'Pagina non trovata.'});return
            file,mime=static[self.path];self.send_bytes(200,(ROOT/file).read_bytes(),mime+'; charset=utf-8')
        except sqlite3.Error:self.respond(500,{'errore':'Archivio non disponibile.'})
    def do_POST(self):
        if not self.allowed():return
        try:
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':self.respond(415,{'errore':'Richiesto formato JSON.'});return
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=20000:raise ValueError('Richiesta vuota o troppo grande.')
            b=json.loads(self.rfile.read(size))
            if not isinstance(b,dict):raise ValueError('Richiesta non valida.')
            if self.path=='/api/ferma':self.respond(200,{'stato':'App fermata. Dati conservati.'});threading.Thread(target=self.server.shutdown,daemon=True).start();return
            with connection() as c:
                c.execute('BEGIN IMMEDIATE')
                if self.path=='/api/beni':result=create_asset(c,b)
                elif self.path=='/api/disponibilita':start,end=period(b);result=availability(c,sku(b.get('sku')),start,end)
                elif self.path=='/api/prenotazioni':result=create_booking(c,b)
                elif self.path=='/api/azione':result=action(c,b)
                else:self.respond(404,{'errore':'Operazione non trovata.'});return
            self.respond(200,result)
        except Conflict as e:self.respond(409,{'errore':str(e)})
        except (ValueError,TypeError) as e:self.respond(400,{'errore':str(e)})
        except sqlite3.IntegrityError:self.respond(409,{'errore':'Operazione già registrata o in conflitto. Ricarica.'})
        except sqlite3.Error:self.respond(500,{'errore':'Archivio non disponibile. Salvataggio non confermato.'})

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8771);parser.add_argument('--no-browser',action='store_true');args=parser.parse_args()
    with connection():pass
    try:server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    except OSError:print('Porta occupata. Se l’app è già avviata apri http://127.0.0.1:8771.');raise SystemExit(1)
    url=f'http://127.0.0.1:{server.server_port}';print(f'Noleggio locale: {url}\nArchivio: {DATA}',flush=True)
    if not args.no_browser:webbrowser.open(url)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
