"""Local GeoJSON parcel reader. Preserve JSON types; never guess or fetch a CRS."""

from pathlib import Path
import json, math
from osgeo import ogr

MAX_JSON_BYTES = 64 * 1024 * 1024
WGS84_NAMES = {
    "EPSG:4326",
    "urn:ogc:def:crs:EPSG::4326",
    "urn:ogc:def:crs:EPSG:6.6:4326",
    "OGC:CRS84",
    "urn:ogc:def:crs:OGC:1.3:CRS84",
    "urn:ogc:def:crs:OGC::CRS84",
    "http://www.opengis.net/def/crs/OGC/1.3/CRS84",
    "http://www.opengis.net/def/crs/EPSG/0/4326",
}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Yinelenen JSON anahtarı: " + key)
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError("Geçersiz JSON sayı sabiti: " + value)


def validate_values(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Sonlu olmayan JSON sayı değeri.")
    if isinstance(value, dict):
        for v in value.values():
            validate_values(v)
    elif isinstance(value, list):
        for v in value:
            validate_values(v)


def crs_definition(obj):
    if "crs" not in obj:
        return "format_defined", "RFC 7946: WGS84, X=boylam / Y=enlem"
    crs = obj["crs"]
    if (
        not isinstance(crs, dict)
        or crs.get("type") != "name"
        or not isinstance(crs.get("properties"), dict)
    ):
        raise ValueError(
            "GeoJSON CRS null/bağlantı/belirsiz tanımı kabul edilmez; tahmin veya ağ erişimi yapılmadı."
        )
    name = crs["properties"].get("name")
    if not isinstance(name, str) or name not in WGS84_NAMES:
        raise ValueError(
            "Bu sürüm yalnız açık WGS84 GeoJSON CRS tanımlarını destekler: "
            + str(name)
        )
    return (
        "legacy_declared",
        "Eski GeoJSON açık CRS: " + name + "; X=boylam / Y=enlem",
    )


def ring(values):
    if not isinstance(values, list) or len(values) < 4:
        raise ValueError("GeoJSON halkası en az dört konum içermeli.")
    points = []
    for point in values:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError(
                "Yalnız 2B GeoJSON konumları desteklenir; Z/ek bileşen sessizce atılmadı."
            )
        if any(
            isinstance(x, bool)
            or not isinstance(x, (int, float))
            or not math.isfinite(x)
            for x in point
        ):
            raise ValueError("GeoJSON koordinatları sonlu sayılar olmalıdır.")
        x, y = point
        if not (-180 <= x <= 180 and -90 <= y <= 90):
            raise ValueError(
                "GeoJSON boylam/enlem aralığı geçersiz; eksenler otomatik değiştirilmedi."
            )
        points.append((x, y))
    if points[0] != points[-1]:
        raise ValueError("GeoJSON halkası kapalı değil; otomatik onarılmadı.")
    if any(abs(a[0] - b[0]) > 180 for a, b in zip(points, points[1:])):
        raise ValueError(
            "Antimeridyen geçen halka bu parsel sürümünde desteklenmiyor."
        )
    out = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in points:
        out.AddPoint_2D(x, y)
    return out


def polygon(coordinates):
    if not isinstance(coordinates, list) or not coordinates:
        raise ValueError("Boş/geçersiz Polygon koordinatları.")
    out = ogr.Geometry(ogr.wkbPolygon)
    for boundary in coordinates:
        out.AddGeometry(ring(boundary))
    return out


def geometry(obj, general=False):
    if not isinstance(obj, dict):
        raise ValueError("GeoJSON geometri null/eksik/geçersiz.")
    if "crs" in obj:
        crs_definition(obj)
    kind = obj.get("type")
    coords = obj.get("coordinates")
    if general and kind in (
        "Point",
        "MultiPoint",
        "LineString",
        "MultiLineString",
    ):

        def point(value):
            if (
                not isinstance(value, list)
                or len(value) != 2
                or any(
                    isinstance(x, bool)
                    or not isinstance(x, (int, float))
                    or not math.isfinite(x)
                    for x in value
                )
            ):
                raise ValueError("Yalnız sonlu 2B koordinatlar desteklenir.")
            if not (-180 <= value[0] <= 180 and -90 <= value[1] <= 90):
                raise ValueError("Koordinat boylam/enlem aralığı dışında.")

        def line(value):
            if not isinstance(value, list) or len(value) < 2:
                raise ValueError("Çizgi en az iki nokta içermeli.")
            for p in value:
                point(p)
            if any(abs(a[0] - b[0]) > 180 for a, b in zip(value, value[1:])):
                raise ValueError("Antimeridyen geçen çizgi desteklenmiyor.")

        if kind == "Point":
            point(coords)
        elif kind == "LineString":
            line(coords)
        else:
            if not isinstance(coords, list) or not coords:
                raise ValueError("Boş çok parçalı geometri.")
            for part in coords:
                (point if kind == "MultiPoint" else line)(part)
        return ogr.CreateGeometryFromJson(json.dumps(obj))
    if kind == "Polygon":
        return polygon(coords)
    if kind != "MultiPolygon":
        raise ValueError(
            "Parsel için Polygon/MultiPolygon gerekli: " + str(kind)
        )
    if not isinstance(coords, list) or not coords:
        raise ValueError("Boş/geçersiz MultiPolygon.")
    out = ogr.Geometry(ogr.wkbMultiPolygon)
    for part in coords:
        out.AddGeometry(polygon(part))
    return out


class Feature:
    def __init__(self, index, obj, collection_metadata, general=False):
        self.index = index
        self.raw = {}
        self.error = ""
        self.geom = None
        self.warnings = []
        self.metadata = {
            "feature_object": obj,
            "collection_metadata": collection_metadata,
        }
        try:
            if not isinstance(obj, dict) or obj.get("type") != "Feature":
                raise ValueError("FeatureCollection üyesi Feature olmalıdır.")
            if "crs" in obj:
                crs_definition(obj)
            if "id" in obj and (
                isinstance(obj["id"], bool)
                or not isinstance(obj["id"], (str, int, float))
            ):
                raise ValueError("GeoJSON id metin veya sayı olmalıdır.")
            if "properties" not in obj:
                raise ValueError("Feature properties üyesi eksik.")
            props = obj["properties"]
            if props is not None and not isinstance(props, dict):
                raise ValueError("Properties nesne veya null olmalıdır.")
            self.raw = props if props is not None else {}
            self.geom = geometry(obj.get("geometry"), general)
        except (ValueError, TypeError) as error:
            self.error = str(error)

    def GetFID(self):
        return self.index

    def GetGeometryRef(self):
        return self.geom


class Dataset:
    def __init__(self, path, general=False):
        path = Path(path)
        if path.stat().st_size > MAX_JSON_BYTES:
            raise ValueError("GeoJSON 64 MiB sınırını aşıyor.")
        data = path.read_bytes()
        self.root = json.loads(
            data.decode("utf-8-sig"),
            object_pairs_hook=unique_object,
            parse_constant=invalid_constant,
        )
        validate_values(self.root)
        if not isinstance(self.root, dict) or self.root.get("type") not in (
            "Feature",
            "FeatureCollection",
        ):
            raise ValueError(
                "JSON bir GeoJSON Feature veya FeatureCollection değil."
            )
        self.crs_basis, self.crs_evidence = crs_definition(self.root)
        if self.root["type"] == "FeatureCollection":
            features = self.root.get("features")
            if not isinstance(features, list) or not features:
                raise ValueError(
                    "FeatureCollection features dizisi boş/eksik/geçersiz."
                )
            metadata = {k: v for k, v in self.root.items() if k != "features"}
        else:
            features = [self.root]
            metadata = {}
        self.features = [
            Feature(i, obj, metadata, general)
            for i, obj in enumerate(features)
        ]
        self.field_names = list(
            dict.fromkeys(k for f in self.features for k in f.raw)
        )
        self.declared_layer_name = (
            self.root.get("name")
            if isinstance(self.root.get("name"), str)
            else ""
        )
        self.layer_name = self.declared_layer_name or path.stem

    def GetName(self):
        return self.layer_name

    def GetLayer(self, index):
        return self

    def GetFeatureCount(self):
        return len(self.features)

    def __iter__(self):
        return iter(self.features)
