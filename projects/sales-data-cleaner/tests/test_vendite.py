import base64,csv,datetime as dt,importlib.util,json,re,tempfile,threading,unittest,zipfile
import xml.etree.ElementTree as ET
from io import StringIO,BytesIO
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('vendite',ROOT/'src/server.py');app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)
SETTINGS={'date':'it','numeri':'it'}

def csv_bytes(rows,headers=None):
    stream=StringIO(newline='');writer=csv.writer(stream,delimiter=';');writer.writerow(headers or app.FIELDS);writer.writerows(rows);return stream.getvalue().encode('utf-8-sig')

class CleaningTest(unittest.TestCase):
    def test_csv_fixture_counts_and_source_unchanged(self):
        raw=(ROOT/'data/vendite-esempio.csv').read_bytes();original=bytes(raw)
        result=app.analyze(raw,'csv',SETTINGS)
        self.assertEqual(result['conteggi'],{'valida':6,'duplicato':1,'errore':2,'conflitto':2});self.assertEqual(result['totale_valido'],'100.00');self.assertEqual(raw,original)
    def test_xlsx_matches_csv_even_without_sheet_dimensions(self):
        xlsx=app.analyze((ROOT/'data/vendite-esempio.xlsx').read_bytes(),'xlsx',SETTINGS)
        csv_result=app.analyze((ROOT/'data/vendite-esempio.csv').read_bytes(),'csv',SETTINGS)
        self.assertEqual(xlsx['conteggi'],csv_result['conteggi']);self.assertEqual(xlsx['totale_valido'],'100.00');self.assertEqual(xlsx['metadati']['foglio'],'Vendite')
    def test_locale_decimal_zero_and_missing(self):
        self.assertEqual(app.amount('1.234,56','it'),123456);self.assertEqual(app.amount('1,234.56','en'),123456);self.assertEqual(app.amount(0,'it'),0)
        for value in [None,True,'NaN','1.234,567']:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):app.amount(value,'it')
    def test_explicit_date_convention(self):
        self.assertEqual(app.parse_date('04/05/2026','it'),'2026-05-04');self.assertEqual(app.parse_date('04/05/2026','us'),'2026-04-05')
        self.assertEqual(app.parse_date('2026-05-04','us'),'2026-05-04')
        with self.assertRaises(ValueError):app.parse_date('31/02/2026','it')
    def test_conflicting_id_excludes_all_even_with_invalid_row(self):
        rows=[['A','01/01/2026','P','C',1,'1,00'],['A','errore','P','C',1,'1,00']]
        report=app.analyze(csv_bytes(rows),'csv',SETTINGS)
        self.assertEqual(report['conteggi']['conflitto'],2);self.assertEqual(report['totale_valido'],'0.00')
    def test_trim_preserves_leading_zero_ids(self):
        report=app.analyze(csv_bytes([[' 001 ','01/01/2026',' P ',' C ',1,'0,00']]),'csv',SETTINGS)
        row=report['righe'][0];self.assertEqual(row['pulita']['id_riga'],'001');self.assertEqual(row['pulita']['prodotto'],'P');self.assertTrue(row['modifiche'])
    def test_csv_export_escapes_formula_text_without_changing_original(self):
        raw=csv_bytes([['001','01/01/2026','=1+1','@categoria',1,'10,00']])
        report=app.analyze(raw,'csv',SETTINGS);export=app.export_csv(report).decode('utf-8-sig');rows=list(csv.reader(StringIO(export),delimiter=';'))
        self.assertEqual(rows[1][2],"'=1+1");self.assertEqual(rows[1][3],"'@categoria");self.assertEqual(report['righe'][0]['pulita']['prodotto'],'=1+1')
    def test_xlsx_formulas_are_not_evaluated(self):
        source=BytesIO((ROOT/'data/vendite-esempio.xlsx').read_bytes());output=BytesIO()
        with zipfile.ZipFile(source) as original,zipfile.ZipFile(output,'w') as changed:
            for item in original.infolist():
                content=original.read(item.filename)
                if item.filename=='xl/worksheets/sheet1.xml':
                    tree=ET.fromstring(content);ns='{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
                    cell=tree.find('.//'+ns+'c[@r="F2"]');self.assertIsNotNone(cell)
                    for child in list(cell):cell.remove(child)
                    ET.SubElement(cell,ns+'f').text='1+1';ET.SubElement(cell,ns+'v').text='2'
                    content=ET.tostring(tree,encoding='utf-8',xml_declaration=True)
                changed.writestr(item,content)
        report=app.analyze(output.getvalue(),'xlsx',SETTINGS)
        self.assertEqual(report['righe'][0]['stato'],'errore');self.assertIn('Formula Excel',report['righe'][0]['motivi'][0])
    def test_dashboard_reconciles_filters_and_returns(self):
        report=app.analyze((ROOT/'data/vendite-esempio.csv').read_bytes(),'csv',SETTINGS)
        all_data=app.dashboard(report);self.assertEqual((all_data['totale'],all_data['quantita_netta'],all_data['righe']),('100.00',14,6));self.assertTrue(all_data['riconciliato'])
        november=app.dashboard(report,'2026-11');self.assertEqual((november['totale'],november['righe']),('0.00',2));self.assertTrue(november['riconciliato'])
        accessories=app.dashboard(report,category='Accessori');self.assertEqual(accessories['totale'],'40.00')
        self.assertEqual(app.dashboard(report,'2025-01')['righe'],0)
    def test_currency_and_signs_are_validated(self):
        rows=[['A','01/01/2026','P','C',1,'10,00','USD'],['B','01/01/2026','P','C',1,'-10,00','EUR']]
        report=app.analyze(csv_bytes(rows,app.FIELDS+['valuta']),'csv',SETTINGS);self.assertEqual(report['conteggi']['errore'],2)
    def test_missing_and_duplicate_headers_rejected(self):
        for headers in [app.FIELDS[:-1],app.FIELDS+['ID']]:
            with self.subTest(headers=headers):
                with self.assertRaises(ValueError):app.analyze(csv_bytes([],headers),'csv',SETTINGS)

class ApiTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();app.DATA=Path(self.temp.name)
        self.server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.url=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()
    def call(self,path,body=None,headers=None):
        req=Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,headers=headers or ({'Content-Type':'application/json'} if body is not None else {}))
        try:
            with urlopen(req,timeout=10) as r:return r.status,json.load(r)
        except HTTPError as e:return e.code,json.load(e)
    def upload(self):return {'nome':'demo.csv','contenuto':base64.b64encode((ROOT/'data/vendite-esempio.csv').read_bytes()).decode(),'impostazioni':dict(SETTINGS)}
    def test_confirm_required_and_idempotent_import(self):
        body=self.upload();code,preview=self.call('/api/anteprima',body);self.assertEqual(code,200);identifier=preview['id']
        self.assertEqual(self.call('/api/dashboard/'+identifier)[0],409);self.assertEqual(self.call('/api/conferma',{'id':identifier,'conferma':False})[0],400)
        self.assertEqual(self.call('/api/conferma',{'id':identifier,'conferma':True})[0],200)
        body['impostazioni']={'numeri':'it','date':'it'};self.assertEqual(self.call('/api/anteprima',body)[1]['id'],identifier)
        self.assertEqual(len(self.call('/api/importazioni')[1]['importazioni']),1);self.assertEqual(self.call('/api/dashboard/'+identifier)[1]['totale'],'100.00')
    def test_original_download_and_saved_report_persist(self):
        body=self.upload();identifier=self.call('/api/anteprima',body)[1]['id']
        with urlopen(self.url+'/api/originale/'+identifier) as r:self.assertEqual(r.read(),base64.b64decode(body['contenuto']))
        with app.connection() as con:self.assertEqual(json.loads(con.execute('SELECT rapporto FROM importazioni WHERE id=?',(identifier,)).fetchone()[0])['totale_valido'],'100.00')
    def test_embedded_example_uses_same_pipeline(self):
        code,result=self.call('/api/esempio',{})
        self.assertEqual(code,200);self.assertFalse(result['confermato']);self.assertEqual(result['rapporto']['conteggi'],{'valida':6,'duplicato':1,'errore':2,'conflitto':2})
        self.assertEqual(self.call('/api/esempio',{})[1]['id'],result['id'])
    def test_confirmed_csv_contains_only_valid_rows_and_exact_total(self):
        identifier=self.call('/api/anteprima',self.upload())[1]['id']
        self.call('/api/conferma',{'id':identifier,'conferma':True})
        with urlopen(self.url+'/api/pulito/'+identifier) as response:
            self.assertIn('text/csv',response.headers['Content-Type']);self.assertIn('attachment',response.headers['Content-Disposition'])
            rows=list(csv.DictReader(StringIO(response.read().decode('utf-8-sig')),delimiter=';'))
        self.assertEqual(len(rows),6)
        self.assertEqual(sum(app.amount(row['importo'],'it') for row in rows),10000)
        self.assertEqual(len({row['id_riga'] for row in rows}),6)
    def test_invalid_upload_protection_and_stop(self):
        body=self.upload();body['contenuto']='not base64';self.assertEqual(self.call('/api/anteprima',body)[0],400)
        self.assertEqual(self.call('/api/anteprima',self.upload(),{'Content-Type':'application/json','Origin':'https://example.com'})[0],403)
        self.assertEqual(self.call('/data/vendite.sqlite3')[0],404);self.assertEqual(self.call('/api/anteprima',{}, {'Content-Type':'text/plain'})[0],415)
        self.call('/api/anteprima',self.upload());self.assertEqual(self.call('/api/ferma',{})[0],200);self.thread.join(timeout=2);self.assertFalse(self.thread.is_alive());self.assertTrue((app.DATA/'vendite.sqlite3').is_file())

if __name__=='__main__':unittest.main(verbosity=2)
