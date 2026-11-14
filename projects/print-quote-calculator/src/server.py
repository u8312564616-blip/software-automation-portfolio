"""Preventivi tipografia: app locale Python, nessun servizio a pagamento."""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from contextlib import contextmanager
from pathlib import Path
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import datetime as dt
import json, os, uuid, re, webbrowser
import threading

ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('TIPOGRAFIA_DATA',str(ROOT.parent/'data')))
DEFAULT={'versione':1,'dimostrativo':True,'valuta':'EUR','avviamento':'12.50','minimo_lavorazione':'15.00','spedizione':'8.50','iva_percentuale':'22.00','carta':{'standard':{'A5':'0.05','A4':'0.10'},'premium':{'A5':'0.09','A4':'0.18'}},'stampa':{'bianco_nero':'0.04','colore':'0.15'},'finitura':{'nessuna':'0.00','piegatura':'0.03','plastificazione':'0.20'}}

@contextmanager
def connection():
    DATA.mkdir(parents=True,exist_ok=True)
    con=sqlite3.connect(DATA/'tipografia.sqlite3',timeout=10)
    try:
        con.execute('CREATE TABLE IF NOT EXISTS listino (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS preventivi (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        con.execute('INSERT OR IGNORE INTO listino VALUES (1,?)',(json.dumps(DEFAULT),))
        with con:yield con
    finally:con.close()

import sqlite3
def money(value):
    return format(value.quantize(Decimal('.01'),rounding=ROUND_HALF_UP),'.2f')

def number(value,maximum=Decimal('1000000')):
    if not isinstance(value,(str,int)) or isinstance(value,bool):raise ValueError('Usa un numero con punto decimale.')
    try:result=Decimal(str(value))
    except InvalidOperation:raise ValueError('Numero non valido.')
    if not result.is_finite() or result<0 or result>maximum:raise ValueError('Numero fuori intervallo.')
    if result.as_tuple().exponent < -2:raise ValueError('Sono ammessi al massimo due decimali.')
    return result

def validate_config(data):
    if not isinstance(data,dict) or data.get('valuta')!='EUR':raise ValueError('Listino EUR richiesto.')
    clean={'valuta':'EUR','dimostrativo':True}
    for key in ['avviamento','minimo_lavorazione','spedizione','iva_percentuale']:
        clean[key]=money(number(data.get(key),Decimal('100') if key=='iva_percentuale' else Decimal('1000000')))
    for group,expected in [('stampa',['bianco_nero','colore']),('finitura',['nessuna','piegatura','plastificazione'])]:
        if not isinstance(data.get(group),dict) or set(data[group])!=set(expected):raise ValueError('Voci listino non valide: '+group)
        clean[group]={key:money(number(data[group][key])) for key in expected}
    if not isinstance(data.get('carta'),dict) or set(data['carta'])!={'standard','premium'}:raise ValueError('Carte non valide.')
    clean['carta']={}
    for paper in ['standard','premium']:
        if not isinstance(data['carta'][paper],dict) or set(data['carta'][paper])!={'A4','A5'}:raise ValueError('Formati carta non validi.')
        clean['carta'][paper]={size:money(number(data['carta'][paper][size])) for size in ['A4','A5']}
    return clean

def calculate(data,config):
    if not isinstance(data,dict):raise ValueError('Richiesta non valida.')
    quantity=data.get('quantita')
    if not isinstance(quantity,int) or isinstance(quantity,bool) or not 1<=quantity<=100000:raise ValueError('Quantità: numero intero da 1 a 100000.')
    size,paper,print_type,finish=(data.get(k) for k in ['formato','carta','stampa','finitura'])
    if size not in ['A4','A5'] or paper not in ['standard','premium'] or print_type not in config['stampa'] or finish not in config['finitura']:raise ValueError('Seleziona formato, carta, stampa e finitura validi.')
    if not isinstance(data.get('consegna'),bool):raise ValueError('Consegna non valida.')
    discount=number(data.get('sconto_percentuale','0'),Decimal('100'))
    rows=[{'voce':'Avviamento','quantita':1,'prezzo_unitario':config['avviamento'],'totale':config['avviamento']}]
    for label,price in [('Carta '+paper+' '+size,config['carta'][paper][size]),('Stampa '+print_type,config['stampa'][print_type]),('Finitura '+finish,config['finitura'][finish])]:
        rows.append({'voce':label,'quantita':quantity,'prezzo_unitario':price,'totale':money(Decimal(price)*quantity)})
    before=sum(Decimal(r['totale']) for r in rows)
    minimum=max(Decimal('0'),Decimal(config['minimo_lavorazione'])-before)
    if minimum:rows.append({'voce':'Adeguamento al minimo lavorazione','quantita':1,'prezzo_unitario':money(minimum),'totale':money(minimum)})
    production=before+minimum
    discount_amount=Decimal(money(production*discount/100))
    shipping=Decimal(config['spedizione']) if data['consegna'] else Decimal('0')
    taxable=production-discount_amount+shipping
    vat=Decimal(money(taxable*Decimal(config['iva_percentuale'])/100))
    params={'quantita':quantity,'formato':size,'carta':paper,'stampa':print_type,'finitura':finish,'consegna':data['consegna'],'sconto_percentuale':money(discount)}
    return {'parametri':params,'righe':rows,'lavorazione':money(production),'sconto':money(discount_amount),'spedizione':money(shipping),'imponibile':money(taxable),'iva_percentuale':config['iva_percentuale'],'iva':money(vat),'totale':money(taxable+vat),'valuta':'EUR','versione_listino':config['versione'],'listino_snapshot':config}

class Handler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args):print(f'{self.log_date_time_string()} {fmt%args}',flush=True)
    def respond(self,code,data):
        raw=json.dumps(data,ensure_ascii=False).encode();self.send_response(code);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(raw)
    def allowed(self):
        host=f'127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host')!=host or self.headers.get('Origin','http://'+host)!='http://'+host:
            self.respond(403,{'errore':'Accesso consentito soltanto da questa app locale.'});return False
        return True
    def do_GET(self):
        if not self.allowed():return
        if self.path in ['/api/listino','/api/preventivi']:
            with connection() as con:
                if self.path=='/api/listino':result=json.loads(con.execute('SELECT payload FROM listino WHERE id=1').fetchone()[0])
                else:result={'preventivi':[json.loads(r[0]) for r in con.execute('SELECT payload FROM preventivi ORDER BY rowid DESC')]}
            self.respond(200,result);return
        if self.path=='/api/status':self.respond(200,{'app':'tipografia-locale','versione':1});return
        static={'/':('index.html','text/html'),'/app.js':('app.js','text/javascript'),'/style.css':('style.css','text/css')}
        if self.path not in static:self.respond(404,{'errore':'Pagina non trovata.'});return
        file,mime=static[self.path];raw=(ROOT/file).read_bytes();self.send_response(200);self.send_header('Content-Type',mime+'; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.send_header('Content-Security-Policy',"default-src 'self'; object-src 'none'; frame-ancestors 'none'");self.end_headers();self.wfile.write(raw)
    def do_POST(self):
        if not self.allowed():return
        try:
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':self.respond(415,{'errore':'Richiesto formato JSON.'});return
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=100000:raise ValueError('Richiesta vuota o troppo grande.')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict):raise ValueError('Richiesta non valida.')
            if self.path=='/api/ferma':
                self.respond(200,{'stato':'Arresto dell’app in corso. Dati conservati.'})
                threading.Thread(target=self.server.shutdown,daemon=True).start();return
            with connection() as con:
                config=json.loads(con.execute('SELECT payload FROM listino WHERE id=1').fetchone()[0])
                if self.path=='/api/listino':
                    clean=validate_config(data)
                    if data.get('versione')!=config['versione']:self.respond(409,{'errore':'Listino aggiornato da un’altra finestra. Ricarica prima di modificarlo.'});return
                    clean['versione']=config['versione']+1
                    changed=con.execute('UPDATE listino SET payload=? WHERE id=1 AND payload=?',(json.dumps(clean),json.dumps(config))).rowcount
                    if not changed:self.respond(409,{'errore':'Listino modificato contemporaneamente. Ricarica.'});return
                    self.respond(200,clean);return
                if self.path not in ['/api/calcola','/api/preventivi']:self.respond(404,{'errore':'Operazione non trovata.'});return
                result=calculate(data,config)
                if self.path=='/api/calcola':self.respond(200,result);return
                cliente=data.get('cliente','')
                if not isinstance(cliente,str) or not 1<=len(cliente.strip())<=160:raise ValueError('Inserisci il cliente, massimo 160 caratteri.')
                try:request_id=str(uuid.UUID(data.get('richiesta_id','')))
                except (ValueError,TypeError,AttributeError):raise ValueError('Riferimento richiesta non valido.')
                previous=con.execute('SELECT payload FROM preventivi WHERE id=?',(request_id,)).fetchone()
                if previous:
                    saved=json.loads(previous[0])
                    if saved['cliente']!=cliente.strip() or saved['calcolo']['parametri']!=result['parametri']:
                        self.respond(409,{'errore':'Riferimento già usato per un preventivo diverso.'});return
                    self.respond(200,saved);return
                if data.get('versione_listino')!=config['versione']:self.respond(409,{'errore':'Il listino è cambiato. Ricalcola prima di salvare.'});return
                saved={'id':request_id,'cliente':cliente.strip(),'creato':dt.datetime.now(dt.timezone.utc).isoformat(),'calcolo':result,'stato':'preventivo_dimostrativo'}
                con.execute('INSERT INTO preventivi VALUES (?,?)',(request_id,json.dumps(saved,ensure_ascii=False)))
            self.respond(200,saved)
        except (ValueError,TypeError,InvalidOperation) as e:self.respond(400,{'errore':str(e)})
        except sqlite3.IntegrityError:self.respond(409,{'errore':'Richiesta già salvata. Ricarica l’archivio.'})
        except sqlite3.Error:self.respond(500,{'errore':'Archivio non disponibile. Salvataggio non confermato.'})

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8767);parser.add_argument('--no-browser',action='store_true');args=parser.parse_args()
    with connection():pass
    try:server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    except OSError:print('Porta occupata: se l’app è già avviata apri http://127.0.0.1:8767.');raise SystemExit(1)
    url=f'http://127.0.0.1:{server.server_port}';print(f'Tipografia locale: {url}\nPrezzi dimostrativi, da validare.\nDati: {DATA}',flush=True)
    if not args.no_browser:webbrowser.open(url)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
