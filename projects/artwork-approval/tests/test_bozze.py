import base64,hashlib,importlib.util,json,tempfile,threading,unittest,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('bozze',ROOT/'src/server.py');app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)

class BozzeTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();app.DATA=Path(self.temp.name)
        self.server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.url=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def call(self,path,body=None,headers=None):
        request=Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,headers=headers or ({'Content-Type':'application/json'} if body is not None else {}))
        try:
            with urlopen(request,timeout=15) as r:return r.status,json.load(r)
        except HTTPError as e:return e.code,json.load(e)
    def new_case(self,**extra):
        body=dict(id=str(uuid.uuid4()),titolo='Etichetta DEMO',cliente='Cliente Demo',ordine='DEMO-01',scadenza='2026-01-01',istruzioni='Controllare il colore.');body.update(extra)
        code,result=self.call('/api/commesse',body);self.assertEqual(code,200)
        return result['id'],body
    def detail(self,case):return self.call('/api/commesse/'+case)[1]
    def revision(self,base=0,version=1,**extra):
        body=dict(id=str(uuid.uuid4()),versione_base=base,nome_file=f'bozza-demo-v{version}.png',contenuto=base64.b64encode((ROOT/f'data/bozza-demo-v{version}.png').read_bytes()).decode(),note='Revisione di prova',sostituisci_approvata=False);body.update(extra);return body
    def add_revision(self,case,body=None):return self.call('/api/commesse/'+case+'/revisioni',body or self.revision())
    def decision(self,detail,**extra):
        r=detail['revisioni'][0]
        body=dict(id=str(uuid.uuid4()),revisione=r['id'],sha256=r['sha256'],tipo='approvata',nome='Approvatore Demo',nota='',conferma=True);body.update(extra);return body
    def decide(self,detail,body=None):return self.call('/api/review/'+detail['token']+'/decisione',body or self.decision(detail))
    def test_case_idempotence_and_required_fields(self):
        case,body=self.new_case();self.assertEqual(self.call('/api/commesse',body)[1]['id'],case)
        body['titolo']='Diverso';self.assertEqual(self.call('/api/commesse',body)[0],409)
        body.update(id=str(uuid.uuid4()),cliente='');self.assertEqual(self.call('/api/commesse',body)[0],400)
        self.assertEqual(len(self.call('/api/commesse')[1]['commesse']),1)
    def test_original_and_preview_preserve_hash(self):
        case,_=self.new_case();self.assertEqual(self.add_revision(case)[0],200);r=self.detail(case)['revisioni'][0]
        original=(ROOT/'data/bozza-demo-v1.png').read_bytes();self.assertEqual(r['sha256'],hashlib.sha256(original).hexdigest())
        with urlopen(self.url+'/api/originale/'+r['id']) as response:self.assertEqual(response.read(),original)
        with urlopen(self.url+'/api/immagine/'+r['id']) as response:self.assertEqual(response.headers['Content-Type'],'image/jpeg');self.assertTrue(response.read().startswith(b'\xff\xd8'))
    def test_image_validation_and_extension(self):
        case,_=self.new_case()
        self.assertEqual(self.add_revision(case,self.revision(contenuto=base64.b64encode(b'<svg/>').decode()))[0],400)
        self.assertEqual(self.add_revision(case,self.revision(nome_file='non-corrisponde.jpg'))[0],400)
        self.assertEqual(self.detail(case)['versione_corrente'],0)
    def test_revision_idempotence_and_stale_operator(self):
        case,_=self.new_case();body=self.revision();self.add_revision(case,body)
        self.assertEqual(self.add_revision(case,body)[1]['versione'],1)
        self.assertEqual(self.add_revision(case,self.revision(base=0,version=2))[0],409)
        self.assertEqual(len(self.detail(case)['revisioni']),1)
    def test_stale_customer_and_wrong_image_hash_are_rejected(self):
        case,_=self.new_case();self.add_revision(case);old=self.detail(case)
        self.add_revision(case,self.revision(base=1,version=2))
        self.assertEqual(self.decide(old)[0],409)
        current=self.detail(case);self.assertEqual(self.decide(current,self.decision(current,sha256='0'*64))[0],409)
        self.assertEqual(self.detail(case)['stato'],'attesa')
    def test_approval_requires_explicit_confirmation(self):
        case,_=self.new_case();self.add_revision(case);detail=self.detail(case)
        self.assertEqual(self.decide(detail,self.decision(detail,conferma=False))[0],400)
        self.assertEqual(self.detail(case)['stato'],'attesa')
    def test_change_request_requires_note_and_is_recorded(self):
        case,_=self.new_case();self.add_revision(case);detail=self.detail(case)
        self.assertEqual(self.decide(detail,self.decision(detail,tipo='modifiche'))[0],400)
        self.assertEqual(self.decide(detail,self.decision(detail,tipo='modifiche',nota='Usare il colore blu.',conferma=False))[0],200)
        current=self.detail(case);self.assertEqual(current['stato'],'modifiche');self.assertEqual(current['revisioni'][0]['decisione']['nota'],'Usare il colore blu.')
    def test_decision_is_immutable_and_retry_idempotent(self):
        case,_=self.new_case();self.add_revision(case);detail=self.detail(case);body=self.decision(detail)
        self.assertEqual(self.decide(detail,body)[0],200);self.assertEqual(self.decide(detail,body)[0],200)
        self.assertEqual(self.decide(detail,self.decision(detail,tipo='modifiche',nota='Cambio idea'))[0],409)
        self.assertEqual(self.detail(case)['stato'],'approvata')
    def test_new_revision_explicitly_supersedes_approval(self):
        case,_=self.new_case();self.add_revision(case);self.decide(self.detail(case))
        body=self.revision(base=1,version=2);self.assertEqual(self.add_revision(case,body)[0],409)
        body['sostituisci_approvata']=True;self.assertEqual(self.add_revision(case,body)[0],200)
        current=self.detail(case);self.assertEqual(current['stato'],'attesa');self.assertEqual(current['revisioni'][1]['decisione']['tipo'],'approvata')
    def test_concurrent_upload_and_approval_never_approve_new_image(self):
        case,_=self.new_case();self.add_revision(case);old=self.detail(case)
        upload=self.revision(base=1,version=2,sostituisci_approvata=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(self.decide,old);second=pool.submit(self.add_revision,case,upload)
            self.assertIn(first.result()[0],[200,409]);self.assertEqual(second.result()[0],200)
        current=self.detail(case);self.assertEqual((current['versione_corrente'],current['stato']),(2,'attesa'));self.assertIsNone(current['revisioni'][0]['decisione'])
    def test_reminder_clears_after_approval(self):
        case,_=self.new_case();self.add_revision(case);self.assertTrue(self.detail(case)['in_ritardo']);self.decide(self.detail(case));self.assertFalse(self.detail(case)['in_ritardo'])
    def test_review_exposes_only_latest_version(self):
        case,_=self.new_case();self.add_revision(case);self.add_revision(case,self.revision(base=1,version=2));detail=self.detail(case)
        code,review=self.call('/api/review/'+detail['token']);self.assertEqual(code,200);self.assertEqual(len(review['revisioni']),1);self.assertEqual(review['revisioni'][0]['numero'],2);self.assertNotIn('token',review)
        self.assertEqual(self.call('/api/review/'+'x'*43)[0],404)
    def test_exported_register_contains_decision_but_not_link(self):
        case,_=self.new_case();self.add_revision(case);self.decide(self.detail(case))
        code,result=self.call('/api/registro/'+case);self.assertEqual(code,200);self.assertNotIn('token',result);self.assertEqual(result['revisioni'][0]['decisione']['nome'],'Approvatore Demo')
    def test_origin_files_json_and_persistence(self):
        self.assertEqual(self.call('/api/commesse',{},headers={'Content-Type':'application/json','Origin':'https://example.com'})[0],403)
        self.assertEqual(self.call('/api/status',headers={'Host':'example.com'})[0],403)
        self.assertEqual(self.call('/data/bozze.sqlite3')[0],404);self.assertEqual(self.call('/api/commesse',{},headers={'Content-Type':'text/plain'})[0],415)
        case,_=self.new_case();self.add_revision(case);self.decide(self.detail(case))
        with app.connection() as con:self.assertEqual(con.execute('SELECT COUNT(*) FROM decisioni').fetchone()[0],1)
    def test_shutdown_preserves_database(self):
        self.new_case();self.assertEqual(self.call('/api/ferma',{})[0],200);self.thread.join(timeout=2);self.assertFalse(self.thread.is_alive());self.assertTrue((app.DATA/'bozze.sqlite3').is_file())
    def test_backup_restores_images_and_decisions(self):
        import sqlite3
        backup_spec=importlib.util.spec_from_file_location('backup_bozze',ROOT/'backup.py');backup_module=importlib.util.module_from_spec(backup_spec);backup_spec.loader.exec_module(backup_module)
        case,_=self.new_case();self.add_revision(case);self.decide(self.detail(case))
        path,manifest=backup_module.create_backup(app.DATA/'bozze.sqlite3',app.DATA/'backup')
        self.assertEqual(manifest['conteggi'],{'commesse':1,'revisioni':1,'decisioni':1})
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),manifest['sha256'])
        source=sqlite3.connect(path);restored=sqlite3.connect(app.DATA/'ripristinato.sqlite3')
        try:
            source.backup(restored)
            self.assertEqual(restored.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(restored.execute('SELECT originale FROM revisioni').fetchone()[0],(ROOT/'data/bozza-demo-v1.png').read_bytes())
            self.assertEqual(restored.execute('SELECT tipo FROM decisioni').fetchone()[0],'approvata')
        finally:source.close();restored.close()

if __name__=='__main__':unittest.main(verbosity=2)
