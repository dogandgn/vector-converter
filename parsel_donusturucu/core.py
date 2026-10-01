"""Shapefile/KML/GeoJSON engine. No source writes or implicit CRS guessing."""

from dataclasses import dataclass, asdict
from pathlib import Path, PurePosixPath
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import html
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
import uuid
import zipfile

from osgeo import gdal, ogr
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsDatumTransform,
    QgsDistanceArea,
    QgsEllipsoidUtils,
    QgsGeometry,
    QgsPointXY,
    QgsProjUtils,
)

from .compat import close_dataset, check_runtime
from .profiles import PROFILES

VERSION = "0.9.0"
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 5000
EXTENSIONS = (".shp", ".shx", ".dbf", ".prj", ".cpg")
ALIASES = {
    "il": "il",
    "İl": "il",
    "Il": "il",
    "ilce": "ilce",
    "İlçe": "ilce",
    "Ilce": "ilce",
    "mahalle": "mahalle",
    "Mahalle": "mahalle",
    "adaNo": "ada_no",
    "Ada": "ada_no",
    "ada": "ada_no",
    "ada_no": "ada_no",
    "parselNo": "parsel_no",
    "ParselNo": "parsel_no",
    "parsel": "parsel_no",
    "parsel_no": "parsel_no",
    "alan": "kaynak_alan_metin",
    "Alan": "kaynak_alan_metin",
    "pafta": "pafta",
    "Pafta": "pafta",
    "nitelik": "nitelik",
    "Nitelik": "nitelik",
    "mevkii": "mevkii",
    "Mevkii": "mevkii",
}
ALIASES["kaynak_alan_metin"] = "kaynak_alan_metin"
STANDARD_TEXT = [
    "il",
    "ilce",
    "mahalle",
    "ada_no",
    "parsel_no",
    "pafta",
    "nitelik",
    "mevkii",
    "kaynak_alan_metin",
]


class Cancelled(Exception):
    pass


class ManagedDataset:
    """Close SQLite handles before the surrounding temporary directory is removed."""

    def __init__(self):
        self.ds = None

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if self.ds is not None:
            close_dataset(self.ds)
            self.ds = None


@dataclass
class Source:
    path: str
    member: str = ""
    status: str = "pending"
    error: str = ""
    count: int = 0
    crs: str = ""
    wkt: str = ""
    encoding: str = ""
    fingerprint: str = ""
    file_sha256: str = ""
    duplicate_of: str = ""
    components: dict = None
    fields: list = None
    warnings: list = None
    format: str = "shapefile"
    selected_fids: list = None
    assigned_crs: str = ""
    assignment_evidence: str = ""
    selection_details: dict = None
    assignment_basis: str = "user_confirmed_per_source"
    coordinate_references: list = None
    data_mode: str = "parcel"
    layer_name: str = ""

    @property
    def label(self):
        return Path(self.path).name + (
            f" / {self.member}" if self.member else ""
        )


def check_cancel(cancel):
    if cancel():
        raise Cancelled("İşlem iptal edildi; nihai çıktı oluşturulmadı.")


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def json_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str)


def safe_zip(z):
    members = z.infolist()
    if (
        len(members) > MAX_MEMBERS
        or sum(m.file_size for m in members) > MAX_ARCHIVE_BYTES
    ):
        raise ValueError(
            "ZIP boyutu/üye sayısı güvenli işleme sınırını aşıyor."
        )
    seen = set()
    for item in members:
        p = PurePosixPath(item.filename.replace("\\", "/"))
        if (
            p.is_absolute()
            or ".." in p.parts
            or ":" in item.filename
            or stat.S_ISLNK(item.external_attr >> 16)
        ):
            raise ValueError("ZIP içinde güvenli olmayan dosya yolu var.")
        name = str(p).casefold()
        if name in seen:
            raise ValueError("ZIP içinde aynı adlı birden fazla üye var.")
        seen.add(name)
    return [x.filename for x in members if not x.is_dir()]


def component_names(names, main):
    base = str(PurePosixPath(main).with_suffix("")).casefold()
    result = {}
    for name in names:
        p = PurePosixPath(name)
        if (
            str(p.with_suffix("")).casefold() == base
            and p.suffix.lower() in EXTENSIONS
        ):
            result[p.suffix.lower()] = name
    missing = {".shp", ".shx", ".dbf"} - set(result)
    if missing:
        raise ValueError(
            "Eksik Shapefile bileşeni: " + ", ".join(sorted(missing))
        )
    return result


def materialize(source, folder):
    """Copy to an isolated temporary dataset. Hash the exact bytes being processed."""
    base = Path(folder)
    if source.format in ("kml", "geojson"):
        if source.member:
            with zipfile.ZipFile(source.path) as z:
                safe_zip(z)
                data = z.read(source.member)
        else:
            data = Path(source.path).read_bytes()
        extension = ".kml" if source.format == "kml" else ".geojson"
        hashes = {extension: hashlib.sha256(data).hexdigest()}
        fingerprint = hashlib.sha256(
            json.dumps(hashes, sort_keys=True).encode()
        ).hexdigest()
        target = base / ("source" + extension)
        target.write_bytes(data)
        return target, hashes, fingerprint
    if source.member:
        with zipfile.ZipFile(source.path) as z:
            names = safe_zip(z)
            components = component_names(names, source.member)
            payloads = {ext: z.read(name) for ext, name in components.items()}
    else:
        p = Path(source.path)
        names = [x.name for x in p.parent.iterdir() if x.is_file()]
        components = component_names(names, p.name)
        payloads = {
            ext: (p.parent / name).read_bytes()
            for ext, name in components.items()
        }
    hashes = {
        ext: hashlib.sha256(data).hexdigest() for ext, data in payloads.items()
    }
    fingerprint = hashlib.sha256(
        json.dumps(hashes, sort_keys=True).encode()
    ).hexdigest()
    for ext, data in payloads.items():
        (base / ("source" + ext)).write_bytes(data)
    return base / "source.shp", hashes, fingerprint


def preflight():
    check_runtime()
    QgsEllipsoidUtils.definitions()  # populate fresh EPSG ellipsoid definitions
    src = QgsCoordinateReferenceSystem("EPSG:4326")
    dst = QgsCoordinateReferenceSystem("EPSG:5254")
    meter = QgsDistanceArea()
    meter.setSourceCrs(src, QgsCoordinateTransformContext())
    if (
        not src.isValid()
        or not dst.isValid()
        or not meter.setEllipsoid("EPSG:7030")
        or not meter.willUseEllipsoid()
    ):
        raise ValueError(
            "QGIS EPSG/elipsoid veritabanı kullanılamıyor. Koordinat ortamı düzeltilmeli."
        )
    reference = meter.measureLine(
        QgsPointXY(29, 41.05), QgsPointXY(29.00001, 41.05)
    )
    if abs(reference - 0.840715724) > 0.00001:
        raise ValueError(
            "Ölçüm birimi/ellipsoid kontrolü başarısız; işlem durduruldu."
        )
    if ogr.GetDriverByName("GPKG") is None:
        raise ValueError("GeoPackage yazma sürücüsü bulunamadı.")
    return {
        "qgis": Qgis.QGIS_VERSION,
        "gdal": gdal.VersionInfo("--version"),
        "qgis_proj_major_minor": f"{QgsProjUtils.projVersionMajor()}.{QgsProjUtils.projVersionMinor()}",
        "epsg_registry": QgsProjUtils.epsgRegistryVersion(),
        "ellipsoid_count": len(QgsEllipsoidUtils.definitions()),
        "reference_distance_m": reference,
    }


def open_source(path, general=False):
    if Path(path).suffix.lower() == ".geojson":
        from .geojson_reader import Dataset

        return Dataset(path, general=general)
    if Path(path).suffix.lower() == ".kml":
        from .kml_reader import Dataset

        return Dataset(path, general=general)
    # QGIS or another provider may set global SHAPE_ENCODING="". Do not inherit it.
    probe = gdal.OpenEx(str(path), gdal.OF_VECTOR | gdal.OF_READONLY)
    if probe is None:
        raise ValueError("Shapefile okunamadı.")
    info = probe.GetLayer(0).GetMetadata("SHAPEFILE")
    encoding = info.get("ENCODING_FROM_CPG") or info.get("ENCODING_FROM_LDID")
    close_dataset(probe)
    probe = None
    if not encoding:
        raise ValueError(
            "CPG/DBF kodlama tanımı çözümlenemedi; tahmini karakter dönüşümü yapılmadı."
        )
    ds = gdal.OpenEx(
        str(path),
        gdal.OF_VECTOR | gdal.OF_READONLY,
        open_options=["ENCODING=" + encoding],
    )
    if ds is None:
        raise ValueError(
            "Shapefile belirtilen kodlamayla okunamadı: " + encoding
        )
    return ds


def scan(
    folder,
    excluded=(),
    cancel=lambda: False,
    progress=lambda n, s: None,
    formats=("shapefile", "kml", "geojson"),
    general=False,
):
    base = Path(folder).resolve()
    if not base.is_dir():
        raise ValueError("Girdi klasörü bulunamadı.")
    exclusions = [Path(x).resolve() for x in excluded]
    candidates = []
    for directory, dirs, files in os.walk(base, followlinks=False):
        check_cancel(cancel)
        dirs[:] = [
            d
            for d in sorted(dirs)
            if not (Path(directory) / d).is_symlink()
            and not any(
                (Path(directory) / d).resolve().is_relative_to(x)
                for x in exclusions
            )
        ]
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink():
                continue
            if (
                path.suffix.lower() in (".json", ".geojson")
                and "geojson" in formats
            ):
                candidates.append(Source(str(path), format="geojson"))
            elif path.suffix.lower() == ".kml" and "kml" in formats:
                candidates.append(Source(str(path), format="kml"))
            elif path.suffix.lower() == ".kmz" and "kml" in formats:
                try:
                    with zipfile.ZipFile(path) as z:
                        names = safe_zip(z)
                    documents = [
                        n
                        for n in names
                        if PurePosixPath(n).suffix.lower() == ".kml"
                    ]
                    if len(documents) != 1:
                        raise ValueError(
                            "KMZ tam bir KML belgesi içermelidir; çoklu belge sessizce atılmadı."
                        )
                    candidates.append(
                        Source(str(path), documents[0], format="kml")
                    )
                except Exception as error:
                    candidates.append(
                        Source(
                            str(path),
                            status="error",
                            error=str(error),
                            format="kml",
                        )
                    )
            elif path.suffix.lower() == ".shp" and "shapefile" in formats:
                candidates.append(Source(str(path)))
            elif path.suffix.lower() == ".zip" and "shapefile" in formats:
                try:
                    with zipfile.ZipFile(path) as z:
                        members = safe_zip(z)
                    shapes = [
                        n
                        for n in members
                        if PurePosixPath(n).suffix.lower() == ".shp"
                    ]
                    candidates.extend(Source(str(path), n) for n in shapes)
                    if not shapes:
                        candidates.append(
                            Source(
                                str(path),
                                status="error",
                                error="ZIP içinde Shapefile yok.",
                            )
                        )
                except Exception as error:
                    candidates.append(
                        Source(str(path), status="error", error=str(error))
                    )
    # Prefer archive over its byte-identical extracted copy, independently of file traversal order.
    candidates.sort(
        key=lambda x: (
            not bool(x.member),
            x.path.casefold(),
            x.member.casefold(),
        )
    )
    seen = {}
    for i, item in enumerate(candidates):
        check_cancel(cancel)
        progress(100 * i / max(1, len(candidates)), item.label)
        if item.status == "error":
            continue
        try:
            with tempfile.TemporaryDirectory(prefix="parsel_scan_") as temp:
                path, hashes, fingerprint = materialize(item, temp)
                item.components = hashes
                item.fingerprint = fingerprint
                item.file_sha256 = sha(item.path)
                item.warnings = []
                ds = open_source(path, general=general)
                if ds is None:
                    raise ValueError("Shapefile okunamadı.")
                lyr = ds.GetLayer(0)
                item.data_mode = "vector" if general else "parcel"
                # Materialized input names are temporary; display the declared or original name.
                item.layer_name = (
                    getattr(lyr, "declared_layer_name", "")
                    or Path(item.member or item.path).stem
                )
                if item.format in ("kml", "geojson"):
                    item.crs = "EPSG:4326"
                    item.wkt = QgsCoordinateReferenceSystem(
                        "EPSG:4326"
                    ).toWkt()
                    item.encoding = (
                        "XML bildirimi / Unicode"
                        if item.format == "kml"
                        else "UTF-8"
                    )
                    item.fields = [
                        {
                            "name": n,
                            "type": (
                                "String"
                                if item.format == "kml"
                                else "JSON value"
                            ),
                            "width": 0,
                        }
                        for n in ds.field_names
                    ]
                    item.warnings = [
                        f"Kayıt {f.index}: {f.error}"
                        for f in ds.features
                        if f.error
                    ]
                    if (
                        item.format == "geojson"
                        and ds.crs_basis == "legacy_declared"
                    ):
                        item.warnings.append("ESKI_GEOJSON_ACIK_CRS")
                else:
                    srs = lyr.GetSpatialRef()
                    if ".prj" in hashes and srs is None:
                        raise ValueError(
                            "PRJ mevcut ancak koordinat sistemi geçersiz; varsayım uygulanmadı."
                        )
                    if srs:
                        crs = QgsCoordinateReferenceSystem.fromWkt(
                            srs.ExportToWkt()
                        )
                        if not crs.isValid():
                            raise ValueError(
                                "Kaynak koordinat sistemi çözümlenemedi."
                            )
                        item.crs = crs.authid() or crs.description()
                        item.wkt = crs.toWkt()
                    else:
                        item.warnings.append("CRS_EKSIK")
                    item.encoding = lyr.GetMetadata("SHAPEFILE").get(
                        "SOURCE_ENCODING", ""
                    )
                    if ".cpg" not in hashes:
                        item.warnings.append("CPG_YOK")
                    item.fields = [
                        {
                            "name": x.name,
                            "type": x.GetTypeName(),
                            "width": x.GetWidth(),
                        }
                        for x in lyr.schema
                    ]
                    if lyr.GetFeatureCount() != int.from_bytes(
                        path.with_suffix(".dbf").read_bytes()[4:8], "little"
                    ):
                        raise ValueError(
                            "SHP/DBF kayıt sayıları uyuşmuyor (silinmiş kayıt olabilir)."
                        )
                item.count = lyr.GetFeatureCount()
                ds = None
            if fingerprint in seen:
                item.status = "duplicate"
                item.duplicate_of = seen[fingerprint]
            else:
                item.status = "ready"
                seen[fingerprint] = item.label
        except Exception as error:
            item.status = "error"
            item.error = str(error)
    progress(100, f"{len(candidates)} kaynak incelendi.")
    return candidates


def parse_area(value):
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Alan boolean olamaz.")
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value < 0:
            raise ValueError("Geçersiz alan sayısı.")
        return float(value)
    s = str(value).strip()
    if not re.fullmatch(r"\d+(?:,\d+)?|\d{1,3}(?:\.\d{3})+(?:,\d+)?", s):
        raise ValueError("Alan Türkçe sayı biçimine uymuyor.")
    return float(Decimal(s.replace(".", "").replace(",", ".")))


def standard_attributes(raw):
    standard = {key: None for key in STANDARD_TEXT}
    for key, value in raw.items():
        mapped = ALIASES.get(key)
        if mapped:
            if isinstance(value, (bool, dict, list)):
                raise ValueError(
                    "Standart alan metin/sayı/null olmalıdır: " + key
                )
            normalized = str(value) if value is not None else None
            if standard[mapped] is not None and standard[mapped] != normalized:
                raise ValueError(
                    f"Aynı anlama eşlenen alanlar çelişiyor: {mapped}"
                )
            standard[mapped] = normalized
    return standard


def dimensional_values(geometry):
    if geometry.GetGeometryCount():
        for i in range(geometry.GetGeometryCount()):
            yield from dimensional_values(geometry.GetGeometryRef(i))
    else:
        for i in range(geometry.GetPointCount()):
            yield (
                geometry.GetZ(i) if geometry.Is3D() else None,
                geometry.GetM(i) if geometry.IsMeasured() else None,
            )


def make_transform(src, dst):
    context = QgsCoordinateTransformContext()
    if src == dst:
        return None, {
            "name": "Kimlik dönüşümü; XY korunur",
            "accuracy_m": 0.0,
            "pipeline": "identity",
        }
    from osgeo import osr
    from qgis.core import QgsRectangle

    a = osr.SpatialReference()
    a.ImportFromWkt(src.toWkt())
    b = osr.SpatialReference()
    b.ImportFromWkt(dst.toWkt())
    same_datum = bool(a.IsSameGeogCS(b))
    # Region filtering is essential for ED50: the smallest global accuracy may
    # belong to Norway/Spain and is not a usable Turkish transformation.
    region = QgsRectangle(25.62, 34.42, 44.83, 43.45)
    for crs in (src, dst):
        bounds = crs.bounds()
        if not bounds.isNull():
            region = region.intersect(bounds)
    operations = [
        x
        for x in QgsDatumTransform.operations(src, dst)
        if x.isAvailable
        and (x.accuracy >= 0 or same_datum)
        and "ballpark" not in x.name.lower()
        and not region.isEmpty()
        and (x.bounds.isNull() or x.bounds.contains(region))
    ]
    if not operations:
        raise ValueError(
            f"{src.authid()} → {dst.authid()} için doğruluğu tanımlı kullanılabilir dönüşüm yok."
        )
    op = min(operations, key=lambda x: x.accuracy)
    context.addCoordinateOperation(src, dst, op.proj, False)
    transform = QgsCoordinateTransform(src, dst, context)
    transform.setAllowFallbackTransforms(False)
    transform.setBallparkTransformsAreAppropriate(False)
    bounds = op.bounds
    return transform, {
        "name": op.name,
        "accuracy_m": 0.0 if same_datum and op.accuracy < 0 else op.accuracy,
        "pipeline": op.proj,
        "bounds": [
            bounds.xMinimum(),
            bounds.yMinimum(),
            bounds.xMaximum(),
            bounds.yMaximum(),
        ],
    }


def qgeom(geom):
    out = QgsGeometry()
    out.fromWkb(bytes(geom.ExportToWkb()))
    return out


def add_row(layer, values, geometry=None):
    feature = ogr.Feature(layer.GetLayerDefn())
    for name, value in values.items():
        if value is not None:
            index = feature.GetFieldIndex(name)
            if index < 0:
                raise RuntimeError("Çıktı alanı yok: " + name)
            kind = feature.GetFieldDefnRef(index).GetType()
            if kind == ogr.OFTReal:
                feature.SetField(index, float(value))
            elif kind in (ogr.OFTInteger, ogr.OFTInteger64):
                feature.SetField(index, int(value))
            else:
                feature.SetField(
                    index,
                    value if isinstance(value, str) else json_text(value),
                )
    if geometry is not None:
        feature.SetGeometry(geometry)
    if layer.CreateFeature(feature) != 0:
        raise RuntimeError(
            "GeoPackage kaydı yazılamadı: " + gdal.GetLastErrorMsg()
        )
    return feature.GetFID()


def match_key(attrs, geographic):
    """Exact XY rings plus administrative identity; no spatial tolerance or snapping."""
    import unicodedata

    def label(value):
        value = (value or "").translate(
            str.maketrans("ıİşŞğĞçÇöÖüÜ", "iIsSgGcCoOuU")
        )
        return unicodedata.normalize("NFC", value).strip().casefold()

    values = [
        attrs[k] for k in ["il", "ilce", "mahalle", "ada_no", "parsel_no"]
    ]
    if not all(values):
        return None
    identity = tuple(label(v) for v in values[:3]) + tuple(values[3:])
    geom = ogr.CreateGeometryFromWkb(bytes(geographic.asWkb()))
    polygons = (
        [geom]
        if ogr.GT_Flatten(geom.GetGeometryType()) == ogr.wkbPolygon
        else [geom.GetGeometryRef(i) for i in range(geom.GetGeometryCount())]
    )

    def ring(g):
        pts = [(g.GetX(i), g.GetY(i)) for i in range(g.GetPointCount())]
        if pts[0] == pts[-1]:
            pts.pop()
        candidates = []
        for seq in [pts, list(reversed(pts))]:
            smallest = min(seq)
            candidates.extend(
                tuple(seq[i:] + seq[:i])
                for i, p in enumerate(seq)
                if p == smallest
            )
        return min(candidates)

    shapes = []
    for poly in polygons:
        shapes.append(
            (
                ring(poly.GetGeometryRef(0)),
                tuple(
                    sorted(
                        ring(poly.GetGeometryRef(i))
                        for i in range(1, poly.GetGeometryCount())
                    )
                ),
            )
        )
    return identity, tuple(sorted(shapes))


def create_table(ds, name, schema, srs=None, spatial=False):
    layer = ds.CreateLayer(
        name,
        srs=srs,
        geom_type=ogr.wkbMultiPolygon if spatial else ogr.wkbNone,
        options=["SPATIAL_INDEX=YES"] if spatial else [],
    )
    if layer is None:
        raise RuntimeError("GeoPackage tablosu oluşturulamadı: " + name)
    for field, kind in schema:
        if layer.CreateField(ogr.FieldDefn(field, kind)) != 0:
            raise RuntimeError("Alan oluşturulamadı: " + field)
    return layer


def write_report(path, report):
    from .comparison import html_section

    comparison_html = (
        html_section(report) if "source_comparison" in report else ""
    )
    esc = lambda x: html.escape(str(x))
    rows = "".join(
        "<tr>"
        + "".join(
            f'<td>{esc(x.get(k,""))}</td>'
            for k in ["source", "fid", "code", "message"]
        )
        + "</tr>"
        for x in report["issues"]
    )
    stats = "".join(
        f"<li><b>{esc(k)}:</b> {esc(v)}</li>"
        for k, v in report["summary"].items()
    )
    conflicts = "".join(
        "<tr>"
        + "".join(
            f'<td>{esc(x.get(k,""))}</td>'
            for k in ["left_source", "right_source", "field", "left", "right"]
        )
        + "</tr>"
        for x in report.get("conflicts", [])
    )
    pairs = {}
    for comparison in report.get("comparisons", []):
        key = (comparison["left_format"], comparison["right_format"])
        entry = pairs.setdefault(key, [0, 0])
        entry[0] += 1
        entry[1] += comparison["content_difference_count"]
    pair_rows = "".join(
        f"<tr><td>{esc(a)} / {esc(b)}</td><td>{count}</td><td>{difference}</td></tr>"
        for (a, b), (count, difference) in pairs.items()
    )
    path.write_text(
        '<!doctype html><html lang="tr"><meta charset="utf-8"><title>Parsel dönüşüm raporu</title>'
        "<style>body{font:16px system-ui;max-width:1100px;margin:40px auto;padding:0 24px;color:#18303b}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccd8dc;padding:8px;text-align:left}th{background:#eef5f5}code{word-break:break-all}</style>"
        f'<h1>Parsel dönüşüm raporu</h1><p>{esc(report["created_utc"])}</p><p>Şablon: {esc(report["profile"]["name"])}. Kaynak alanı ve hesaplanan alan ayrı tutulmuştur. İşlem doğruluğu kaynak verinin saha doğruluğu değildir.</p>{comparison_html}<details><summary>Teknik sayaçlar</summary><ul>{stats}</ul></details>'
        f'<h2>Çıktı ve doğrulama</h2><p>{esc(report["output"])}</p><pre style="white-space:pre-wrap">{esc(json.dumps({k:report[k] for k in ("output_format","storage","validation") if k in report},ensure_ascii=False,indent=2))}</pre>'
        + (
            f'<details><summary>Girdi seçiminin teknik ayrıntıları</summary><pre style="white-space:pre-wrap">{esc(json.dumps(report["input_selection"],ensure_ascii=False,indent=2))}</pre></details>'
            if "input_selection" in report
            else ""
        )
        + f'<p>Eşleştirme: {esc(report.get("merge_mode","separate"))}; alan politikası: {esc(report.get("attribute_policy","preserve"))}. İlk kaynak politikası Shapefile → KML → GeoJSON sırasını kullanır. Eşleşme idari kimlik ve tam XY eşitliğidir; tolerans uygulanmaz.</p>'
        f"<h2>Kaynak çifti karşılaştırması</h2><p>Fark sayıları kaynak çifti başınadır; aynı alan birden fazla çiftte farklı olabilir.</p><table><tr><th>Biçimler</th><th>Eşleşen kayıt çifti</th><th>İçerik farkı</th></tr>{pair_rows}</table>"
        f"<h2>Öznitelik farkları</h2><table><tr><th>İlk kaynak</th><th>Diğer kaynak</th><th>Alan</th><th>İlk değer</th><th>Diğer değer</th></tr>{conflicts}</table>"
        f"<h2>Kontrol kayıtları</h2><table><tr><th>Kaynak</th><th>Kayıt</th><th>Kod</th><th>Açıklama</th></tr>{rows}</table>"
        f'<h2>Öznitelik şeması</h2><pre style="white-space:pre-wrap">{esc(json.dumps(report.get("attribute_schema", "Standart şema"),ensure_ascii=False,indent=2))}</pre>'
        f'<h2>İşlem ayrıntıları</h2><pre style="white-space:pre-wrap">{esc(json.dumps(report["operations"],ensure_ascii=False,indent=2))}</pre></html>',
        encoding="utf-8",
    )


def convert(sources, output, *args, **kwargs):
    from .public_output import convert as publish_source_data

    return publish_source_data(sources, output, *args, **kwargs)


def _convert_legacy(sources, output, *args, **kwargs):
    """Private audit-model serialization, retained for engine regression checks."""
    if Path(output).suffix.lower() in (".geojson", ".kml"):
        from .exchange_writer import convert_exchange

        return convert_exchange(sources, output, *args, **kwargs)
    if Path(output).suffix.lower() == ".gdb":
        from .gdb_writer import convert_gdb

        return convert_gdb(sources, output, *args, **kwargs)
    return _convert_gpkg(sources, output, *args, **kwargs)


def _convert_gpkg(
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
    environment = preflight()
    if attribute_schema is not None:
        from . import attribute_schema as schema_editor

        schema_editor.validate(attribute_schema, sources)
    if merge_mode not in ("separate", "merge") or attribute_policy not in (
        "preserve",
        "prefer_kml",
        "prefer_geojson",
    ):
        raise ValueError("Geçersiz birleştirme politikası.")
    if profile_id not in PROFILES:
        raise ValueError("Bilinmeyen şablon.")
    profile = PROFILES[profile_id]
    target = QgsCoordinateReferenceSystem(profile["crs"])
    output = Path(output).resolve()
    if output.suffix.lower() != ".gpkg":
        raise ValueError(
            "Desteklenen çıktı uzantıları .gpkg, .gdb, .geojson ve .kml."
        )
    if any(output.is_relative_to(Path(p).resolve()) for p in protected):
        raise ValueError(
            "Ham arşiv/kaynak klasörü çıktı yeri olarak kullanılamaz."
        )
    side_json = output.with_suffix(".rapor.json")
    side_html = output.with_suffix(".rapor.html")
    if any(p.exists() for p in (output, side_json, side_html)):
        raise ValueError("Çıktı veya rapor zaten var; yeni bir ad seçin.")
    output.parent.mkdir(parents=True, exist_ok=True)
    ready = [s for s in sources if s.status == "ready"]
    ready.sort(
        key=lambda s: (
            {"shapefile": 0, "kml": 1, "geojson": 2}[s.format],
            s.path.casefold(),
            s.member,
        )
    )
    if not ready:
        raise ValueError("İşlenebilir parsel kaynağı bulunamadı.")
    if not missing_crs and all(
        not s.wkt and not s.assigned_crs for s in ready
    ):
        raise ValueError(
            "Bütün kaynaklarda CRS eksik. Kaynak koordinat sistemini seçip tekrar çalıştırın."
        )
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    report = {
        "version": VERSION,
        "run_id": run_id,
        "created_utc": now,
        "output": str(output),
        "profile": profile,
        "environment": environment,
        "sources": [asdict(s) for s in sources],
        "operations": [],
        "issues": [],
        "merge_mode": merge_mode,
        "attribute_policy": attribute_policy,
        "conflicts": [],
        "representation_differences": [],
        "comparisons": [],
        "summary": {
            "taranan_kaynak": len(sources),
            "islenen_kaynak": 0,
            "ayni_dosya_kopyasi": sum(
                s.status == "duplicate" for s in sources
            ),
            "kaynak_hatasi": sum(s.status == "error" for s in sources),
            "girdi_kaydi": sum(
                (
                    len(s.selected_fids)
                    if s.selected_fids is not None
                    else s.count
                )
                for s in ready
            ),
            "aktarilan_parsel": 0,
            "aktarilamayan_kayit": 0,
            "alan_farki_kaydi": 0,
            "z_m_cikarilan_kayit": 0,
            "kaynak_baglantisi": 0,
            "birlestirilen_kayit": 0,
            "oznitelik_farki": 0,
            "bos_deger_temsil_farki": 0,
        },
    }
    if any(s.selected_fids is not None for s in sources):
        report["input_selection"] = [
            {
                "path": s.path,
                "member": s.member,
                "format": s.format,
                "status": s.status,
                "source_count": s.count,
                "selected_fids": s.selected_fids,
                "selection_details": s.selection_details,
            }
            for s in sources
        ]

    def issue(source, fid, code, message):
        item = {
            "source": source.label if isinstance(source, Source) else source,
            "fid": fid,
            "code": code,
            "message": message,
        }
        if isinstance(source, Source):
            item.update(source_path=source.path, source_member=source.member)
        report["issues"].append(item)

    for s in sources:
        if s.status == "error":
            issue(s, None, "KAYNAK_HATASI", s.error)
        elif s.status == "duplicate":
            issue(
                s,
                None,
                "AYNI_DOSYA_KOPYASI",
                "Aynı bileşenler bir kez işlenir: " + s.duplicate_of,
            )
    # Everything is assembled in a private directory, validated, then published without overwrite.
    with (
        tempfile.TemporaryDirectory(
            prefix=".parsel_build_", dir=output.parent
        ) as staging,
        ManagedDataset() as holder,
    ):
        stage = Path(staging)
        stage_gpkg = stage / output.name
        ds = ogr.GetDriverByName("GPKG").CreateDataSource(str(stage_gpkg))
        holder.ds = ds
        if ds is None:
            raise RuntimeError("GeoPackage oluşturulamadı.")
        from osgeo import osr

        target_srs = osr.SpatialReference()
        # WKT1 exported without axes may lose the EPSG identity for north/east CRSs.
        if target_srs.ImportFromEPSG(int(profile["crs"].split(":")[1])) != 0:
            raise ValueError("Hedef EPSG tanımı GDAL tarafından okunamadı.")
        target_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        S = ogr.OFTString
        R = ogr.OFTReal
        parcels = create_table(
            ds,
            "parseller",
            [("record_uid", S), ("parsel_anahtari", S)]
            + [(x, S) for x in STANDARD_TEXT]
            + [
                ("kaynak_alan_m2", R),
                ("hesap_alan_m2", R),
                ("hesap_cevre_m", R),
                ("alan_farki_m2", R),
                ("alan_farki_yuzde", R),
                ("olcum_yontemi", S),
                ("hedef_crs", S),
                ("kontrol_durumu", S),
                ("kaynak_fid", S),
                ("alan_celiskileri_json", S),
                ("alan_kaynaklari_json", S),
            ],
            target_srs,
            True,
        )
        source_table = create_table(
            ds,
            "kaynaklar",
            [
                (x, S)
                for x in [
                    "source_uid",
                    "dosya",
                    "zip_uyesi",
                    "bicim",
                    "sha256",
                    "bilesen_hashleri",
                    "kaynak_crs",
                    "crs_durumu",
                    "crs_kaniti",
                    "encoding",
                    "alan_semasi",
                ]
            ],
        )
        links = create_table(
            ds,
            "kayit_kaynaklari",
            [
                (x, S)
                for x in [
                    "record_uid",
                    "source_uid",
                    "source_fid",
                    "ozgun_alanlar_json",
                    "kaynak_metadata_json",
                ]
            ],
        )
        checks = create_table(
            ds,
            "kontroller",
            [("kaynak", S), ("source_fid", S), ("kod", S), ("aciklama", S)],
        )
        run = create_table(
            ds,
            "islem_ozeti",
            [("run_id", S), ("tarih_utc", S), ("surum", S), ("rapor_json", S)],
        )
        ds.StartTransaction()
        seen_parcels = {}
        matched = {}
        for index, source in enumerate(ready):
            check_cancel(cancel)
            progress(100 * index / len(ready), source.label)
            source_uid = str(uuid.uuid4())
            written_from_source = 0
            attempted_from_source = 0
            allowed = (
                None
                if source.selected_fids is None
                else set(source.selected_fids)
            )
            found = set()
            with tempfile.TemporaryDirectory(
                prefix="source_", dir=stage
            ) as folder:
                source_ds = None
                try:
                    path, hashes, fingerprint = materialize(source, folder)
                    if (
                        fingerprint != source.fingerprint
                        or sha(source.path) != source.file_sha256
                    ):
                        raise ValueError(
                            "Kaynak taramadan sonra değişmiş; yeniden tarayın."
                        )
                    source_ds = open_source(path)
                    layer = source_ds.GetLayer(0)
                    if source.format == "geojson":
                        src = QgsCoordinateReferenceSystem("EPSG:4326")
                        basis = source_ds.crs_basis
                        evidence = source_ds.crs_evidence
                        if basis == "legacy_declared":
                            issue(
                                source, None, "ESKI_GEOJSON_ACIK_CRS", evidence
                            )
                    elif source.format == "kml":
                        src = QgsCoordinateReferenceSystem("EPSG:4326")
                        basis = "format_defined"
                        evidence = "KML 2.2: boylam,enlem / WGS84"
                    elif source.wkt:
                        src = QgsCoordinateReferenceSystem.fromWkt(source.wkt)
                        basis = "declared"
                        evidence = "PRJ"
                    elif source.assigned_crs or missing_crs:
                        src = QgsCoordinateReferenceSystem(
                            source.assigned_crs or missing_crs
                        )
                        basis = (
                            source.assignment_basis
                            if source.assigned_crs
                            else crs_basis
                        )
                        if basis == "matched_reference":
                            if not source.coordinate_references:
                                raise RuntimeError(
                                    "Eş kayıt dayanağı bulunamadı; yeniden tarayın."
                                )
                            for reference in source.coordinate_references:
                                try:
                                    unchanged = (
                                        sha(Path(reference["path"]))
                                        == reference["sha256"]
                                    )
                                except OSError:
                                    unchanged = False
                                if not unchanged:
                                    raise RuntimeError(
                                        "Koordinat dayanağı olan eş dosya değişmiş veya bulunamadı; yeniden tarayın."
                                    )
                        evidence = (
                            source.assignment_evidence
                            if source.assigned_crs
                            else crs_evidence
                        )
                        if not evidence:
                            evidence = (
                                "Kullanıcı eksik kaynak CRS seçimini yaptı."
                            )
                        issue(
                            source,
                            None,
                            "CRS_TANIMLANDI",
                            f"PRJ yok: {src.authid()}; kanıt/karar: {evidence}",
                        )
                    else:
                        raise ValueError(
                            "Kaynak CRS eksik; koordinat sistemi seçilmeden dönüştürülmedi."
                        )
                    if not src.isValid():
                        raise ValueError("Kaynak koordinat sistemi geçersiz.")
                    transform, operation = make_transform(src, target)
                    to_geo, geo_operation = make_transform(
                        src, QgsCoordinateReferenceSystem("EPSG:4326")
                    )
                    operation_record = {
                        "source": source.label,
                        "source_crs": src.authid(),
                        "target_crs": target.authid(),
                        **operation,
                    }
                    report["operations"].append(operation_record)
                    meter = QgsDistanceArea()
                    meter.setSourceCrs(src, QgsCoordinateTransformContext())
                    if (
                        not meter.setEllipsoid(src.ellipsoidAcronym())
                        or not meter.willUseEllipsoid()
                    ):
                        raise ValueError(
                            "Kaynak elipsoidinde metre/m² hesabı kurulamadı."
                        )
                    add_row(
                        source_table,
                        {
                            "source_uid": source_uid,
                            "dosya": source.path,
                            "zip_uyesi": source.member,
                            "bicim": source.format,
                            "sha256": source.file_sha256,
                            "bilesen_hashleri": hashes,
                            "kaynak_crs": src.authid() or src.toWkt(),
                            "crs_durumu": basis,
                            "crs_kaniti": evidence,
                            "encoding": source.encoding,
                            "alan_semasi": source.fields,
                        },
                    )
                    if source.format == "shapefile" and ".cpg" not in hashes:
                        issue(
                            source,
                            None,
                            "CPG_YOK",
                            f"Kodlama sürücü bilgisine göre okundu: {source.encoding}. Harf tahmini yapılmadı.",
                        )
                    report["summary"]["islenen_kaynak"] += 1
                    for feature in layer:
                        check_cancel(cancel)
                        fid = str(feature.GetFID())
                        if allowed is not None and fid not in allowed:
                            continue
                        found.add(fid)
                        attempted_from_source += 1
                        try:
                            if getattr(feature, "error", ""):
                                raise ValueError(feature.error)
                            for warning in getattr(feature, "warnings", []):
                                issue(source, fid, "KML_ICERIK_NOTU", warning)
                            geom = feature.GetGeometryRef()
                            if geom is None or geom.IsEmpty():
                                raise ValueError("Boş geometri.")
                            if ogr.GT_Flatten(geom.GetGeometryType()) not in (
                                ogr.wkbPolygon,
                                ogr.wkbMultiPolygon,
                            ):
                                raise ValueError(
                                    "Parsel geometrisi Polygon/MultiPolygon olmalıdır."
                                )
                            geometry = geom.Clone()
                            flags = []
                            if geometry.Is3D() or geometry.IsMeasured():
                                dimensions = list(dimensional_values(geometry))
                                if any(
                                    z is not None and math.isfinite(z)
                                    for z, m in dimensions
                                ) or any(
                                    m is not None
                                    and math.isfinite(m)
                                    and m > -1e38
                                    for z, m in dimensions
                                ):
                                    raise ValueError(
                                        "Gerçek/sonlu Z veya M var; 2B çıktıda sessiz kayıp yapılmadı."
                                    )
                                geometry.FlattenTo2D()
                                geometry.SetMeasured(False)
                                flags.append("BOS_Z_M_CIKARILDI")
                                issue(
                                    source,
                                    fid,
                                    "BOS_Z_M_CIKARILDI",
                                    "NaN Z/no-data M kaldırıldı; yatay koordinatlar korundu.",
                                )
                                report["summary"]["z_m_cikarilan_kayit"] += 1
                            qg = qgeom(geometry)
                            if not qg.isGeosValid():
                                raise ValueError(
                                    "Geçersiz geometri; otomatik onarım uygulanmadı."
                                )
                            geographic = QgsGeometry(qg)
                            if to_geo:
                                geographic.transform(to_geo)
                            bounds = geographic.boundingBox()
                            if not (
                                -180
                                <= bounds.xMinimum()
                                <= bounds.xMaximum()
                                <= 180
                                and -90
                                <= bounds.yMinimum()
                                <= bounds.yMaximum()
                                <= 90
                            ):
                                raise ValueError(
                                    "Koordinatlar seçilen CRS ile uyumlu coğrafi aralıkta değil."
                                )
                            target_bounds = target.bounds()
                            source_bounds = src.bounds()
                            if (
                                not source_bounds.isNull()
                                and not source_bounds.contains(bounds)
                            ):
                                raise ValueError(
                                    "Parsel kaynak koordinat sisteminin kullanım alanı dışında."
                                )
                            if (
                                not target_bounds.isNull()
                                and not target_bounds.contains(bounds)
                            ):
                                raise ValueError(
                                    "Parsel hedef şablonun kullanım alanı dışında."
                                )
                            if operation.get("bounds"):
                                from qgis.core import QgsRectangle

                                if not QgsRectangle(
                                    *operation["bounds"]
                                ).contains(bounds):
                                    raise ValueError(
                                        "Parsel dönüşüm işleminin kullanım alanı dışında."
                                    )
                            if geo_operation.get("bounds"):
                                from qgis.core import QgsRectangle

                                if not QgsRectangle(
                                    *geo_operation["bounds"]
                                ).contains(bounds):
                                    raise ValueError(
                                        "Parsel coğrafi kontrol dönüşümünün kullanım alanı dışında."
                                    )
                            raw = (
                                feature.raw
                                if source.format in ("kml", "geojson")
                                else {
                                    field.name: feature.GetField(i)
                                    for i, field in enumerate(layer.schema)
                                }
                            )
                            json_text(raw).encode("utf-8", errors="strict")
                            attrs = standard_attributes(raw)
                            if not attrs["parsel_no"]:
                                raise ValueError(
                                    "Parsel numarası yok; kaydı parsel olarak sınıflandıramadı."
                                )
                            area_value = next(
                                (
                                    v
                                    for k, v in raw.items()
                                    if ALIASES.get(k) == "kaynak_alan_metin"
                                ),
                                None,
                            )
                            try:
                                source_area = parse_area(area_value)
                            except ValueError as error:
                                source_area = None
                                flags.append("ALAN_OKUNAMADI")
                                issue(
                                    source, fid, "ALAN_OKUNAMADI", str(error)
                                )
                            projected = QgsGeometry(qg)
                            if transform:
                                projected.transform(transform)
                                if transform.fallbackOperationOccurred():
                                    raise ValueError(
                                        "İzin verilmeyen alternatif dönüşüm kullanıldı."
                                    )
                            if profile["measurement"] == "planar":
                                area = projected.area()
                                perimeter = projected.length()
                                measurement = (
                                    "Düzlemsel / "
                                    + target.authid()
                                    + " / m²,m"
                                )
                            else:
                                area = meter.measureArea(qg)
                                perimeter = meter.measurePerimeter(qg)
                                measurement = (
                                    "Elipsoidal / "
                                    + src.ellipsoidAcronym()
                                    + " / m²,m"
                                )
                            if (
                                not math.isfinite(area)
                                or area <= 0
                                or not math.isfinite(perimeter)
                            ):
                                raise ValueError("Geçersiz alan/çevre hesabı.")
                            delta = (
                                area - source_area
                                if source_area is not None
                                else None
                            )
                            percent = (
                                delta / source_area * 100
                                if source_area
                                else None
                            )
                            if delta is not None and abs(delta) > 0.01:
                                flags.append("ALAN_FARKI")
                                report["summary"]["alan_farki_kaydi"] += 1
                                issue(
                                    source,
                                    fid,
                                    "ALAN_FARKI",
                                    f"Kaynak {source_area:.2f} m²; hesap {area:.2f} m²; fark {delta:+.2f} m². Sınır değiştirilmedi.",
                                )
                            key = json_text(
                                [
                                    attrs[x]
                                    for x in [
                                        "il",
                                        "ilce",
                                        "mahalle",
                                        "ada_no",
                                        "parsel_no",
                                    ]
                                ]
                            )
                            if key in seen_parcels:
                                flags.append("KIMLIK_TEKRARI")
                                issue(
                                    source,
                                    fid,
                                    "KIMLIK_TEKRARI",
                                    "Aynı idari bilgiler/ada/parsel mevcut; ayrı tut/birleştir seçimi ve tam XY eşleşmesi değerlendirildi.",
                                )
                            seen_parcels[key] = True
                            record = str(uuid.uuid4())
                            output_geom = ogr.CreateGeometryFromWkb(
                                bytes(projected.asWkb())
                            )
                            if (
                                ogr.GT_Flatten(output_geom.GetGeometryType())
                                == ogr.wkbPolygon
                            ):
                                multi = ogr.Geometry(ogr.wkbMultiPolygon)
                                multi.AddGeometry(output_geom)
                                output_geom = multi
                            values = {
                                "record_uid": record,
                                "parsel_anahtari": key,
                                **attrs,
                                "kaynak_alan_m2": source_area,
                                "hesap_alan_m2": area,
                                "hesap_cevre_m": perimeter,
                                "alan_farki_m2": delta,
                                "alan_farki_yuzde": percent,
                                "olcum_yontemi": measurement,
                                "hedef_crs": target.authid(),
                                "kontrol_durumu": ",".join(
                                    flags
                                    + (
                                        ["KAYNAK_KAYDI"]
                                        if merge_mode == "separate"
                                        else []
                                    )
                                )
                                or "OK",
                                "kaynak_fid": fid,
                                "alan_celiskileri_json": [],
                                "alan_kaynaklari_json": {
                                    k: {
                                        "source_uid": source_uid,
                                        "source_fid": fid,
                                        "format": source.format,
                                    }
                                    for k in STANDARD_TEXT
                                },
                            }
                            match = match_key(attrs, geographic)
                            existing = (
                                matched.get(match)
                                if match is not None
                                else None
                            )
                            if existing:
                                differences = []
                                for previous in existing["members"]:
                                    pair_diffs = [
                                        {
                                            "field": k,
                                            "left": previous["attrs"][k],
                                            "right": attrs[k],
                                            "left_source_uid": previous[
                                                "source_uid"
                                            ],
                                            "right_source_uid": source_uid,
                                            "left_source": previous["source"],
                                            "right_source": source.label,
                                            "left_format": previous["format"],
                                            "right_format": source.format,
                                            "left_record_uid": previous[
                                                "record_uid"
                                            ],
                                            "right_record_uid": (
                                                existing["record_uid"]
                                                if merge_mode == "merge"
                                                else record
                                            ),
                                        }
                                        for k in STANDARD_TEXT
                                        if previous["attrs"][k] != attrs[k]
                                    ]
                                    report["comparisons"].append(
                                        {
                                            "left_source": previous["source"],
                                            "right_source": source.label,
                                            "left_format": previous["format"],
                                            "right_format": source.format,
                                            "exact_xy_match": True,
                                            "content_difference_count": sum(
                                                not (
                                                    x["left"] in (None, "")
                                                    and x["right"]
                                                    in (None, "")
                                                )
                                                for x in pair_diffs
                                            ),
                                        }
                                    )
                                    differences.extend(pair_diffs)
                                representations = [
                                    d
                                    for d in differences
                                    if d["left"] in (None, "")
                                    and d["right"] in (None, "")
                                ]
                                differences = [
                                    d
                                    for d in differences
                                    if d not in representations
                                ]
                                report["representation_differences"].extend(
                                    representations
                                )
                                report["summary"][
                                    "bos_deger_temsil_farki"
                                ] += len(representations)
                                for diff in representations:
                                    issue(
                                        source,
                                        fid,
                                        "BOS_DEGER_TEMSILI",
                                        json_text(diff),
                                    )
                                report["conflicts"].extend(differences)
                                report["summary"]["oznitelik_farki"] += len(
                                    differences
                                )
                                for diff in differences:
                                    issue(
                                        source,
                                        fid,
                                        "OZNITELIK_FARKI",
                                        json_text(diff),
                                    )
                                if merge_mode == "merge":
                                    record = existing["record_uid"]
                                    row = parcels.GetFeature(existing["fid"])
                                    conflicts = (
                                        json.loads(
                                            row.GetField(
                                                "alan_celiskileri_json"
                                            )
                                            or "[]"
                                        )
                                        + differences
                                    )
                                    preferred = {
                                        "prefer_kml": "kml",
                                        "prefer_geojson": "geojson",
                                    }.get(attribute_policy)
                                    if (
                                        preferred
                                        and source.format == preferred
                                        and existing["selected_format"]
                                        != preferred
                                    ):
                                        for k, v in values.items():
                                            if k in (
                                                "record_uid",
                                                "alan_celiskileri_json",
                                            ):
                                                continue
                                            if v is None:
                                                row.SetFieldNull(
                                                    row.GetFieldIndex(k)
                                                )
                                            else:
                                                row.SetField(
                                                    k,
                                                    (
                                                        json_text(v)
                                                        if isinstance(
                                                            v, (dict, list)
                                                        )
                                                        else v
                                                    ),
                                                )
                                        existing["selected_format"] = preferred
                                    row.SetField(
                                        "alan_celiskileri_json",
                                        json_text(conflicts),
                                    )
                                    current = (
                                        row.GetField("kontrol_durumu") or ""
                                    )
                                    row.SetField(
                                        "kontrol_durumu",
                                        ",".join(
                                            dict.fromkeys(
                                                current.split(",")
                                                + ["BIRLESTIRILDI"]
                                                + (
                                                    ["KAYNAK_CELISKISI"]
                                                    if conflicts
                                                    else []
                                                )
                                            )
                                        ),
                                    )
                                    if parcels.SetFeature(row) != 0:
                                        raise RuntimeError(
                                            "Birleştirilmiş parsel güncellenemedi."
                                        )
                                    row = None
                                    report["summary"][
                                        "birlestirilen_kayit"
                                    ] += 1
                            if not existing or merge_mode == "separate":
                                row_id = add_row(parcels, values, output_geom)
                                report["summary"]["aktarilan_parsel"] += 1
                                if match is not None and not existing:
                                    matched[match] = {
                                        "fid": row_id,
                                        "record_uid": record,
                                        "selected_format": source.format,
                                        "members": [],
                                    }
                            if match is not None:
                                matched[match]["members"].append(
                                    {
                                        "attrs": attrs,
                                        "source_uid": source_uid,
                                        "source": source.label,
                                        "format": source.format,
                                        "record_uid": record,
                                    }
                                )
                            add_row(
                                links,
                                {
                                    "record_uid": record,
                                    "source_uid": source_uid,
                                    "source_fid": fid,
                                    "ozgun_alanlar_json": raw,
                                    "kaynak_metadata_json": getattr(
                                        feature, "metadata", {}
                                    ),
                                },
                            )
                            written_from_source += 1
                            report["summary"]["kaynak_baglantisi"] += 1
                        except Cancelled:
                            raise
                        except Exception as error:
                            # A storage failure invalidates the transaction, not just this feature.
                            if isinstance(error, RuntimeError):
                                raise
                            issue(source, fid, "KAYIT_REDDEDILDI", str(error))
                            report["summary"]["aktarilamayan_kayit"] += 1
                    if allowed is not None and found != allowed:
                        raise RuntimeError(
                            "Önizlemede seçilen kayıtlar kaynakta bulunamadı; yeniden tarayın."
                        )
                except Cancelled:
                    raise
                except RuntimeError:
                    raise
                except Exception as error:
                    issue(source, None, "KAYNAK_REDDEDILDI", str(error))
                    report["summary"]["kaynak_hatasi"] += 1
                    report["summary"]["aktarilamayan_kayit"] += (
                        len(allowed) if allowed is not None else source.count
                    ) - attempted_from_source
                finally:
                    source_ds = None
        for i in report["issues"]:
            add_row(
                checks,
                {
                    "kaynak": i["source"],
                    "source_fid": i["fid"],
                    "kod": i["code"],
                    "aciklama": i["message"],
                },
            )
        add_row(
            run,
            {
                "run_id": run_id,
                "tarih_utc": now,
                "surum": VERSION,
                "rapor_json": report,
            },
        )
        if ds.CommitTransaction() != 0:
            raise RuntimeError("GeoPackage işlemi tamamlanamadı.")
        parcels = source_table = links = checks = run = None
        close_dataset(ds)
        holder.ds = None
        ds = None
        check_cancel(cancel)
        if attribute_schema is not None:
            schema_editor.project(stage_gpkg, attribute_schema, report, cancel)
        reopened = ogr.Open(str(stage_gpkg), 0)
        if (
            reopened is None
            or reopened.GetLayerByName("parseller").GetFeatureCount()
            != report["summary"]["aktarilan_parsel"]
        ):
            raise RuntimeError(
                "Çıktıyı yeniden açma/kayıt sayısı kontrolü başarısız."
            )
        if (
            reopened.GetLayerByName("kayit_kaynaklari").GetFeatureCount()
            != report["summary"]["kaynak_baglantisi"]
        ):
            raise RuntimeError("Kaynak ilişkileri eksik.")
        reopened = None
        con = sqlite3.connect(stage_gpkg)
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("GeoPackage bütünlük kontrolü başarısız.")
            registration = con.execute(
                "SELECT s.organization,s.organization_coordsys_id FROM gpkg_geometry_columns g JOIN gpkg_spatial_ref_sys s USING(srs_id) WHERE g.table_name='parseller'"
            ).fetchone()
            if registration != ("EPSG", int(profile["crs"].split(":")[1])):
                raise RuntimeError(
                    "GeoPackage hedef EPSG kimliği doğrulanamadı."
                )
        finally:
            con.close()
        report["validation"] = {
            "reopened": True,
            "sqlite_integrity": "ok",
            "source_bytes_unchanged": True,
        }
        for s in ready:
            if sha(s.path) != s.file_sha256:
                raise RuntimeError(
                    "Kaynak dosya işlem sırasında değişti; çıktı yayınlanmadı."
                )
            with tempfile.TemporaryDirectory(
                prefix="verify_", dir=stage
            ) as folder:
                _, _, fingerprint = materialize(s, folder)
                if fingerprint != s.fingerprint:
                    raise RuntimeError(
                        "Kaynak bileşenleri işlem sırasında değişti; çıktı yayınlanmadı."
                    )
        from . import comparison

        comparison.attach(report, stage_gpkg)
        con = sqlite3.connect(stage_gpkg)
        try:
            con.execute(
                "UPDATE islem_ozeti SET rapor_json=? WHERE run_id=?",
                (json_text(report), run_id),
            )
            con.commit()
        finally:
            con.close()
        stage_json = stage / side_json.name
        stage_html = stage / side_html.name
        stage_json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        write_report(stage_html, report)
        check_cancel(cancel)
        published = []
        try:
            # Hard links provide atomic, no-overwrite publication on the same NTFS volume.
            for temporary, final in [
                (stage_json, side_json),
                (stage_html, side_html),
                (stage_gpkg, output),
            ]:
                os.link(temporary, final)
                published.append(final)
        except Exception:
            for final in published:
                final.unlink()
            raise
    progress(100, "Tamamlandı.")
    return report
