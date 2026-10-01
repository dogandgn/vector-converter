"""WGS84 GeoJSON/KML products, with lossless companion tables and verified publication."""

from pathlib import Path
import json
import os
import tempfile
import xml.etree.ElementTree as ET  # nosec B405
from osgeo import ogr
from . import core, safe_xml

TABLES = (
    "parseller",
    "kaynaklar",
    "kayit_kaynaklari",
    "kontroller",
    "islem_ozeti",
)
NS = "http://www.opengis.net/kml/2.2"


def geometry_object(geometry):
    """Retain binary64 coordinates across JSON and KML text round trips."""

    # OGR's text formatter can shorten near-integer decimals even with 17
    # significant figures. Python's JSON/repr preserve the actual float value.
    def coordinates(part):
        if ogr.GT_Flatten(part.GetGeometryType()) == ogr.wkbPoint:
            return list(part.GetPoint_2D(0))
        if part.GetGeometryCount():
            return [
                coordinates(part.GetGeometryRef(i))
                for i in range(part.GetGeometryCount())
            ]
        return [list(part.GetPoint_2D(i)) for i in range(part.GetPointCount())]

    names = {
        ogr.wkbPoint: "Point",
        ogr.wkbMultiPoint: "MultiPoint",
        ogr.wkbLineString: "LineString",
        ogr.wkbMultiLineString: "MultiLineString",
        ogr.wkbPolygon: "Polygon",
        ogr.wkbMultiPolygon: "MultiPolygon",
    }
    return {
        "type": names[ogr.GT_Flatten(geometry.GetGeometryType())],
        "coordinates": coordinates(geometry),
    }


def oriented(geometry):
    """RFC7946 exterior rings counterclockwise, holes clockwise, retain all vertices."""
    obj = geometry_object(geometry)
    polygons = (
        obj["coordinates"] if obj["type"] == "MultiPolygon" else [obj["coordinates"]]
    )
    for polygon in polygons:
        for i, ring in enumerate(polygon):
            area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:]))
            if (area > 0) != (i == 0):
                ring.reverse()
    return obj


def write_kml(path, features):
    def node(parent, tag, text=None, **attrs):
        child = ET.SubElement(parent, tag, attrs)
        if text is not None:
            child.text = str(text)
        return child

    # A local xmlns declaration avoids changing ElementTree's process-global
    # namespace registry, which also affects preserved source XML metadata.
    root = ET.Element("kml", {"xmlns": NS})
    doc = node(root, "Document")
    node(doc, "name", "Parseller")
    for feature in features:
        attrs = feature["properties"]
        pm = node(doc, "Placemark", id="p_" + attrs["record_uid"])
        node(
            pm,
            "name",
            (
                (
                    str(attrs.get("ada_no") or "")
                    + "/"
                    + str(attrs.get("parsel_no") or "")
                )
                if attrs.get("parsel_no")
                else str(attrs.get("parsel_anahtari") or attrs["record_uid"])
            ),
        )
        ext = node(pm, "ExtendedData")
        for key, value in attrs.items():
            if value is not None:
                node(node(ext, "Data", name=key), "value", value)
        # KML Data values are strings; this carries exact types and NULLs as JSON.
        node(
            node(ext, "Data", name="parsel_oznitelikleri_json"),
            "value",
            core.json_text(attrs),
        )
        multi = node(pm, "MultiGeometry")
        geometry = feature["geometry"]
        polygons = (
            geometry["coordinates"]
            if geometry["type"] == "MultiPolygon"
            else [geometry["coordinates"]]
        )
        for rings in polygons:
            polygon = node(multi, "Polygon")
            node(polygon, "altitudeMode", "clampToGround")
            for index, ring in enumerate(rings):
                boundary = node(
                    polygon,
                    "outerBoundaryIs" if index == 0 else "innerBoundaryIs",
                )
                linear = node(boundary, "LinearRing")
                node(
                    linear,
                    "coordinates",
                    " ".join(f"{x!r},{y!r}" for x, y in ring),
                )
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def verify_main(path, features):
    if path.suffix == ".geojson":
        obj = json.loads(path.read_text(encoding="utf-8"))
        if (
            obj.get("type") != "FeatureCollection"
            or "crs" in obj
            or obj["features"] != features
        ):
            raise RuntimeError("GeoJSON yeniden okuma değer kontrolü başarısız.")
    else:
        tree = safe_xml.parse(path)
        placemarks = tree.findall(".//{" + NS + "}Placemark")
        if len(placemarks) != len(features):
            raise RuntimeError("KML kayıt sayısı değişti.")
        for pm, f in zip(placemarks, features):
            values = {
                d.attrib["name"]: d.findtext("{" + NS + "}value", default="")
                for d in pm.findall("./{" + NS + "}ExtendedData/{" + NS + "}Data")
            }
            if json.loads(values.pop("parsel_oznitelikleri_json")) != f["properties"]:
                raise RuntimeError("KML tipli alan değerleri değişti.")
            if values != {
                k: str(v) for k, v in f["properties"].items() if v is not None
            }:
                raise RuntimeError("KML ExtendedData alanları değişti.")
    # Independent GDAL reader: verify the serialized geometries and spatial reference.
    ds = ogr.Open(str(path), 0)
    if ds is None:
        raise RuntimeError("GIS sürücüsü çıktıyı açamadı.")
    try:
        layer = ds.GetLayer(0)
        if layer.GetFeatureCount() != len(features):
            raise RuntimeError("GIS sürücüsü kayıt sayısı farklı.")
        if layer.GetSpatialRef() is None or not layer.GetSpatialRef().IsGeographic():
            raise RuntimeError("Coğrafi koordinat tanımı okunamadı.")
        for expected, actual in zip(features, layer):
            a = ogr.CreateGeometryFromJson(json.dumps(expected["geometry"]))
            b = actual.GetGeometryRef()
            if b is None or not b.IsValid():
                raise RuntimeError("GIS sürücüsü geçersiz geometri okudu.")
            b = b.Clone()
            b.FlattenTo2D()
            qa = core.qgeom(a)
            qb = core.qgeom(b)
            if not qa.isGeosEqual(qb) or abs(qa.area() - qb.area()) > 1e-14:
                raise RuntimeError("GIS sürücüsü geometriyi farklı okudu.")
    finally:
        ds = None
    return {
        "reopened": True,
        "parcels": len(features),
        "crs": "EPSG:4326",
        "unit": "degree",
        "attribute_values_verified": sum(len(f["properties"]) for f in features),
        "geometry_topologically_equal": True,
        "companion_tables_verified": True,
        "source_bytes_unchanged": True,
    }


def convert_exchange(
    sources,
    output,
    profile_id="wgs84",
    missing_crs="",
    crs_basis="user_confirmed",
    crs_evidence="",
    cancel=lambda: False,
    progress=lambda n, s: None,
    protected=(),
    merge_mode="separate",
    attribute_policy="preserve",
    attribute_schema=None,
):
    output = Path(output).resolve()
    if profile_id != "wgs84":
        raise ValueError(
            "KML ve standart GeoJSON çıktısı WGS84 derece (EPSG:4326) olmalıdır. Metre/datum seçimi için GeoPackage veya FileGDB kullanın."
        )
    if any(output.is_relative_to(Path(p).resolve()) for p in protected):
        raise ValueError("Ham arşiv/kaynak klasörüne çıktı yazılmaz.")
    side_json = output.with_suffix(".rapor.json")
    side_html = output.with_suffix(".rapor.html")
    side_data = output.with_suffix(".veri.json")
    if any(os.path.lexists(p) for p in (output, side_json, side_html, side_data)):
        raise ValueError("Çıktı veya eşlik eden dosya zaten var; yeni ad seçin.")
    core.check_cancel(cancel)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".parsel_exchange_", dir=output.parent
    ) as folder:
        stage = Path(folder)
        canonical = stage / "canonical.gpkg"
        target = stage / output.name
        report = core._convert_gpkg(
            sources,
            canonical,
            profile_id,
            missing_crs,
            crs_basis,
            crs_evidence,
            cancel,
            lambda n, s: progress(n * 0.7, s),
            protected,
            merge_mode,
            attribute_policy,
            attribute_schema,
        )
        report["output"] = str(output)
        report["output_format"] = (
            "KML 2.2" if output.suffix.lower() == ".kml" else "GeoJSON RFC7946"
        )
        report["canonical_validation"] = report.pop("validation")
        report["storage"] = {
            "crs": "EPSG:4326",
            "unit": "degree",
            "companion": str(side_data),
            "note": "Ana dosya parsel geometrilerini ve standart alanları taşır. Beş tablonun tüm alanları, özgün metadata ve ilişkiler eşlik eden .veri.json içindedir. Birlikte paylaşın.",
            "kml_types": "KML Data alanları metindir. Sayı/NULL türleri parsel_oznitelikleri_json ve eşlik eden veride korunur.",
        }
        ds = ogr.Open(str(canonical), 0)
        tables = {}
        features = []
        try:
            for name in TABLES:
                tables[name] = []
                for f in ds.GetLayerByName(name):
                    core.check_cancel(cancel)
                    attrs = {
                        f.GetFieldDefnRef(i).GetName(): f.GetField(i)
                        for i in range(f.GetFieldCount())
                    }
                    if name == "islem_ozeti":
                        attrs["rapor_json"] = core.json_text(report)
                    tables[name].append(attrs)
                    if name == "parseller":
                        features.append(
                            {
                                "type": "Feature",
                                "id": attrs["record_uid"],
                                "properties": attrs,
                                "geometry": oriented(f.GetGeometryRef()),
                            }
                        )
        finally:
            ds = None
        progress(75, "Değişim dosyası yazılıyor")
        core.check_cancel(cancel)
        if output.suffix.lower() == ".kml":
            write_kml(target, features)
        else:
            target.write_text(
                json.dumps(
                    {"type": "FeatureCollection", "features": features},
                    ensure_ascii=False,
                    allow_nan=False,
                ),
                encoding="utf-8",
            )
        companion = {
            "schema": "parsel-donusturucu/tables-v1",
            "version": core.VERSION,
            "main_file": output.name,
            "crs": "EPSG:4326",
            "tables": tables,
        }
        data_path = stage / side_data.name
        data_path.write_text(
            json.dumps(companion, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
        if json.loads(data_path.read_text(encoding="utf-8")) != companion:
            raise RuntimeError("Eşlik eden tablolar değişti.")
        report["validation"] = verify_main(target, features)
        report["validation"]["table_counts"] = {
            name: len(rows) for name, rows in tables.items()
        }
        report["validation"]["main_sha256"] = core.sha(target)
        report["validation"]["companion_sha256"] = core.sha(data_path)
        for source in sources:
            core.check_cancel(cancel)
            if source.status != "ready":
                continue
            if core.sha(source.path) != source.file_sha256:
                raise RuntimeError("Kaynak işlem sırasında değişti.")
            with tempfile.TemporaryDirectory(dir=stage) as temp:
                if core.materialize(source, temp)[2] != source.fingerprint:
                    raise RuntimeError("Kaynak bileşenleri işlem sırasında değişti.")
        js = stage / side_json.name
        ht = stage / side_html.name
        js.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        core.write_report(ht, report)
        progress(95, "Doğrulanan ürün yayınlanıyor")
        core.check_cancel(cancel)
        published = []
        try:
            for a, b in [
                (data_path, side_data),
                (js, side_json),
                (ht, side_html),
                (target, output),
            ]:
                os.link(a, b)
                published.append(b)
        except Exception:
            for p in published:
                p.unlink()
            raise
    progress(100, "Tamamlandı.")
    return report
