"""Readable source/output comparisons based on persisted provenance links."""

from pathlib import Path
import csv
import html
import sqlite3
from . import source_crs

SOURCE_COLUMNS = [
    ("file", "Girdi dosyası"),
    ("format", "Biçim"),
    ("input_status", "Girdi durumu"),
    ("source_crs", "Girdi koordinatı"),
    ("basis", "Koordinat dayanağı"),
    ("count", "Dosyadaki kayıt"),
    ("selected", "Seçili kayıt"),
    ("path", "Kaynak yolu"),
    ("member", "Arşiv üyesi"),
    ("notes", "Kontrol notları"),
]
RESULT_COLUMNS = (
    SOURCE_COLUMNS[:7]
    + [
        ("exported", "Aktarılan girdi"),
        ("rejected", "Aktarılamayan girdi"),
        ("output_records", "İlişkili çıktı parseli"),
        ("output_status", "Çıktı durumu"),
        ("target_crs", "Çıktı koordinatı"),
        ("output", "Çıktı yolu"),
        ("notes", "Kontrol notları"),
    ]
    + SOURCE_COLUMNS[7:9]
)
SUMMARY_LABELS = [
    ("source_count", "Taranan kaynak"),
    ("input_records", "Dosyalardaki kayıt"),
    ("selected_records", "Seçili girdi kaydı"),
    ("unselected_records", "Seçim dışı kayıt"),
    ("exported_records", "Aktarılan girdi kaydı"),
    ("output_records", "Üretilen parsel"),
    ("rejected_records", "Aktarılamayan seçili kayıt"),
    ("duplicate_sources", "Kopya kaynak"),
    ("error_sources", "Okunamayan kaynak"),
    ("pending_sources", "Koordinatı belirsiz kaynak"),
]
MERGE_NOTE = "Birleştirmede aynı çıktı parseli birden fazla girdiye bağlı olabilir. Toplam çıktı sayısı satır toplamı değil, üretilen gerçek parsel sayısıdır."


def wording(text, model):
    return (
        text.replace("parseller", "kayıtlar")
        .replace("parseli", "kaydı")
        .replace("parsel", "kayıt")
        if model.get("mode") == "vector"
        else text
    )


def columns(model):
    return [
        (k, wording(label, model))
        for k, label in (RESULT_COLUMNS if model["result"] else SOURCE_COLUMNS)
    ]


def summary_labels(model):
    return [(k, wording(label, model)) for k, label in SUMMARY_LABELS]


def source_row(source, selected=0):
    info = source_crs.describe(source)
    status = {"error": "Okuma hatası", "duplicate": "Dosya kopyası"}.get(
        source.status,
        "Koordinat belirlenemedi" if not info["wkt"] else "Okundu",
    )
    return {
        "file": Path(source.path).name,
        "format": {
            "shapefile": "Shapefile",
            "kml": "KML",
            "geojson": "GeoJSON",
        }.get(source.format, source.format),
        "input_status": status,
        "source_crs": info["name"],
        "basis": info["basis"],
        "count": source.count,
        "selected": selected,
        "path": source.path,
        "member": source.member,
        "notes": source.error or "",
    }


def summarize(rows, output_records=None):
    result = {
        "source_count": len(rows),
        "input_records": sum(r["count"] for r in rows),
        "selected_records": sum(r["selected"] for r in rows),
        "duplicate_sources": sum(
            r["input_status"] == "Dosya kopyası" for r in rows
        ),
        "error_sources": sum(
            r["input_status"] == "Okuma hatası" for r in rows
        ),
        "pending_sources": sum(
            r["input_status"] == "Koordinat belirlenemedi" for r in rows
        ),
    }
    result["unselected_records"] = (
        result["input_records"] - result["selected_records"]
    )
    if output_records is not None:
        result.update(
            exported_records=sum(r["exported"] for r in rows),
            rejected_records=sum(r["rejected"] for r in rows),
            output_records=output_records,
        )
    return result


def input_model(sources, selected=(), pending=None):
    pending = pending or {}
    counts = {}
    for si, fid in selected:
        counts[si] = counts.get(si, 0) + 1
    rows = []
    for i, s in enumerate(sources):
        issue = pending.get(i, {})
        row = source_row(
            s,
            counts.get(i, 0)
            + (issue.get("count", 0) if issue.get("selected", True) else 0),
        )
        if issue:
            row["notes"] = issue["message"]
            if row["input_status"] == "Okundu":
                row["input_status"] = "Kontrol gerekiyor"
        rows.append(row)
    return {
        "rows": rows,
        "totals": summarize(rows),
        "title": "Girdi koordinat dökümü",
        "result": False,
        "mode": (
            "vector"
            if any(s.data_mode == "vector" for s in sources)
            else "parcel"
        ),
    }


def attach(report, canonical):
    """Read exact per-source counts from the validated canonical database."""
    from .core import Source

    con = sqlite3.connect(
        "file:" + Path(canonical).as_posix() + "?mode=ro", uri=True
    )
    try:
        metadata = {
            (p, m or ""): (uid, crs, basis)
            for uid, p, m, crs, basis in con.execute(
                "SELECT source_uid,dosya,zip_uyesi,kaynak_crs,crs_durumu FROM kaynaklar"
            )
        }
        counts = {
            uid: (linked, outputs)
            for uid, linked, outputs in con.execute(
                "SELECT source_uid,COUNT(*),COUNT(DISTINCT record_uid) FROM kayit_kaynaklari GROUP BY source_uid"
            )
        }
        actual_outputs = con.execute(
            "SELECT COUNT(*) FROM parseller"
        ).fetchone()[0]
    finally:
        con.close()
    issues = {}
    for issue in report.get("issues", []):
        key = (issue.get("source_path"), issue.get("source_member", ""))
        if key[0]:
            issues.setdefault(key, []).append(issue["message"])
    rows = []
    for data in report["sources"]:
        source = Source(**data)
        selected = (
            (
                len(source.selected_fids)
                if source.selected_fids is not None
                else source.count
            )
            if source.status == "ready"
            else 0
        )
        row = source_row(source, selected)
        meta = metadata.get((source.path, source.member))
        exported, outputs = counts.get(meta[0], (0, 0)) if meta else (0, 0)
        if exported > selected:
            raise RuntimeError(
                "Girdi/çıktı karşılaştırmasında kayıt sayısı tutarsız."
            )
        if meta and not source.wkt and not source.assigned_crs:
            # Legacy engine callers may supply an explicit bulk source definition.
            from qgis.core import QgsCoordinateReferenceSystem

            crs = QgsCoordinateReferenceSystem(meta[1])
            row["source_crs"] = (
                (crs.authid() or meta[1]) + " · " + crs.description()
            )
            row["basis"] = "İşlemde kullanılan kaynak tanımı"
            row["input_status"] = "Okundu"
        rejected = selected - exported
        status = (
            "Seçim dışı"
            if not selected
            else (
                "Aktarıldı"
                if not rejected
                else "Kısmen aktarıldı" if exported else "Aktarılamadı"
            )
        )
        notes = list(
            dict.fromkeys(issues.get((source.path, source.member), []))
        )
        if source.error:
            notes.insert(0, source.error)
        row.update(
            exported=exported,
            rejected=rejected,
            output_records=outputs,
            output_status=status,
            notes="; ".join(notes),
        )
        rows.append(row)
    totals = summarize(rows, actual_outputs)
    if (
        totals["exported_records"] != report["summary"]["kaynak_baglantisi"]
        or actual_outputs != report["summary"]["aktarilan_parsel"]
    ):
        raise RuntimeError("Girdi/çıktı toplamları gerçek veriyle eşleşmedi.")
    report["source_comparison"] = {
        "rows": rows,
        "totals": totals,
        "state": (
            "Kayıt üretilemedi"
            if not actual_outputs
            else (
                "Kısmen tamamlandı"
                if totals["rejected_records"]
                else "Tamamlandı"
            )
        ),
    }


def result_model(report):
    data = report["source_comparison"]
    rows = []
    for source in data["rows"]:
        row = dict(source)
        row.update(
            target_crs=(
                report["profile"]["name"] if row.get("exported") else "—"
            ),
            output=report["output"],
        )
        rows.append(row)
    return {
        "title": "Girdi / çıktı karşılaştırması",
        "rows": rows,
        "totals": dict(data["totals"]),
        "mode": report.get("mode", "parcel"),
        "state": data["state"],
        "output": report["output"],
        "target": report["profile"]["name"],
        "result": True,
    }


def failed_report(sources, output, profile, message, cancelled=False):
    rows = []
    for source in sources:
        selected = (
            (
                len(source.selected_fids)
                if source.selected_fids is not None
                else source.count
            )
            if source.status == "ready"
            else 0
        )
        row = source_row(source, selected)
        row.update(
            exported=0,
            rejected=selected,
            output_records=0,
            output_status=(
                ("İptal edildi" if cancelled else "Oluşturulamadı")
                if selected
                else "Seçim dışı"
            ),
            notes=message if selected else "",
        )
        rows.append(row)
    return {
        "output": str(output),
        "profile": profile,
        "source_comparison": {
            "rows": rows,
            "totals": summarize(rows, 0),
            "state": "İptal edildi" if cancelled else "Başarısız",
        },
    }


def total_row(model):
    t = model["totals"]
    return {
        "file": "TOPLAM",
        "count": t["input_records"],
        "selected": t["selected_records"],
        "exported": t.get("exported_records", ""),
        "rejected": t.get("rejected_records", ""),
        "output_records": t.get("output_records", ""),
        "output_status": model.get("state", ""),
        "output": model.get("output", ""),
        "target_crs": model.get("target", ""),
        "notes": "; ".join(
            f"{label}: {t[key]}"
            for key, label in summary_labels(model)
            if key in t
        ),
    }


def csv_export(path, model, protected=()):
    path = Path(path).resolve()
    if any(path.is_relative_to(Path(p).resolve()) for p in protected):
        raise ValueError(
            "CSV için girdi ve eklenti klasörlerinin dışında bir konum seçin."
        )
    if path.suffix.lower() != ".csv":
        raise ValueError("CSV dosya uzantısı .csv olmalıdır.")

    def cell(value):
        if isinstance(value, str) and value.lstrip().startswith(
            ("=", "+", "-", "@")
        ):
            return "'" + value
        return value

    headers = columns(model)
    # Exclusive creation protects existing reports; UTF-8 BOM preserves Turkish in Excel.
    with path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow([label for key, label in headers])
        for row in model["rows"] + [total_row(model)]:
            writer.writerow([cell(row.get(key, "")) for key, label in headers])
    return path


def html_section(report):
    model = result_model(report)
    esc = lambda value: html.escape(str(value))
    headers = columns(model)[:12] + [("notes", "Kontrol notları")]
    head = "".join("<th>" + label + "</th>" for key, label in headers)
    body = "".join(
        "<tr>"
        + "".join(
            "<td>" + esc(row.get(key, "")) + "</td>" for key, label in headers
        )
        + "</tr>"
        for row in model["rows"]
    )
    summary = "".join(
        f'<tr><th>{label}</th><td>{model["totals"][key]}</td></tr>'
        for key, label in summary_labels(model)
        if key in model["totals"]
    )
    return f'<h2>{esc(model["state"])}</h2><table>{summary}</table><h2>Girdi / çıktı karşılaştırması</h2><p>{wording(MERGE_NOTE,model)}</p><div style="overflow-x:auto"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
