"""Pulizia vendite e cruscotto locali. CSV standard; XLSX con openpyxl già disponibile."""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from contextlib import contextmanager
from pathlib import Path
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit, parse_qs
from io import StringIO, BytesIO
from collections import Counter, defaultdict
import base64, csv, datetime as dt, hashlib, json, os, re, sqlite3, threading, unicodedata, uuid, webbrowser, zipfile

ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('VENDITE_DATA',str(ROOT.parent/'data')))
FIELDS=['id_riga','data','prodotto','categoria','quantita','importo']
MAX_FILE=2_000_000
ALIASES={'idriga':'id_riga','id':'id_riga','recordid':'id_riga','data':'data','date':'data','prodotto':'prodotto','product':'prodotto','categoria':'categoria','category':'categoria','quantita':'quantita','quantity':'quantita','qty':'quantita','importo':'importo','amount':'importo','valuta':'valuta','currency':'valuta'}

class Conflict(ValueError):pass

@contextmanager
def connection():
    DATA.mkdir(parents=True,exist_ok=True);con=sqlite3.connect(DATA/'vendite.sqlite3',timeout=15);con.row_factory=sqlite3.Row
    try:
        con.execute('CREATE TABLE IF NOT EXISTS importazioni(id TEXT PRIMARY KEY, identita TEXT UNIQUE NOT NULL, nome TEXT NOT NULL, formato TEXT NOT NULL, originale BLOB NOT NULL, rapporto TEXT NOT NULL, confermato INTEGER NOT NULL DEFAULT 0, creato TEXT NOT NULL)')
        yield con;con.commit()
    except Exception:con.rollback();raise
    finally:con.close()

def dump(data):return json.dumps(data,ensure_ascii=False,separators=(',',':'))
def currency(cents):return format(Decimal(cents)/100,'.2f')
def header(value):return re.sub(r'[^a-z0-9]','',unicodedata.normalize('NFKD',str(value or '').strip().lower()).encode('ascii','ignore').decode())
def clean_text(value):
    if value is None:return ''
    if isinstance(value,bool):return str(value)
    if isinstance(value,dt.datetime):return value.isoformat()
    if isinstance(value,dt.date):return value.isoformat()
    return str(value)

def parse_date(value,mode):
    if isinstance(value,dt.datetime):return value.date().isoformat()
    if isinstance(value,dt.date):return value.isoformat()
    text=str(value or '').strip()
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}',text):fmt='%Y-%m-%d'
    elif re.fullmatch(r'\d{2}/\d{2}/\d{4}',text):fmt='%d/%m/%Y' if mode=='it' else '%m/%d/%Y'
    else:raise ValueError('Data non valida: usa ISO o il formato selezionato.')
    try:return dt.datetime.strptime(text,fmt).date().isoformat()
    except ValueError:raise ValueError('Data inesistente o non coerente con il formato selezionato.')

def amount(value,mode):
    if isinstance(value,bool) or value is None:raise ValueError('Importo mancante o non numerico.')
    if isinstance(value,(int,float,Decimal)):
        text=str(value)
    else:
        text=str(value).strip()
        # La valuta è unica per tutto il file. Nessuna conversione automatica.
        pattern=r'-?(?:\d+|\d{1,3}(?:\.\d{3})+)(?:,\d{1,2})?' if mode=='it' else r'-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?'
        if not re.fullmatch(pattern,text):raise ValueError('Importo non valido per i separatori selezionati (massimo due decimali).')
        text=text.replace('.','').replace(',','.') if mode=='it' else text.replace(',','')
    try:result=Decimal(text)
    except InvalidOperation:raise ValueError('Importo non numerico.')
    if not result.is_finite() or abs(result)>Decimal('1000000000') or result.as_tuple().exponent < -2:raise ValueError('Importo fuori intervallo o con più di due decimali.')
    return int(result*100)

def quantity(value):
    if isinstance(value,bool) or value is None:raise ValueError('Quantità mancante o non valida.')
    try:result=Decimal(str(value).strip())
    except InvalidOperation:raise ValueError('Quantità non numerica.')
    if not result.is_finite() or result!=result.to_integral_value() or abs(result)>1000000 or result==0:raise ValueError('Quantità: intero non nullo tra -1000000 e 1000000.')
    return int(result)

def load_rows(raw,kind):
    if kind=='csv':
        try:text=raw.decode('utf-8-sig')
        except UnicodeDecodeError:raise ValueError('CSV non UTF-8: esportalo come CSV UTF-8 oppure usa XLSX.')
        first=text.splitlines()[0] if text.splitlines() else ''
        delimiter=';' if first.count(';')>=first.count(',') else ','
        try:rows=list(csv.reader(StringIO(text),delimiter=delimiter,strict=True))
        except csv.Error:raise ValueError('CSV non valido: controlla virgolette e separatori.')
        return rows,{'foglio':None,'fogli_totali':None,'separatore':delimiter},set()
    if kind!='xlsx':raise ValueError('Usa un file CSV UTF-8 o XLSX. XLS e XLSM non sono supportati.')
    try:
        with zipfile.ZipFile(BytesIO(raw)) as z:
            if len(z.infolist())>500 or sum(i.file_size for i in z.infolist())>50_000_000:raise ValueError('File XLSX troppo complesso o superiore a 50 MB decompressi.')
    except zipfile.BadZipFile:raise ValueError('File XLSX non riconosciuto.')
    try:from openpyxl import load_workbook
    except ImportError:raise ValueError('Per leggere XLSX avvia tramite Avvia-vendite.cmd; in alternativa usa CSV UTF-8.')
    try:
        workbook=load_workbook(BytesIO(raw),read_only=True,data_only=False,keep_links=False)
        try:
            sheet=workbook.worksheets[0]
            if (sheet.max_column or 0)>50 or (sheet.max_row or 0)>10001:raise ValueError('Limite: 50 colonne e 10000 righe dati nel primo foglio.')
            rows=[];formulas=set()
            for line,cells in enumerate(sheet.iter_rows(),1):
                if line>10001 or len(cells)>50:raise ValueError('Limite: 50 colonne e 10000 righe dati nel primo foglio.')
                values=[]
                for col,cell in enumerate(cells):
                    values.append(cell.value)
                    if cell.data_type=='f':formulas.add((line,col))
                rows.append(values)
            return rows,{'foglio':sheet.title,'fogli_totali':len(workbook.worksheets),'separatore':None},formulas
        finally:workbook.close()
    except ValueError:raise
    except Exception:raise ValueError('Impossibile leggere il file XLSX. Verifica che non sia danneggiato o protetto.')

def analyze(raw,kind,settings):
    if settings not in [{'date':'it','numeri':'it'},{'date':'it','numeri':'en'},{'date':'us','numeri':'it'},{'date':'us','numeri':'en'}]:raise ValueError('Impostazioni date o numeri non valide.')
    rows,metadata,formulas=load_rows(raw,kind)
    if not rows:raise ValueError('File vuoto.')
    if len(rows)>10001 or len(rows[0])>50:raise ValueError('Limite: 10000 righe dati e 50 colonne.')
    mapped={};ignored=[]
    for i,name in enumerate(rows[0]):
        key=ALIASES.get(header(name))
        if key:
            if key in mapped:raise ValueError('Intestazione duplicata o ambigua: '+key)
            mapped[key]=i
        else:ignored.append(clean_text(name))
    missing=[k for k in FIELDS if k not in mapped]
    if missing:raise ValueError('Intestazioni mancanti: '+', '.join(missing)+'. Devono essere nella prima riga.')
    results=[]
    for line,values in enumerate(rows[1:],2):
        if not any(v is not None and str(v).strip() for v in values):continue
        original={k:clean_text(values[i]) if i<len(values) else '' for k,i in mapped.items()}
        record={'riga':line,'originale':original,'stato':'valida','motivi':[],'modifiche':[],'pulita':None}
        try:
            if len(values)!=len(rows[0]):raise ValueError('Numero colonne diverso dall’intestazione.')
            if any((line,col) in formulas for col in mapped.values()):raise ValueError('Formula Excel presente: esporta prima i valori, senza formule.')
            if 'valuta' in mapped and original['valuta'].strip().upper() not in ['EUR','€']:raise ValueError('Valuta diversa da EUR o mancante. Non vengono sommate valute diverse.')
            normalized={k:original[k].strip() for k in ['id_riga','prodotto','categoria']}
            for key in ['id_riga','prodotto']:
                if not normalized[key]:raise ValueError('Campo obbligatorio mancante: '+key)
            if any(len(normalized[k])>200 for k in normalized):raise ValueError('ID, prodotto o categoria oltre 200 caratteri.')
            normalized['data']=parse_date(values[mapped['data']],settings['date'])
            normalized['quantita']=quantity(values[mapped['quantita']])
            cents=amount(values[mapped['importo']],settings['numeri']);normalized['importo']=currency(cents)
            if normalized['quantita']<0 and cents>0 or normalized['quantita']>0 and cents<0:raise ValueError('Segni di quantità e importo incoerenti. Per un reso usa entrambi negativi.')
            record['pulita']=normalized
            for key in FIELDS:
                if original[key]!=str(normalized[key]):record['modifiche'].append(key+': '+repr(original[key])+' → '+repr(normalized[key]))
        except (ValueError,TypeError,IndexError) as e:
            record['stato']='errore';record['motivi']=[str(e)]
        results.append(record)
    grouped=defaultdict(list)
    for r in results:
        identifier=r['originale']['id_riga'].strip()
        if identifier:grouped[identifier].append(r)
    for identifier,group in grouped.items():
        if len(group)<2:continue
        valid=[r for r in group if r['stato']=='valida']
        if len(valid)==len(group) and len({dump(r['pulita']) for r in valid})==1:
            for r in valid[1:]:r['stato']='duplicato';r['motivi']=[f'Duplicato identico nelle sei colonne dopo normalizzazione; mantenuta la riga {valid[0]["riga"]}.']
        else:
            for r in group:r['stato']='conflitto';r['motivi'].append('Stesso ID riga con dati diversi o non validi: tutte le occorrenze sono escluse.')
    counts={key:sum(r['stato']==key for r in results) for key in ['valida','duplicato','errore','conflitto']}
    clean=[r['pulita'] for r in results if r['stato']=='valida']
    return {'righe':results,'conteggi':counts,'righe_totali':len(results),'righe_con_modifiche':sum(bool(r['modifiche']) for r in results),'totale_valido':currency(sum(int(Decimal(r['importo'])*100) for r in clean)),'impostazioni':settings,'metadati':metadata,'colonne_ignorate':ignored}

def dashboard(report,month='',category=''):
    if month and not re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])',month):raise ValueError('Filtro mese non valido.')
    all_rows=[r['pulita'] for r in report['righe'] if r['stato']=='valida']
    rows=[r for r in all_rows if (not month or r['data'][:7]==month) and (not category or (r['categoria'] or '(Senza categoria)')==category)]
    total=sum(int(Decimal(r['importo'])*100) for r in rows)
    def group_by(key):
        groups={}
        for r in rows:
            name=r['data'][:7] if key=='mese' else (r[key] or '(Senza categoria)')
            g=groups.setdefault(name,{'nome':name,'centesimi':0,'quantita':0,'righe':0})
            g['centesimi']+=int(Decimal(r['importo'])*100);g['quantita']+=r['quantita'];g['righe']+=1
        result=[]
        for g in groups.values():g['totale']=currency(g.pop('centesimi'));result.append(g)
        return sorted(result,key=lambda g:g['nome'])
    months,products,categories=(group_by(k) for k in ['mese','prodotto','categoria'])
    reconcile=all(sum(int(Decimal(g['totale'])*100) for g in groups)==total for groups in [months,products,categories])
    return {'totale':currency(total),'righe':len(rows),'quantita_netta':sum(r['quantita'] for r in rows),'mesi':months,'prodotti':products,'categorie':categories,'riconciliato':reconcile,'mesi_disponibili':sorted({r['data'][:7] for r in all_rows}),'categorie_disponibili':sorted({r['categoria'] or '(Senza categoria)' for r in all_rows})}

def export_csv(report):
    output=StringIO(newline='');writer=csv.writer(output,delimiter=';',lineterminator='\r\n');writer.writerow(FIELDS)
    for row in report['righe']:
        if row['stato']!='valida':continue
        r=row['pulita'];values=[]
        for key in FIELDS:
            value=str(r[key])
            if key in ['id_riga','prodotto','categoria'] and (value.lstrip().startswith(('=','+','-','@')) or value.startswith(('\t','\r','\n'))):value="'"+value
            if key=='importo':value=value.replace('.',',')
            values.append(value)
        writer.writerow(values)
    return ('\ufeff'+output.getvalue()).encode('utf-8')

class Handler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args):print(f'{self.log_date_time_string()} {fmt%args}',flush=True)
    def respond(self,code,data):
        raw=dump(data).encode();self.send_bytes(code,raw,'application/json; charset=utf-8')
    def send_bytes(self,code,raw,mime,filename=None):
        self.send_response(code);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        if filename:self.send_header('Content-Disposition',f'attachment; filename="{filename}"')
        self.end_headers();self.wfile.write(raw)
    def allowed(self):
        host=f'127.0.0.1:{self.server.server_port}'
        if self.headers.get('Host')!=host or self.headers.get('Origin','http://'+host)!='http://'+host:self.respond(403,{'errore':'Accesso consentito solo dall’app locale.'});return False
        return True
    def do_GET(self):
        if not self.allowed():return
        try:
            path=urlsplit(self.path)
            if path.path=='/api/status':self.respond(200,{'app':'vendite-locali','versione':1});return
            if path.path=='/api/importazioni':
                with connection() as con:items=[{k:r[k] for k in ['id','nome','formato','confermato','creato']} for r in con.execute('SELECT * FROM importazioni ORDER BY rowid DESC')]
                self.respond(200,{'importazioni':items});return
            match=re.fullmatch(r'/api/(anteprima|dashboard|pulito|rapporto|originale)/([0-9a-f-]{36})',path.path)
            if match:
                action,identifier=match.groups()
                with connection() as con:r=con.execute('SELECT * FROM importazioni WHERE id=?',(identifier,)).fetchone()
                if not r:self.respond(404,{'errore':'Importazione non trovata.'});return
                report=json.loads(r['rapporto'])
                if action=='anteprima':self.respond(200,{'id':r['id'],'nome':r['nome'],'confermato':bool(r['confermato']),'rapporto':report});return
                if action=='originale':self.send_bytes(200,r['originale'],'application/octet-stream','originale-'+identifier+'.'+r['formato']);return
                if action=='rapporto':self.send_bytes(200,dump(report).encode(),'application/json; charset=utf-8','rapporto-'+identifier+'.json');return
                if not r['confermato']:raise Conflict('Conferma prima l’importazione delle righe valide.')
                if action=='pulito':self.send_bytes(200,export_csv(report),'text/csv; charset=utf-8','vendite-pulite-'+identifier+'.csv');return
                filters=parse_qs(path.query);self.respond(200,dashboard(report,filters.get('mese',[''])[0],filters.get('categoria',[''])[0]));return
            static={'/':('index.html','text/html'),'/app.js':('app.js','text/javascript'),'/style.css':('style.css','text/css')}
            if path.path in ['/esempio.csv','/esempio.xlsx']:
                f=ROOT.parent/'data'/('vendite-esempio.'+path.path.rsplit('.',1)[1])
                if not f.is_file():self.respond(404,{'errore':'Esempio non disponibile.'});return
                self.send_bytes(200,f.read_bytes(),'application/octet-stream',f.name);return
            if path.path not in static:self.respond(404,{'errore':'Pagina non trovata.'});return
            name,mime=static[path.path];raw=(ROOT/name).read_bytes();self.send_response(200);self.send_header('Content-Type',mime+'; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.send_header('Content-Security-Policy',"default-src 'self'; object-src 'none'; frame-ancestors 'none'");self.end_headers();self.wfile.write(raw)
        except Conflict as e:self.respond(409,{'errore':str(e)})
        except ValueError as e:self.respond(400,{'errore':str(e)})
        except sqlite3.Error:self.respond(500,{'errore':'Archivio non disponibile.'})
    def do_POST(self):
        if not self.allowed():return
        try:
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':self.respond(415,{'errore':'Richiesto formato JSON.'});return
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=3_000_000:raise ValueError('Richiesta troppo grande o vuota.')
            body=json.loads(self.rfile.read(length))
            if not isinstance(body,dict):raise ValueError('Formato richiesta non valido.')
            if self.path=='/api/ferma':self.respond(200,{'stato':'App fermata. Dati conservati.'});threading.Thread(target=self.server.shutdown,daemon=True).start();return
            if self.path=='/api/esempio':
                example=ROOT.parent/'data/vendite-esempio.xlsx'
                if not example.is_file():raise ValueError('Esempio incorporato non disponibile.')
                body={'nome':'vendite-esempio.xlsx','contenuto':base64.b64encode(example.read_bytes()).decode(),'impostazioni':{'date':'it','numeri':'it'}}
            if self.path in ['/api/anteprima','/api/esempio']:
                filename=body.get('nome');encoded=body.get('contenuto');settings=body.get('impostazioni')
                if not isinstance(filename,str) or not 1<=len(filename)<=200 or not isinstance(encoded,str):raise ValueError('Nome o contenuto file non valido.')
                kind=filename.rsplit('.',1)[-1].lower()
                if kind not in ['csv','xlsx']:raise ValueError('Usa CSV UTF-8 o XLSX.')
                try:raw=base64.b64decode(encoded,validate=True)
                except (ValueError,base64.binascii.Error):raise ValueError('Contenuto file non valido.')
                if not 0<len(raw)<=MAX_FILE:raise ValueError('File vuoto o superiore a 2 MB.')
                report=analyze(raw,kind,settings)
                identity=hashlib.sha256(raw+kind.encode()+json.dumps(settings,sort_keys=True).encode()).hexdigest()
                with connection() as con:
                    con.execute('BEGIN IMMEDIATE')
                    previous=con.execute('SELECT id,nome,confermato FROM importazioni WHERE identita=?',(identity,)).fetchone()
                    if previous:identifier=previous['id'];confirmed=bool(previous['confermato']);filename=previous['nome']
                    else:
                        if con.execute('SELECT COUNT(*) FROM importazioni').fetchone()[0]>=30:raise ValueError('Limite della prima versione: 30 importazioni. Esporta i dati prima di pianificare un archivio più grande.')
                        identifier=str(uuid.uuid4());confirmed=False
                        con.execute('INSERT INTO importazioni VALUES (?,?,?,?,?,?,0,?)',(identifier,identity,filename,kind,raw,dump(report),dt.datetime.now(dt.timezone.utc).isoformat()))
                self.respond(200,{'id':identifier,'nome':filename,'confermato':confirmed,'rapporto':report});return
            if self.path=='/api/conferma':
                if body.get('conferma') is not True:raise ValueError('Conferma esplicitamente l’importazione delle sole righe valide.')
                with connection() as con:
                    con.execute('BEGIN IMMEDIATE');r=con.execute('SELECT rapporto FROM importazioni WHERE id=?',(body.get('id'),)).fetchone()
                    if not r:self.respond(404,{'errore':'Anteprima non trovata.'});return
                    if json.loads(r['rapporto'])['conteggi']['valida']==0:raise ValueError('Nessuna riga valida da importare.')
                    con.execute('UPDATE importazioni SET confermato=1 WHERE id=?',(body['id'],))
                self.respond(200,{'stato':'Importazione confermata. Sono incluse solo le righe valide.'});return
            self.respond(404,{'errore':'Operazione non trovata.'})
        except Conflict as e:self.respond(409,{'errore':str(e)})
        except (ValueError,TypeError,csv.Error) as e:self.respond(400,{'errore':str(e)})
        except sqlite3.Error:self.respond(500,{'errore':'Archivio non disponibile. Salvataggio non confermato.'})

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8769);parser.add_argument('--no-browser',action='store_true');args=parser.parse_args()
    with connection():pass
    try:server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    except OSError:print('Porta occupata. Se l’app è già avviata apri http://127.0.0.1:8769.');raise SystemExit(1)
    url=f'http://127.0.0.1:{server.server_port}';print(f'Dati vendite locali: {url}\nArchivio: {DATA}',flush=True)
    if not args.no_browser:webbrowser.open(url)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
