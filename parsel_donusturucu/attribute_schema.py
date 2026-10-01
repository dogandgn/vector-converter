"""Explicit output schema. Source values and the matching model remain immutable."""

import hashlib
import json
import math
import re
import unicodedata
import uuid
from collections import defaultdict
from pathlib import Path
from osgeo import ogr
from . import core
from .compat import close_dataset

TYPES = {"text": ogr.OFTString, "integer": ogr.OFTInteger, "real": ogr.OFTReal}
SYSTEM = {
    "record_uid",
    "parsel_anahtari",
    "kaynak_alan_m2",
    "hesap_alan_m2",
    "hesap_cevre_m",
    "alan_farki_m2",
    "alan_farki_yuzde",
    "olcum_yontemi",
    "hedef_crs",
    "kontrol_durumu",
    "kaynak_fid",
    "alan_celiskileri_json",
    "alan_kaynaklari_json",
}
RESERVED = SYSTEM | {
    "fid",
    "geom",
    "geometry",
    "objectid",
    "shape",
    "parsel_oznitelikleri_json",
    "globalid",
}
RESERVED.update(
    "add alter and as asc between by column create date delete desc drop exists for from group in insert into is like not null or order select set table update values where all result oid".split()
)
RESERVED.update(
    {
        "geometri_turu",
        "kaynak_sayisi",
        "hesap_uzunluk_m",
        "vektor_oznitelikleri_json",
    }
)
TABLES = (
    "parseller",
    "kaynaklar",
    "kayit_kaynaklari",
    "kontroller",
    "islem_ozeti",
)


def signature(sources):
    active = sorted(
        (
            s.path,
            s.member,
            s.fingerprint,
            (
                None
                if s.selected_fids is None
                else sorted(map(str, s.selected_fids))
            ),
        )
        for s in sources
        if s.status == "ready"
    )
    return hashlib.sha256(core.json_text(active).encode("utf-8")).hexdigest()


def inventory(sources, review, selected):
    """Presence is per selected feature, not merely the union of a layer's schema."""
    rows = [
        r
        for g in review["groups"]
        for r in g["records"]
        if (r["source_index"], r["fid"]) in selected
    ]
    names = sorted({name for r in rows for name in r.get("raw", {})})
    result = []
    for name in names:
        files = []
        values = []
        for si in sorted({r["source_index"] for r in rows}):
            records = [r for r in rows if r["source_index"] == si]
            vals = [
                r["raw"][name] for r in records if name in r.get("raw", {})
            ]
            values.extend(vals)
            examples = list(dict.fromkeys(core.json_text(v) for v in vals))[:3]
            files.append(
                {
                    "source_index": si,
                    "path": sources[si].path,
                    "member": sources[si].member,
                    "format": sources[si].format,
                    "records": len(records),
                    "present": len(vals),
                    "null": sum(v is None for v in vals),
                    "examples": examples,
                }
            )
        result.append(
            {
                "source_name": name,
                "canonical": (
                    name
                    if review.get("mode") == "vector"
                    else core.ALIASES.get(name, name)
                ),
                "files": files,
                "values": values,
                "present": len(values),
                "records": len(rows),
                "null": sum(v is None for v in values),
            }
        )
    return result


def portable_name(value):
    value = (
        unicodedata.normalize("NFKD", value.replace("ı", "i"))
        .encode("ascii", "ignore")
        .decode()
    )
    value = re.sub("[^A-Za-z0-9_]+", "_", value).strip("_") or "alan"
    if value[0].isdigit():
        value = "alan_" + value
    if value.casefold() in RESERVED:
        value = "kaynak_" + value
    return value[:60]


def default_schema(items, sources, general=False):
    general = general or any(s.data_mode == "vector" for s in sources)
    groups = {}
    used = set()
    for item in items:
        key = item["canonical"]
        if key not in groups:
            base = portable_name(key)
            name = base
            n = 2
            while name.casefold() in used:
                name = base + "_" + str(n)
                n += 1
            used.add(name.casefold())
            groups[key] = {
                "name": name,
                "sources": [],
                "type": "text",
                "enabled": True,
                "values": [],
            }
        groups[key]["sources"].append(item["source_name"])
        groups[key]["values"].extend(item["values"])
    for key, f in groups.items():
        values = [v for v in f.pop("values") if v is not None]
        if (general or key not in core.STANDARD_TEXT) and values:
            if all(
                type(v) is int and -(2**31) <= v <= 2**31 - 1 for v in values
            ):
                f["type"] = "integer"
            elif any(type(v) is float for v in values) and all(
                type(v) in (int, float)
                and math.isfinite(v)
                and abs(v) <= 2**53 - 1
                for v in values
            ):
                f["type"] = "real"
    return {
        "version": 1,
        "selection_signature": signature(sources),
        "fields": list(groups.values()),
        "allow_missing": True,
        "fill_from_other_source": False,
    }


def validate(plan, sources):
    if not isinstance(plan, dict) or plan.get("version") != 1:
        raise ValueError("Öznitelik planının sürümü geçersiz.")
    if plan.get("selection_signature") != signature(sources):
        raise ValueError(
            "Girdi seçimi değişti. Öznitelik düzenleyicisini açıp yeni seçimi onaylayın."
        )
    available = {
        f["name"]
        for s in sources
        if s.status == "ready"
        for f in (s.fields or [])
    }
    used = set()
    mapped = set()
    for field in plan["fields"]:
        if not field["enabled"]:
            continue
        name = field["name"]
        if not re.fullmatch("[A-Za-z][A-Za-z0-9_]{0,63}", name):
            raise ValueError(
                "Sütun adı harfle başlamalı; en fazla 64 ASCII harf, rakam veya alt çizgi: "
                + name
            )
        if name.casefold() in RESERVED or name.casefold() in used:
            raise ValueError("Ayrılmış veya tekrarlanan sütun adı: " + name)
        used.add(name.casefold())
        if field["type"] not in TYPES:
            raise ValueError("Desteklenmeyen alan türü.")
        if not field["sources"] or not set(field["sources"]).issubset(
            available
        ):
            raise ValueError(
                "Kaynak alan eşlemesi eksik veya geçersiz: " + name
            )
        if mapped.intersection(field["sources"]):
            raise ValueError(
                "Bir kaynak alanı iki çıktı sütununa atanmış. Eşlemeyi düzeltin: "
                + name
            )
        mapped.update(field["sources"])
    return plan


def coerce(value, kind):
    if value is None:
        return None
    if kind == "text":
        return value if isinstance(value, str) else core.json_text(value)
    # IDs with leading zeros and locale decimal strings require an explicit text field.
    if isinstance(value, bool):
        raise ValueError("Mantıksal değer sayıya otomatik çevrilmez.")
    if kind == "integer":
        if type(value) is int:
            v = value
        elif isinstance(value, str) and re.fullmatch(
            "-?(0|[1-9][0-9]*)", value
        ):
            v = int(value)
        else:
            raise ValueError("Tam sayıya kayıpsız dönüşüm yapılamadı.")
        if not -(2**31) <= v <= 2**31 - 1:
            raise ValueError(
                "Ortak 32 bit tam sayı sınırı aşıldı; metin seçin."
            )
        return v
    if type(value) not in (int, float):
        raise ValueError(
            "Ondalık alan için gerçek sayısal değer gerekli; metin seçin."
        )
    if not math.isfinite(value) or (
        type(value) is int and abs(value) > 2**53 - 1
    ):
        raise ValueError("Sayısal kesinlik sınırı aşıldı; metin seçin.")
    return float(value)


def pick(field, members, policy, fill):
    preferred = {"prefer_kml": "kml", "prefer_geojson": "geojson"}.get(policy)
    ordered = sorted(
        members,
        key=lambda m: 0 if preferred and m["format"] == preferred else 1,
    )
    candidates = []
    for member in ordered:
        values = [
            coerce(member["raw"][key], field["type"])
            for key in field["sources"]
            if key in member["raw"]
        ]
        if values and any(v != values[0] for v in values[1:]):
            raise ValueError(
                "Aynı kayıttaki eşlenen alanlar farklı değerler içeriyor: "
                + field["name"]
            )
        candidates.append(
            {
                "source_uid": member["source_uid"],
                "source_fid": member["source_fid"],
                "format": member["format"],
                "present": bool(values),
                "value": values[0] if values else None,
            }
        )
    chosen = (
        next((c for c in candidates if c["value"] is not None), candidates[0])
        if fill
        else candidates[0]
    )
    return chosen, candidates


def project(path, plan, report, cancel):
    """Build and verify a second private dataset before any public output is created."""
    original = ogr.Open(str(path), 0)
    dest = None
    verified = None
    target = Path(path).with_name(".schema_" + uuid.uuid4().hex + ".gpkg")
    expected = {}
    audit = {
        "plan": plan,
        "missing_values": 0,
        "null_values": 0,
        "conflicts": [],
        "value_sources": [],
        "verified_values": 0,
        "verified_geometries": 0,
    }
    try:
        formats = {
            f.GetField("source_uid"): f.GetField("bicim")
            for f in original.GetLayerByName("kaynaklar")
        }
        links = defaultdict(list)
        for f in original.GetLayerByName("kayit_kaynaklari"):
            core.check_cancel(cancel)
            sid = f.GetField("source_uid")
            links[f.GetField("record_uid")].append(
                {
                    "source_uid": sid,
                    "source_fid": f.GetField("source_fid"),
                    "format": formats[sid],
                    "raw": json.loads(f.GetField("ozgun_alanlar_json")),
                }
            )
        fields = [f for f in plan["fields"] if f["enabled"]]
        dest = ogr.GetDriverByName("GPKG").CreateDataSource(str(target))
        if dest.StartTransaction() != 0:
            raise RuntimeError("Öznitelik işlemi başlatılamadı.")
        for name in TABLES:
            core.check_cancel(cancel)
            layer = original.GetLayerByName(name)
            if name != "parseller":
                out = core.create_table(
                    dest, name, [(f.name, f.GetType()) for f in layer.schema]
                )
                for feature in layer:
                    core.check_cancel(cancel)
                    core.add_row(out, feature.items())
                continue
            system = [
                f for f in layer.schema if f.name not in core.STANDARD_TEXT
            ]
            out = core.create_table(
                dest,
                name,
                [(f.name, f.GetType()) for f in system]
                + [(f["name"], TYPES[f["type"]]) for f in fields],
                layer.GetSpatialRef(),
                True,
            )
            for feature in layer:
                core.check_cancel(cancel)
                uid = feature.GetField("record_uid")
                values = {f.name: feature.GetField(f.name) for f in system}
                field_sources = {}
                field_conflicts = []
                for field in fields:
                    try:
                        chosen, candidates = pick(
                            field,
                            links[uid],
                            report["attribute_policy"],
                            plan["fill_from_other_source"],
                        )
                    except ValueError as error:
                        raise ValueError(
                            field["name"] + " / " + uid + ": " + str(error)
                        ) from error
                    if not chosen["present"]:
                        if not plan["allow_missing"]:
                            raise ValueError(
                                "Eksik alanın NULL olarak eklenmesine izin verilmedi: "
                                + field["name"]
                            )
                        audit["missing_values"] += 1
                    if chosen["value"] is None:
                        audit["null_values"] += 1
                    values[field["name"]] = chosen["value"]
                    field_sources[field["name"]] = {
                        k: v for k, v in chosen.items() if k != "value"
                    }
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
                        conflict = {
                            "record_uid": uid,
                            "field": field["name"],
                            "candidates": candidates,
                        }
                        audit["conflicts"].append(conflict)
                        field_conflicts.append(conflict)
                    audit["value_sources"].append(
                        {"record_uid": uid, "field": field["name"], **chosen}
                    )
                values["alan_kaynaklari_json"] = core.json_text(field_sources)
                values["alan_celiskileri_json"] = core.json_text(
                    field_conflicts
                )
                if field_conflicts and "KAYNAK_CELISKISI" not in (
                    values["kontrol_durumu"] or ""
                ):
                    values["kontrol_durumu"] = (
                        values["kontrol_durumu"] or ""
                    ) + ",KAYNAK_CELISKISI"
                geometry = feature.GetGeometryRef()
                core.add_row(out, values, geometry)
                expected[uid] = (values, bytes(geometry.ExportToWkb()))
        if dest.CommitTransaction() != 0:
            raise RuntimeError("Öznitelik şeması kaydedilemedi.")
        out = layer = feature = None
        close_dataset(dest)
        dest = None
        verified = ogr.Open(str(target), 0)
        layer = verified.GetLayerByName("parseller")
        if layer.GetFeatureCount() != len(expected):
            raise RuntimeError("Öznitelik çıktı sayısı değişti.")
        for f in layer:
            core.check_cancel(cancel)
            values, wkb = expected[f.GetField("record_uid")]
            if [d.name for d in layer.schema] != list(values):
                raise RuntimeError("Çıktı sütun adları değişti.")
            if any(f.GetField(k) != v for k, v in values.items()):
                raise RuntimeError("Öznitelik değeri yeniden okumada değişti.")
            if bytes(f.GetGeometryRef().ExportToWkb()) != wkb:
                raise RuntimeError(
                    "Öznitelik düzenlemesi geometriyi değiştirdi."
                )
            audit["verified_values"] += len(values)
            audit["verified_geometries"] += 1
        for name in TABLES[1:]:
            left = original.GetLayerByName(name)
            right = verified.GetLayerByName(name)
            if left.GetFeatureCount() != right.GetFeatureCount():
                raise RuntimeError("İlişki tablosu kayıt sayısı değişti.")
            for a, b in zip(left, right):
                core.check_cancel(cancel)
                if a.items() != b.items():
                    raise RuntimeError("Özgün kaynak öznitelikleri değişti.")
        report["attribute_schema"] = audit
        f = layer = left = right = a = b = None
        close_dataset(verified)
        verified = None
        close_dataset(original)
        original = None
        # Update the embedded report too; the original attribute links stay byte-for-byte values.
        dest = ogr.Open(str(target), 1)
        run = dest.GetLayerByName("islem_ozeti")
        row = run.GetNextFeature()
        row.SetField("rapor_json", core.json_text(report))
        if run.SetFeature(row) != 0:
            raise RuntimeError("Öznitelik denetimi rapora eklenemedi.")
        row = run = None
        close_dataset(dest)
        dest = None
        target.replace(path)
    finally:
        for ds in (original, dest, verified):
            if ds is not None:
                close_dataset(ds)
