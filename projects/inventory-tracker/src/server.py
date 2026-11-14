"""Magazzino locale: giacenze derivate da movimenti, solo libreria standard."""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from contextlib import contextmanager
from pathlib import Path
import datetime as dt
import json, os, re, sqlite3, threading, uuid, webbrowser

ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('MAGAZZINO_DATA',str(ROOT.parent/'data')))
MAX_BODY=20_000_000

class Conflict(ValueError):pass

@contextmanager
def connection():
    DATA.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(DATA/'magazzino.sqlite3',timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    try:
        con.execute('CREATE TABLE IF NOT EXISTS articoli (sku TEXT PRIMARY KEY, nome TEXT NOT NULL, categoria TEXT NOT NULL, soglia INTEGER NOT NULL CHECK(soglia>=0), versione INTEGER NOT NULL, creato TEXT NOT NULL)')
        con.execute("CREATE TABLE IF NOT EXISTS movimenti (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, sku TEXT NOT NULL REFERENCES articoli(sku), tipo TEXT NOT NULL CHECK(tipo IN ('carico','scarico')), quantita INTEGER NOT NULL CHECK(quantita>0), data TEXT NOT NULL, nota TEXT NOT NULL, registrato TEXT NOT NULL)")
        yield con
        con.commit()
    except Exception:
        con.rollback();raise
    finally:con.close()

def text(value,label,maximum,required=True):
    if not isinstance(value,str) or len(value.strip())>maximum or (required and not value.strip()):raise ValueError(f'{label}: campo obbligatorio o troppo lungo (massimo {maximum}).')
    return value.strip()

def integer(value,label,minimum=0,maximum=1_000_000):
    if not isinstance(value,int) or isinstance(value,bool) or not minimum<=value<=maximum:raise ValueError(f'{label}: numero intero da {minimum} a {maximum}.')
    return value

def sku(value):
    result=text(value,'SKU',64).upper()
    if not re.fullmatch(r'[A-Z0-9][A-Z0-9._-]{0,63}',result):raise ValueError('SKU: usa lettere A-Z, numeri, punti, trattini o underscore.')
    return result

def stamp(value=None):
    if value is None:return dt.datetime.now(dt.timezone.utc).isoformat()
    if not isinstance(value,str) or len(value)>40:raise ValueError('Data registrazione non valida.')
    try:
        parsed=dt.datetime.fromisoformat(value)
        if parsed.tzinfo is None:raise ValueError()
    except ValueError:raise ValueError('Data registrazione non valida.')
    return value

def article(data):
    if not isinstance(data,dict):raise ValueError('Articolo non valido.')
    return {'sku':sku(data.get('sku')),'nome':text(data.get('nome'),'Nome',160),'categoria':text(data.get('categoria',''),'Categoria',100,False),'soglia':integer(data.get('soglia'),'Soglia')}

def movement(data):
    if not isinstance(data,dict):raise ValueError('Movimento non valido.')
    try:identifier=str(uuid.UUID(data.get('id','')))
    except (ValueError,TypeError,AttributeError):raise ValueError('Riferimento movimento non valido.')
    date=data.get('data','')
    if not isinstance(date,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',date):raise ValueError('Data movimento non valida.')
    try:parsed=dt.date.fromisoformat(date)
    except ValueError:raise ValueError('Data movimento non valida.')
    if parsed>dt.date.today():raise ValueError('Un movimento non può avere una data futura.')
    if data.get('tipo') not in ['carico','scarico']:raise ValueError('Tipo movimento non valido.')
    return {'id':identifier,'sku':sku(data.get('sku')),'tipo':data['tipo'],'quantita':integer(data.get('quantita'),'Quantità',1),'data':date,'nota':text(data.get('nota'),'Causale',500)}

def balance(con,code):
    return con.execute("SELECT COALESCE(SUM(CASE WHEN tipo='carico' THEN quantita ELSE -quantita END),0) FROM movimenti WHERE sku=?",(code,)).fetchone()[0]

def add_article(con,data,created=None):
    a=article(data)
    if con.execute('SELECT 1 FROM articoli WHERE sku=?',(a['sku'],)).fetchone():raise Conflict('SKU già presente: non è stato creato un duplicato.')
    if con.execute('SELECT COUNT(*) FROM articoli').fetchone()[0]>=2000:raise ValueError('Limite della prima versione: 2000 articoli.')
    con.execute('INSERT INTO articoli VALUES (?,?,?,?,1,?)',(a['sku'],a['nome'],a['categoria'],a['soglia'],stamp(created)))
    return a

def add_movement(con,data,registered=None):
    m=movement(data)
    previous=con.execute('SELECT * FROM movimenti WHERE id=?',(m['id'],)).fetchone()
    if previous:
        if any(previous[k]!=v for k,v in m.items()):raise Conflict('Riferimento già usato per un movimento diverso.')
        return dict(previous),False
    if not con.execute('SELECT 1 FROM articoli WHERE sku=?',(m['sku'],)).fetchone():raise ValueError('Articolo non trovato.')
    if con.execute('SELECT COUNT(*) FROM movimenti').fetchone()[0]>=20000:raise ValueError('Limite della prima versione: 20000 movimenti.')
    if m['tipo']=='scarico' and balance(con,m['sku'])<m['quantita']:raise Conflict('Giacenza insufficiente: scarico non registrato.')
    con.execute('INSERT INTO movimenti(id,sku,tipo,quantita,data,nota,registrato) VALUES (?,?,?,?,?,?,?)',tuple(m[k] for k in ['id','sku','tipo','quantita','data','nota'])+(stamp(registered),))
    return m,True

def snapshot(con):
    rows=con.execute("""SELECT a.*,COALESCE(SUM(CASE WHEN m.tipo='carico' THEN m.quantita ELSE -m.quantita END),0) AS giacenza,
      MIN(CASE WHEN m.tipo='carico' THEN m.data END) AS primo_carico,
      MAX(CASE WHEN m.tipo='scarico' THEN m.data END) AS ultimo_scarico,
      COALESCE(SUM(CASE WHEN m.tipo='scarico' AND m.data>=? THEN m.quantita ELSE 0 END),0) AS scarichi_30gg
      FROM articoli a LEFT JOIN movimenti m ON a.sku=m.sku GROUP BY a.sku ORDER BY a.sku""",((dt.date.today()-dt.timedelta(days=29)).isoformat(),)).fetchall()
    items=[]
    for row in rows:
        a=dict(row);ref=a['ultimo_scarico'] or a['primo_carico'];a['giorni_senza_scarico']=(dt.date.today()-dt.date.fromisoformat(ref)).days if ref else None
        a['scorta_bassa']=a['giacenza']<=a['soglia'];items.append(a)
    history=[dict(r) for r in con.execute('SELECT m.*,a.nome FROM movimenti m JOIN articoli a ON m.sku=a.sku ORDER BY seq DESC LIMIT 100')]
    return {'articoli':items,'movimenti':history,'totale_movimenti':con.execute('SELECT COUNT(*) FROM movimenti').fetchone()[0],'data_riferimento':dt.date.today().isoformat()}

def backup(con):
    return {'formato':'magazzino-locale-v1','esportato':stamp(),'articoli':[dict(r) for r in con.execute('SELECT * FROM articoli ORDER BY sku')],'movimenti':[dict(r) for r in con.execute('SELECT * FROM movimenti ORDER BY seq')]}

def restore(con,data):
    if not isinstance(data,dict) or data.get('formato')!='magazzino-locale-v1':raise ValueError('Backup non riconosciuto.')
    if not isinstance(data.get('articoli'),list) or not isinstance(data.get('movimenti'),list):raise ValueError('Backup incompleto.')
    if len(data['articoli'])>2000 or len(data['movimenti'])>20000:raise ValueError('Limite ripristino: 2000 articoli e 20000 movimenti.')
    if con.execute('SELECT COUNT(*) FROM articoli').fetchone()[0]:raise Conflict('Ripristino consentito solo su un archivio vuoto. I dati esistenti restano invariati.')
    for original in data['articoli']:
        add_article(con,original,stamp(original.get('creato')))
        version=integer(original.get('versione',1),'Versione',1)
        con.execute('UPDATE articoli SET versione=? WHERE sku=?',(version,sku(original['sku'])))
    seen=set()
    for original in data['movimenti']:
        if not isinstance(original,dict):raise ValueError('Movimento nel backup non valido.')
        m=movement(original)
        if m['id'] in seen:raise ValueError('Identificativo movimento duplicato nel backup.')
        seen.add(m['id']);add_movement(con,original,stamp(original.get('registrato')))
    return {'articoli':len(data['articoli']),'movimenti':len(data['movimenti'])}

def demo(con):
    if con.execute('SELECT COUNT(*) FROM articoli').fetchone()[0]:raise Conflict('Gli esempi si possono caricare solo su un archivio vuoto.')
    samples=[('DEMO-FILTRO','Filtro dimostrativo','Ricambi',5,20,17,5),('DEMO-GUANTO','Guanti dimostrativi','Consumabili',10,50,0,120),('DEMO-NASTRO','Nastro dimostrativo','Consumabili',3,12,2,10)]
    for code,name,category,threshold,loaded,out,age in samples:
        add_article(con,dict(sku=code,nome=name,categoria=category,soglia=threshold))
        add_movement(con,dict(id=str(uuid.uuid4()),sku=code,tipo='carico',quantita=loaded,data=(dt.date.today()-dt.timedelta(days=age)).isoformat(),nota='DEMO: carico iniziale sintetico'))
        if out:add_movement(con,dict(id=str(uuid.uuid4()),sku=code,tipo='scarico',quantita=out,data=(dt.date.today()-dt.timedelta(days=age)).isoformat(),nota='DEMO: consumo sintetico'))
    return {'stato':'Caricati 3 articoli e 5 movimenti dimostrativi.'}

class Handler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args):print(f'{self.log_date_time_string()} {fmt%args}',flush=True)
    def respond(self,code,data):
        raw=json.dumps(data,ensure_ascii=False).encode('utf-8');self.send_response(code);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(raw)
    def allowed(self):
        host=f'127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host')!=host or self.headers.get('Origin','http://'+host)!='http://'+host:
            self.respond(403,{'errore':'Accesso consentito soltanto dall’app locale.'});return False
        return True
    def do_GET(self):
        if not self.allowed():return
        try:
            if self.path=='/api/status':self.respond(200,{'app':'magazzino-locale','versione':1});return
            if self.path in ['/api/stato','/api/backup']:
                with connection() as con:
                    con.execute('BEGIN')
                    result=snapshot(con) if self.path=='/api/stato' else backup(con)
                self.respond(200,result);return
            static={'/':('index.html','text/html'),'/app.js':('app.js','text/javascript'),'/style.css':('style.css','text/css')}
            if self.path not in static:self.respond(404,{'errore':'Pagina non trovata.'});return
            name,mime=static[self.path];raw=(ROOT/name).read_bytes();self.send_response(200);self.send_header('Content-Type',mime+'; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.send_header('Content-Security-Policy',"default-src 'self'; object-src 'none'; frame-ancestors 'none'");self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(raw)
        except sqlite3.Error:self.respond(500,{'errore':'Archivio non disponibile.'})
    def do_POST(self):
        if not self.allowed():return
        try:
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':self.respond(415,{'errore':'Richiesto formato JSON.'});return
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=MAX_BODY:raise ValueError('Richiesta vuota o superiore a 20 MB.')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict):raise ValueError('Formato richiesta non valido.')
            if self.path=='/api/ferma':self.respond(200,{'stato':'App fermata. Dati conservati.'});threading.Thread(target=self.server.shutdown,daemon=True).start();return
            with connection() as con:
                con.execute('BEGIN IMMEDIATE')
                if self.path=='/api/articoli':result=add_article(con,data)
                elif self.path=='/api/movimenti':
                    m,added=add_movement(con,data);result={'movimento':m,'nuovo':added,'giacenza':balance(con,m['sku'])}
                elif self.path=='/api/soglia':
                    code=sku(data.get('sku'));threshold=integer(data.get('soglia'),'Soglia');version=integer(data.get('versione'),'Versione',1)
                    changed=con.execute('UPDATE articoli SET soglia=?,versione=versione+1 WHERE sku=? AND versione=?',(threshold,code,version)).rowcount
                    if not changed:raise Conflict('Articolo modificato o non trovato. Ricarica prima di aggiornare la soglia.')
                    result={'sku':code,'soglia':threshold,'versione':version+1}
                elif self.path=='/api/importa':result=restore(con,data)
                elif self.path=='/api/demo':result=demo(con)
                else:self.respond(404,{'errore':'Operazione non trovata.'});return
            self.respond(200,result)
        except Conflict as e:self.respond(409,{'errore':str(e)})
        except (ValueError,TypeError) as e:self.respond(400,{'errore':str(e)})
        except sqlite3.Error:self.respond(500,{'errore':'Archivio non disponibile. Nessun salvataggio confermato.'})

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8768);parser.add_argument('--no-browser',action='store_true');args=parser.parse_args()
    with connection():pass
    try:server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    except OSError:print('Porta occupata. Se l’app è già avviata apri http://127.0.0.1:8768.');raise SystemExit(1)
    url=f'http://127.0.0.1:{server.server_port}';print(f'Magazzino locale: {url}\nArchivio: {DATA}\nCtrl+C per fermare.',flush=True)
    if not args.no_browser:webbrowser.open(url)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
