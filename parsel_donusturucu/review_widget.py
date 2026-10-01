"""Input file and parcel selection, with visible duplicate groups before export."""

from pathlib import Path
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor, QBrush
from qgis.PyQt.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QHeaderView,
    QLabel,
    QPushButton,
    QComboBox,
    QPlainTextEdit,
    QSplitter,
    QCheckBox,
)
from itertools import permutations
from dataclasses import replace
from . import input_review, source_crs

CHECKED = Qt.CheckState.Checked
UNCHECKED = Qt.CheckState.Unchecked


class FormatSelector(QWidget):
    """All is binary: checked iff every format is checked; never partial."""

    formatsChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("İşleme katılacak türler:"))
        self.all = QCheckBox("Tümü")
        self.all.setTristate(False)
        self.all.setChecked(True)
        row.addWidget(self.all)
        self.boxes = {}
        for key, label in [
            ("shapefile", "SHP / ZIP"),
            ("kml", "KML / KMZ"),
            ("geojson", "GeoJSON"),
        ]:
            box = QCheckBox(label)
            box.setChecked(True)
            self.boxes[key] = box
            row.addWidget(box)
            box.toggled.connect(self.update_all)
        row.addStretch()
        self.all.toggled.connect(self.toggle_all)

    def currentData(self):
        return tuple(k for k, b in self.boxes.items() if b.isChecked())

    def update_all(self, *args):
        self.all.blockSignals(True)
        self.all.setChecked(all(b.isChecked() for b in self.boxes.values()))
        self.all.blockSignals(False)
        self.formatsChanged.emit()

    def toggle_all(self, checked):
        for box in self.boxes.values():
            box.blockSignals(True)
            box.setChecked(checked)
            box.blockSignals(False)
        self.formatsChanged.emit()


class ReviewWidget(QWidget):
    selectionChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sources = []
        self.review = None
        self.records = {}
        self.blocked = {}
        self.groups = {}
        self.manual_excluded = set()
        self.manual_pins = {}
        self.blocked_excluded = set()
        self.reasons = {}
        self.general = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.formats = FormatSelector()
        layout.addWidget(self.formats)
        rule = QHBoxLayout()
        self.rule_label = QLabel("Aynı parsel birden fazla kaynakta:")
        rule.addWidget(self.rule_label)
        self.mode = QComboBox()
        for text, key in [
            ("Kayıtları ayrı tut", "separate"),
            ("Kaynakları birleştir (tüm kaynak bilgileri korunur)", "merge"),
            ("Önceliğe göre tek kaynak seç", "priority"),
        ]:
            self.mode.addItem(text, key)
        rule.addWidget(self.mode, 1)
        layout.addLayout(rule)
        row = QHBoxLayout()
        self.preference = QComboBox()
        labels = {"shapefile": "SHP", "kml": "KML", "geojson": "GeoJSON"}
        orders = list(permutations(["geojson", "kml", "shapefile"]))
        for order in orders:
            self.preference.addItem(
                " → ".join(labels[x] for x in order), ",".join(order)
            )
        self.preference.setToolTip(
            "Tercih edilen tür yoksa sıradaki mevcut ve işleme açık tür seçilir. Benzersiz parseller korunur."
        )
        self.all_button = QPushButton("Kurala göre seç")
        self.none_button = QPushButton("Seçimi temizle")
        self.all_button.setToolTip(
            "Seçimi baştan kurar; elle yapılan seçim ve elemeleri sıfırlar. Yalnız işaretli dosya türleri kullanılır."
        )
        self.none_button.setToolTip(
            "Bütün kayıtların seçimini kaldırır. Yeniden seçim için yanındaki seçim düğmesini kullanın."
        )
        for w in [self.preference, self.all_button, self.none_button]:
            row.addWidget(w)
        layout.addLayout(row)
        self.summary = QLabel("Tarama sonrası eşleşmeler burada görünür.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.files = QTableWidget(0, 8)
        self.files.setHorizontalHeaderLabels(
            [
                "Dahil",
                "Dosya",
                "Biçim",
                "Kayıt",
                "Koordinat sistemi",
                "Kodlama",
                "Durum",
                "Açıklama",
            ]
        )
        self.files.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.files.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.files.horizontalHeader().setSectionResizeMode(
            7, QHeaderView.ResizeMode.Stretch
        )
        self.files.setColumnWidth(0, 45)
        self.files.setColumnWidth(3, 50)
        self.tabs.addTab(self.files, "Dosyalar")
        page = QWidget()
        form = QVBoxLayout(page)
        form.setContentsMargins(2, 2, 2, 2)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(
            ["Parsel / kaynak", "Biçim", "Kayıt ID", "Kontrol"]
        )
        self.tree.setColumnWidth(0, 410)
        self.tree.setColumnWidth(1, 85)
        self.tree.setColumnWidth(2, 65)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setPlaceholderText(
            "Kaynakları ve alan farklarını görmek için bir parsel grubuna tıklayın."
        )
        split.addWidget(self.tree)
        split.addWidget(self.detail)
        split.setSizes([740, 350])
        form.addWidget(split, 1)
        self.tabs.addTab(page, "Parseller ve tekrarlar")
        self.similarities = QTableWidget(0, 7, self)
        self.similarities.setHorizontalHeaderLabels(
            [
                "İlk katman",
                "Diğer katman",
                "Benzerlik puanı",
                "Eş geometri",
                "Kesin eş kayıt",
                "Ortak alanlar",
                "Karar",
            ]
        )
        self.similarities.hide()
        self.similarities.horizontalHeaderItem(2).setToolTip(
            "0–100 yardımcı puan; doğruluk veya eşleşme olasılığı değildir. Geometri, öznitelikler, ortak alanlar ve ad birlikte değerlendirilir."
        )
        self.similarities.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.similarities.horizontalHeader().setSectionResizeMode(
            5, QHeaderView.ResizeMode.Stretch
        )
        self.files.itemChanged.connect(self.file_changed)
        self.tree.itemChanged.connect(self.record_changed)
        self.tree.currentItemChanged.connect(self.show_detail)
        self.all_button.clicked.connect(self.reset_manual)
        self.none_button.clicked.connect(self.clear_selection)
        self.formats.formatsChanged.connect(self.apply_rules)
        self.mode.currentIndexChanged.connect(self.apply_rules)
        self.preference.currentIndexChanged.connect(self.apply_rules)
        self.apply_rules()
        self.setMinimumHeight(275)

    def set_general(self, enabled):
        self.general = enabled
        self.rule_label.setText(
            "Aynı kayıt birden fazla kaynakta:"
            if enabled
            else "Aynı parsel birden fazla kaynakta:"
        )
        self.tabs.setTabText(
            1, "Kayıtlar ve tekrarlar" if enabled else "Parseller ve tekrarlar"
        )
        self.tree.setHeaderLabels(
            [
                "Kayıt / kaynak" if enabled else "Parsel / kaynak",
                "Biçim",
                "Kayıt ID",
                "Kontrol",
            ]
        )
        self.detail.setPlaceholderText(
            "Karşılaştırma ayrıntıları için bir kayıt grubuna tıklayın."
            if enabled
            else "Kaynakları ve alan farklarını görmek için bir parsel grubuna tıklayın."
        )
        if enabled and self.tabs.indexOf(self.similarities) < 0:
            self.tabs.addTab(self.similarities, "Katman benzerlikleri")
        elif not enabled:
            if self.tabs.indexOf(self.similarities) >= 0:
                self.tabs.removeTab(self.tabs.indexOf(self.similarities))
            self.similarities.hide()

    def clear(self):
        self.sources = []
        self.review = None
        self.records = {}
        self.blocked = {}
        self.groups = {}
        self.manual_excluded = set()
        self.manual_pins = {}
        self.blocked_excluded = set()
        self.reasons = {}
        self.files.setRowCount(0)
        self.tree.clear()
        self.detail.clear()
        self.similarities.setRowCount(0)
        self.summary.setText(
            "Girdi veya kaynak koordinat tanımı değişti; Tara ve karşılaştır düğmesine basın."
        )
        self.all_button.setEnabled(False)
        self.none_button.setEnabled(False)
        self.selectionChanged.emit()

    def populate(self, sources, review):
        self.clear()
        self.sources = sources
        self.review = review
        self.files.blockSignals(True)
        self.tree.blockSignals(True)
        self.files.setRowCount(len(sources))
        labels = {
            "ready": "İncelendi",
            "duplicate": "Aynı dosya kopyası",
            "error": "Okuma hatası",
        }
        for i, s in enumerate(sources):
            details = s.error or (
                "Kopyası: " + s.duplicate_of
                if s.status == "duplicate"
                else ", ".join(s.warnings or [])
            )
            if s.assignment_basis == "matched_reference":
                details = "Koordinat: eş KML/GeoJSON kaydından çıkarım. Kaynak tanım dosyası yok."
            crs_info = source_crs.describe(s, review.get("missing_crs", ""))
            pending = next(
                (x for x in review["blocked"] if x["source_index"] == i), None
            )
            if pending:
                details = pending["message"]
            state = (
                "⚠ Koordinat tanımı bekliyor"
                if pending and pending.get("kind") == "missing_crs"
                else "⚠ Kontrol gerekiyor" if pending else labels[s.status]
            )
            values = [
                "",
                Path(s.path).name,
                s.format,
                s.count,
                crs_info["name"],
                s.encoding,
                state,
                details,
            ]
            for j, value in enumerate(values):
                cell = QTableWidgetItem(str(value))
                cell.setToolTip(
                    s.path
                    + ("\nArşiv üyesi: " + s.member if s.member else "")
                    + "\n"
                    + details
                )
                if j == 4:
                    cell.setToolTip(
                        crs_info["name"]
                        + "\n"
                        + crs_info["unit"]
                        + "\n"
                        + crs_info["basis"]
                        + "\n"
                        + crs_info["wkt"]
                    )
                if pending and j in (4, 6, 7):
                    cell.setForeground(QBrush(QColor("#ad6800")))
                if j == 0:
                    cell.setFlags(
                        Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsUserCheckable
                        if s.status == "ready"
                        else Qt.ItemFlag.NoItemFlags
                    )
                    cell.setCheckState(UNCHECKED)
                self.files.setItem(i, j, cell)
        for group in review["groups"]:
            n = len(group["records"])
            warning = (
                "Aynı kimlik, farklı geometri!"
                if group["same_identity_other_geometry"]
                else ("Tekrarlı parsel" if n > 1 else "Tek kayıt")
            )
            if self.general:
                warning = group.get("notice", "Tek kayıt")
            content = sum(
                not d["empty_representation"] for d in group["differences"]
            )
            parent = QTreeWidgetItem(
                [
                    group["identity"],
                    "",
                    str(n),
                    warning + f" · {content} alan farkı",
                ]
            )
            parent.setData(0, Qt.ItemDataRole.UserRole, group["id"])
            self.tree.addTopLevelItem(parent)
            self.groups[group["id"]] = group
            parent.setToolTip(
                0,
                group.get(
                    "match_note",
                    "Eşleşme: idari kimlik + tam XY. Yakınlık toleransı uygulanmaz.",
                ),
            )
            for record in group["records"]:
                token = (record["source_index"], record["fid"])
                child = QTreeWidgetItem(
                    [
                        record["label"],
                        record["format"],
                        record["fid"],
                        "Aynı kimlik + XY" if n > 1 else "Tek kayıt",
                    ]
                )
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, CHECKED)
                child.setData(0, Qt.ItemDataRole.UserRole, group["id"])
                child.setToolTip(0, sources[token[0]].path)
                parent.addChild(child)
                self.records[token] = child
            parent.setExpanded(False)
        if review["blocked"]:
            for issue in review["blocked"]:
                si = issue["source_index"]
                s = sources[si]
                preview = issue.get("preview", [])
                name = (
                    preview[0]["identity"]
                    if s.count == 1 and preview
                    else s.label
                )
                parent = QTreeWidgetItem(
                    [
                        name,
                        s.format,
                        str(issue.get("count", s.count)),
                        (
                            "⚠ Koordinat tanımı bekliyor"
                            if issue.get("kind") == "missing_crs"
                            else "⚠ Kontrol gerekiyor"
                        ),
                    ]
                )
                parent.setData(0, Qt.ItemDataRole.UserRole, issue["message"])
                parent.setToolTip(3, issue["message"])
                self.tree.addTopLevelItem(parent)
                child = QTreeWidgetItem(
                    [
                        s.label,
                        s.format,
                        (
                            preview[0]["fid"]
                            if s.count == 1 and preview
                            else str(issue.get("count", s.count))
                        ),
                        "⚠ " + issue["message"],
                    ]
                )
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, CHECKED)
                child.setToolTip(3, issue["message"])
                child.setData(0, Qt.ItemDataRole.UserRole, issue["message"])
                parent.addChild(child)
                self.blocked[si] = child
                parent.setForeground(3, QBrush(QColor("#ad6800")))
                child.setForeground(3, QBrush(QColor("#ad6800")))
                parent.setExpanded(True)
        self.tree.sortItems(0, Qt.SortOrder.AscendingOrder)
        pairs = review.get("layer_similarities", [])
        self.similarities.setRowCount(len(pairs))
        for row, pair in enumerate(pairs):
            a = sources[pair["left"]]
            b = sources[pair["right"]]
            values = [
                a.layer_name or a.label,
                b.layer_name or b.label,
                pair["score"],
                pair["geometry_matches"],
                pair["exact_matches"],
                ", ".join(pair["common_fields"]),
                pair["reason"],
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem()
                item.setData(Qt.ItemDataRole.DisplayRole, value)
                item.setToolTip(
                    a.label
                    + "\n"
                    + b.label
                    + "\n"
                    + a.path
                    + "\n"
                    + b.path
                    + "\nAd benzerliği: %"
                    + str(pair["name_similarity"])
                    + "\n"
                    + str(value)
                )
                self.similarities.setItem(row, col, item)
        self.tree.blockSignals(False)
        self.files.blockSignals(False)
        self.tabs.setCurrentIndex(1)
        self.apply_rules()

    def selected(self):
        return {
            key
            for key, item in self.records.items()
            if item.checkState(0) == CHECKED
        }

    def unresolved(self):
        return {
            si
            for si, item in self.blocked.items()
            if item.checkState(0) == CHECKED
        }

    def selected_sources(self):
        sources = input_review.selection_sources(
            self.sources, self.review, self.selected(), self.unresolved()
        )
        return [
            replace(
                s,
                selection_details={
                    "formats": list(self.formats.currentData()),
                    "mode": self.mode.currentData(),
                    "priority": self.preference.currentData().split(","),
                    "records": {
                        fid: reason
                        for (i, fid), reason in self.reasons.items()
                        if i == si
                    },
                },
            )
            for si, s in enumerate(sources)
        ]

    def ready(self):
        return (
            self.review is not None
            and bool(self.selected())
            and not self.unresolved()
        )

    def expected_count(self, merge=False):
        selected = self.selected()
        return (
            sum(
                any(
                    (r["source_index"], r["fid"]) in selected
                    for r in g["records"]
                )
                for g in self.review["groups"]
            )
            if merge and self.review
            else len(selected)
        )

    def refresh(self):
        if self.review is None:
            return
        selected = self.selected()
        self.files.blockSignals(True)
        for si, s in enumerate(self.sources):
            items = [
                item for (i, fid), item in self.records.items() if i == si
            ]
            if si in self.blocked:
                items.append(self.blocked[si])
            checked = sum(x.checkState(0) == CHECKED for x in items)
            state = (
                UNCHECKED
                if not checked
                else (
                    CHECKED
                    if checked == len(items)
                    else Qt.CheckState.PartiallyChecked
                )
            )
            self.files.item(si, 0).setCheckState(state)
        self.files.blockSignals(False)
        repeats = sum(
            sum(
                (r["source_index"], r["fid"]) in selected for r in g["records"]
            )
            > 1
            for g in self.review["groups"]
        )
        total = sum(len(g["records"]) for g in self.review["groups"])
        copies = sum(s.status == "duplicate" for s in self.sources)
        noun = "kayıt" if self.general else "parsel"
        self.summary.setText(
            f"{len(selected)} / {total} aktarılabilir kayıt seçili · {self.expected_count(True)} {noun} grubu · {repeats} grupta tekrar seçili · {len(self.blocked)} kaynak tanım / kontrol bekliyor ({len(self.unresolved())} seçili) · {copies} dosya kopyası elendi"
        )
        self.selectionChanged.emit()

    def file_changed(self, item):
        if item.column() != 0 or self.review is None:
            return
        si = item.row()
        include = item.checkState() != UNCHECKED
        for (i, fid), child in self.records.items():
            if i == si:
                self.set_manual((i, fid), include)
        if si in self.blocked:
            if include:
                self.blocked_excluded.discard(si)
            else:
                self.blocked_excluded.add(si)
        self.apply_rules()

    def record_changed(self, item, column):
        if column != 0:
            return
        for key, child in self.records.items():
            if child is item:
                self.set_manual(key, item.checkState(0) == CHECKED)
                break
        for si, child in self.blocked.items():
            if child is item:
                if item.checkState(0) == CHECKED:
                    self.blocked_excluded.discard(si)
                else:
                    self.blocked_excluded.add(si)
        self.apply_rules()

    def set_selected(self, selected):
        self.tree.blockSignals(True)
        for key, item in self.records.items():
            item.setCheckState(0, CHECKED if key in selected else UNCHECKED)
        self.tree.blockSignals(False)
        self.refresh()

    def select_all(self, include):
        self.tree.blockSignals(True)
        for item in self.blocked.values():
            item.setCheckState(0, UNCHECKED)
        self.tree.blockSignals(False)
        self.set_selected(set(self.records) if include else set())

    def reduce(self):
        if self.review:
            self.set_selected(
                input_review.choose_one(
                    self.review, self.selected(), self.preference.currentData()
                )
            )

    def set_manual(self, token, include):
        group = next(
            (
                g
                for g in self.review["groups"]
                if any(
                    (r["source_index"], r["fid"]) == token
                    for r in g["records"]
                )
            ),
            None,
        )
        if include:
            self.manual_excluded.discard(token)
            if group:
                self.manual_pins[group["id"]] = token
        else:
            self.manual_excluded.add(token)
            if group and self.manual_pins.get(group["id"]) == token:
                self.manual_pins.pop(group["id"], None)

    def clear_selection(self):
        self.manual_excluded = set(self.records)
        self.manual_pins = {}
        self.blocked_excluded = set(self.blocked)
        self.apply_rules()

    def reset_manual(self):
        self.manual_excluded = set()
        self.manual_pins = {}
        self.blocked_excluded = set()
        self.apply_rules()

    def exclude_unresolved(self):
        self.blocked_excluded.update(self.unresolved())
        self.apply_rules()

    def apply_rules(self, *args):
        priority = self.mode.currentData() == "priority"
        self.preference.setVisible(priority)
        self.all_button.setText(
            "Önceliğe göre seç" if priority else "Uygun kayıtların tümünü seç"
        )
        self.all_button.setEnabled(
            self.review is not None and bool(self.formats.currentData())
        )
        self.none_button.setEnabled(self.review is not None)
        if self.review is None:
            return
        types = set(self.formats.currentData())
        eligible = {
            key
            for key in self.records
            if self.sources[key[0]].format in types
            and key not in self.manual_excluded
        }
        selected = (
            input_review.choose_one(
                self.review, eligible, self.preference.currentData()
            )
            if priority
            else eligible
        )
        if priority:
            for group in self.review["groups"]:
                pin = self.manual_pins.get(group["id"])
                if pin in eligible:
                    selected.difference_update(
                        (r["source_index"], r["fid"]) for r in group["records"]
                    )
                    selected.add(pin)
        self.tree.blockSignals(True)
        for token, item in self.records.items():
            enabled = self.sources[token[0]].format in types
            flags = item.flags()
            item.setFlags(
                flags | Qt.ItemFlag.ItemIsEnabled
                if enabled
                else flags & ~Qt.ItemFlag.ItemIsEnabled
            )
            item.setCheckState(0, CHECKED if token in selected else UNCHECKED)
            if not enabled:
                reason = "Tür filtresi dışında"
            elif token in self.manual_excluded:
                reason = "Kullanıcı hariç tuttu"
            elif token in selected:
                reason = (
                    "Manuel seçim"
                    if token in self.manual_pins.values()
                    else (
                        "Öncelik sırasına göre seçildi"
                        if priority
                        else "Seçili"
                    )
                )
            else:
                reason = (
                    "Aynı kayıt için başka kaynak seçildi"
                    if self.general
                    else "Aynı parsel için başka kaynak seçildi"
                )
            self.reasons[token] = reason
            item.setText(3, reason)
            item.setToolTip(3, reason)
        for si, item in self.blocked.items():
            enabled = self.sources[si].format in types
            flags = item.flags()
            item.setFlags(
                flags | Qt.ItemFlag.ItemIsEnabled
                if enabled
                else flags & ~Qt.ItemFlag.ItemIsEnabled
            )
            item.setCheckState(
                0,
                (
                    CHECKED
                    if enabled and si not in self.blocked_excluded
                    else UNCHECKED
                ),
            )
        self.tree.blockSignals(False)
        self.files.blockSignals(True)
        for si, s in enumerate(self.sources):
            self.files.item(si, 0).setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
                if s.status == "ready" and s.format in types
                else Qt.ItemFlag.NoItemFlags
            )
        self.files.blockSignals(False)
        self.refresh()

    def snapshot(self):
        def stable(token):
            s = self.sources[token[0]]
            return (s.path, s.member, s.fingerprint, token[1])

        return {
            "excluded": {stable(t) for t in self.manual_excluded},
            "pins": {stable(t) for t in self.manual_pins.values()},
            "blocked": {
                (
                    self.sources[i].path,
                    self.sources[i].member,
                    self.sources[i].fingerprint,
                )
                for i in self.blocked_excluded
            },
        }

    def restore(self, snapshot):
        if not self.review or not snapshot:
            return
        for token in self.records:
            s = self.sources[token[0]]
            stable = (s.path, s.member, s.fingerprint, token[1])
            if stable in snapshot["excluded"]:
                self.manual_excluded.add(token)
            if (s.path, s.member, s.fingerprint) in snapshot["blocked"]:
                self.manual_excluded.add(token)
            if stable in snapshot["pins"]:
                self.set_manual(token, True)
        self.blocked_excluded = {
            i
            for i in self.blocked
            if (
                self.sources[i].path,
                self.sources[i].member,
                self.sources[i].fingerprint,
            )
            in snapshot["blocked"]
        }
        self.apply_rules()

    def show_detail(self, item, previous=None):
        if item is None:
            self.detail.clear()
            return
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(key, int):
            self.detail.setPlainText(str(key or item.text(3)))
            return
        g = self.groups[key]
        lines = [g["identity"], ""]
        if self.general:
            import json

            lines += [g.get("match_note", ""), ""]
            for other in [g] + [
                self.groups[i] for i in g.get("similar_ids", [])
            ]:
                lines.append(
                    (
                        "BU GRUP: "
                        if other is g
                        else "AYRI TUTULAN BENZER GRUP: "
                    )
                    + other["identity"]
                )
                for r in other["records"]:
                    lines += [
                        self.sources[r["source_index"]].label
                        + " · kayıt "
                        + r["fid"],
                        json.dumps(r["raw"], ensure_ascii=False, indent=2),
                    ]
            self.detail.setPlainText("\n".join(lines))
            return
        if g["same_identity_other_geometry"]:
            lines += [
                "UYARI: Aynı kimlik başka geometride de bulundu; bu gruplar birbirinin tekrarı sayılmadı.",
                "",
            ]
        lines += ["", "ALAN FARKLARI (kaynak çifti başına)"]
        for d in g["differences"]:
            left = self.sources[d["left_source_index"]]
            right = self.sources[d["right_source_index"]]
            display = lambda v: (
                "NULL" if v is None else "boş metin" if v == "" else str(v)
            )
            lines.append(
                f"{d['field']}: {left.format} [{d['left_fid']}] {display(d['left'])} → {right.format} [{d['right_fid']}] {display(d['right'])}"
            )
        if not g["differences"]:
            lines.append("Standart alanlarda fark yok.")
        lines += ["", "KAYNAKLAR"]
        for r in g["records"]:
            lines.append(
                f"{r['format']} · kayıt {r['fid']}\n{self.sources[r['source_index']].path}"
            )
        self.detail.setPlainText("\n".join(lines))
