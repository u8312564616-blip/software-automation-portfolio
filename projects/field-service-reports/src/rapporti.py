"""Rapportini PDF da record salvati. Non invia dati a servizi esterni."""
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

def create_pdf(record):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, LongTable
    regular=Path('C:/Windows/Fonts/arial.ttf')
    bold=Path('C:/Windows/Fonts/arialbd.ttf')
    if 'Rapporto' not in pdfmetrics.getRegisteredFontNames():
        if not regular.is_file():
            import reportlab
            regular=Path(reportlab.__file__).parent/'fonts'/'Vera.ttf'
            bold=regular.with_name('VeraBd.ttf')
        pdfmetrics.registerFont(TTFont('Rapporto',str(regular)))
        pdfmetrics.registerFont(TTFont('RapportoBold',str(bold)))
    green=colors.HexColor('#205447');muted=colors.HexColor('#52655e')
    body=ParagraphStyle('Body',fontName='Rapporto',fontSize=9.5,leading=14,spaceAfter=8,splitLongWords=True)
    small=ParagraphStyle('Small',parent=body,fontSize=8,leading=11,textColor=muted)
    title=ParagraphStyle('Title',parent=body,fontName='RapportoBold',fontSize=24,leading=29,textColor=green,spaceAfter=14)
    heading=ParagraphStyle('Heading',parent=body,fontName='RapportoBold',fontSize=12,leading=17,textColor=green,spaceBefore=12,spaceAfter=8)
    def paragraph(text,style=body):
        return Paragraph(escape(str(text or '-')).replace('\n','<br/>'),style)
    def date(s):return '/'.join(s.split('-')[::-1]) if s else 'Non prevista'
    stream=BytesIO()
    doc=SimpleDocTemplate(stream,pagesize=A4,rightMargin=42,leftMargin=42,topMargin=42,bottomMargin=48,title='Rapporto di intervento',author='Quaderno tecnico')
    flow=[paragraph('QUADERNO TECNICO / RAPPORTINO',small),paragraph('Rapporto di intervento',title),paragraph('COMPLETATO' if record['stato']=='completato' else 'BOZZA - controlli da completare',heading),paragraph('Riferimento: '+record['id'],small)]
    details=[]
    for a,b in [('Cliente','cliente'),('Tecnico','tecnico'),('Data intervento','data'),('Sede','sede'),('Impianto / apparecchio','impianto'),('Prossima scadenza','prossima_data')]:
        details.append([paragraph(a,small),paragraph(date(record.get(b,'')) if b in ['data','prossima_data'] else record.get(b,''))])
    table=Table(details,colWidths=[128,383],hAlign='LEFT')
    table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#f2f5ef')),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),10),('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
    flow.extend([Spacer(1,12),table,paragraph('Attività svolta',heading),paragraph(record['attivita']),paragraph('Controlli effettuati',heading)])
    labels={'ok':'Regolare','anomalia':'Anomalia','non_applicabile':'Non applicabile','':'Da verificare'}
    rows=[[paragraph(t,small) for t in ['Controllo','Esito','Nota']]]
    for check in record['controlli']:
        rows.append([paragraph(check['nome']),paragraph(labels[check['esito']]),paragraph(check['nota'])])
    checks=LongTable(rows,colWidths=[150,95,266],repeatRows=1,splitInRow=1,hAlign='LEFT')
    checks.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e6eee2')),('GRID',(0,0),(-1,-1),.4,colors.HexColor('#c5d0c1')),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),7)]))
    flow.extend([checks,paragraph('Note e raccomandazioni',heading),paragraph(record.get('note') or 'Nessuna nota aggiuntiva.'),Spacer(1,14),paragraph('Rapportino operativo: non costituisce certificazione, dichiarazione di conformità o firma digitale.',small)])
    def footer(canvas,document):
        canvas.saveState();canvas.setStrokeColor(colors.HexColor('#c5d0c1'));canvas.line(42,36,553,36);canvas.setFont('Rapporto',8);canvas.setFillColor(muted);canvas.drawString(42,23,'Quaderno tecnico | '+record['id'][:8]);canvas.drawRightString(553,23,f'Pagina {document.page}');canvas.restoreState()
    doc.build(flow,onFirstPage=footer,onLaterPages=footer)
    return stream.getvalue()
