"""Read-only input review using the same validation and matching rules as conversion."""

from dataclasses import replace
from pathlib import Path
import itertools
import json
import tempfile
from osgeo import ogr
from . import core


def pending_details(sources, result, cancel, temp_parent):
    """Show unidentified sources without inventing a CRS or a duplicate match."""
    for issue in result["blocked"]:
        source = sources[issue["source_index"]]
        if source.wkt or source.assigned_crs or result["missing_crs"]:
            continue
        issue["kind"] = "missing_crs"
        issue["message"] = (
            "Kaynak koordinatı belirlenemedi. Koordinat tanım dosyasını veya eş KML/GeoJSON kaydını ekleyip yeniden tarayın."
        )
        issue["preview"] = []
        with tempfile.TemporaryDirectory(
            prefix=".parsel_pending_", dir=temp_parent
        ) as folder:
            with core.ManagedDataset() as holder:
                path, _, fingerprint = core.materialize(source, folder)
                if fingerprint != source.fingerprint:
                    raise ValueError("Kaynak değişmiş; yeniden tarayın.")
                holder.ds = core.open_source(path)
                for feature in holder.ds.GetLayer(0):
                    core.check_cancel(cancel)
                    raw = {
                        feature.GetFieldDefnRef(i).GetName(): feature.GetField(
                            i
                        )
                        for i in range(feature.GetFieldCount())
                    }
                    try:
                        attrs = core.standard_attributes(raw)
                    except ValueError:
                        attrs = {k: None for k in core.STANDARD_TEXT}
                    # Preview labels are not used as matching keys.
                    identity = " / ".join(
                        str(attrs[k] or "—")
                        for k in [
                            "il",
                            "ilce",
                            "mahalle",
                            "ada_no",
                            "parsel_no",
                        ]
                    )
                    issue["preview"].append(
                        {"identity": identity, "fid": str(feature.GetFID())}
                    )
                    if len(issue["preview"]) >= 20:
                        break


def build(
    sources,
    missing_crs="",
    crs_evidence="",
    cancel=lambda: False,
    progress=lambda n, s: None,
    temp_parent=None,
):
    ready = [s for s in sources if s.status == "ready"]
    result = {
        "groups": [],
        "blocked": [],
        "missing_crs": missing_crs,
        "source_count": len(sources),
    }
    if not ready:
        return result
    if temp_parent:
        Path(temp_parent).mkdir(parents=True, exist_ok=True)
    if not missing_crs and all(
        not s.wkt and not s.assigned_crs for s in ready
    ):
        result["blocked"] = [
            {
                "source_index": i,
                "fid": None,
                "count": s.count,
                "message": "Kaynak koordinat sistemi eksik.",
            }
            for i, s in enumerate(sources)
            if s.status == "ready"
        ]
        pending_details(sources, result, cancel, temp_parent)
        return result
    if temp_parent:
        Path(temp_parent).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".parsel_review_", dir=temp_parent
    ) as folder:
        path = Path(folder) / "review.gpkg"
        report = core._convert_gpkg(
            sources,
            path,
            missing_crs=missing_crs,
            crs_evidence=crs_evidence,
            cancel=cancel,
            progress=progress,
            merge_mode="separate",
        )
        index = {(s.path, s.member): i for i, s in enumerate(sources)}
        ds = ogr.Open(str(path), 0)
        try:
            source_ids = {
                f.GetField("source_uid"): index[
                    (f.GetField("dosya"), f.GetField("zip_uyesi") or "")
                ]
                for f in ds.GetLayerByName("kaynaklar")
            }
            links = {
                f.GetField("record_uid"): (
                    source_ids[f.GetField("source_uid")],
                    f.GetField("source_fid"),
                )
                for f in ds.GetLayerByName("kayit_kaynaklari")
            }
            raw_values = {
                f.GetField("record_uid"): json.loads(
                    f.GetField("ozgun_alanlar_json")
                )
                for f in ds.GetLayerByName("kayit_kaynaklari")
            }
            groups = {}
            identity_groups = {}
            for f in ds.GetLayerByName("parseller"):
                core.check_cancel(cancel)
                si, fid = links[f.GetField("record_uid")]
                attrs = {k: f.GetField(k) for k in core.STANDARD_TEXT}
                match = core.match_key(attrs, core.qgeom(f.GetGeometryRef()))
                key = match if match is not None else ("unmatched", si, fid)
                if key not in groups:
                    group = {
                        "id": len(groups) + 1,
                        "identity": " / ".join(
                            str(attrs[k] or "—")
                            for k in [
                                "il",
                                "ilce",
                                "mahalle",
                                "ada_no",
                                "parsel_no",
                            ]
                        ),
                        "records": [],
                        "differences": [],
                        "same_identity_other_geometry": False,
                    }
                    groups[key] = group
                    if match is not None:
                        identity_groups.setdefault(match[0], []).append(group)
                groups[key]["records"].append(
                    {
                        "source_index": si,
                        "fid": fid,
                        "attrs": attrs,
                        "format": sources[si].format,
                        "label": sources[si].label,
                        "mergeable": match is not None,
                        "raw": raw_values[f.GetField("record_uid")],
                    }
                )
            for group in groups.values():
                for a, b in itertools.combinations(group["records"], 2):
                    for field in core.STANDARD_TEXT:
                        if a["attrs"][field] != b["attrs"][field]:
                            group["differences"].append(
                                {
                                    "field": field,
                                    "left": a["attrs"][field],
                                    "right": b["attrs"][field],
                                    "left_source_index": a["source_index"],
                                    "right_source_index": b["source_index"],
                                    "left_fid": a["fid"],
                                    "right_fid": b["fid"],
                                    "empty_representation": a["attrs"][field]
                                    in (None, "")
                                    and b["attrs"][field] in (None, ""),
                                }
                            )
            for same in identity_groups.values():
                if len(same) > 1:
                    for group in same:
                        group["same_identity_other_geometry"] = True
            result["groups"] = list(groups.values())
            # Every ready feature not in the valid model remains visible and blocks
            # execution while its source is selected; no implicit omission.
            valid_by_source = {i: set() for i in range(len(sources))}
            for group in result["groups"]:
                for row in group["records"]:
                    valid_by_source[row["source_index"]].add(row["fid"])
            errors = [
                x
                for x in report["issues"]
                if x["code"] in ("KAYNAK_REDDEDILDI", "KAYIT_REDDEDILDI")
            ]
            for i, s in enumerate(sources):
                if s.status != "ready" or len(valid_by_source[i]) == s.count:
                    continue
                details = [x for x in errors if x["source"] == s.label]
                # Labels may collide across directories; selection identity never uses labels.
                messages = (
                    "; ".join(dict.fromkeys(x["message"] for x in details))
                    or "Kaynak kayıtlarının bir kısmı ön kontrolden geçmedi."
                )
                result["blocked"].append(
                    {
                        "source_index": i,
                        "fid": None,
                        "message": messages,
                        "count": s.count - len(valid_by_source[i]),
                    }
                )
        finally:
            ds = None
    pending_details(sources, result, cancel, temp_parent)
    result["summary"] = {
        "valid_records": sum(len(g["records"]) for g in result["groups"]),
        "groups": len(result["groups"]),
        "repeated_groups": sum(
            len(g["records"]) > 1 for g in result["groups"]
        ),
        "attribute_differences": sum(
            not d["empty_representation"]
            for g in result["groups"]
            for d in g["differences"]
        ),
        "empty_representation_differences": sum(
            d["empty_representation"]
            for g in result["groups"]
            for d in g["differences"]
        ),
    }
    return result


def choose_one(review, selected, preferred):
    """Only remove checked duplicates; never remove a unique record or revive exclusions."""
    if isinstance(preferred, str) and "," in preferred:
        preferred = preferred.split(",")
    order = (
        list(preferred)
        if not isinstance(preferred, str)
        else [preferred]
        + [x for x in ["shapefile", "kml", "geojson"] if x != preferred]
    )
    if set(order) != {"shapefile", "kml", "geojson"} or len(order) != 3:
        raise ValueError("Geçersiz kaynak öncelik sırası.")
    ranks = {fmt: i for i, fmt in enumerate(order)}
    result = set(selected)
    for group in review["groups"]:
        members = [
            r
            for r in group["records"]
            if (r["source_index"], r["fid"]) in selected
        ]
        if len(members) < 2:
            continue
        keep = min(
            members,
            key=lambda r: (ranks[r["format"]], r["source_index"], r["fid"]),
        )
        result.difference_update(
            (r["source_index"], r["fid"]) for r in members if r is not keep
        )
    return result


def selection_sources(sources, review, selected, blocked_sources=()):
    if blocked_sources:
        raise ValueError(
            "Seçili girdilerde çözümlenmemiş kayıtlar var; düzeltin veya dosyayı hariç tutun."
        )
    valid = {
        (r["source_index"], r["fid"])
        for g in review["groups"]
        for r in g["records"]
    }
    if not selected or not set(selected).issubset(valid):
        raise ValueError(
            "Geçerli girdi kaydı seçin; gerekirse yeniden tarayın."
        )
    result = []
    for i, source in enumerate(sources):
        fids = sorted(fid for si, fid in selected if si == i)
        result.append(
            replace(
                source,
                status=(
                    "ready"
                    if fids
                    else (
                        "excluded"
                        if source.status == "ready"
                        else source.status
                    )
                ),
                selected_fids=fids,
            )
        )
    return result
