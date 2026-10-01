"""General vector outputs with typed geometry layers and lossless provenance."""

from pathlib import Path
from dataclasses import asdict
from datetime import datetime, timezone
import html, json, math, os, tempfile, uuid
import xml.etree.ElementTree as ET  # nosec B405
from osgeo import ogr, osr
from qgis.core import (
    QgsGeometry,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransformContext,
    QgsDistanceArea,
)
from . import (
    core,
    vector_review as review,
    attribute_schema as schema,
    comparison,
)
from .gdb_writer import geometry_points, ring_sizes

SYSTEM = [
    ("record_uid", ogr.OFTString),
    ("geometri_turu", ogr.OFTString),
    ("kaynak_sayisi", ogr.OFTInteger),
    ("hesap_alan_m2", ogr.OFTReal),
    ("hesap_cevre_m", ogr.OFTReal),
    ("hesap_uzunluk_m", ogr.OFTReal),
]
GEOMETRIES = {
    "noktalar": ogr.wkbMultiPoint,
    "cizgiler": ogr.wkbMultiLineString,
    "poligonlar": ogr.wkbMultiPolygon,
}


def multi(geom, kind):
    if geom.GetGeometryType() == GEOMETRIES[kind]:
        return geom
    result = ogr.Geometry(GEOMETRIES[kind])
    result.AddGeometry(geom)
    return result


def write_database(path, features, fields, tables, target, cancel):
    driver = ogr.GetDriverByName("OpenFileGDB" if path.suffix == ".gdb" else "GPKG")
    if driver is None or driver.GetMetadataItem("DCAP_CREATE") != "YES":
        raise ValueError("Çıktı yazma sürücüsü bulunamadı.")
    ds = driver.CreateDataSource(str(path))
    if ds is None:
        raise RuntimeError("Veritabanı oluşturulamadı.")
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(int(target.authid().split(":")[1]))
    resolution = 1e-9 if target.isGeographic() else 0.0001
    try:
        layers = [
            (
                name,
                [
                    (
                        f["properties"],
                        ogr.CreateGeometryFromJson(json.dumps(f["geometry"])),
                    )
                    for f in features
                    if f["layer"] == name
                ],
                SYSTEM + fields,
                GEOMETRIES[name],
            )
            for name in GEOMETRIES
            if any(f["layer"] == name for f in features)
        ]
        layers += [
            (
                name,
                [(row, None) for row in rows],
                (
                    [(key, ogr.OFTString) for key in rows[0]]
                    if rows
                    else [("aciklama", ogr.OFTString)]
                ),
                ogr.wkbNone,
            )
            for name, rows in tables.items()
        ]
        if path.suffix == ".gpkg" and ds.StartTransaction() != 0:
            raise RuntimeError("Veritabanı işlemi başlatılamadı.")
        for name, rows, columns, geometry in layers:
            core.check_cancel(cancel)
            options = []
            if path.suffix == ".gdb":
                options = ["TARGET_ARCGIS_VERSION=ALL"]
                if geometry != ogr.wkbNone:
                    options += [
                        "XYSCALE=" + str(1 / resolution),
                        "XYTOLERANCE=" + str(resolution * 10),
                    ]
            layer = ds.CreateLayer(
                name,
                srs=srs if geometry != ogr.wkbNone else None,
                geom_type=geometry,
                options=options,
            )
            if layer is None:
                raise RuntimeError("Katman oluşturulamadı: " + name)
            for key, kind in columns:
                field = ogr.FieldDefn(key, kind)
                if path.suffix == ".gdb" and kind == ogr.OFTString:
                    field.SetWidth(
                        max(
                            1,
                            max(
                                (
                                    len(str(values[key]).encode("utf-8"))
                                    for values, g in rows
                                    if values.get(key) is not None
                                ),
                                default=1,
                            ),
                        )
                    )
                if layer.CreateField(field) != 0:
                    raise RuntimeError("Alan oluşturulamadı: " + key)
            for values, geom in rows:
                core.check_cancel(cancel)
                core.add_row(layer, values, geom)
            layer = None
        if path.suffix == ".gpkg" and ds.CommitTransaction() != 0:
            raise RuntimeError("Veritabanı işlemi tamamlanamadı.")
    finally:
        core.close_dataset(ds)
    return resolution if path.suffix == ".gdb" else 0


def write_kml(path, features):
    def node(parent, tag, value=None, **attrs):
        child = ET.SubElement(parent, tag, attrs)
        if value is not None:
            child.text = str(value)
        return child

    root = ET.Element("kml", {"xmlns": "http://www.opengis.net/kml/2.2"})
    doc = node(root, "Document")
    node(doc, "name", "Vektörler")
    folders = {}

    def geometry(parent, obj):
        kind = obj["type"]
        coords = obj["coordinates"]
        if kind.startswith("Multi"):
            multi_node = node(parent, "MultiGeometry")
            for part in coords:
                geometry(multi_node, {"type": kind[5:], "coordinates": part})
        elif kind == "Polygon":
            poly = node(parent, "Polygon")
            for i, ring in enumerate(coords):
                node(
                    node(
                        node(
                            poly,
                            "outerBoundaryIs" if i == 0 else "innerBoundaryIs",
                        ),
                        "LinearRing",
                    ),
                    "coordinates",
                    " ".join(f"{x!r},{y!r}" for x, y in ring),
                )
        else:
            part = node(parent, kind)
            points = [coords] if kind == "Point" else coords
            node(
                part,
                "coordinates",
                " ".join(f"{x!r},{y!r}" for x, y in points),
            )

    for feature in features:
        name = feature["layer"]
        if name not in folders:
            folders[name] = node(doc, "Folder")
            node(folders[name], "name", name)
        pm = node(folders[name], "Placemark")
        node(pm, "name", feature["properties"]["record_uid"])
        ext = node(pm, "ExtendedData")
        for key, value in feature["properties"].items():
            if value is not None:
                node(node(ext, "Data", name=key), "value", value)
        node(
            node(ext, "Data", name="vektor_oznitelikleri_json"),
            "value",
            core.json_text(feature["properties"]),
        )
        geometry(pm, feature["geometry"])
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def verify(path, features, tables, target, tolerance, cancel):
    expected = {f["properties"]["record_uid"]: f for f in features}
    seen = set()
    ds = None
    counts = {}
    if path.suffix == ".geojson":
        obj = json.loads(path.read_text(encoding="utf-8"))
        wanted = [
            {k: f[k] for k in ("type", "geometry", "properties")} for f in features
        ]
        if obj != {"type": "FeatureCollection", "features": wanted}:
            raise RuntimeError("GeoJSON kayıt/alan doğrulaması başarısız.")
    if path.suffix == ".kml":
        from .kml_reader import Dataset

        source = Dataset(path, general=True)
        for f in source:
            core.check_cancel(cancel)
            if f.error:
                raise RuntimeError(f.error)
            attrs = json.loads(f.raw["vektor_oznitelikleri_json"])
            uid = attrs["record_uid"]
            if uid in seen or uid not in expected:
                raise RuntimeError("KML kimlik doğrulaması başarısız.")
            if attrs != expected[uid]["properties"]:
                raise RuntimeError("KML alan değerleri değişmiş.")
            wanted = ogr.CreateGeometryFromJson(json.dumps(expected[uid]["geometry"]))
            if review.geometry_key(f.geom) != review.geometry_key(wanted):
                raise RuntimeError("KML geometrisi değişmiş.")
            seen.add(uid)
    else:
        ds = ogr.Open(str(path), 0)
        if ds is None:
            raise RuntimeError("Çıktı tekrar açılamadı.")
        try:
            for i in range(ds.GetLayerCount()):
                layer = ds.GetLayer(i)
                name = layer.GetName()
                counts[name] = layer.GetFeatureCount()
                if name in tables:
                    actual = [f.items() for f in layer]
                    wanted = tables[name]
                    if actual != wanted:
                        raise RuntimeError("İzleme tablosu değişmiş: " + name)
                    continue
                actual_crs = layer.GetSpatialRef()
                if actual_crs is None:
                    raise RuntimeError("Çıktı koordinatı bulunamadı.")
                desired = osr.SpatialReference()
                desired.ImportFromEPSG(int(target.authid().split(":")[1]))
                if not actual_crs.IsSame(
                    desired,
                    [
                        "IGNORE_DATA_AXIS_TO_SRS_AXIS_MAPPING=YES",
                        "CRITERION=EQUIVALENT_EXCEPT_AXIS_ORDER_GEOGCRS",
                    ],
                ):
                    raise RuntimeError("Çıktı koordinatı değişmiş.")
                for f in layer:
                    core.check_cancel(cancel)
                    uid = f.GetField("record_uid")
                    if uid in seen or uid not in expected:
                        raise RuntimeError("Çıktı kayıt kimliği değişmiş.")
                    item = expected[uid]
                    if f.items() != item["properties"]:
                        raise RuntimeError("Çıktı öznitelikleri değişmiş.")
                    a = ogr.CreateGeometryFromJson(json.dumps(item["geometry"]))
                    b = f.GetGeometryRef()
                    if (
                        b is None
                        or b.IsEmpty()
                        or not core.qgeom(b).isGeosValid()
                        or ring_sizes(a) != ring_sizes(b)
                    ):
                        raise RuntimeError("Çıktı geometrisi geçersiz.")
                    if not tolerance:
                        if review.geometry_key(a) != review.geometry_key(b):
                            raise RuntimeError("Çıktı geometrisi değişmiş.")
                    else:
                        pa = list(geometry_points(a))
                        pb = list(geometry_points(b))
                        limit = tolerance * 2

                        def near(left, right):
                            grid = {}
                            for x, y in right:
                                grid.setdefault(
                                    (
                                        math.floor(x / limit),
                                        math.floor(y / limit),
                                    ),
                                    [],
                                ).append((x, y))
                            for x, y in left:
                                cell = (
                                    math.floor(x / limit),
                                    math.floor(y / limit),
                                )
                                if not any(
                                    math.hypot(x - u, y - v) <= limit
                                    for dx in (-1, 0, 1)
                                    for dy in (-1, 0, 1)
                                    for u, v in grid.get(
                                        (cell[0] + dx, cell[1] + dy), ()
                                    )
                                ):
                                    return False
                            return True

                        if not near(pa, pb) or not near(pb, pa):
                            raise RuntimeError(
                                "FileGDB ızgara farkı beklenen sınırı aşıyor."
                            )
                        if item["layer"] == "cizgiler" and (
                            len(pa) != len(pb)
                            or any(
                                math.hypot(x - u, y - v) > limit
                                for (x, y), (u, v) in zip(pa, pb)
                            )
                        ):
                            raise RuntimeError("Çizgi yönü veya parça sırası değişti.")
                        if (
                            item["layer"] == "poligonlar"
                            and core.qgeom(a).hausdorffDistance(core.qgeom(b)) > limit
                        ):
                            raise RuntimeError("Poligon sınırları değişti.")
                    seen.add(uid)
        finally:
            layer = f = None
            core.close_dataset(ds)
    if seen != set(expected):
        raise RuntimeError("Çıktıda eksik kayıt var.")
    return {
        "reopened": True,
        "records": len(seen),
        "field_values_verified": True,
        "geometry_verified": True,
        "xy_resolution": tolerance,
        "counts": counts,
    }


def convert(sources, output, *args, **kwargs):
    from .public_output import convert as publish_source_data

    return publish_source_data(sources, output, *args, general=True, **kwargs)


def _convert_model(
    sources,
    output,
    profile_id="wgs84",
    cancel=lambda: False,
    progress=lambda n, s: None,
    protected=(),
    merge_mode="separate",
    attribute_policy="preserve",
    attribute_schema=None,
):
    core.preflight()
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
    finals = [
        output,
        output.with_suffix(".rapor.json"),
        output.with_suffix(".rapor.html"),
    ]
    if suffix in (".kml", ".geojson"):
        finals.append(output.with_suffix(".veri.json"))
    if any(os.path.lexists(p) for p in finals):
        raise ValueError("Çıktı veya yan dosya mevcut; yeni ad seçin.")
    if merge_mode not in ("separate", "merge"):
        raise ValueError("Geçersiz birleştirme seçimi.")
    model = review.build(sources, cancel, lambda n, s: progress(n * 0.35, s))
    if model["blocked"]:
        raise ValueError(
            "Seçili girdilerde çözümlenemeyen kayıtlar var: "
            + model["blocked"][0]["message"]
        )
    rows = [r for g in model["groups"] for r in g["records"]]
    if not rows:
        raise ValueError("Aktarılacak geçerli vektör kaydı yok.")
    selected = {(r["source_index"], r["fid"]) for r in rows}
    plan = attribute_schema or schema.default_schema(
        schema.inventory(sources, model, selected), sources, general=True
    )
    schema.validate(plan, sources)
    if any(f["name"] in {k for k, t in SYSTEM} for f in plan["fields"] if f["enabled"]):
        raise ValueError("Çıktı sütunu sistem alanıyla çakışıyor.")
    fields = [
        (f["name"], schema.TYPES[f["type"]]) for f in plan["fields"] if f["enabled"]
    ]
    profile = core.PROFILES[profile_id]
    target = QgsCoordinateReferenceSystem(profile["crs"])
    cache = {}
    features = []
    links = []
    operations = {}
    groups = (
        [g["records"] for g in model["groups"]]
        if merge_mode == "merge"
        else [[r] for r in rows]
    )
    for gi, group in enumerate(groups):
        core.check_cancel(cancel)
        r = group[0]
        src = QgsCoordinateReferenceSystem.fromWkt(r["source_wkt"])
        qg = QgsGeometry()
        qg.fromWkb(bytes.fromhex(r["source_geometry"]))
        geo = QgsGeometry()
        geo.fromWkb(bytes.fromhex(r["geographic"]))
        if not target.bounds().isNull() and not target.bounds().contains(
            geo.boundingBox()
        ):
            raise ValueError("Kayıt hedef koordinat sisteminin kullanım alanı dışında.")
        projected, operation = review.transformed(
            qg, src, target, cache, geo.boundingBox()
        )
        operations[operation["pipeline"]] = operation
        if projected.isEmpty() or not projected.isGeosValid():
            raise ValueError("Dönüşüm sonrası geometri geçersiz.")
        kind = r["kind"]
        uid = str(uuid.uuid4())
        props = {key: None for key, t in SYSTEM}
        props.update(
            record_uid=uid,
            geometri_turu=review.LABELS[kind],
            kaynak_sayisi=len(group),
        )
        meter = QgsDistanceArea()
        meter.setSourceCrs(src, QgsCoordinateTransformContext())
        meter.setEllipsoid(src.ellipsoidAcronym())
        planar = profile["measurement"] == "planar"
        if kind == "poligonlar":
            props["hesap_alan_m2"] = (
                projected.area() if planar else meter.measureArea(qg)
            )
            props["hesap_cevre_m"] = (
                projected.length() if planar else meter.measurePerimeter(qg)
            )
        if kind == "cizgiler":
            props["hesap_uzunluk_m"] = (
                projected.length() if planar else meter.measureLength(qg)
            )
        if any(
            v is not None and (not math.isfinite(v) or v <= 0)
            for k, v in props.items()
            if k.startswith("hesap_")
        ):
            raise ValueError("Geçersiz ölçüm sonucu.")
        members = [
            {
                "source_uid": str(x["source_index"]),
                "source_fid": x["fid"],
                "format": x["format"],
                "raw": x["raw"],
            }
            for x in group
        ]
        for field in plan["fields"]:
            if not field["enabled"]:
                continue
            chosen, candidates = schema.pick(
                field,
                members,
                attribute_policy,
                plan["fill_from_other_source"],
            )
            if not plan["allow_missing"] and any(not c["present"] for c in candidates):
                raise ValueError("Seçili kayıtta sütun eksik: " + field["name"])
            props[field["name"]] = chosen["value"]
        geom = multi(ogr.CreateGeometryFromWkb(bytes(projected.asWkb())), kind)
        from .exchange_writer import geometry_object

        obj = geometry_object(geom)
        if suffix == ".geojson" and kind == "poligonlar":
            from .exchange_writer import oriented

            obj = oriented(geom)
        features.append(
            {
                "type": "Feature",
                "layer": kind,
                "geometry": obj,
                "properties": props,
            }
        )
        for x in group:
            links.append(
                {
                    "record_uid": uid,
                    "source_uid": str(x["source_index"]),
                    "source_fid": x["fid"],
                    "ozgun_alanlar_json": core.json_text(x["raw"]),
                    "kaynak_metadata_json": core.json_text(x["metadata"]),
                }
            )
        progress(
            35 + gi / max(1, len(groups)) * 35,
            "Vektör kayıtları hazırlanıyor.",
        )
    source_rows = []
    for i, s in enumerate(sources):
        selected_count = (
            (len(s.selected_fids) if s.selected_fids is not None else s.count)
            if s.status == "ready"
            else 0
        )
        row = comparison.source_row(s, selected_count)
        own = [x for x in links if x["source_uid"] == str(i)]
        row.update(
            exported=len(own),
            rejected=selected_count - len(own),
            output_records=len({x["record_uid"] for x in own}),
            output_status="Aktarıldı" if own else "Seçim dışı",
            notes=s.error or "",
        )
        source_rows.append(row)
    report = {
        "mode": "vector",
        "version": core.VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "output": str(output),
        "profile": profile,
        "output_layers": list(dict.fromkeys(f["layer"] for f in features)),
        "sources": [asdict(s) for s in sources],
        "merge_mode": merge_mode,
        "attribute_schema": plan,
        "operations": list(operations.values()),
        "layer_similarities": model["layer_similarities"],
        "summary": {
            "aktarilan_parsel": len(features),
            "aktarilan_kayit": len(features),
            "kaynak_baglantisi": len(links),
            "oznitelik_farki": 0,
            "aktarilamayan_kayit": 0,
        },
        "source_comparison": {
            "rows": source_rows,
            "totals": comparison.summarize(source_rows, len(features)),
            "state": "Tamamlandı",
        },
    }
    tables = {
        "kaynaklar": [
            {
                "source_uid": str(i),
                "dosya": s.path,
                "zip_uyesi": s.member,
                "katman": s.layer_name,
                "kaynak_crs": s.wkt or s.assigned_crs,
                "metadata_json": core.json_text(asdict(s)),
            }
            for i, s in enumerate(sources)
        ],
        "kayit_kaynaklari": links,
        "kontroller": [{"aciklama": review.MATCH_NOTE}],
        "islem_ozeti": [{"surum": core.VERSION, "rapor_json": core.json_text(report)}],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".vector_output_", dir=output.parent
    ) as folder:
        stage = Path(folder)
        path = stage / output.name
        tolerance = 0
        if suffix in (".gpkg", ".gdb"):
            tolerance = write_database(path, features, fields, tables, target, cancel)
        elif suffix == ".geojson":
            path.write_text(
                core.json_text(
                    {
                        "type": "FeatureCollection",
                        "features": [
                            {k: f[k] for k in ("type", "geometry", "properties")}
                            for f in features
                        ],
                    }
                ),
                encoding="utf-8",
            )
        else:
            write_kml(path, features)
        progress(80, "Çıktı yeniden okunarak doğrulanıyor.")
        report["validation"] = verify(path, features, tables, target, tolerance, cancel)
        if suffix in (".kml", ".geojson"):
            companion = {
                "mode": "vector",
                "tables": tables,
                "output_layers": report["output_layers"],
            }
            side = stage / output.with_suffix(".veri.json").name
            side.write_text(core.json_text(companion), encoding="utf-8")
            if json.loads(side.read_text(encoding="utf-8")) != companion:
                raise RuntimeError("Kaynak bilgisi yan dosyası doğrulanamadı.")
        for s in sources:
            core.check_cancel(cancel)
            if s.status != "ready":
                continue
            if core.sha(s.path) != s.file_sha256:
                raise RuntimeError("Girdi aktarım sırasında değişti.")
            with tempfile.TemporaryDirectory(dir=stage) as temp:
                if core.materialize(s, temp)[2] != s.fingerprint:
                    raise RuntimeError("Girdi bileşeni aktarım sırasında değişti.")
        (stage / output.with_suffix(".rapor.json").name).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        body = (
            '<!doctype html><html lang="tr"><meta charset="utf-8"><title>Vektör dönüşüm raporu</title><style>body{font:15px system-ui;margin:32px}table{border-collapse:collapse}td,th{border:1px solid #aaa;padding:8px}</style><h1>Genel vektör dönüşüm raporu</h1><p>'
            + html.escape(str(output))
            + "</p>"
            + comparison.html_section(report)
        )
        body += (
            "<h2>Çıktı katmanları</h2><p>"
            + html.escape(", ".join(report["output_layers"]))
            + "</p><p>"
            + html.escape(review.MATCH_NOTE)
            + "</p>"
        )
        body += "<h2>Katman benzerlikleri</h2><table><tr><th>İlk katman</th><th>Diğer katman</th><th>Puan</th><th>Eş geometri</th><th>Kesin eş kayıt</th><th>Ortak alanlar</th><th>Karar</th></tr>"
        for pair in model["layer_similarities"]:
            values = [
                sources[pair["left"]].label,
                sources[pair["right"]].label,
                pair["score"],
                pair["geometry_matches"],
                pair["exact_matches"],
                ", ".join(pair["common_fields"]),
                pair["reason"],
            ]
            body += (
                "<tr>"
                + "".join("<td>" + html.escape(str(v)) + "</td>" for v in values)
                + "</tr>"
            )
        body += "</table></html>"
        (stage / output.with_suffix(".rapor.html").name).write_text(
            body, encoding="utf-8"
        )
        core.check_cancel(cancel)
        published = []
        try:
            for final in finals[1:]:
                os.link(stage / final.name, final)
                published.append(final)
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
