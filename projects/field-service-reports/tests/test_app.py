import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
spec=importlib.util.spec_from_file_location('interventi',Path(__file__).resolve().parents[1]/'src'/'server.py')
app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)

class AppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();app.DATA=Path(cls.temp.name)
        cls.server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.url=f'http://127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.temp.cleanup()
    def call(self,path,body=None,headers=None):
        data=json.dumps(body).encode() if body is not None else None
        req=Request(self.url+path,data=data,headers=headers or ({'Content-Type':'application/json'} if data else {}))
        try:
            with urlopen(req) as r:return r.status,json.load(r)
        except HTTPError as e:return e.code,json.load(e)
    def example(self):
        return dict(cliente='Cliente Demo',tecnico='Tecnico Demo',data='2026-11-13',prossima_data='2026-12-01',sede='Sede Demo',impianto='Impianto Demo',attivita='Controllo dimostrativo',note='',stato='completato',controlli=[dict(nome=n,esito='ok',nota='') for n in app.CHECKS])
    def test_01_save_reload_update_and_persistence(self):
        code,result=self.call('/api/interventi',self.example());self.assertEqual(code,200)
        record=result['intervento'];record['note']='Aggiornamento verificato'
        self.assertEqual(self.call('/api/interventi',record)[0],200)
        with app.connection() as con:
            self.assertEqual(json.loads(con.execute('SELECT payload FROM interventi WHERE id=?',(record['id'],)).fetchone()[0])['note'],'Aggiornamento verificato')
        self.assertEqual(len([r for r in self.call('/api/interventi')[1]['interventi'] if r['id']==record['id']]),1)
    def test_02_incomplete_completion_rejected_draft_allowed(self):
        r=self.example();r['controlli'][0]['esito']=''
        self.assertEqual(self.call('/api/interventi',r)[0],400)
        r['stato']='bozza';self.assertEqual(self.call('/api/interventi',r)[0],200)
    def test_03_anomaly_requires_note(self):
        r=self.example();r['controlli'][0]['esito']='anomalia'
        self.assertEqual(self.call('/api/interventi',r)[0],400)
        r['controlli'][0]['nota']='Rumore anomalo da verificare'
        self.assertEqual(self.call('/api/interventi',r)[0],200)
    def test_04_required_fields_and_dates(self):
        for key,value in [('cliente',''),('data','2026-99-99'),('prossima_data','2025-01-01'),('stato','inventato')]:
            with self.subTest(key=key):
                r=self.example();r[key]=value;self.assertEqual(self.call('/api/interventi',r)[0],400)
    def test_05_backup_import_deduplicates_and_restores(self):
        _,saved=self.call('/api/interventi',self.example());record=saved['intervento']
        backup={'formato':'interventi-locali-v1','interventi':[record]}
        self.assertEqual(self.call('/api/importa',backup)[1]['gia_presenti'],1)
        with app.connection() as con:con.execute('DELETE FROM interventi WHERE id=?',(record['id'],))
        self.assertEqual(self.call('/api/importa',backup)[1]['aggiunti'],1)
        self.assertIn(record['id'],[r['id'] for r in self.call('/api/interventi')[1]['interventi']])
    def test_06_bad_import_is_atomic(self):
        import uuid
        before=len(self.call('/api/interventi')[1]['interventi'])
        good=self.example();good['id']=str(uuid.uuid4());bad=self.example();bad['id']='invalid'
        self.assertEqual(self.call('/api/importa',{'formato':'interventi-locali-v1','interventi':[good,bad]})[0],400)
        self.assertEqual(len(self.call('/api/interventi')[1]['interventi']),before)
    def test_07_external_origin_and_host_blocked(self):
        self.assertEqual(self.call('/api/interventi',self.example(),{'Content-Type':'application/json','Origin':'https://example.com'})[0],403)
        self.assertEqual(self.call('/api/status',headers={'Host':'example.com'})[0],403)
        self.assertEqual(self.call('/api/interventi',self.example(),{'Content-Type':'text/plain'})[0],415)
    def test_08_static_and_unknown_files(self):
        with urlopen(self.url+'/') as r:self.assertIn('Ogni intervento',r.read().decode())
        self.assertEqual(self.call('/data/interventi.sqlite3')[0],404)
    def test_09_pdf_download_and_missing_record(self):
        from io import BytesIO
        from pypdf import PdfReader
        record=self.call('/api/interventi',self.example())[1]['intervento']
        with urlopen(self.url+'/api/rapporti/'+record['id']+'.pdf') as response:
            self.assertEqual(response.headers['Content-Type'],'application/pdf')
            text='\n'.join(page.extract_text() for page in PdfReader(BytesIO(response.read())).pages)
            self.assertIn('Cliente Demo',text);self.assertIn('COMPLETATO',text)
        self.assertEqual(self.call('/api/rapporti/00000000-0000-0000-0000-000000000000.pdf')[0],404)
    def test_10_pdf_long_text_and_escaped_markup(self):
        from rapporti import create_pdf
        from pypdf import PdfReader
        from io import BytesIO
        import uuid
        record=self.example();record['id']=str(uuid.uuid4());record['cliente']='Società <Demo> & Caffè'
        record['attivita']='Controllo completato. '*190
        record['note']='Raccomandazione verificata. '*140
        for c in record['controlli']:c['nota']='Dettaglio rilevato durante il controllo. '*35
        pdf=PdfReader(BytesIO(create_pdf(record)))
        text='\n'.join(page.extract_text() for page in pdf.pages)
        self.assertIn('Società <Demo> & Caffè',text);self.assertIn('non costituisce certificazione',text)
        self.assertGreater(len(pdf.pages),1)
    def test_11_stop_preserves_database(self):
        self.assertEqual(self.call('/api/ferma',{})[0],200)
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())
        self.assertTrue((app.DATA/'interventi.sqlite3').is_file())

if __name__=='__main__':unittest.main(verbosity=2)
