"""Show declared CRS before any user assignment; source files are never modified."""

from pathlib import Path
import math
import tempfile
from dataclasses import replace
from collections import Counter
from qgis.core import QgsCoordinateReferenceSystem, QgsUnitTypes
from qgis.gui import QgsProjectionSelectionWidget
from qgis.PyQt.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QLineEdit,
    QCheckBox,
    QDialogButtonBox,
    QMessageBox,
)


def key(source):
    return (source.path, source.member, source.fingerprint)


def apply_assignments(sources, assignments):
    result = []
    for s in sources:
        assignment = assignments.get(key(s), {}) if not s.wkt else {}
        result.append(
            replace(
                s,
                assigned_crs=assignment.get("crs", ""),
                assignment_evidence=assignment.get("evidence", ""),
            )
        )
    return result


def _counterpart_keys(source, cancel):
    """Read complete identity and exact 2D rings without reprojection or rounding."""
    from osgeo import ogr
    from . import core

    with tempfile.TemporaryDirectory(prefix=".parsel_crs_match_") as folder:
        path, _, fingerprint = core.materialize(source, folder)
        if (
            fingerprint != source.fingerprint
            or core.sha(Path(source.path)) != source.file_sha256
        ):
            raise ValueError(
                "Eşleştirme kaynağı taramadan sonra değişmiş; yeniden tarayın."
            )
        dataset = core.open_source(path)
        try:
            layer = dataset.GetLayer(0)
            keys = []
            for feature in layer:
                core.check_cancel(cancel)
                if getattr(feature, "error", ""):
                    return []
                geom = feature.GetGeometryRef()
                if (
                    geom is None
                    or geom.IsEmpty()
                    or ogr.GT_Flatten(geom.GetGeometryType())
                    not in (ogr.wkbPolygon, ogr.wkbMultiPolygon)
                ):
                    return []
                geom = geom.Clone()
                if geom.Is3D() or geom.IsMeasured():
                    dims = list(core.dimensional_values(geom))
                    if any(
                        z is not None and math.isfinite(z) for z, m in dims
                    ) or any(
                        m is not None and math.isfinite(m) and m > -1e38
                        for z, m in dims
                    ):
                        return []
                    geom.FlattenTo2D()
                    geom.SetMeasured(False)
                geometry = core.qgeom(geom)
                if not geometry.isGeosValid():
                    return []
                bounds = geometry.boundingBox()
                if not (
                    -180 <= bounds.xMinimum() <= bounds.xMaximum() <= 180
                    and -90 <= bounds.yMinimum() <= bounds.yMaximum() <= 90
                ):
                    return []
                raw = (
                    feature.raw
                    if source.format in ("kml", "geojson")
                    else {
                        field.name: feature.GetField(i)
                        for i, field in enumerate(layer.schema)
                    }
                )
                try:
                    match = core.match_key(
                        core.standard_attributes(raw), geometry
                    )
                except (ValueError, IndexError):
                    return []
                if match is None:
                    return []
                keys.append(match)
            return keys if len(keys) == source.count else []
        finally:
            layer = None
            if source.format == "shapefile":
                core.close_dataset(dataset)
            dataset = None


def resolve_counterparts(sources, cancel=lambda: False):
    """Infer missing SHP WGS84 only from complete, exact, unambiguous peer records.

    The result retains its inferred basis and dependency hashes. Target CRS is
    deliberately not an input to this function. Never mutate declared metadata.
    """
    from . import core

    candidates = {
        key(s)
        for s in sources
        if s.status == "ready"
        and s.format == "shapefile"
        and not s.wkt
        and not s.assigned_crs
    }
    if not candidates:
        return sources
    references = {}
    identities = {}
    for source in sources:
        core.check_cancel(cancel)
        if (
            source.status != "ready"
            or source.format not in ("kml", "geojson")
            or not source.wkt
        ):
            continue
        if QgsCoordinateReferenceSystem.fromWkt(
            source.wkt
        ) != QgsCoordinateReferenceSystem("EPSG:4326"):
            continue
        for match in _counterpart_keys(source, cancel):
            references.setdefault(match, []).append(source)
            identities.setdefault(match[0], set()).add(match)
    result = []
    for source in sources:
        core.check_cancel(cancel)
        if key(source) not in candidates or not references:
            result.append(source)
            continue
        keys = _counterpart_keys(source, cancel)
        if not keys or any(
            k not in references or len(identities[k[0]]) != 1 for k in keys
        ):
            result.append(source)
            continue
        peers = {key(s): s for match in keys for s in references[match]}
        dependencies = [
            {
                "path": s.path,
                "member": s.member,
                "sha256": s.file_sha256,
                "format": s.format,
            }
            for s in peers.values()
        ]
        evidence = core.json_text(
            {
                "method": "exact_identity_and_all_xy_rings",
                "matched_records": len(keys),
                "reference_system": "EPSG:4326",
                "references": dependencies,
            }
        )
        result.append(
            replace(
                source,
                assigned_crs="EPSG:4326",
                assignment_basis="matched_reference",
                assignment_evidence=evidence,
                coordinate_references=dependencies,
            )
        )
    return result


def describe(source, bulk_crs=""):
    if source.wkt:
        crs = QgsCoordinateReferenceSystem.fromWkt(source.wkt)
        basis = (
            "Koordinat tanım dosyası (.prj)"
            if source.format == "shapefile"
            else (
                "KML standardı"
                if source.format == "kml"
                else "GeoJSON tanımı / standardı"
            )
        )
    elif source.assigned_crs or bulk_crs:
        crs = QgsCoordinateReferenceSystem(source.assigned_crs or bulk_crs)
        basis = (
            "Eş KML/GeoJSON kaydından çıkarım"
            if source.assignment_basis == "matched_reference"
            else (
                "Kullanıcı teyidi (dosya)"
                if source.assigned_crs
                else "Kullanıcı teyidi (toplu)"
            )
        )
    else:
        return {
            "name": "Bilinmiyor",
            "unit": "Belirsiz",
            "basis": "Koordinat sistemi eksik",
            "wkt": "",
            "declared": False,
        }
    if not crs.isValid():
        return {
            "name": "Geçersiz koordinat sistemi",
            "unit": "Belirsiz",
            "basis": basis,
            "wkt": "",
            "declared": bool(source.wkt),
        }
    name = (
        (crs.authid() or "Özel koordinat sistemi")
        + " · "
        + (crs.description() or "Özel tanım")
    )
    return {
        "name": name,
        "unit": QgsUnitTypes.toString(crs.mapUnits()),
        "basis": basis,
        "wkt": crs.toWkt(),
        "declared": bool(source.wkt),
    }


def summary(sources, bulk_crs=""):
    rows = [describe(s, bulk_crs) for s in sources if s.status == "ready"]
    if not rows:
        return "Kaynak koordinat bilgisi: klasörü tarayın."
    counts = Counter((r["name"], r["unit"], r["basis"]) for r in rows)
    prefix = (
        "Kaynak koordinat sistemleri (birden fazla tanım):"
        if len({(r["name"], r["unit"]) for r in rows}) > 1
        else "Kaynak koordinat sistemi:"
    )
    ordered = sorted(
        counts.items(),
        key=lambda item: (
            "Kullanıcı" in item[0][2]
            or item[0][2] == "Koordinat sistemi eksik",
            item[0],
        ),
    )
    return (
        prefix
        + "\n"
        + "\n".join(
            f"{n} kaynak · {name} · {unit} · {basis}"
            for (name, unit, basis), n in ordered
        )
    )


class SourceCrsDialog(QDialog):
    def __init__(self, sources, assignments, parent=None, bulk_crs=""):
        super().__init__(parent)
        self.setWindowTitle("Kaynak koordinat sistemleri ve eksik tanımlar")
        self.resize(1120, 600)
        self.assignments = dict(assignments)
        self.editors = []
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Dosyanın mevcut koordinat tanımı korunur. Yalnız koordinat sistemi bilinmeyen dosyalar için sistem seçin ve doğrulayın.\n"
            "Ham dosyalara yazılmaz. Teyit edilmeyen eksik kaynaklarda, seçilmişse toplu tanım geçerlidir."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            [
                "Kaynak",
                "Mevcut koordinat sistemi",
                "Birim",
                "Dayanak",
                "Eksik koordinat tanımı",
                "Dayanak notu",
                "Teyit",
            ]
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        for s in sorted(
            sources,
            key=lambda s: (not bool(s.wkt), s.path.casefold(), s.member),
        ):
            if s.status != "ready":
                continue
            row = self.table.rowCount()
            self.table.insertRow(row)
            d = describe(s, bulk_crs)
            for col, text in enumerate(
                [s.label, d["name"], d["unit"], d["basis"]]
            ):
                cell = QTableWidgetItem(text)
                cell.setToolTip(s.path + "\n" + s.member + "\n" + d["wkt"])
                self.table.setItem(row, col, cell)
            if s.wkt:
                self.table.setItem(
                    row,
                    4,
                    QTableWidgetItem("Kaynağın kendi tanımı kullanılır"),
                )
                continue
            crs = QgsProjectionSelectionWidget()
            crs.setCrs(QgsCoordinateReferenceSystem(s.assigned_crs))
            note = QLineEdit(s.assignment_evidence)
            note.setPlaceholderText("Bilginin kaynağı / karşılaştırma")
            confirm = QCheckBox()
            confirm.setChecked(bool(s.assigned_crs))
            self.table.setCellWidget(row, 4, crs)
            self.table.setCellWidget(row, 5, note)
            self.table.setCellWidget(row, 6, confirm)
            self.editors.append((s, crs, note, confirm))
        self.table.setColumnWidth(1, 210)
        self.table.setColumnWidth(4, 235)
        layout.addWidget(self.table)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Tanımları uygula ve yeniden tara"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def accept(self):
        updated = dict(self.assignments)
        for s, crs, note, confirm in self.editors:
            updated.pop(key(s), None)
            if confirm.isChecked():
                if not crs.crs().isValid():
                    QMessageBox.warning(
                        self,
                        "Eksik koordinat sistemi",
                        "Teyit edilen kaynak için geçerli bir koordinat sistemi seçin: "
                        + s.label,
                    )
                    return
                updated[key(s)] = {
                    "crs": crs.crs().authid() or crs.crs().toWkt(),
                    "evidence": note.text().strip(),
                }
        self.assignments = updated
        super().accept()
