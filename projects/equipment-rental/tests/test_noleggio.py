import sys,tempfile,unittest,uuid,sqlite3,threading,json,urllib.request,urllib.error
from pathlib import Path
from unittest.mock import patch
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import server as app

class RentalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.old=app.DATA;app.DATA=Path(self.temp.name)
        self.clock=patch.object(app,'today',return_value='2026-11-14');self.clock.start()
        self.asset(3)
    def tearDown(self):self.clock.stop();app.DATA=self.old;self.temp.cleanup()
    def tx(self,fn,b):
        with app.connection() as c:c.execute('BEGIN IMMEDIATE');return fn(c,b)
    def asset(self,n):return self.tx(app.create_asset,dict(sku='A',nome='Demo',quantita=n))
    def body(self,n=1,start='2026-11-14',end='2026-11-16'):
        return dict(id=str(uuid.uuid4()),sku='A',cliente='Cliente demo',inizio=start,fine=end,quantita=n)
    def book(self,**kw):
        b=self.body(**kw);self.tx(app.create_booking,b);return b
    def act(self,b,kind,version=1,note='prova'):
        return dict(id=b['id'],richiesta_id=str(uuid.uuid4()),azione=kind,versione=version,motivo=note)
    def available(self,start='2026-11-14',end='2026-11-16'):
        with app.connection() as c:return app.availability(c,'A',start,end)['disponibili']
    def test_asset_unique(self):
        with self.assertRaises(app.Conflict):self.asset(2)
    def test_strict_quantities(self):
        for n in [0,-1,True,1.5,'2',10001]:
            with self.subTest(n=n),self.assertRaises(ValueError):self.tx(app.create_booking,self.body(n))
    def test_overlap_rejected_atomic(self):
        self.book(n=3)
        with self.assertRaises(app.Conflict):self.book()
        with app.connection() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM prenotazioni').fetchone()[0],1)
    def test_adjacent_periods(self):
        self.book(n=3);self.book(n=3,start='2026-11-16',end='2026-11-18')
        self.assertEqual(self.available(),0)
    def test_peak_not_sum(self):
        self.book(n=2);self.book(n=2,start='2026-11-16',end='2026-11-18')
        self.book(n=1,end='2026-11-18');self.assertEqual(self.available(end='2026-11-18'),0)
    def test_idempotent_booking(self):
        b=self.book();self.assertTrue(self.tx(app.create_booking,b)['gia_presente'])
        b['quantita']=2
        with self.assertRaises(app.Conflict):self.tx(app.create_booking,b)
    def test_concurrent_capacity(self):
        def save(_):
            try:self.book(n=2);return 'ok'
            except app.Conflict:return 'conflict'
        with ThreadPoolExecutor(2) as pool:self.assertCountEqual(pool.map(save,range(2)),['ok','conflict'])
    def test_pickup_return_persistence(self):
        b=self.book(n=2);self.tx(app.action,self.act(b,'ritiro'));self.tx(app.action,self.act(b,'reso',2))
        with app.connection() as c:
            s=app.snapshot(c);self.assertEqual(s['beni'][0]['presenti'],3);self.assertEqual(s['prenotazioni'][0]['stato'],'restituita');self.assertEqual(len(s['eventi']),3)
    def test_invalid_transitions(self):
        b=self.book()
        with self.assertRaises(app.Conflict):self.tx(app.action,self.act(b,'reso'))
        self.tx(app.action,self.act(b,'ritiro'))
        with self.assertRaises(app.Conflict):self.tx(app.action,self.act(b,'annulla',2))
    def test_version_and_idempotent_action(self):
        b=self.book();a=self.act(b,'ritiro');self.tx(app.action,a);self.tx(app.action,a)
        with self.assertRaises(app.Conflict):self.tx(app.action,self.act(b,'reso',1))
        a['motivo']='cambiato'
        with self.assertRaises(app.Conflict):self.tx(app.action,a)
    def test_cancel_restore(self):
        b=self.book(n=3)
        with self.assertRaises(ValueError):self.tx(app.action,self.act(b,'annulla',note=''))
        self.tx(app.action,self.act(b,'annulla'));self.assertEqual(self.available(),3)
        self.tx(app.action,self.act(b,'ripristina',2));self.assertEqual(self.available(),0)
    def test_restore_rechecks_capacity(self):
        b=self.book(n=3);self.tx(app.action,self.act(b,'annulla'));self.book(n=3)
        with self.assertRaises(app.Conflict):self.tx(app.action,self.act(b,'ripristina',2))
    def test_future_pickup(self):
        b=self.book(start='2026-11-16',end='2026-11-18')
        with self.assertRaises(app.Conflict):self.tx(app.action,self.act(b,'ritiro'))
    def test_dates(self):
        for start,end in [('2026-11-13','2026-11-16'),('2026-11-14','2026-11-14'),('2026-02-30','2026-11-16'),('bad','bad')]:
            with self.subTest(start=start),self.assertRaises(ValueError):self.book(start=start,end=end)
    def test_overdue_blocks_until_return(self):
        b=self.book(n=3,end='2026-11-15');future=self.book(n=3,start='2026-11-16',end='2026-11-18');self.tx(app.action,self.act(b,'ritiro'))
        with patch.object(app,'today',return_value='2026-11-16'):
            self.assertEqual(self.available('2026-11-19','2026-11-20'),0)
            with self.assertRaises(app.Conflict):self.tx(app.action,self.act(future,'ritiro'))
            self.tx(app.action,self.act(b,'reso',2));self.assertEqual(self.available('2026-11-19','2026-11-20'),3)
            self.tx(app.action,self.act(future,'ritiro'))
    def test_http_export_and_security(self):
        self.book();http=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler);thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(http.server_port)
        try:
            with urllib.request.urlopen(base+'/api/registro') as r:
                data=json.load(r);self.assertEqual(len(data['prenotazioni']),1);self.assertIn('attachment',r.headers['Content-Disposition'])
            for path,headers,code in [('/data/noleggio.sqlite3',{},404),('/api/stato',{'Origin':'https://example.com'},403),('/api/stato',{'Host':'evil.test'},403)]:
                with self.subTest(path=path),self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(urllib.request.Request(base+path,headers=headers))
                self.assertEqual(error.exception.code,code)
        finally:http.shutdown();thread.join();http.server_close()
    def test_verified_backup_restore(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]));import backup
        self.book();target,manifest=backup.create_backup(app.DATA/'noleggio.sqlite3',app.DATA/'copies')
        self.assertEqual(manifest['conteggi']['prenotazioni'],1)
        with closing(sqlite3.connect(target)) as c:self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok');self.assertEqual(c.execute('SELECT COUNT(*) FROM eventi').fetchone()[0],1)

if __name__=='__main__':unittest.main(verbosity=2)
