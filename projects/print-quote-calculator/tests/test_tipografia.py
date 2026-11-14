import importlib.util,json,tempfile,threading,unittest,uuid
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError

spec=importlib.util.spec_from_file_location('tipografia',Path(__file__).resolve().parents[1]/'src/server.py');app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)

class TipografiaTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();app.DATA=Path(self.temp.name)
        self.server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def call(self,path,body=None,headers=None):
        request=Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,headers=headers or ({'Content-Type':'application/json'} if body is not None else {}))
        try:
            with urlopen(request) as r:return r.status,json.load(r)
        except HTTPError as e:return e.code,json.load(e)
    def example(self):return {'cliente':'Cliente Demo','quantita':100,'formato':'A4','carta':'standard','stampa':'bianco_nero','finitura':'nessuna','consegna':False,'sconto_percentuale':'0'}
    def test_basic_price_and_minimum(self):
        r=self.call('/api/calcola',self.example())[1]
        self.assertEqual((r['imponibile'],r['iva'],r['totale']),('26.50','5.83','32.33'))
        p=self.example();p['quantita']=1
        self.assertEqual(self.call('/api/calcola',p)[1]['totale'],'18.30')
    def test_discount_shipping_and_rounding(self):
        p=self.example();p.update(carta='premium',stampa='colore',finitura='plastificazione',consegna=True,sconto_percentuale='10')
        r=self.call('/api/calcola',p)[1]
        self.assertEqual((r['lavorazione'],r['sconto'],r['imponibile'],r['iva'],r['totale']),('65.50','6.55','67.45','14.84','82.29'))
        p['sconto_percentuale']='100';self.assertEqual(self.call('/api/calcola',p)[1]['totale'],'10.37')
    def test_invalid_values(self):
        for key,value in [('quantita',0),('quantita',1.5),('quantita',True),('formato','A3'),('consegna','true'),('sconto_percentuale','NaN'),('sconto_percentuale','100.01'),('sconto_percentuale','1.111')]:
            with self.subTest(key=key,value=value):
                p=self.example();p[key]=value;self.assertEqual(self.call('/api/calcola',p)[0],400)
    def test_idempotent_save_and_immutable_history(self):
        p=self.example();p.update(richiesta_id=str(uuid.uuid4()),versione_listino=1)
        code,original=self.call('/api/preventivi',p);self.assertEqual(code,200)
        self.assertEqual(self.call('/api/preventivi',p)[1]['id'],original['id'])
        config=self.call('/api/listino')[1];config['avviamento']='30.00'
        self.assertEqual(self.call('/api/listino',config)[1]['versione'],2)
        archived=self.call('/api/preventivi')[1]['preventivi'];self.assertEqual(len(archived),1)
        self.assertEqual(archived[0]['calcolo']['totale'],'32.33');self.assertEqual(archived[0]['calcolo']['listino_snapshot']['avviamento'],'12.50')
        self.assertEqual(self.call('/api/preventivi',p)[1]['calcolo']['totale'],'32.33')
    def test_listino_changes_require_recalculation(self):
        config=self.call('/api/listino')[1];self.assertEqual(self.call('/api/listino',config)[0],200)
        p=self.example();p.update(richiesta_id=str(uuid.uuid4()),versione_listino=1)
        self.assertEqual(self.call('/api/preventivi',p)[0],409)
        self.assertEqual(self.call('/api/listino',config)[0],409)
    def test_configuration_validation_and_persistence(self):
        config=self.call('/api/listino')[1];config['stampa']['colore']='-0.01'
        self.assertEqual(self.call('/api/listino',config)[0],400)
        self.assertEqual(self.call('/api/listino')[1]['versione'],1)
        config['stampa']['colore']='0.22';self.assertEqual(self.call('/api/listino',config)[0],200)
        with app.connection() as con:self.assertEqual(json.loads(con.execute('SELECT payload FROM listino').fetchone()[0])['stampa']['colore'],'0.22')
    def test_save_requires_client_and_valid_request(self):
        p=self.example();p.update(cliente='',richiesta_id=str(uuid.uuid4()),versione_listino=1)
        self.assertEqual(self.call('/api/preventivi',p)[0],400)
        p.update(cliente='Demo',richiesta_id='bad');self.assertEqual(self.call('/api/preventivi',p)[0],400)
    def test_origin_and_files_are_protected(self):
        self.assertEqual(self.call('/api/calcola',self.example(),{'Content-Type':'application/json','Origin':'https://example.com'})[0],403)
        self.assertEqual(self.call('/data/tipografia.sqlite3')[0],404)
        self.assertEqual(self.call('/api/calcola',self.example(),{'Content-Type':'text/plain'})[0],415)
    def test_shutdown_preserves_database(self):
        self.call('/api/calcola',self.example())
        self.assertEqual(self.call('/api/ferma',{})[0],200)
        self.thread.join(timeout=2);self.assertFalse(self.thread.is_alive())
        self.assertTrue((app.DATA/'tipografia.sqlite3').is_file())

if __name__=='__main__':unittest.main(verbosity=2)
