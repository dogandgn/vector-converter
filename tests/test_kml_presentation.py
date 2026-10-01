"""KML display naming, clickable attribute descriptions and mode-specific UI."""
from pathlib import Path
from datetime import datetime
from html.parser import HTMLParser
import os,sys,json,traceback,copy,html
os.environ['QT_QPA_PLATFORM']='offscreen'
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'.test_artifacts/kml_presentation'/datetime.now().strftime('%Y%m%d_%H%M%S_%f');OUT.mkdir(parents=True)
os.environ['QGIS_CUSTOM_CONFIG_PATH']=str(OUT/'profile');sys.path.insert(0,str(ROOT))
from qgis.core import QgsApplication,Qgis,QgsProject,QgsVectorLayer
from qgis.PyQt.QtCore import QEventLoop,QTimer
from qgis.PyQt.QtWidgets import QMainWindow
from osgeo import ogr
app=QgsApplication([],True,str(OUT/'profile'));app.initQgis()
from parsel_donusturucu import core,vector_export as ve,public_output as po,plugin,safe_xml,attribute_schema as schema,input_review
DEST=OUT.parent/f'kml_presentation_qgis_{Qgis.QGIS_VERSION_INT}.json';results=[];errors=[]
def hook(t,v,tb):errors.append(str(v));traceback.print_exception(t,v,tb)
sys.excepthook=hook
def save(complete=False):DEST.write_text(json.dumps({'qgis':Qgis.QGIS_VERSION,'complete':complete,'passed':sum(r['passed'] for r in results),'tests':results},ensure_ascii=False,indent=2),encoding='utf-8')
def run(name,fn):
    try:detail=fn();assert not errors,errors;results.append({'name':name,'passed':True,'detail':detail})
    except Exception as e:results.append({'name':name,'passed':False,'error':repr(e)});save();traceback.print_exc();raise SystemExit(1)
    save()
def feature(props,x=29,kind='Polygon'):
    coords=[[[x,41],[x+.001,41],[x+.001,41.001],[x,41.001],[x,41]]]
    if kind=='Point':coords=[x,41]
    if kind=='LineString':coords=[[x,41],[x+.001,41.001]]
    return {'type':'Feature','geometry':{'type':kind,'coordinates':coords},'properties':props}
def write(folder,features):
    folder.mkdir();(folder/'a.geojson').write_text(json.dumps({'type':'FeatureCollection','features':features},ensure_ascii=False),encoding='utf-8')
attrs={'Il':'İstanbul','Ilce':'Avcılar','Mahalle':'Deneme','Ada':'00358','ParselNo':'014','Alan':'123,45','Nitelik':'Arsa','bos':None}
parcel=OUT/'parcel';write(parcel,[feature(attrs),feature(dict(attrs,ParselNo='015'),29.002)])
def pms(path):return safe_xml.parse(path).findall('.//{'+po.NS+'}Placemark')
def attrs_of(pm):return {f.get('name'):f.text or '' for f in pm.findall('.//{'+po.NS+'}SimpleData')}
def parcel_names():
    path=OUT/'parcels.kml';r=core.convert(core.scan(parcel),path)
    names=[p.findtext('{'+po.NS+'}name') for p in pms(path)]
    assert names==['İstanbul / Avcılar / Deneme — Ada 00358, Parsel 014','İstanbul / Avcılar / Deneme — Ada 00358, Parsel 015'],names
    assert len(safe_xml.parse(path).findall('.//{'+po.NS+'}Folder'))==1
    assert r['output_layers']==['parseller']
    for p in pms(path):
        props=attrs_of(p);assert set(props)==set(attrs)-{'bos'}|{'Shape_Area','Shape_Length'}
        assert not any(k in props for k in ('Name','name','description','hedef_crs','record_uid'))
    return names
run('parcel_id_display_names_without_splitting_layer_or_adding_attributes',parcel_names)
class Table(HTMLParser):
    def __init__(self):super().__init__();self.tags=[];self.text=[]
    def handle_starttag(self,tag,attrs):self.tags.append(tag)
    def handle_data(self,data):self.text.append(data)
def popup():
    for p in pms(OUT/'parcels.kml'):
        desc=p.findtext('{'+po.NS+'}description');t=Table();t.feed(desc)
        assert set(t.tags)=={'table','tr','th','td'}
        for key,value in attrs_of(p).items():assert key in ''.join(t.text) and value in t.text
        assert 'bos' in t.text and '—' in t.text and 'Shape_Area (m²)' in t.text and 'Shape_Length (metre)' in t.text
    layer=QgsVectorLayer(str(OUT/'parcels.kml'),'parseller','ogr');assert layer.isValid()
    for f in layer.getFeatures():assert 'Avcılar' in f['Name'] and '<table ' in f['description'] and 'Nitelik' in f['description']
    return 'Description read back by QGIS, with source values and generated measurement units.'
run('clickable_kml_description_contains_all_selected_source_attributes',popup)
def excluded():
    src=core.scan(parcel);review=input_review.build(src);tokens={(r['source_index'],r['fid']) for g in review['groups'] for r in g['records']}
    plan=schema.default_schema(schema.inventory(src,review,tokens),src,general=True)
    for f in plan['fields']:
        if 'Nitelik' not in f['sources']:f['enabled']=False
        else:f['name']='tur'
    path=OUT/'mapped.kml';core.convert(src,path,attribute_schema=plan)
    for p in pms(path):
        assert 'Ada 00358' in p.findtext('{'+po.NS+'}name')
        assert set(attrs_of(p))=={'tur','Shape_Area','Shape_Length'}
        assert 'Nitelik' not in p.findtext('{'+po.NS+'}description')
    return 'Display identity still available when user excludes/renames identity attribute columns.'
run('parcel_names_independent_of_attribute_exclusion_and_renaming',excluded)
vectors=OUT/'vectors';write(vectors,[feature({'name':'Park A','kod':'P1'},kind='Polygon'),feature({'name':'Yol A','kod':'Y1'},kind='LineString'),feature({'name':None,'kod':'N1'},kind='Point')])
def general_auto():
    src=core.scan(vectors,general=True);path=OUT/'vectors.kml';r=ve.convert(src,path)
    names=[p.findtext('{'+po.NS+'}name') for p in pms(path)]
    assert 'Park A' in names and 'Yol A' in names and any('a — Kayıt ' in n for n in names),names
    assert all('Ada ' not in n and 'Parsel ' not in n for n in names)
    assert len(r['output_layers'])==3
    return names
run('general_mode_keeps_existing_names_and_uses_source_fallback',general_auto)
def chosen():
    path=OUT/'chosen.kml';ve.convert(core.scan(vectors,general=True),path,kml_name_field='kod')
    assert {p.findtext('{'+po.NS+'}name') for p in pms(path)}=={'P1','Y1','N1'}
    for p in pms(path):assert attrs_of(p)['kod']==p.findtext('{'+po.NS+'}name')
    return 'Explicit output field controls display names without changing source values.'
run('general_custom_name_column_preserves_attributes',chosen)
def invalid():
    for mode in (False,True):
        path=OUT/('invalid_'+str(mode)+'.kml')
        try:(ve.convert if mode else core.convert)(core.scan(vectors if mode else parcel,general=mode),path,kml_name_field='absent')
        except ValueError:pass
        else:raise AssertionError('Invalid name field accepted')
        assert not path.exists() and not path.with_suffix('.veri.json').exists()
    return 'Invalid field fails without publication; parcel naming cannot be accidentally overridden.'
run('invalid_name_field_rejected_without_partial_output',invalid)
def zero_and_blank():
    folder=OUT/'zero';write(folder,[feature({'kod':0}),feature({'kod':None},29.002)])
    path=OUT/'zero.kml';ve.convert(core.scan(folder,general=True),path,kml_name_field='kod')
    names=[p.findtext('{'+po.NS+'}name') for p in pms(path)]
    assert '0' in names and any('Kayıt ' in n for n in names),names
    return names
run('zero_is_a_valid_name_null_uses_source_fallback',zero_and_blank)
def safe_text():
    folder=OUT/'escaped';danger='<script>alert("x")</script> & ç';write(folder,[feature({'name':danger,'other':'<img src=x onerror=alert(1)>','bos':None})])
    path=OUT/'safe.kml';ve.convert(core.scan(folder,general=True),path)
    pm=pms(path)[0];assert pm.findtext('{'+po.NS+'}name')==danger and attrs_of(pm)['name']==danger
    desc=pm.findtext('{'+po.NS+'}description');assert '<script>' not in desc and '<img' not in desc
    t=Table();t.feed(desc);assert set(t.tags)=={'table','tr','th','td'} and danger in t.text
    return 'Source HTML-like text is escaped; no links, scripts or fetched resources are injected.'
run('xml_and_html_safe_turkish_special_characters',safe_text)
def existing_metadata():
    folder=OUT/'kml_input';folder.mkdir();(folder/'a.kml').write_text('<kml xmlns="'+po.NS+'"><Document><name>Kaynak Katman</name><Placemark><name>Özgün yer adı</name><description>old</description><ExtendedData><Data name="kod"><value>K1</value></Data></ExtendedData><Point><coordinates>29,41</coordinates></Point></Placemark></Document></kml>',encoding='utf-8')
    path=OUT/'kept.kml';ve.convert(core.scan(folder,general=True),path)
    pm=pms(path)[0];assert pm.findtext('{'+po.NS+'}name')=='Özgün yer adı' and set(attrs_of(pm))=={'kod'}
    return 'Original KML display name preserved without adding it to ExtendedData.'
run('general_original_kml_name_metadata_preserved',existing_metadata)
def other_formats():
    for ext in ('gpkg','gdb','geojson'):
        path=OUT/('single_'+ext+'.'+ext);r=core.convert(core.scan(parcel),path,'tm30' if ext!='geojson' else 'wgs84')
        ds=ogr.Open(str(path));assert ds.GetLayerCount()==1 and ds.GetLayer(0).GetFeatureCount()==2
        names={f.name for f in ds.GetLayer(0).schema};assert not names&{'Name','name','description'};core.close_dataset(ds)
        assert r['public_schema']['kml_display_names'] is None
    return 'Parcel GPKG/GDB/GeoJSON remain one data layer/collection.'
run('non_kml_formats_remain_single_parcel_layer_without_name_columns',other_formats)
class Canvas:
    def refresh(self):pass
class Host:
    window=QMainWindow()
    def mainWindow(self):return self.window
    def mapCanvas(self):return Canvas()
def wait(d):
    loop=QEventLoop();timer=QTimer();end=QTimer();end.setSingleShot(True)
    timer.timeout.connect(lambda:loop.quit() if d.task is None else None);end.timeout.connect(loop.quit);timer.start(10);end.start(60000);loop.exec();timer.stop();end.stop();assert not d.task and not errors,errors
def ui():
    d=plugin.ParselDialog(Host());assert d.kml_name_field.isHidden()
    d.data_mode.setCurrentIndex(d.data_mode.findData('vector'));d.input.setText(str(vectors));d.output_format.setCurrentIndex(d.output_format.findData('.kml'));d.start_scan();wait(d)
    assert not d.kml_name_field.isHidden() and d.kml_name_field.isEnabled() and d.kml_name_field.findData('kod')>=0
    d.kml_name_field.setCurrentIndex(d.kml_name_field.findData('kod'));d.output.setText(str(OUT/'ui.kml'));d.add_to_qgis.setChecked(True);d.start_convert();wait(d)
    assert d.last_report and {p.findtext('{'+po.NS+'}name') for p in pms(OUT/'ui.kml')}=={'P1','Y1','N1'}
    assert len(QgsProject.instance().mapLayers())==3
    d.output_format.setCurrentIndex(d.output_format.findData('.gpkg'));assert d.kml_name_field.isHidden()
    d.output_format.setCurrentIndex(d.output_format.findData('.kml'));d.data_mode.setCurrentIndex(d.data_mode.findData('parcel'));assert d.kml_name_field.isHidden()
    QgsProject.instance().removeAllMapLayers();d.close();d.deleteLater()
    return 'One naming control shown only for general KML; actual UI exports chosen values and 3 geometry layers.'
run('mode_specific_name_selector_and_actual_ui_export',ui)
def remapped_ui():
    d=plugin.ParselDialog(Host());d.data_mode.setCurrentIndex(d.data_mode.findData('vector'));d.output_format.setCurrentIndex(d.output_format.findData('.kml'));d.input.setText(str(vectors));d.start_scan();wait(d)
    src=d.review.selected_sources();plan=schema.default_schema(schema.inventory(src,d.review.review,d.review.selected()),src,general=True)
    for f in plan['fields']:
        if f['name']=='kod':f['name']='yeni_ad'
    d.attribute_plan=plan;d.selection_changed();assert d.kml_name_field.findData('yeni_ad')>=0 and d.kml_name_field.findData('kod')<0
    d.kml_name_field.setCurrentIndex(d.kml_name_field.findData('yeni_ad'))
    for f in plan['fields']:
        if f['name']=='yeni_ad':f['enabled']=False
    d.selection_changed();assert d.kml_name_field.currentData() is None
    d.close();d.deleteLater();return 'Name list uses final output fields; deleted chosen field resets to automatic.'
run('name_selector_tracks_attribute_renames_and_exclusions',remapped_ui)
save(True);print('STAGE21',len(results),'PASS',flush=True);app.exitQgis()
