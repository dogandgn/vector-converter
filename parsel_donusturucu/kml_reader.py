"""Strict local KML 2.2 reader, independent of LIBKML field-name rewriting."""

from pathlib import Path
import math
import xml.etree.ElementTree as ET  # nosec B405
from osgeo import ogr
from . import safe_xml

NS = "{http://www.opengis.net/kml/2.2}"


def text(node, name):
    child = node.find(NS + name)
    return "".join(child.itertext()) if child is not None else None


def polygon(node):
    if any(
        e.tag.startswith("{http://www.google.com/kml/ext/")
        for e in node.iter()
    ):
        raise ValueError("Google gx geometri uzantısı desteklenmiyor.")
    mode = text(node, "altitudeMode") or "clampToGround"
    if mode != "clampToGround":
        raise ValueError(
            "Yükseklik anlamı taşıyan altitudeMode 2B çıktıya alınmadı: "
            + mode
        )
    for e in node.iter(NS + "altitudeMode"):
        if e.text != "clampToGround":
            raise ValueError(
                "İç halkada yükseklik anlamı taşıyan altitudeMode desteklenmiyor."
            )
    outers = node.findall(NS + "outerBoundaryIs")
    if len(outers) != 1:
        raise ValueError("Polygon tam bir dış sınır içermelidir.")
    geom = ogr.Geometry(ogr.wkbPolygon)
    had_zero_z = False
    for boundary in outers + node.findall(NS + "innerBoundaryIs"):
        rings = boundary.findall(NS + "LinearRing")
        if len(rings) != 1:
            raise ValueError("Sınır tam bir LinearRing içermelidir.")
        coords = rings[0].findall(NS + "coordinates")
        if len(coords) != 1 or not coords[0].text:
            raise ValueError("Koordinat dizisi eksik.")
        points = []
        for token in coords[0].text.split():
            values = token.split(",")
            if len(values) not in (2, 3):
                raise ValueError("Geçersiz KML koordinat bileşen sayısı.")
            p = [float(v) for v in values]
            if not all(math.isfinite(v) for v in p):
                raise ValueError("Sonlu olmayan KML koordinatı.")
            if not (-180 <= p[0] <= 180 and -90 <= p[1] <= 90):
                raise ValueError("KML boylam/enlem aralığı geçersiz.")
            if len(p) == 3:
                if p[2] != 0:
                    raise ValueError(
                        "KML gerçek Z değeri 2B çıktıda sessizce atılmadı."
                    )
                had_zero_z = True
            points.append(p[:2])
        if len(points) < 4 or points[0] != points[-1]:
            raise ValueError(
                "KML halkası kapalı değil veya dört noktadan az; otomatik onarılmadı."
            )
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in points:
            ring.AddPoint_2D(x, y)
        geom.AddGeometry(ring)
    return geom, had_zero_z


def geometry(node, general=False):
    if node.tag == NS + "Polygon":
        return polygon(node)
    if general and node.tag in (NS + "Point", NS + "LineString"):
        if any(
            e.tag.startswith("{http://www.google.com/kml/ext/")
            for e in node.iter()
        ):
            raise ValueError("gx uzantısı desteklenmiyor.")
        if (text(node, "altitudeMode") or "clampToGround") != "clampToGround":
            raise ValueError("Gerçek yükseklik anlamı 2B çıktıya alınmaz.")
        nodes = node.findall(NS + "coordinates")
        if len(nodes) != 1 or not nodes[0].text:
            raise ValueError("Koordinatlar eksik.")
        points = []
        zero = False
        for item in nodes[0].text.split():
            values = [float(v) for v in item.split(",")]
            if len(values) not in (2, 3) or not all(
                math.isfinite(v) for v in values
            ):
                raise ValueError("Geçersiz koordinat.")
            if len(values) == 3:
                if values[2] != 0:
                    raise ValueError(
                        "Gerçek Z değeri 2B çıktıda kaybedilemez."
                    )
                zero = True
            if not (-180 <= values[0] <= 180 and -90 <= values[1] <= 90):
                raise ValueError("Koordinat boylam/enlem aralığı dışında.")
            points.append(values[:2])
        is_point = node.tag == NS + "Point"
        if (is_point and len(points) != 1) or (
            not is_point and len(points) < 2
        ):
            raise ValueError("Geometri için nokta sayısı geçersiz.")
        if any(abs(a[0] - b[0]) > 180 for a, b in zip(points, points[1:])):
            raise ValueError("Antimeridyen geçen çizgi desteklenmiyor.")
        out = ogr.Geometry(ogr.wkbPoint if is_point else ogr.wkbLineString)
        for x, y in points:
            out.AddPoint_2D(x, y)
        return out, zero
    if node.tag != NS + "MultiGeometry":
        raise ValueError(
            "Yalnız Polygon/MultiGeometry parselleri desteklenir."
        )
    if general:
        parts = [geometry(child, True) for child in node]
        if not parts:
            raise ValueError("Boş çok parçalı geometri.")
        families = {
            ogr.GT_GetCollection(ogr.GT_Flatten(g.GetGeometryType()))
            for g, z in parts
        }
        if len(families) != 1:
            raise ValueError(
                "Tek kayıt içindeki karma GeometryCollection desteklenmiyor; geometri türlerini ayrı kayıtlara ayırın."
            )
        kind = next(iter(families))
        out = ogr.Geometry(kind)
        for geom, zero in parts:
            if geom.GetGeometryType() == kind:
                for i in range(geom.GetGeometryCount()):
                    out.AddGeometry(geom.GetGeometryRef(i))
            else:
                out.AddGeometry(geom)
        return out, any(z for g, z in parts)
    out = ogr.Geometry(ogr.wkbMultiPolygon)
    zero = False
    for child in node:
        geom, had_zero = geometry(child)
        zero |= had_zero
        if geom.GetGeometryType() == ogr.wkbPolygon:
            out.AddGeometry(geom)
        else:
            for i in range(geom.GetGeometryCount()):
                out.AddGeometry(geom.GetGeometryRef(i))
    if out.IsEmpty():
        raise ValueError("Boş MultiGeometry.")
    return out, zero


class Feature:
    def __init__(self, index, pm, general=False):
        self.index = index
        self.raw = {}
        self.geom = None
        self.error = ""
        self.warnings = []
        self.metadata = {
            "name": text(pm, "name"),
            "description": text(pm, "description"),
            "placemark_id": pm.get("id"),
            "placemark_xml": ET.tostring(pm, encoding="unicode"),
        }
        try:
            extended = pm.findall(NS + "ExtendedData")
            if len(extended) > 1:
                raise ValueError("Birden fazla ExtendedData desteklenmiyor.")
            if extended:
                for child in extended[0]:
                    if child.tag == NS + "Data":
                        values = child.findall(NS + "value")
                        if len(values) != 1:
                            raise ValueError(
                                "Data alanında tek value gerekli."
                            )
                        pairs = [
                            (child.get("name"), "".join(values[0].itertext()))
                        ]
                    elif child.tag == NS + "SchemaData":
                        pairs = []
                        for field in child:
                            if field.tag != NS + "SimpleData":
                                raise ValueError(
                                    "Desteklenmeyen SchemaData içeriği."
                                )
                            pairs.append(
                                (field.get("name"), "".join(field.itertext()))
                            )
                    else:
                        raise ValueError(
                            "Desteklenmeyen ExtendedData içeriği; sessizce atılmadı."
                        )
                    for name, value in pairs:
                        if not name or name in self.raw:
                            raise ValueError(
                                "Eksik veya yinelenen KML alan adı."
                            )
                        self.raw[name] = value
            else:
                self.warnings.append(
                    "ExtendedData yok; description saklandı, HTML içinden parsel alanı tahmin edilmedi."
                )
            geometries = [
                c
                for c in pm
                if c.tag
                in {
                    NS + x
                    for x in [
                        "Polygon",
                        "MultiGeometry",
                        "Point",
                        "LineString",
                        "LinearRing",
                        "Model",
                    ]
                }
                or c.tag.endswith("}Track")
                or c.tag.endswith("}MultiTrack")
            ]
            if len(geometries) != 1:
                raise ValueError(
                    "Placemark tek bir desteklenen geometri içermelidir."
                )
            self.geom, zero = geometry(geometries[0], general)
            if zero:
                self.warnings.append(
                    "clampToGround sıfır Z değeri 2B işlendi; özgün XML saklandı."
                )
        except (ValueError, TypeError) as error:
            self.error = str(error)

    def GetFID(self):
        return self.index

    def GetGeometryRef(self):
        return self.geom


class Dataset:
    def __init__(self, path, general=False):
        path = Path(path)
        self.root = safe_xml.parse(path)
        if self.root.tag != NS + "kml":
            raise ValueError("OGC KML 2.2 namespace gerekli.")
        if any(
            e.tag
            in {
                NS + "NetworkLink",
                NS + "Update",
                NS + "GroundOverlay",
                NS + "PhotoOverlay",
                NS + "ScreenOverlay",
            }
            for e in self.root.iter()
        ):
            raise ValueError(
                "Bağlantı, güncelleme veya overlay içeriği desteklenmiyor; ağ erişimi yapılmadı."
            )
        self.features = [
            Feature(i, pm, general)
            for i, pm in enumerate(self.root.iter(NS + "Placemark"))
        ]
        if not self.features:
            raise ValueError("KML içinde Placemark yok.")
        self.field_names = list(
            dict.fromkeys(k for f in self.features for k in f.raw)
        )
        document = self.root.find(NS + "Document")
        self.declared_layer_name = (
            text(document, "name") if document is not None else None
        ) or ""
        self.layer_name = self.declared_layer_name or path.stem

    def GetName(self):
        return self.layer_name

    def GetLayer(self, index):
        return self

    def GetFeatureCount(self):
        return len(self.features)

    def __iter__(self):
        return iter(self.features)
