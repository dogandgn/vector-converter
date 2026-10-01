"""FileGDB export from the validated common data model, with full read-back checks."""

from pathlib import Path
import json
import math
import os
import tempfile
from osgeo import ogr, osr
from qgis.core import QgsGeometry
from . import core

TABLES = (
    "parseller",
    "kaynaklar",
    "kayit_kaynaklari",
    "kontroller",
    "islem_ozeti",
)


def spatial_reference(path):
    ds = ogr.Open(str(path), 0)
    if ds is None:
        raise RuntimeError("Veritabanı yeniden açılamadı.")
    try:
        layer = ds.GetLayerByName("parseller")
        if layer is None or layer.GetSpatialRef() is None:
            raise RuntimeError("Parsel koordinat sistemi okunamadı.")
        return layer.GetSpatialRef().Clone()
    finally:
        ds = None


def geometry_points(g):
    if g.GetGeometryCount():
        for i in range(g.GetGeometryCount()):
            yield from geometry_points(g.GetGeometryRef(i))
    else:
        yield from ((g.GetX(i), g.GetY(i)) for i in range(g.GetPointCount()))


def ring_sizes(g):
    if g.GetGeometryCount():
        return sorted(
            (
                ring_sizes(g.GetGeometryRef(i))
                for i in range(g.GetGeometryCount())
            ),
            key=str,
        )
    return g.GetPointCount()


def verify_export(canonical, output, epsg, resolution, cancel=lambda: False):
    """Compare every field value, geometry, and relation after closing the writer."""
    left = ogr.Open(str(canonical), 0)
    right = ogr.Open(str(output), 0)
    if left is None or right is None:
        raise RuntimeError("FileGDB yeniden açılamadı.")
    counts = {}
    fields_checked = 0
    maximum = 0.0
    try:
        expected = osr.SpatialReference()
        expected.ImportFromEPSG(epsg)
        actual = right.GetLayerByName("parseller").GetSpatialRef()
        # FileGDB stores Esri WKT; compare the definition, ignoring axis order only.
        if actual is None or not actual.IsSame(
            expected,
            [
                "IGNORE_DATA_AXIS_TO_SRS_AXIS_MAPPING=YES",
                "CRITERION=EQUIVALENT_EXCEPT_AXIS_ORDER_GEOGCRS",
            ],
        ):
            raise RuntimeError("FileGDB hedef koordinat tanımı değişti.")
        for name in TABLES:
            core.check_cancel(cancel)
            a = left.GetLayerByName(name)
            b = right.GetLayerByName(name)
            if b is None or a.GetFeatureCount() != b.GetFeatureCount():
                raise RuntimeError("FileGDB kayıt sayısı farklı: " + name)
            counts[name] = a.GetFeatureCount()
            if (
                a.GetLayerDefn().GetFieldCount()
                != b.GetLayerDefn().GetFieldCount()
            ):
                raise RuntimeError("FileGDB alan sayısı farklı: " + name)
            for i in range(a.GetLayerDefn().GetFieldCount()):
                x = a.GetLayerDefn().GetFieldDefn(i)
                y = b.GetLayerDefn().GetFieldDefn(i)
                if (x.GetName(), x.GetType()) != (y.GetName(), y.GetType()):
                    raise RuntimeError(
                        "FileGDB alan şeması farklı: " + x.GetName()
                    )
            for x in a:
                core.check_cancel(cancel)
                y = b.GetFeature(x.GetFID())
                if y is None:
                    raise RuntimeError("FileGDB satırı bulunamadı.")
                for i in range(x.GetFieldCount()):
                    if x.GetField(i) != y.GetField(i):
                        raise RuntimeError(
                            "FileGDB alan değeri değişti: "
                            + name
                            + "."
                            + x.GetFieldDefnRef(i).GetName()
                        )
                    fields_checked += 1
                if name == "parseller":
                    gx = x.GetGeometryRef()
                    gy = y.GetGeometryRef()
                    if (
                        gy is None
                        or gy.IsEmpty()
                        or not gy.IsValid()
                        or gy.Is3D()
                        or gy.IsMeasured()
                        or ring_sizes(gx) != ring_sizes(gy)
                    ):
                        raise RuntimeError(
                            "FileGDB geometri/topoloji kontrolü başarısız."
                        )
                    px = list(geometry_points(gx))
                    py = list(geometry_points(gy))
                    delta = max(
                        max(
                            min(
                                math.hypot(u[0] - v[0], u[1] - v[1])
                                for v in py
                            )
                            for u in px
                        ),
                        max(
                            min(
                                math.hypot(u[0] - v[0], u[1] - v[1])
                                for v in px
                            )
                            for u in py
                        ),
                    )
                    qx = QgsGeometry()
                    qx.fromWkb(bytes(gx.ExportToWkb()))
                    qy = QgsGeometry()
                    qy.fromWkb(bytes(gy.ExportToWkb()))
                    distance = qx.hausdorffDistance(qy)
                    # Grid rounding is <= half a cell per axis, plus floating point error.
                    limit = resolution * 0.72 + (
                        1e-12 if expected.IsGeographic() else 1e-6
                    )
                    if delta > limit or distance < 0 or distance > limit:
                        raise RuntimeError(
                            "FileGDB koordinat farkı ızgara sınırını aşıyor."
                        )
                    maximum = max(maximum, delta)
        return {
            "reopened": True,
            "counts": counts,
            "field_values_verified": fields_checked,
            "target_epsg": epsg,
            "crs_definition_verified": True,
            "max_vertex_difference_crs_units": maximum,
            "xy_resolution_crs_units": resolution,
            "geometry_valid": True,
            "source_bytes_unchanged": True,
        }
    finally:
        left = right = None


def convert_gdb(
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
    if os.name != "nt":
        raise ValueError(
            "Bu geliştirme sürümünde FileGDB güvenli yayınlama Windows üzerinde desteklenir."
        )
    if any(output.is_relative_to(Path(p).resolve()) for p in protected):
        raise ValueError(
            "Ham arşiv/kaynak klasörü çıktı yeri olarak kullanılamaz."
        )
    side_json = output.with_suffix(".rapor.json")
    side_html = output.with_suffix(".rapor.html")
    if any(os.path.lexists(p) for p in (output, side_json, side_html)):
        raise ValueError("Çıktı veya rapor zaten var; yeni bir ad seçin.")
    driver = ogr.GetDriverByName("OpenFileGDB")
    if driver is None or driver.GetMetadataItem("DCAP_CREATE") != "YES":
        raise ValueError(
            "OpenFileGDB yazma sürücüsü bulunamadı; GDAL 3.6 veya üzeri gerekir."
        )
    core.check_cancel(cancel)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".parsel_gdb_", dir=output.parent
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
        epsg = int(report["profile"]["crs"].split(":")[1])
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(epsg)
        geographic = bool(srs.IsGeographic())
        resolution = 1e-9 if geographic else 0.0001
        report["output"] = str(output)
        report["output_format"] = "OpenFileGDB"
        report["canonical_validation"] = report.pop("validation")
        report["storage"] = {
            "xy_resolution": resolution,
            "unit": "degree" if geographic else "metre",
            "xy_tolerance": 0.000000008983153 if geographic else 0.001,
            "measurement_note": "Alan/çevre dönüşüm sonrası, FileGDB ızgara yuvarlamasından önce hesaplanır. Izgara çözünürlüğü saha doğruluğu değildir.",
            "relationships": "record_uid/source_uid alanları ile mantıksal ilişkiler; Esri relationship class oluşturulmaz.",
            "compatibility": "TARGET_ARCGIS_VERSION=ALL; ArcGIS uygulamasında ayrıca test edilmedi.",
        }
        # Store the final destination/format in the run table before exporting it.
        model = ogr.Open(str(canonical), 1)
        try:
            runs = model.GetLayerByName("islem_ozeti")
            row = runs.GetNextFeature()
            row.SetField("rapor_json", core.json_text(report))
            if runs.SetFeature(row) != 0:
                raise RuntimeError("İşlem özeti güncellenemedi.")
            row = runs = None
        finally:
            model = None
        src = ogr.Open(str(canonical), 0)
        dst = None
        try:
            dst = driver.CreateDataSource(str(target))
            if dst is None:
                raise RuntimeError("FileGDB oluşturulamadı.")
            for index, name in enumerate(TABLES):
                core.check_cancel(cancel)
                progress(70 + index * 4, "FileGDB: " + name)
                layer = src.GetLayerByName(name)
                options = ["TARGET_ARCGIS_VERSION=ALL"]
                if name == "parseller":
                    options += [
                        "XYSCALE=" + str(1 / resolution),
                        "XYTOLERANCE="
                        + str(report["storage"]["xy_tolerance"]),
                    ]
                out = dst.CreateLayer(
                    name,
                    srs=layer.GetSpatialRef(),
                    geom_type=layer.GetGeomType(),
                    options=options,
                )
                if out is None:
                    raise RuntimeError(
                        "FileGDB tablosu oluşturulamadı: " + name
                    )
                for i in range(layer.GetLayerDefn().GetFieldCount()):
                    field = layer.GetLayerDefn().GetFieldDefn(i)
                    copy = ogr.FieldDefn(field.GetName(), field.GetType())
                    if field.GetType() == ogr.OFTString:
                        # Explicit data-dependent widths prevent truncation of long JSON/XML.
                        width = max(
                            (
                                len(f.GetField(i).encode("utf-8"))
                                for f in layer
                                if f.GetField(i) is not None
                            ),
                            default=0,
                        )
                        layer.ResetReading()
                        copy.SetWidth(max(1, width))
                    if out.CreateField(copy) != 0:
                        raise RuntimeError("FileGDB alanı oluşturulamadı.")
                for feature in layer:
                    core.check_cancel(cancel)
                    new = ogr.Feature(out.GetLayerDefn())
                    if new.SetFrom(feature) != 0:
                        raise RuntimeError("FileGDB kayıt eşleme hatası.")
                    new.SetFID(feature.GetFID())
                    if out.CreateFeature(new) != 0:
                        raise RuntimeError("FileGDB kayıt yazma hatası.")
                    new = None
                out = layer = None
            core.close_dataset(dst)
            dst = None
        finally:
            if dst is not None:
                core.close_dataset(dst)
            src = None
        report["validation"] = verify_export(
            canonical, target, epsg, resolution, cancel
        )
        # Recheck source bytes after export as well, before publishing the product.
        for source in sources:
            core.check_cancel(cancel)
            if source.status != "ready":
                continue
            if core.sha(source.path) != source.file_sha256:
                raise RuntimeError("Kaynak dosya işlem sırasında değişti.")
            with tempfile.TemporaryDirectory(dir=stage) as temp:
                if core.materialize(source, temp)[2] != source.fingerprint:
                    raise RuntimeError(
                        "Kaynak bileşenleri işlem sırasında değişti."
                    )
        stage_json = stage / side_json.name
        stage_html = stage / side_html.name
        stage_json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        core.write_report(stage_html, report)
        core.check_cancel(cancel)
        published = []
        try:
            for a, b in ((stage_json, side_json), (stage_html, side_html)):
                os.link(a, b)
                published.append(b)
            # Windows rename fails if the destination already exists, even an empty directory.
            # Publish the closed, fully verified directory last; no partially written .gdb.
            os.rename(target, output)
        except Exception:
            for path in published:
                path.unlink()
            raise
    progress(100, "Tamamlandı.")
    return report
