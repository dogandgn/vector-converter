"""Publish source attributes and metric shape measurements, never audit columns."""

from collections import Counter, defaultdict
from pathlib import Path
import html
import json
import math
import os
import tempfile
import xml.etree.ElementTree as ET  # nosec B405
from osgeo import ogr
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransformContext,
    QgsDistanceArea,
)
from . import core, attribute_schema as schema, safe_xml
from .gdb_writer import geometry_points, ring_sizes

NS = "http://www.opengis.net/kml/2.2"


def source_plan(links, sources):
    values = defaultdict(list)
    for link in links:
        for name, value in json.loads(link["ozgun_alanlar_json"]).items():
            values[name].append(value)
    items = [
        {"source_name": name, "canonical": name, "values": vals}
        for name, vals in sorted(values.items())
    ]
    return schema.default_schema(items, sources, general=True)


def shape_fields(geometry_type, fields):
    existing = {name.casefold() for name, kind in fields}
    flat = ogr.GT_Flatten(geometry_type)
    additions = []
    if flat in (ogr.wkbPolygon, ogr.wkbMultiPolygon):
        additions = ["Shape_Area", "Shape_Length"]
    elif flat in (ogr.wkbLineString, ogr.wkbMultiLineString):
        additions = ["Shape_Length"]
    return [name for name in additions if name.casefold() not in existing]


class ShapeMeasurements:
    """Measure the stored shape in metres, without a degree-unit fallback."""

    def __init__(self, srs):
        self.crs = QgsCoordinateReferenceSystem(srs.ExportToWkt())
        if not self.crs.isValid():
            raise ValueError("Geometri ölçümü için çıktı koordinat sistemi geçersiz.")
        self.meter = None
        if self.crs.isGeographic():
            self.meter = QgsDistanceArea()
            self.meter.setSourceCrs(self.crs, QgsCoordinateTransformContext())
            if (
                not self.meter.setEllipsoid(self.crs.ellipsoidAcronym())
                or not self.meter.willUseEllipsoid()
            ):
                raise ValueError(
                    "Çıktı koordinat sisteminin elipsoidiyle metre ölçümü yapılamadı."
                )
        elif self.crs.mapUnits() != Qgis.DistanceUnit.Meters:
            raise ValueError("Düzlemsel ölçüm için metre koordinat sistemi gerekir.")

    def __call__(self, geometry, names):
        if not names:
            return {}
        shape = core.qgeom(geometry)
        polygon = ogr.GT_Flatten(geometry.GetGeometryType()) in (
            ogr.wkbPolygon,
            ogr.wkbMultiPolygon,
        )
        values = {}
        for name in names:
            if self.meter is None:
                value = geometry.GetArea() if name == "Shape_Area" else shape.length()
            elif name == "Shape_Area":
                value = self.meter.convertAreaMeasurement(
                    self.meter.measureArea(shape), Qgis.AreaUnit.SquareMeters
                )
            else:
                value = self.meter.convertLengthMeasurement(
                    (
                        self.meter.measurePerimeter(shape)
                        if polygon
                        else self.meter.measureLength(shape)
                    ),
                    Qgis.DistanceUnit.Meters,
                )
            if not math.isfinite(value) or value < 0:
                raise ValueError("Geometri için geçerli alan/uzunluk hesaplanamadı.")
            values[name] = value
        return values


def read_model(path, sources, plan, policy, cancel):
    ds = ogr.Open(str(path), 0)
    if ds is None:
        raise RuntimeError("Doğrulanmış işlem modeli açılamadı.")
    layers, tables = {}, {}
    try:
        for layer in ds:
            core.check_cancel(cancel)
            if layer.GetGeomType() == ogr.wkbNone:
                tables[layer.GetName()] = [f.items() for f in layer]
        links = tables["kayit_kaynaklari"]
        plan = plan or source_plan(links, sources)
        schema.validate(plan, sources)
        fields = [f for f in plan["fields"] if f["enabled"]]
        formats = {
            str(row["source_uid"]): row.get("bicim") for row in tables["kaynaklar"]
        }
        # General mode uses the source index as its private source identity.
        for i, source in enumerate(sources):
            if not formats.get(str(i)):
                formats[str(i)] = source.format
        by_uid = defaultdict(list)
        by_source = {str(i): s for i, s in enumerate(sources)}
        by_path = {(s.path, s.member or ""): s for s in sources}
        for row in tables["kaynaklar"]:
            if row.get("dosya"):
                by_source[str(row["source_uid"])] = by_path[
                    (row["dosya"], row.get("zip_uyesi") or "")
                ]
        for link in links:
            by_uid[link["record_uid"]].append(
                {
                    "source_uid": link["source_uid"],
                    "source_fid": link["source_fid"],
                    "format": formats[str(link["source_uid"])],
                    "raw": json.loads(link["ozgun_alanlar_json"]),
                    "metadata": json.loads(link["kaynak_metadata_json"]),
                }
            )
        audit = {"fields": fields, "value_sources": [], "conflicts": []}
        for layer in ds:
            if layer.GetGeomType() == ogr.wkbNone:
                continue
            columns = [(f["name"], schema.TYPES[f["type"]]) for f in fields]
            added = shape_fields(layer.GetGeomType(), columns)
            measure = ShapeMeasurements(layer.GetSpatialRef())
            rows = []
            for f in layer:
                core.check_cancel(cancel)
                uid = f.GetField("record_uid")
                props = {}
                for field in fields:
                    chosen, candidates = schema.pick(
                        field,
                        by_uid[uid],
                        policy,
                        plan["fill_from_other_source"],
                    )
                    if not chosen["present"] and not plan["allow_missing"]:
                        raise ValueError("Seçili kayıtta sütun eksik: " + field["name"])
                    props[field["name"]] = chosen["value"]
                    audit["value_sources"].append(
                        {"record_uid": uid, "field": field["name"], **chosen}
                    )
                    if (
                        len(
                            {
                                core.json_text(c["value"])
                                for c in candidates
                                if c["present"]
                            }
                        )
                        > 1
                    ):
                        audit["conflicts"].append(
                            {
                                "record_uid": uid,
                                "field": field["name"],
                                "candidates": candidates,
                            }
                        )
                geom = f.GetGeometryRef().Clone()
                props.update(measure(geom, added))
                primary = by_uid[uid][0]
                source = by_source[str(primary["source_uid"])]
                rows.append(
                    {
                        "uid": uid,
                        "properties": props,
                        "geometry": geom,
                        "label_context": {
                            "raw": primary["raw"],
                            "metadata": primary["metadata"],
                            "fallback": (source.layer_name or Path(source.path).stem)
                            + " — Kayıt "
                            + str(primary["source_fid"]),
                            "parcel": {
                                k: f.GetField(k)
                                for k in (
                                    "il",
                                    "ilce",
                                    "mahalle",
                                    "ada_no",
                                    "parsel_no",
                                )
                                if f.GetFieldIndex(k) >= 0
                            },
                        },
                    }
                )
            layers[layer.GetName()] = {
                "columns": columns + [(name, ogr.OFTReal) for name in added],
                "added": added,
                "geometry_type": layer.GetGeomType(),
                "srs": layer.GetSpatialRef().Clone(),
                "rows": rows,
            }
        return layers, tables, audit
    finally:
        f = layer = None
        core.close_dataset(ds)


def write_database(path, layers, cancel):
    is_gdb = path.suffix.lower() == ".gdb"
    driver = ogr.GetDriverByName("OpenFileGDB" if is_gdb else "GPKG")
    if driver is None or driver.GetMetadataItem("DCAP_CREATE") != "YES":
        raise ValueError("Çıktı yazma sürücüsü bulunamadı.")
    ds = driver.CreateDataSource(str(path))
    if ds is None:
        raise RuntimeError("Çıktı oluşturulamadı.")
    try:
        for name, model in layers.items():
            core.check_cancel(cancel)
            srs = model["srs"]
            resolution = 1e-9 if srs.IsGeographic() else 0.0001
            options = []
            if is_gdb:
                options = [
                    "TARGET_ARCGIS_VERSION=ALL",
                    "XYSCALE=" + str(1 / resolution),
                    "XYTOLERANCE=" + str(resolution * 10),
                ]
            layer = ds.CreateLayer(
                name,
                srs=srs,
                geom_type=model["geometry_type"],
                options=options,
            )
            if layer is None:
                raise RuntimeError("Katman oluşturulamadı: " + name)
            for key, kind in model["columns"]:
                field = ogr.FieldDefn(key, kind)
                if is_gdb and kind == ogr.OFTString:
                    field.SetWidth(
                        max(
                            1,
                            max(
                                (
                                    len(str(r["properties"][key]).encode("utf-8"))
                                    for r in model["rows"]
                                    if r["properties"][key] is not None
                                ),
                                default=1,
                            ),
                        )
                    )
                if layer.CreateField(field) != 0:
                    raise RuntimeError("Kaynak alanı oluşturulamadı: " + key)
            for row in model["rows"]:
                core.check_cancel(cancel)
                core.add_row(layer, row["properties"], row["geometry"])
            layer = None
    finally:
        core.close_dataset(ds)
    if is_gdb:
        # Metric measurements refer to the stored grid-rounded shape, not a
        # pre-write geometry. Existing source measurement columns stay intact.
        ds = ogr.Open(str(path), 1)
        try:
            for name, model in layers.items():
                layer = ds.GetLayerByName(name)
                measure = ShapeMeasurements(layer.GetSpatialRef())
                for actual, row in zip(layer, model["rows"]):
                    core.check_cancel(cancel)
                    values = measure(actual.GetGeometryRef(), model["added"])
                    for key, value in values.items():
                        actual.SetField(key, value)
                        row["properties"][key] = value
                    if values and layer.SetFeature(actual) != 0:
                        raise RuntimeError("Geometri ölçümü kaydedilemedi.")
                actual = layer = None
        finally:
            core.close_dataset(ds)


def feature_rows(layers):
    from .exchange_writer import geometry_object, oriented

    for name, model in layers.items():
        for row in model["rows"]:
            geom = row["geometry"]
            flat = ogr.GT_Flatten(geom.GetGeometryType())
            obj = (
                oriented(geom)
                if flat in (ogr.wkbPolygon, ogr.wkbMultiPolygon)
                else geometry_object(geom)
            )
            yield name, {
                "type": "Feature",
                "geometry": obj,
                "properties": row["properties"],
            }


def display_value(value):
    if value is None or isinstance(value, (bool, list, dict)):
        return ""
    return str(value).strip()


def name_kml_records(layers, general=False, field=None):
    """KML display names are independent of the source attribute schema."""
    if field is not None and (
        not general or not all(field in dict(m["columns"]) for m in layers.values())
    ):
        raise ValueError("KML kayıt adı için geçerli bir çıktı sütunu seçin.")
    for model in layers.values():
        for row in model["rows"]:
            context = row["label_context"]
            value = ""
            if general:
                if field is not None:
                    value = display_value(row["properties"].get(field))
                else:
                    value = display_value(context["metadata"].get("name"))
                    raw = context["raw"]
                    for key in (
                        "name",
                        "ad",
                        "adi",
                        "isim",
                        "title",
                        "baslik",
                        "label",
                        "etiket",
                    ):
                        if value:
                            break
                        value = next(
                            (
                                display_value(v)
                                for k, v in raw.items()
                                if schema.portable_name(k).casefold() == key
                                and display_value(v)
                            ),
                            "",
                        )
            else:
                p = context["parcel"]
                location = " / ".join(
                    display_value(p.get(k))
                    for k in ("il", "ilce", "mahalle")
                    if display_value(p.get(k))
                )
                identity = ", ".join(
                    label + " " + display_value(p.get(k))
                    for k, label in (
                        ("ada_no", "Ada"),
                        ("parsel_no", "Parsel"),
                    )
                    if display_value(p.get(k))
                )
                value = " — ".join(v for v in (location, identity) if v)
            row["display_name"] = value or context["fallback"]


def write_kml(path, layers, cancel):
    from .exchange_writer import geometry_object

    def node(parent, tag, value=None, **attrs):
        child = ET.SubElement(parent, tag, attrs)
        if value is not None:
            child.text = str(value)
        return child

    def geometry(parent, obj):
        kind, coords = obj["type"], obj["coordinates"]
        if kind.startswith("Multi"):
            multi = node(parent, "MultiGeometry")
            for part in coords:
                geometry(multi, {"type": kind[5:], "coordinates": part})
        elif kind == "Polygon":
            polygon = node(parent, "Polygon")
            for i, ring in enumerate(coords):
                boundary = node(
                    polygon, "outerBoundaryIs" if i == 0 else "innerBoundaryIs"
                )
                node(
                    node(boundary, "LinearRing"),
                    "coordinates",
                    " ".join(f"{x!r},{y!r}" for x, y in ring),
                )
        else:
            node(
                node(parent, kind),
                "coordinates",
                " ".join(
                    f"{x!r},{y!r}" for x, y in ([coords] if kind == "Point" else coords)
                ),
            )

    root = ET.Element("kml", {"xmlns": NS})
    doc = node(root, "Document")
    for name, model in layers.items():
        definition = node(doc, "Schema", id=name, name=name)
        for key, kind in model["columns"]:
            node(
                definition,
                "SimpleField",
                name=key,
                type={ogr.OFTInteger: "int", ogr.OFTReal: "double"}.get(kind, "string"),
            )
        folder = node(doc, "Folder")
        node(folder, "name", name)
        for row in model["rows"]:
            core.check_cancel(cancel)
            pm = node(folder, "Placemark")
            node(pm, "name", row["display_name"])
            body = []
            for key, value in row["properties"].items():
                unit = (
                    (
                        " (m²)"
                        if key == "Shape_Area"
                        else " (metre)" if key == "Shape_Length" else ""
                    )
                    if key in model["added"]
                    else ""
                )
                body.append(
                    "<tr><th>"
                    + html.escape(key + unit)
                    + "</th><td>"
                    + html.escape(str(value) if value is not None else "—")
                    + "</td></tr>"
                )
            node(
                pm,
                "description",
                '<table border="1" cellpadding="5" cellspacing="0">'
                + "".join(body)
                + "</table>",
            )
            data = node(node(pm, "ExtendedData"), "SchemaData", schemaUrl="#" + name)
            for key, value in row["properties"].items():
                if value is not None:
                    node(data, "SimpleData", value, name=key)
            geometry(pm, geometry_object(row["geometry"]))
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def verify_geometry(expected, actual, resolution):
    if (
        actual is None
        or actual.IsEmpty()
        or not actual.IsValid()
        or ring_sizes(expected) != ring_sizes(actual)
    ):
        raise RuntimeError("Çıktı geometrisi veya parça yapısı değişti.")
    if not resolution:
        actual = actual.Clone()
        actual.FlattenTo2D()
        if Counter(geometry_points(expected)) != Counter(geometry_points(actual)):
            raise RuntimeError("Çıktı koordinatları değişti.")
        if not core.qgeom(expected).isGeosEqual(core.qgeom(actual)):
            raise RuntimeError("Çıktı geometrisi değişti.")
        return
    flat = ogr.GT_Flatten(expected.GetGeometryType())
    limit = resolution * 2
    if flat in (ogr.wkbPolygon, ogr.wkbMultiPolygon):
        if core.qgeom(expected).hausdorffDistance(core.qgeom(actual)) > limit:
            raise RuntimeError("FileGDB sınır farkı ızgarayı aşıyor.")
    else:
        if any(
            math.hypot(x - u, y - v) > limit
            for (x, y), (u, v) in zip(
                geometry_points(expected), geometry_points(actual)
            )
        ):
            raise RuntimeError("FileGDB nokta/çizgi veya yön değişti.")


def verify(path, layers, cancel):
    suffix = path.suffix.lower()
    records = sum(len(m["rows"]) for m in layers.values())
    if suffix == ".geojson":
        actual = json.loads(path.read_text(encoding="utf-8"))
        expected = {
            "type": "FeatureCollection",
            "features": [f for name, f in feature_rows(layers)],
        }
        if actual != expected:
            raise RuntimeError("GeoJSON kaynak alanları veya değerleri değişti.")
    elif suffix == ".kml":
        root = safe_xml.parse(path)
        for folder, (name, model) in zip(
            root.findall(".//{" + NS + "}Folder"), layers.items()
        ):
            placemarks = folder.findall("{" + NS + "}Placemark")
            if len(placemarks) != len(model["rows"]):
                raise RuntimeError("KML kayıt sayısı değişti.")
            for pm, row in zip(placemarks, model["rows"]):
                core.check_cancel(cancel)
                if pm.findtext("{" + NS + "}name") != row["display_name"]:
                    raise RuntimeError("KML kayıt adı değişti veya eksik.")
                actual = {
                    item.attrib["name"]: item.text or ""
                    for item in pm.findall(".//{" + NS + "}SimpleData")
                }
                expected = {
                    k: str(v) for k, v in row["properties"].items() if v is not None
                }
                if actual != expected:
                    raise RuntimeError("KML kaynak alanları değişti.")
        if len(root.findall(".//{" + NS + "}Placemark")) != records:
            raise RuntimeError("KML kayıt sayısı değişti.")
    ds = ogr.Open(str(path), 0)
    if ds is None:
        raise RuntimeError("Temiz çıktı yeniden açılamadı.")
    checked = 0
    try:
        if suffix in (".gpkg", ".gdb") and ds.GetLayerCount() != len(layers):
            raise RuntimeError("Çıktıda beklenmeyen işlem tablosu var.")
        for index, (name, model) in enumerate(layers.items()):
            # Mixed GeoJSON is a single feature collection, verified as a whole.
            if suffix == ".geojson":
                continue
            layer = (
                ds.GetLayerByName(name)
                if suffix in (".gpkg", ".gdb")
                else ds.GetLayer(index)
            )
            if layer is None or layer.GetFeatureCount() != len(model["rows"]):
                raise RuntimeError("Çıktı katman/kayıt sayısı değişti.")
            srs = layer.GetSpatialRef()
            if srs is None or not srs.IsSame(
                model["srs"],
                [
                    "IGNORE_DATA_AXIS_TO_SRS_AXIS_MAPPING=YES",
                    "CRITERION=EQUIVALENT_EXCEPT_AXIS_ORDER_GEOGCRS",
                ],
            ):
                raise RuntimeError("Çıktı koordinat tanımı değişti.")
            if (
                suffix in (".gpkg", ".gdb")
                and [(f.name, f.GetType()) for f in layer.schema] != model["columns"]
            ):
                raise RuntimeError("Çıktıya beklenmeyen öznitelik eklendi.")
            resolution = (
                (1e-9 if model["srs"].IsGeographic() else 0.0001)
                if suffix == ".gdb"
                else 0
            )
            for f, row in zip(layer, model["rows"]):
                core.check_cancel(cancel)
                if suffix in (".gpkg", ".gdb") and f.items() != row["properties"]:
                    raise RuntimeError("Kaynak öznitelik değeri değişti.")
                verify_geometry(row["geometry"], f.GetGeometryRef(), resolution)
                checked += 1
        if suffix == ".geojson":
            layer = ds.GetLayer(0)
            expected_rows = [row for model in layers.values() for row in model["rows"]]
            if (
                layer.GetFeatureCount() != records
                or not layer.GetSpatialRef().IsGeographic()
            ):
                raise RuntimeError("GeoJSON kayıt/koordinat denetimi başarısız.")
            for f, row in zip(layer, expected_rows):
                core.check_cancel(cancel)
                verify_geometry(row["geometry"], f.GetGeometryRef(), 0)
                checked += 1
        if checked != records:
            raise RuntimeError("Çıktı kaydı eksik.")
    finally:
        f = layer = None
        core.close_dataset(ds)
    return {
        "reopened": True,
        "records": checked,
        "field_values_verified": True,
        "geometry_verified": True,
        "source_attributes_only": True,
        "counts": {name: len(m["rows"]) for name, m in layers.items()},
    }


def convert(
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
    general=False,
    kml_name_field=None,
):
    output = Path(output).resolve()
    suffix = output.suffix.lower()
    if suffix not in (".gpkg", ".gdb", ".geojson", ".kml"):
        raise ValueError("Desteklenmeyen çıktı biçimi.")
    if suffix == ".gdb" and os.name != "nt":
        raise ValueError("FileGDB yazımı bu sürümde Windows ile sınırlı.")
    if suffix in (".kml", ".geojson") and profile_id != "wgs84":
        raise ValueError("KML/GeoJSON için WGS84 derece gerekir.")
    if any(output.is_relative_to(Path(p).resolve()) for p in protected):
        raise ValueError("Girdi veya eklenti klasörüne çıktı yazılmaz.")
    finals = [output] + [
        output.with_suffix(ext) for ext in (".rapor.json", ".rapor.html", ".veri.json")
    ]
    if any(os.path.lexists(p) for p in finals):
        raise ValueError("Çıktı veya yan dosya mevcut; yeni ad seçin.")
    if attribute_schema is not None:
        schema.validate(attribute_schema, sources)
    core.check_cancel(cancel)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".clean_output_", dir=output.parent
    ) as folder:
        stage = Path(folder)
        canonical = stage / "_internal" / "model.gpkg"
        options = {
            "cancel": cancel,
            "progress": lambda n, s: progress(n * 0.65, s),
            "protected": protected,
            "merge_mode": merge_mode,
            "attribute_policy": attribute_policy,
        }
        if general:
            from .vector_export import _convert_model

            report = _convert_model(sources, canonical, profile_id, **options)
        else:
            report = core._convert_gpkg(
                sources,
                canonical,
                profile_id,
                missing_crs,
                crs_basis,
                crs_evidence,
                **options,
            )
        layers, tables, audit = read_model(
            canonical, sources, attribute_schema, attribute_policy, cancel
        )
        path = stage / output.name
        if suffix in (".gpkg", ".gdb"):
            write_database(path, layers, cancel)
        elif suffix == ".geojson":
            path.write_text(
                core.json_text(
                    {
                        "type": "FeatureCollection",
                        "features": [f for name, f in feature_rows(layers)],
                    }
                ),
                encoding="utf-8",
            )
        else:
            name_kml_records(layers, general, kml_name_field)
            write_kml(path, layers, cancel)
        progress(85, "Kaynak öznitelikleri ve geometri yeniden doğrulanıyor.")
        report["canonical_validation"] = report.pop("validation")
        report["validation"] = verify(path, layers, cancel)
        report["output"] = str(output)
        report["output_format"] = {
            ".gpkg": "GeoPackage",
            ".gdb": "OpenFileGDB",
            ".geojson": "GeoJSON RFC7946",
            ".kml": "KML 2.2",
        }[suffix]
        report["attribute_schema"] = audit
        report["output_layers"] = list(layers)
        target = QgsCoordinateReferenceSystem(core.PROFILES[profile_id]["crs"])
        report["public_schema"] = {
            "source_attributes_only": True,
            "columns": {
                name: [key for key, kind in model["columns"]]
                for name, model in layers.items()
            },
            "generated_shape_fields": {
                name: model["added"] for name, model in layers.items()
            },
            "measurement_units": "m² / m",
            "measurement_method": (
                "ellipsoidal" if target.isGeographic() else "planar"
            ),
            "measurement_ellipsoid": (
                target.ellipsoidAcronym() if target.isGeographic() else None
            ),
            "source_measurements_preserved": True,
            "audit_in_data_layer": False,
            "kml_display_names": (
                {
                    "method": ("source_or_layer" if general else "parcel_identity"),
                    "field": kml_name_field,
                    "attribute_column_added": False,
                    "attribute_popup": True,
                }
                if suffix == ".kml"
                else None
            ),
        }
        report["storage"] = {
            "companion": str(finals[3]),
            "note": "Ana dosya yalnız kaynak öznitelikleri ve eksikse geometri ölçümleri içerir. İşlem/kaynak bilgileri ve tam tipli değerler .veri.json ve raporlarda tutulur.",
        }
        companion = {
            "mode": "vector" if general else "parcel",
            "tables": tables,
            "output_layers": list(layers),
            "records": [
                {
                    "layer": name,
                    "row": i + 1,
                    "record_uid": row["uid"],
                    "properties": row["properties"],
                    **(
                        {"display_name": row["display_name"]}
                        if "display_name" in row
                        else {}
                    ),
                }
                for name, model in layers.items()
                for i, row in enumerate(model["rows"])
            ],
        }
        for row in tables.get("islem_ozeti", []):
            row["rapor_json"] = core.json_text(report)
        data = stage / finals[3].name
        data.write_text(core.json_text(companion), encoding="utf-8")
        if json.loads(data.read_text(encoding="utf-8")) != companion:
            raise RuntimeError("Kaynak bilgisi yan dosyası değişti.")
        for source in sources:
            core.check_cancel(cancel)
            if source.status != "ready":
                continue
            if core.sha(source.path) != source.file_sha256:
                raise RuntimeError("Kaynak aktarım sırasında değişti.")
            with tempfile.TemporaryDirectory(dir=stage) as temp:
                if core.materialize(source, temp)[2] != source.fingerprint:
                    raise RuntimeError("Kaynak bileşeni aktarım sırasında değişti.")
        (stage / finals[1].name).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        from .comparison import html_section

        title = "Genel vektör dönüşüm raporu" if general else "Parsel dönüşüm raporu"
        columns = "".join(
            "<tr><td>"
            + html.escape(name)
            + "</td><td>"
            + html.escape(", ".join(key for key, kind in model["columns"]))
            + "</td></tr>"
            for name, model in layers.items()
        )
        body = (
            '<!doctype html><html lang="tr"><meta charset="utf-8"><title>'
            + title
            + "</title><style>body{font:16px system-ui;max-width:1100px;margin:32px auto;padding:20px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #cad7da;padding:8px}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><h1>"
            + title
            + "</h1><p>"
            + html.escape(str(output))
            + "</p>"
            + html_section(report)
            + "<h2>Çıktı sütunları</h2><p>Yalnız seçilen kaynak öznitelikleri ve eksikse geometri ölçümleri. Kaynak ölçüm sütunları değiştirilmedi. Yeni ölçüm birimleri: "
            + html.escape(report["public_schema"]["measurement_units"])
            + ". "
            + (
                "Derece koordinatları çıktı sisteminin elipsoidi üzerinde ölçülür."
                if target.isGeographic()
                else "Metre koordinatları düzlemsel olarak ölçülür."
            )
            + " Kaynakta zaten bulunan ölçüm sütunlarının birimi kaynak veriye aittir.</p><table><tr><th>Katman</th><th>Sütunlar</th></tr>"
            + columns
            + "</table><h2>İşlem ve kaynak bilgileri</h2><p>Bu bilgiler ana veri katmanına sütun olarak eklenmez; raporlar ve .veri.json dosyasında tutulur.</p><details><summary>Denetim ayrıntıları</summary><pre>"
            + html.escape(json.dumps(report, ensure_ascii=False, indent=2))
            + "</pre></details></html>"
        )
        (stage / finals[2].name).write_text(body, encoding="utf-8")
        published = []
        try:
            for final in finals[1:]:
                core.check_cancel(cancel)
                os.link(stage / final.name, final)
                published.append(final)
            core.check_cancel(cancel)
            if suffix == ".gdb":
                os.rename(path, output)
            else:
                os.link(path, output)
        except Exception:
            for final in published:
                final.unlink()
            raise
    progress(100, "Tamamlandı.")
    return report
