"""Bozze grafiche locali: versioni immutabili e decisioni riferite all'immagine."""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from contextlib import contextmanager
from pathlib import Path
from io import BytesIO
from zoneinfo import ZoneInfo
import base64, datetime as dt, hashlib, json, os, re, secrets, sqlite3, threading, uuid, warnings, webbrowser

ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('BOZZE_DATA',str(ROOT.parent/'data')))
ROME=ZoneInfo('Europe/Rome')
MAX_IMAGE=4_000_000

class Conflict(ValueError):pass

def now():return dt.datetime.now(dt.timezone.utc).isoformat()
def today():return dt.datetime.now(ROME).date()
def dump(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'))
def identifier(value):
    try:return str(uuid.UUID(value))
    except (ValueError,TypeError,AttributeError):raise ValueError('Identificativo non valido.')
def text(value,label,maximum,required=True):
    if not isinstance(value,str) or len(value.strip())>maximum or required and not value.strip():raise ValueError(f'{label}: campo obbligatorio o oltre {maximum} caratteri.')
    return value.strip()
def integer(value):
    if not isinstance(value,int) or isinstance(value,bool) or not 0<=value<=20:raise ValueError('Versione di partenza non valida.')
    return value

@contextmanager
def connection():
    DATA.mkdir(parents=True,exist_ok=True);con=sqlite3.connect(DATA/'bozze.sqlite3',timeout=15);con.row_factory=sqlite3.Row;con.execute('PRAGMA foreign_keys=ON')
    try:
        con.execute('CREATE TABLE IF NOT EXISTS commesse(id TEXT PRIMARY KEY, titolo TEXT NOT NULL, cliente TEXT NOT NULL, ordine TEXT NOT NULL, scadenza TEXT NOT NULL, istruzioni TEXT NOT NULL, token TEXT UNIQUE NOT NULL, creato TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS revisioni(id TEXT PRIMARY KEY, commessa TEXT NOT NULL REFERENCES commesse(id), numero INTEGER NOT NULL, nome_file TEXT NOT NULL, originale BLOB NOT NULL, anteprima BLOB NOT NULL, sha256 TEXT NOT NULL, note TEXT NOT NULL, creato TEXT NOT NULL, UNIQUE(commessa,numero))')
        con.execute("CREATE TABLE IF NOT EXISTS decisioni(revisione TEXT PRIMARY KEY REFERENCES revisioni(id), id TEXT UNIQUE NOT NULL, tipo TEXT NOT NULL CHECK(tipo IN ('approvata','modifiche')), nome TEXT NOT NULL, nota TEXT NOT NULL, creato TEXT NOT NULL)")
        yield con;con.commit()
    except Exception:con.rollback();raise
    finally:con.close()

def latest(con,case):return con.execute('SELECT * FROM revisioni WHERE commessa=? ORDER BY numero DESC LIMIT 1',(case,)).fetchone()

def image_bytes(encoded):
    if not isinstance(encoded,str):raise ValueError('Immagine mancante.')
    try:raw=base64.b64decode(encoded,validate=True)
    except (ValueError,base64.binascii.Error):raise ValueError('Immagine non valida.')
    if not 0<len(raw)<=MAX_IMAGE:raise ValueError('Immagine vuota o superiore a 4 MB.')
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as probe:
                if probe.format not in ['PNG','JPEG']:raise ValueError('Sono accettate solo immagini PNG o JPEG.')
                kind='png' if probe.format=='PNG' else 'jpg'
                if probe.width*probe.height>16_000_000:raise ValueError('Limite: 16 milioni di pixel.')
                if getattr(probe,'n_frames',1)!=1:raise ValueError('Le immagini animate non sono supportate.')
                probe.verify()
            with Image.open(BytesIO(raw)) as source:
                source=ImageOps.exif_transpose(source).convert('RGBA');source.thumbnail((1600,1600))
                background=Image.new('RGB',source.size,'white');background.paste(source,mask=source.getchannel('A'))
                output=BytesIO();background.save(output,'JPEG',quality=88,optimize=True)
        return raw,output.getvalue(),hashlib.sha256(raw).hexdigest(),kind
    except ValueError:raise
    except (UnidentifiedImageError,OSError,Image.DecompressionBombError,Image.DecompressionBombWarning):raise ValueError('Immagine danneggiata o non supportata.')

def create_case(con,body):
    case_id=identifier(body.get('id'))
    fields={k:text(body.get(k,''),label,limit,required) for k,label,limit,required in [('titolo','Titolo',180,True),('cliente','Cliente',160,True),('ordine','Riferimento ordine',100,False),('istruzioni','Istruzioni',2000,False)]}
    date=body.get('scadenza','')
    if not isinstance(date,str):raise ValueError('Scadenza non valida.')
    if date:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',date):raise ValueError('Scadenza non valida.')
        try:dt.date.fromisoformat(date)
        except ValueError:raise ValueError('Scadenza inesistente.')
    fields['scadenza']=date
    previous=con.execute('SELECT * FROM commesse WHERE id=?',(case_id,)).fetchone()
    if previous:
        if any(previous[k]!=v for k,v in fields.items()):raise Conflict('Riferimento già utilizzato per una pratica diversa.')
        return case_id
    if con.execute('SELECT COUNT(*) FROM commesse').fetchone()[0]>=200:raise ValueError('Limite del prototipo: 200 pratiche.')
    con.execute('INSERT INTO commesse VALUES (?,?,?,?,?,?,?,?)',(case_id,fields['titolo'],fields['cliente'],fields['ordine'],date,fields['istruzioni'],secrets.token_urlsafe(32),now()))
    return case_id

def add_revision(con,case_id,body,prepared):
    revision_id=identifier(body.get('id'));base=integer(body.get('versione_base'));notes=text(body.get('note',''),'Note revisione',2000,False);filename=text(body.get('nome_file'),'Nome file',200)
    if filename.rsplit('.',1)[-1].lower() not in ['png','jpg','jpeg']:raise ValueError('Usa un nome file PNG o JPEG.')
    if not con.execute('SELECT 1 FROM commesse WHERE id=?',(case_id,)).fetchone():raise ValueError('Pratica non trovata.')
    raw,preview,digest,kind=prepared
    suffix=filename.rsplit('.',1)[-1].lower()
    if ('jpg' if suffix=='jpeg' else suffix)!=kind:raise ValueError('L’estensione del file non corrisponde al contenuto dell’immagine.')
    previous=con.execute('SELECT * FROM revisioni WHERE id=?',(revision_id,)).fetchone()
    if previous:
        if previous['commessa']!=case_id or previous['sha256']!=digest or previous['note']!=notes:raise Conflict('Riferimento già usato per una revisione diversa.')
        return previous['numero']
    current=latest(con,case_id);version=current['numero'] if current else 0
    if base!=version:raise Conflict('È già stata caricata una nuova versione. Ricarica la pratica prima di continuare.')
    if version>=20:raise ValueError('Limite del prototipo: 20 versioni per pratica.')
    if current:
        decision=con.execute('SELECT tipo FROM decisioni WHERE revisione=?',(current['id'],)).fetchone()
        if decision and decision['tipo']=='approvata' and body.get('sostituisci_approvata') is not True:raise Conflict('La versione attuale è approvata. Conferma esplicitamente che la nuova bozza richiederà una nuova approvazione.')
    con.execute('INSERT INTO revisioni VALUES (?,?,?,?,?,?,?,?,?)',(revision_id,case_id,version+1,filename,raw,preview,digest,notes,now()))
    return version+1

def decide(con,token,body):
    case=con.execute('SELECT id FROM commesse WHERE token=?',(token,)).fetchone()
    if not case:raise ValueError('Pagina di revisione non trovata.')
    revision_id=identifier(body.get('revisione'));decision_id=identifier(body.get('id'));name=text(body.get('nome'),'Nome di chi conferma',160);note=text(body.get('nota',''),'Nota',2000,False)
    kind=body.get('tipo')
    if kind not in ['approvata','modifiche']:raise ValueError('Scegli approvazione o richiesta di modifiche.')
    if kind=='approvata' and body.get('conferma') is not True:raise ValueError('Conferma di aver controllato la versione mostrata.')
    if kind=='modifiche' and not note:raise ValueError('Descrivi le modifiche richieste.')
    current=latest(con,case['id'])
    if not current or revision_id!=current['id'] or body.get('sha256')!=current['sha256']:raise Conflict('Questa bozza è superata o non corrisponde all’immagine corrente. Ricarica la pagina e controlla la nuova versione.')
    previous=con.execute('SELECT * FROM decisioni WHERE revisione=?',(revision_id,)).fetchone()
    if previous:
        if previous['id']==decision_id and previous['tipo']==kind and previous['nome']==name and previous['nota']==note:return {'stato':'Decisione già registrata, nessun duplicato.'}
        raise Conflict('Esiste già una decisione per questa versione. Per una nuova conferma serve una nuova bozza.')
    con.execute('INSERT INTO decisioni VALUES (?,?,?,?,?,?)',(revision_id,decision_id,kind,name,note,now()))
    return {'stato':'Approvazione registrata per questa versione.' if kind=='approvata' else 'Richiesta di modifiche registrata.'}

def case_detail(con,case_id,review=False):
    row=con.execute('SELECT * FROM commesse WHERE id=?',(case_id,)).fetchone()
    if not row:raise ValueError('Pratica non trovata.')
    data=dict(row);revisions=[]
    for r in con.execute('SELECT id,numero,nome_file,sha256,note,creato FROM revisioni WHERE commessa=? ORDER BY numero DESC',(case_id,)):
        revision=dict(r);d=con.execute('SELECT id,tipo,nome,nota,creato FROM decisioni WHERE revisione=?',(r['id'],)).fetchone();revision['decisione']=dict(d) if d else None;revisions.append(revision)
    data['revisioni']=revisions;current=revisions[0] if revisions else None
    data['stato']=current['decisione']['tipo'] if current and current['decisione'] else ('attesa' if current else 'senza_bozza')
    data['in_ritardo']=data['stato']=='attesa' and bool(data['scadenza']) and data['scadenza']<today().isoformat()
    data['versione_corrente']=current['numero'] if current else 0
    if review:data['revisioni']=revisions[:1];data.pop('token',None)
    return data

class Handler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args):
        safe=tuple(re.sub(r'/r/[A-Za-z0-9_-]+','/r/[link]',v) if isinstance(v,str) else v for v in args)
        safe=tuple(re.sub(r'/api/review/[A-Za-z0-9_-]+','/api/review/[link]',v) if isinstance(v,str) else v for v in safe)
        print(f'{self.log_date_time_string()} {fmt%safe}',flush=True)
    def send_bytes(self,code,raw,mime,filename=None,html=False):
        self.send_response(code);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer')
        if filename:self.send_header('Content-Disposition',f'attachment; filename="{filename}"')
        if html:self.send_header('Content-Security-Policy',"default-src 'self'; object-src 'none'; frame-ancestors 'none'")
        self.end_headers();self.wfile.write(raw)
    def respond(self,code,data):self.send_bytes(code,dump(data).encode(),'application/json; charset=utf-8')
    def allowed(self):
        host=f'127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host')!=host or self.headers.get('Origin','http://'+host)!='http://'+host:self.respond(403,{'errore':'Accesso consentito soltanto dall’app locale.'});return False
        return True
    def do_GET(self):
        if not self.allowed():return
        try:
            if self.path=='/api/status':self.respond(200,{'app':'bozze-locali','versione':1});return
            if self.path=='/api/commesse':
                with connection() as con:
                    con.execute('BEGIN');data=[case_detail(con,r['id']) for r in con.execute('SELECT id FROM commesse ORDER BY rowid DESC')]
                self.respond(200,{'commesse':data,'data_locale':today().isoformat()});return
            match=re.fullmatch(r'/api/commesse/([0-9a-f-]{36})',self.path)
            if match:
                with connection() as con:con.execute('BEGIN');data=case_detail(con,match.group(1))
                self.respond(200,data);return
            match=re.fullmatch(r'/api/review/([A-Za-z0-9_-]{43})',self.path)
            if match:
                with connection() as con:
                    con.execute('BEGIN');case=con.execute('SELECT id FROM commesse WHERE token=?',(match.group(1),)).fetchone()
                    if not case:self.respond(404,{'errore':'Pagina non trovata.'});return
                    data=case_detail(con,case['id'],True)
                self.respond(200,data);return
            match=re.fullmatch(r'/api/(immagine|originale)/([0-9a-f-]{36})',self.path)
            if match:
                with connection() as con:r=con.execute('SELECT anteprima,originale,nome_file FROM revisioni WHERE id=?',(match.group(2),)).fetchone()
                if not r:self.respond(404,{'errore':'Immagine non trovata.'});return
                if match.group(1)=='immagine':self.send_bytes(200,r['anteprima'],'image/jpeg')
                else:self.send_bytes(200,r['originale'],'application/octet-stream','bozza-'+match.group(2)+'.'+r['nome_file'].rsplit('.',1)[-1].lower())
                return
            match=re.fullmatch(r'/api/registro/([0-9a-f-]{36})',self.path)
            if match:
                with connection() as con:con.execute('BEGIN');data=case_detail(con,match.group(1))
                data.pop('token',None);data['formato']='registro-bozze-v1';data['nota']='Registro operativo, non firma digitale. Le decisioni precedenti non autorizzano versioni successive.'
                self.send_bytes(200,dump(data).encode(),'application/json; charset=utf-8','registro-'+match.group(1)+'.json');return
            static={'/':('index.html','text/html'),'/app.js':('app.js','text/javascript'),'/review.js':('review.js','text/javascript'),'/style.css':('style.css','text/css')}
            if re.fullmatch(r'/r/[A-Za-z0-9_-]{43}',self.path):file,mime='review.html','text/html'
            elif self.path in static:file,mime=static[self.path]
            else:self.respond(404,{'errore':'Pagina non trovata.'});return
            self.send_bytes(200,(ROOT/file).read_bytes(),mime+'; charset=utf-8',html=mime=='text/html')
        except ValueError as e:self.respond(404,{'errore':str(e)})
        except sqlite3.Error:self.respond(500,{'errore':'Archivio non disponibile.'})
    def do_POST(self):
        if not self.allowed():return
        try:
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':self.respond(415,{'errore':'Richiesto formato JSON.'});return
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=5_500_000:raise ValueError('Richiesta vuota o troppo grande.')
            body=json.loads(self.rfile.read(size))
            if not isinstance(body,dict):raise ValueError('Richiesta non valida.')
            if self.path=='/api/ferma':self.respond(200,{'stato':'App fermata. Dati conservati.'});threading.Thread(target=self.server.shutdown,daemon=True).start();return
            revision_route=re.fullmatch(r'/api/commesse/([0-9a-f-]{36})/revisioni',self.path)
            decision_route=re.fullmatch(r'/api/review/([A-Za-z0-9_-]{43})/decisione',self.path)
            prepared=image_bytes(body.get('contenuto')) if revision_route else None
            with connection() as con:
                con.execute('BEGIN IMMEDIATE')
                if self.path=='/api/commesse':result={'id':create_case(con,body)}
                elif revision_route:result={'versione':add_revision(con,revision_route.group(1),body,prepared)}
                elif decision_route:result=decide(con,decision_route.group(1),body)
                else:self.respond(404,{'errore':'Operazione non trovata.'});return
            self.respond(200,result)
        except Conflict as e:self.respond(409,{'errore':str(e)})
        except (ValueError,TypeError) as e:self.respond(400,{'errore':str(e)})
        except sqlite3.IntegrityError:self.respond(409,{'errore':'Operazione già registrata o in conflitto. Ricarica prima di riprovare.'})
        except sqlite3.Error:self.respond(500,{'errore':'Archivio non disponibile. Salvataggio non confermato.'})

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8770);parser.add_argument('--no-browser',action='store_true');args=parser.parse_args()
    with connection():pass
    try:server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    except OSError:print('Porta occupata. Se l’app è già avviata apri http://127.0.0.1:8770.');raise SystemExit(1)
    url=f'http://127.0.0.1:{server.server_port}';print(f'Bozze locali: {url}\nArchivio: {DATA}\nPagina cliente dimostrativa utilizzabile solo su questo PC.',flush=True)
    if not args.no_browser:webbrowser.open(url)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
