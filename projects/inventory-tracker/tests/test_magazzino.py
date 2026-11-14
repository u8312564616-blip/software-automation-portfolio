import importlib.util,json,tempfile,threading,unittest,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import datetime as dt

spec=importlib.util.spec_from_file_location('magazzino',Path(__file__).resolve().parents[1]/'src/server.py');app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)

class MagazzinoTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();app.DATA=Path(self.temp.name)
        self.server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def call(self,path,body=None,headers=None):
        req=Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,headers=headers or ({'Content-Type':'application/json'} if body is not None else {}))
        try:
            with urlopen(req,timeout=10) as r:return r.status,json.load(r)
        except HTTPError as e:return e.code,json.load(e)
    def item(self,code='TEST-01',threshold=5):return self.call('/api/articoli',{'sku':code,'nome':'Articolo Demo','categoria':'Prova','soglia':threshold})
    def move(self,kind='carico',qty=10,**overrides):
        data={'id':str(uuid.uuid4()),'sku':'TEST-01','tipo':kind,'quantita':qty,'data':dt.date.today().isoformat(),'nota':'Movimento di prova'};data.update(overrides)
        return data
    def state(self):return self.call('/api/stato')[1]
    def test_create_normalize_and_duplicates(self):
        self.assertEqual(self.item('test-01')[1]['sku'],'TEST-01')
        self.assertEqual(self.item()[0],409)
        self.assertEqual(self.state()['articoli'][0]['giacenza'],0)
    def test_ledger_balance_and_reload(self):
        self.item();self.assertEqual(self.call('/api/movimenti',self.move())[0],200)
        self.assertEqual(self.call('/api/movimenti',self.move('scarico',4))[1]['giacenza'],6)
        state=self.state();self.assertEqual((state['articoli'][0]['giacenza'],state['totale_movimenti']),(6,2))
        with app.connection() as con:self.assertEqual(app.balance(con,'TEST-01'),6)
    def test_insufficient_stock_is_atomic(self):
        self.item();self.call('/api/movimenti',self.move(qty=3))
        self.assertEqual(self.call('/api/movimenti',self.move('scarico',4))[0],409)
        self.assertEqual((self.state()['articoli'][0]['giacenza'],self.state()['totale_movimenti']),(3,1))
    def test_repeated_request_is_idempotent(self):
        self.item();data=self.move();self.call('/api/movimenti',data)
        self.assertFalse(self.call('/api/movimenti',data)[1]['nuovo'])
        data['quantita']=5;self.assertEqual(self.call('/api/movimenti',data)[0],409)
        self.assertEqual(self.state()['totale_movimenti'],1)
    def test_concurrent_withdrawals_never_go_negative(self):
        self.item();self.call('/api/movimenti',self.move(qty=10))
        requests=[self.move('scarico',8),self.move('scarico',8)]
        with ThreadPoolExecutor(max_workers=2) as pool:codes=list(pool.map(lambda data:self.call('/api/movimenti',data)[0],requests))
        self.assertEqual(sorted(codes),[200,409]);self.assertEqual(self.state()['articoli'][0]['giacenza'],2)
    def test_validation_quantities_dates_and_cause(self):
        self.item()
        bad=[('quantita',0),('quantita',-1),('quantita',True),('quantita',1.5),('data','2026-99-99'),('data',(dt.date.today()+dt.timedelta(days=1)).isoformat()),('nota',''),('tipo','altro'),('sku','INVALID CODE')]
        for key,value in bad:
            with self.subTest(key=key,value=value):
                data=self.move();data[key]=value;self.assertEqual(self.call('/api/movimenti',data)[0],400)
        self.assertEqual(self.state()['totale_movimenti'],0)
    def test_low_stock_boundary_and_threshold_version(self):
        self.item(threshold=5);self.call('/api/movimenti',self.move(qty=5));self.assertTrue(self.state()['articoli'][0]['scorta_bassa'])
        self.assertEqual(self.call('/api/soglia',{'sku':'TEST-01','soglia':4,'versione':1})[0],200)
        self.assertFalse(self.state()['articoli'][0]['scorta_bassa'])
        self.assertEqual(self.call('/api/soglia',{'sku':'TEST-01','soglia':9,'versione':1})[0],409)
    def test_inactivity_and_outflows_are_not_sales(self):
        self.item();old=(dt.date.today()-dt.timedelta(days=120)).isoformat();recent=(dt.date.today()-dt.timedelta(days=5)).isoformat()
        self.call('/api/movimenti',self.move(qty=30,data=old))
        self.assertEqual(self.state()['articoli'][0]['giorni_senza_scarico'],120)
        self.call('/api/movimenti',self.move('scarico',4,data=recent))
        a=self.state()['articoli'][0];self.assertEqual((a['giorni_senza_scarico'],a['scarichi_30gg']),(5,4))
    def test_backup_restore_and_nonempty_refusal(self):
        self.item();self.call('/api/movimenti',self.move());self.call('/api/movimenti',self.move('scarico',3))
        backup=self.call('/api/backup')[1]
        self.assertEqual(self.call('/api/importa',backup)[0],409)
        # Solo nell'archivio temporaneo del test, per simulare un nuovo PC.
        with app.connection() as con:con.execute('DELETE FROM movimenti');con.execute('DELETE FROM articoli')
        self.assertEqual(self.call('/api/importa',backup)[1],{'articoli':1,'movimenti':2})
        self.assertEqual(self.state()['articoli'][0]['giacenza'],7)
    def test_invalid_backup_rolls_back_everything(self):
        self.item();self.call('/api/movimenti',self.move())
        backup=self.call('/api/backup')[1];backup['movimenti'].append(self.move('scarico',99))
        with app.connection() as con:con.execute('DELETE FROM movimenti');con.execute('DELETE FROM articoli')
        self.assertEqual(self.call('/api/importa',backup)[0],409)
        self.assertEqual((len(self.state()['articoli']),self.state()['totale_movimenti']),(0,0))
        backup['movimenti']=[];backup['articoli'].append({'sku':'BAD'})
        self.assertEqual(self.call('/api/importa',backup)[0],400);self.assertFalse(self.state()['articoli'])
    def test_demo_only_empty_and_expected_analysis(self):
        self.assertEqual(self.call('/api/demo',{})[0],200);state=self.state()
        self.assertEqual((len(state['articoli']),state['totale_movimenti']),(3,5))
        self.assertEqual([a['sku'] for a in state['articoli'] if a['scorta_bassa']],['DEMO-FILTRO'])
        self.assertEqual([a['sku'] for a in state['articoli'] if a['giorni_senza_scarico']>=90],['DEMO-GUANTO'])
        self.assertEqual(self.call('/api/demo',{})[0],409)
    def test_origin_json_and_files_protected(self):
        self.assertEqual(self.call('/api/demo',{},headers={'Content-Type':'application/json','Origin':'https://example.com'})[0],403)
        self.assertEqual(self.call('/api/status',headers={'Host':'example.com'})[0],403)
        self.assertEqual(self.call('/api/demo',{},headers={'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.call('/data/magazzino.sqlite3')[0],404)
    def test_shutdown_keeps_data(self):
        self.item();self.assertEqual(self.call('/api/ferma',{})[0],200)
        self.thread.join(timeout=2);self.assertFalse(self.thread.is_alive());self.assertTrue((app.DATA/'magazzino.sqlite3').is_file())

if __name__=='__main__':unittest.main(verbosity=2)
