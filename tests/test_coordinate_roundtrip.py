"""Regression: exact coordinate round trips without weakening verification."""
from pathlib import Path
from datetime import datetime
from collections import Counter
import os,sys,json,hashlib,math,traceback
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'.test_artifacts/coordinate_roundtrip'/datetime.now().strftime('%Y%m%d_%H%M%S_%f')
OUT.mkdir(parents=True)
os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['QGIS_CUSTOM_CONFIG_PATH']=str(OUT/'profile')
sys.path.insert(0,str(ROOT));sys.dont_write_bytecode=True
from qgis.core import QgsApplication,Qgis
from osgeo import ogr,gdal
gdal.UseExceptions()
app=QgsApplication([],True,str(OUT/'profile'));app.initQgis()
from parsel_donusturucu import core,vector_export,public_output as po,safe_xml
DEST=OUT.parent/f'coordinate_roundtrip_qgis_{Qgis.QGIS_VERSION_INT}.json'
results=[];source_hashes={};products={}
def save(complete=False):
    DEST.write_text(json.dumps({'qgis':Qgis.QGIS_VERSION,'complete':complete,'passed':sum(t['passed'] for t in results),'tests':results,'root':str(OUT)},ensure_ascii=False,indent=2),encoding='utf-8')
def run(name,fn):
    try:
        detail=fn();results.append({'name':name,'passed':True,'detail':detail})
    except Exception as e:
        results.append({'name':name,'passed':False,'error':repr(e)});save();traceback.print_exc();raise SystemExit(1)
    save()
def polygon(x,y=41,d=.001):
    return {'type':'Polygon','coordinates':[[[x,y],[x+d,y],[x+d,y+d],[x,y+d],[x,y]]]}
def dataset(name,items):
    folder=OUT/name;folder.mkdir()
    path=folder/'a.geojson'
    path.write_text(json.dumps({'type':'FeatureCollection','features':items},ensure_ascii=False),encoding='utf-8')
    source_hashes[path]=hashlib.sha256(path.read_bytes()).hexdigest()
    return folder
def feature(geometry,props): return {'type':'Feature','properties':props,'geometry':geometry}
def xy_values(obj):
    if isinstance(obj[0],(int,float)):return [tuple(obj[:2])]
    return [p for child in obj for p in xy_values(child)]
def read_coordinates(path):
    if path.suffix=='.geojson':
        return {f['properties']['kod']:Counter(xy_values(f['geometry']['coordinates'])) for f in json.loads(path.read_text(encoding='utf-8'))['features']}
    root=safe_xml.parse(path);ns='{'+po.NS+'}'
    rows={}
    for pm in root.findall('.//'+ns+'Placemark'):
        key=next(n.text for n in pm.findall('.//'+ns+'SimpleData') if n.get('name')=='kod')
        coords=[]
        for node in pm.findall('.//'+ns+'coordinates'):
            coords.extend(tuple(float(v) for v in token.split(',')[:2]) for token in node.text.split())
        rows[key]=Counter(coords)
    return rows
props={'Il':'İstanbul','Ilce':'Avcılar','Mahalle':'Deneme','Ada':'00358','Nitelik':'Arsa','aciklama':'Türkçe & özgün','bos':None}
for x in (28.7,29.0):
    items=[feature(polygon(x+n*.002),dict(props,ParselNo=str(14+n),kod=f'P{n}')) for n in range(2)]
    folder=dataset('parcel_'+str(x),items)
    for ext in ('geojson','kml','gpkg','gdb'):
        def original_case(x=x,ext=ext,folder=folder,items=items):
            path=OUT/f'parcel_{x}_{ext}.{ext}'
            report=core.convert(core.scan(folder),path)
            assert report['validation']['geometry_verified'] and report['validation']['records']==2
            if ext in ('geojson','kml'):
                expected={f['properties']['kod']:Counter(xy_values(f['geometry']['coordinates'])) for f in items}
                assert read_coordinates(path)==expected,'Coordinate float changed in text format'
            products[(x,ext)]=path
            return {'records':2,'read_back_geometry':True,'exact_text_coordinates':ext in ('kml','geojson')}
        run(f'original_{x}_{ext}',original_case)

x=28.703000000000003;y=41.00000000000001
outer=[[x,y],[x+.01,y],[x+.01,y+.01],[x,y+.01],[x,y]]
hole=[[x+.002,y+.002],[x+.002,y+.004],[x+.004,y+.004],[x+.004,y+.002],[x+.002,y+.002]]
geometries=[{'type':'Point','coordinates':[x,y]},
 {'type':'MultiPoint','coordinates':[[x,y],[x+.002,y+.002]]},
 {'type':'LineString','coordinates':[[x,y],[x+.00000000000001,y+.001],[x+.01,y+.002]]},
 {'type':'MultiLineString','coordinates':[[[x,y],[x+.01,y+.001]],[[x+.002,y+.003],[x+.003,y+.004]]]},
 {'type':'Polygon','coordinates':[outer,hole]},
 {'type':'MultiPolygon','coordinates':[[outer,hole],polygon(x+.02,y)['coordinates']]},
 {'type':'Point','coordinates':[1.2345678901234567e-20,2.3456789012345678e-20]},
 {'type':'LineString','coordinates':[[x+.03,y],[math.nextafter(x+.03,math.inf),y+.001]]}]
items=[feature(g,{'kod':f'V{i}','ad':'Özgün '+str(i),'deger':'001'}) for i,g in enumerate(geometries)]
folder=dataset('vectors',items)
for ext in ('geojson','kml'):
    def general_case(ext=ext):
        path=OUT/f'vectors_{ext}.{ext}'
        report=vector_export.convert(core.scan(folder,general=True),path)
        assert report['validation']['records']==len(items)
        expected={f['properties']['kod']:Counter(xy_values(f['geometry']['coordinates'])) for f in items}
        assert read_coordinates(path)==expected,'General point/line/polygon/hole coordinates changed'
        products[('vectors',ext)]=path
        return {'records':len(items),'families':6,'exact_coordinates_and_vertex_multiplicity':True}
    run('general_all_geometry_families_'+ext,general_case)

def orientation():
    data=json.loads(products[('vectors','geojson')].read_text(encoding='utf-8'))
    for f in data['features']:
        kind=f['geometry']['type']
        if kind not in ('Polygon','MultiPolygon'):continue
        parts=[f['geometry']['coordinates']] if kind=='Polygon' else f['geometry']['coordinates']
        for rings in parts:
            for i,ring in enumerate(rings):
                area=sum((a[0]-ring[0][0])*(b[1]-ring[0][1])-(b[0]-ring[0][0])*(a[1]-ring[0][1]) for a,b in zip(ring,ring[1:]))
                assert (area>0)==(i==0)
    return 'Exterior CCW, holes CW, exact coordinates already independently verified.'
run('rfc7946_orientation_and_holes',orientation)
def reject(expected,actual):
    a=ogr.CreateGeometryFromJson(json.dumps(expected));b=ogr.CreateGeometryFromJson(json.dumps(actual))
    try:po.verify_geometry(a,b,0)
    except RuntimeError:return 'Changed geometry rejected with original strict verification.'
    raise AssertionError('Changed geometry incorrectly accepted')
run('moved_vertex_still_rejected',lambda:reject(polygon(x),polygon(x+.0001)))
def changed_hole():
    return reject({'type':'Polygon','coordinates':[outer,hole]}, {'type':'Polygon','coordinates':[outer,polygon(x+.0021,y+.002,.002)['coordinates'][0]]})
run('changed_hole_still_rejected',changed_hole)
run('missing_vertex_still_rejected',lambda:reject(polygon(x),{'type':'Polygon','coordinates':[[[x,41],[x+.001,41],[x+.001,41.001],[x,41]]] }))
run('one_ulp_point_change_still_rejected',lambda:reject({'type':'Point','coordinates':[x,y]},{'type':'Point','coordinates':[math.nextafter(x,math.inf),y]}))
def preservation():
    assert all(hashlib.sha256(p.read_bytes()).hexdigest()==h for p,h in source_hashes.items())
    for key,path in products.items():
        raw=json.loads(path.with_suffix('.veri.json').read_text(encoding='utf-8'))
        assert 'Türkçe' in json.dumps(raw,ensure_ascii=False) or key[0]=='vectors'
    return 'Source bytes unchanged; original source text retained in companions.'
run('source_bytes_and_original_properties_preserved',preservation)
assert len(results)==16
save(True);print('STAGE22 16 PASS',flush=True);app.exitQgis()
