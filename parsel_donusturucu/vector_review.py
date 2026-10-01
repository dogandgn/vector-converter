"""General 2D vector validation and conservative, explainable record matching."""

from difflib import SequenceMatcher
from itertools import combinations
import hashlib, math, tempfile, unicodedata
from osgeo import ogr, osr
from qgis.core import (
    QgsGeometry,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsDatumTransform,
)
from . import core

KINDS = {
    ogr.wkbPoint: "noktalar",
    ogr.wkbMultiPoint: "noktalar",
    ogr.wkbLineString: "cizgiler",
    ogr.wkbMultiLineString: "cizgiler",
    ogr.wkbPolygon: "poligonlar",
    ogr.wkbMultiPolygon: "poligonlar",
}
LABELS = {"noktalar": "Nokta", "cizgiler": "Çizgi", "poligonlar": "Poligon"}
MATCH_NOTE = "Kesin tekrar: aynı 2B geometri + aynı dolu öznitelikler. Ad benzerliği tek başına seçim veya birleştirme yapmaz."


def geometry_key(geom):
    kind = KINDS[ogr.GT_Flatten(geom.GetGeometryType())]
    simple = ogr.GT_Flatten(geom.GetGeometryType()) in (
        ogr.wkbPoint,
        ogr.wkbLineString,
        ogr.wkbPolygon,
    )
    parts = (
        [geom]
        if simple
        else [geom.GetGeometryRef(i) for i in range(geom.GetGeometryCount())]
    )

    def points(g):
        return tuple((g.GetX(i), g.GetY(i)) for i in range(g.GetPointCount()))

    def ring(g):
        pts = list(points(g))
        pts = pts[:-1] if pts and pts[0] == pts[-1] else pts
        if not pts:
            raise ValueError("Boş halka.")
        candidates = []
        for seq in (pts, list(reversed(pts))):
            smallest = min(seq)
            candidates.extend(
                tuple(seq[i:] + seq[:i])
                for i, p in enumerate(seq)
                if p == smallest
            )
        return min(candidates)

    if kind == "poligonlar":
        value = sorted(
            (
                ring(p.GetGeometryRef(0)),
                tuple(
                    sorted(
                        ring(p.GetGeometryRef(i))
                        for i in range(1, p.GetGeometryCount())
                    )
                ),
            )
            for p in parts
        )
    elif kind == "noktalar":
        value = sorted(points(p) for p in parts)
    else:
        # Line direction and part order can carry meaning; never reverse them.
        value = [points(p) for p in parts]
    return hashlib.sha256(
        core.json_text([kind, value]).encode("utf-8")
    ).hexdigest()


def attributes_key(raw):
    if not raw or not any(v is not None and v != "" for v in raw.values()):
        return None
    values = {}
    for key, value in raw.items():
        name = unicodedata.normalize("NFC", key).strip().casefold()
        if name in values:
            return None
        # Type tags prevent conflating identifiers like "001", 1, True and 1.0.
        values[name] = [type(value).__name__, value]
    return core.json_text(sorted(values.items()))


def transformed(geometry, src, dst, cache, bounds=None):
    # WGS 84 ensembles are not an epoch-specific realization requiring a time transform.
    if any(
        c.isDynamic() and not c.datumEnsemble().isValid() for c in (src, dst)
    ):
        raise ValueError(
            "Dinamik koordinat sistemi için epok dönüşümü bu sürümde desteklenmiyor."
        )
    if src == dst:
        return QgsGeometry(geometry), {
            "name": "Aynı koordinat sistemi",
            "pipeline": "identity",
            "accuracy_m": 0,
        }
    key = (src.toWkt(), dst.toWkt())
    if key not in cache:
        a = osr.SpatialReference()
        a.ImportFromWkt(src.toWkt())
        b = osr.SpatialReference()
        b.ImportFromWkt(dst.toWkt())
        same = bool(a.IsSameGeogCS(b))
        cache[key] = sorted(
            (
                o
                for o in QgsDatumTransform.operations(src, dst)
                if o.isAvailable
                and (o.accuracy >= 0 or same)
                and "ballpark" not in o.name.lower()
            ),
            key=lambda o: o.accuracy,
        )
    failures = []
    for operation in cache[key]:
        if (
            bounds is not None
            and not operation.bounds.isNull()
            and not operation.bounds.contains(bounds)
        ):
            continue
        context = QgsCoordinateTransformContext()
        context.addCoordinateOperation(src, dst, operation.proj, False)
        transform = QgsCoordinateTransform(src, dst, context)
        transform.setAllowFallbackTransforms(False)
        transform.setBallparkTransformsAreAppropriate(False)
        result = QgsGeometry(geometry)
        try:
            result.transform(transform)
        except Exception as error:
            failures.append(str(error))
            continue
        if transform.fallbackOperationOccurred():
            continue
        if (
            dst.authid() == "EPSG:4326"
            and not operation.bounds.isNull()
            and not operation.bounds.contains(result.boundingBox())
        ):
            continue
        return result, {
            "name": operation.name,
            "pipeline": operation.proj,
            "accuracy_m": max(0, operation.accuracy),
        }
    detail = (
        " Ayrıntı: " + "; ".join(dict.fromkeys(failures))[:600]
        if failures
        else ""
    )
    raise ValueError(
        "Verinin konumu için kullanılabilir, doğruluğu tanımlı koordinat dönüşümü bulunamadı."
        + detail
    )


def read_records(sources, cancel, progress):
    records = []
    blocked = []
    cache = {}
    wgs = QgsCoordinateReferenceSystem("EPSG:4326")
    for si, source in enumerate(sources):
        core.check_cancel(cancel)
        if source.status != "ready":
            continue
        progress(90 * si / max(1, len(sources)), source.label)
        allowed = (
            None
            if source.selected_fids is None
            else set(map(str, source.selected_fids))
        )
        count = source.count if allowed is None else len(allowed)
        src = (
            QgsCoordinateReferenceSystem.fromWkt(source.wkt)
            if source.wkt
            else QgsCoordinateReferenceSystem(source.assigned_crs)
        )
        if not src.isValid():
            blocked.append(
                {
                    "source_index": si,
                    "count": count,
                    "kind": "missing_crs",
                    "message": "Kaynak koordinatı belirlenemedi. Koordinat tanım dosyasını ekleyin veya bu girdiyi seçimden çıkarın.",
                }
            )
            continue
        errors = []
        found = set()
        good = []
        try:
            if core.sha(source.path) != source.file_sha256:
                raise RuntimeError(
                    "Girdi taramadan sonra değişmiş; yeniden tarayın."
                )
            with tempfile.TemporaryDirectory(prefix=".vector_read_") as folder:
                path, _, fingerprint = core.materialize(source, folder)
                if fingerprint != source.fingerprint:
                    raise RuntimeError(
                        "Kaynak bileşenleri değişmiş; yeniden tarayın."
                    )
                ds = core.open_source(path, general=True)
                try:
                    for feature in ds.GetLayer(0):
                        core.check_cancel(cancel)
                        fid = str(feature.GetFID())
                        if allowed is not None and fid not in allowed:
                            continue
                        found.add(fid)
                        try:
                            if getattr(feature, "error", ""):
                                raise ValueError(feature.error)
                            geom = feature.GetGeometryRef()
                            if geom is None or geom.IsEmpty():
                                raise ValueError("Boş geometri.")
                            kind = KINDS.get(
                                ogr.GT_Flatten(geom.GetGeometryType())
                            )
                            if not kind:
                                raise ValueError(
                                    "Yalnız nokta, çizgi, poligon ve bunların çok parçalı türleri desteklenir."
                                )
                            geom = geom.Clone()
                            if geom.Is3D() or geom.IsMeasured():
                                if any(
                                    (z is not None and math.isfinite(z))
                                    or (
                                        m is not None
                                        and math.isfinite(m)
                                        and m > -1e38
                                    )
                                    for z, m in core.dimensional_values(geom)
                                ):
                                    raise ValueError(
                                        "Gerçek Z/M verisi 2B çıktıda sessizce atılmaz."
                                    )
                                geom.FlattenTo2D()
                                geom.SetMeasured(False)
                            qg = core.qgeom(geom)
                            if not qg.isGeosValid():
                                raise ValueError(
                                    "Geçersiz geometri; otomatik onarım uygulanmadı."
                                )
                            geographic, operation = transformed(
                                qg, src, wgs, cache
                            )
                            b = geographic.boundingBox()
                            if not (
                                -180 <= b.xMinimum() <= b.xMaximum() <= 180
                                and -90 <= b.yMinimum() <= b.yMaximum() <= 90
                            ):
                                raise ValueError(
                                    "Geçersiz coğrafi koordinat aralığı."
                                )
                            if (
                                not src.bounds().isNull()
                                and not src.bounds().contains(b)
                            ):
                                raise ValueError(
                                    "Kaynak koordinat sisteminin kullanım alanı dışında."
                                )
                            if kind == "cizgiler" and qg.length() <= 0:
                                raise ValueError("Çizgi uzunluğu sıfır.")
                            if kind == "poligonlar" and qg.area() <= 0:
                                raise ValueError("Poligon alanı sıfır.")
                            raw = (
                                feature.raw
                                if source.format in ("kml", "geojson")
                                else {
                                    field.name: feature.GetField(i)
                                    for i, field in enumerate(
                                        ds.GetLayer(0).schema
                                    )
                                }
                            )
                            core.json_text(raw).encode(
                                "utf-8", errors="strict"
                            )
                            canonical = ogr.CreateGeometryFromWkb(
                                bytes(geographic.asWkb())
                            )
                            good.append(
                                {
                                    "source_index": si,
                                    "fid": fid,
                                    "format": source.format,
                                    "label": source.label,
                                    "raw": raw,
                                    "attrs": raw,
                                    "kind": kind,
                                    "geometry_key": geometry_key(canonical),
                                    "attributes_key": attributes_key(raw),
                                    "source_geometry": bytes(qg.asWkb()).hex(),
                                    "geographic": bytes(
                                        geographic.asWkb()
                                    ).hex(),
                                    "source_wkt": src.toWkt(),
                                    "operation": operation,
                                    "mergeable": True,
                                    "metadata": getattr(
                                        feature, "metadata", {}
                                    ),
                                }
                            )
                        except ValueError as error:
                            errors.append(f"Kayıt {fid}: {error}")
                finally:
                    feature = None
                    if source.format == "shapefile":
                        core.close_dataset(ds)
                    ds = None
            if allowed is not None and found != allowed:
                raise RuntimeError(
                    "Seçili kayıtlar değişmiş; yeniden tarayın."
                )
        except core.Cancelled:
            raise
        except RuntimeError:
            raise
        except Exception as error:
            errors.append(str(error))
        records.extend(good)
        if errors:
            blocked.append(
                {
                    "source_index": si,
                    "count": max(0, count - len(good)),
                    "message": "; ".join(dict.fromkeys(errors)),
                }
            )
    return records, blocked


def build(sources, cancel=lambda: False, progress=lambda n, s: None):
    records, blocked = read_records(sources, cancel, progress)
    groups = {}
    geometries = {}
    for record in records:
        core.check_cancel(cancel)
        key = (
            (record["kind"], record["geometry_key"], record["attributes_key"])
            if record["attributes_key"] is not None
            else ("unique", record["source_index"], record["fid"])
        )
        if key not in groups:
            group = {
                "id": len(groups) + 1,
                "identity": LABELS[record["kind"]]
                + " · "
                + str(len(groups) + 1),
                "records": [],
                "differences": [],
                "same_identity_other_geometry": False,
                "match_note": MATCH_NOTE,
                "kind": record["kind"],
            }
            groups[key] = group
            geometries.setdefault(record["geometry_key"], []).append(group)
        groups[key]["records"].append(record)
    for group in groups.values():
        peers = geometries[group["records"][0]["geometry_key"]]
        group["similar_ids"] = [g["id"] for g in peers if g is not group]
        group["notice"] = (
            "Aynı geometri, farklı/eksik öznitelik; ayrı tutulur"
            if group["similar_ids"]
            else "Kesin tekrar" if len(group["records"]) > 1 else "Tek kayıt"
        )
    layers = []
    for si, source in enumerate(sources):
        rows = [r for r in records if r["source_index"] == si]
        if rows:
            layers.append(
                (
                    si,
                    {r["kind"] for r in rows},
                    {n for r in rows for n in r["raw"]},
                    {r["geometry_key"] for r in rows},
                    {
                        (r["geometry_key"], r["attributes_key"])
                        for r in rows
                        if r["attributes_key"] is not None
                    },
                )
            )
    similarities = []
    for a, b in combinations(layers, 2):
        core.check_cancel(cancel)
        ai, ak, af, ag, ar = a
        bi, bk, bf, bg, br = b
        common = af & bf
        geoms = ag & bg
        exact = ar & br
        if not ak & bk or not (common or geoms):
            continue
        names = SequenceMatcher(
            None,
            sources[ai].layer_name.casefold(),
            sources[bi].layer_name.casefold(),
        ).ratio()
        score = round(
            55 * len(geoms) / max(1, len(ag | bg))
            + 20 * len(common) / max(1, len(af | bf))
            + 20 * len(exact) / max(1, len(ar | br))
            + 5 * names,
            1,
        )
        similarities.append(
            {
                "left": ai,
                "right": bi,
                "score": score,
                "geometry_matches": len(geoms),
                "exact_matches": len(exact),
                "common_fields": sorted(common),
                "name_similarity": round(names * 100),
                "reason": (
                    "Kesin eş kayıtlar var"
                    if exact
                    else "İnceleme önerisi; otomatik eleme yok"
                ),
            }
        )
    progress(100, "Vektör karşılaştırması hazır.")
    return {
        "mode": "vector",
        "groups": list(groups.values()),
        "blocked": blocked,
        "missing_crs": "",
        "source_count": len(sources),
        "layer_similarities": sorted(similarities, key=lambda r: -r["score"]),
        "summary": {
            "valid_records": len(records),
            "groups": len(groups),
            "repeated_groups": sum(
                len(g["records"]) > 1 for g in groups.values()
            ),
        },
    }
