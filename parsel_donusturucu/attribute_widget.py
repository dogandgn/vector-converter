"""QGIS-style field inventory and explicit output mapping editor (Qt5/Qt6)."""

import copy
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QComboBox,
    QCheckBox,
    QPushButton,
    QPlainTextEdit,
    QHeaderView,
    QDialogButtonBox,
)
from . import attribute_schema as schema

CHECKED = Qt.CheckState.Checked


class AttributeDialog(QDialog):
    def __init__(self, sources, review, selected, plan=None, parent=None):
        super().__init__(parent)
        self.sources = sources
        self.items = schema.inventory(sources, review, selected)
        self.records = [
            r
            for g in review["groups"]
            for r in g["records"]
            if (r["source_index"], r["fid"]) in selected
        ]
        self.plan = (
            copy.deepcopy(plan)
            if plan
            else schema.default_schema(self.items, sources)
        )
        self.plan["selection_signature"] = schema.signature(sources)
        self.setWindowTitle(
            "Öznitelikler — kaynakları incele ve çıktı sütunlarını düzenle"
        )
        self.resize(1040, 730)
        layout = QVBoxLayout(self)
        note = QLabel(
            "Yalnız seçili parseller incelenir. İşaretli sütunlar çıktıya yazılır. Sütun adını ve türünü düzenleyin; eşleme ile farklı kaynak adlarını aynı sütunda toplayın. Sütun adları dört biçim için ASCII harf/rakam/alt çizgi ve en fazla 64 karakterdir; Türkçe değerler korunur. Ham dosyalar değişmez."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            [
                "Çıktıya al",
                "Çıktı sütunu",
                "Tür",
                "Kaynak alanları",
                "Bulunan / seçili kayıt",
                "Ortak kaynak",
            ]
        )
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        self.table.setColumnWidth(1, 180)
        self.table.setColumnWidth(4, 150)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.mapping_button = QPushButton("Seçili sütunun eşlemesini düzenle")
        self.mapping_button.clicked.connect(self.edit_mapping)
        self.reset_button = QPushButton("Otomatik şemaya dön")
        self.reset_button.clicked.connect(self.reset)
        row.addWidget(self.mapping_button)
        row.addWidget(self.reset_button)
        layout.addLayout(row)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(185)
        layout.addWidget(self.detail)
        self.allow_missing = QCheckBox(
            "Eksik sütunları birleşik çıktıya NULL (değer yok) olarak ekle"
        )
        self.allow_missing.setChecked(self.plan["allow_missing"])
        layout.addWidget(self.allow_missing)
        self.fill = QCheckBox(
            "Birleştirilen aynı parselde: boş/eksik değeri diğer seçili kaynaktan tamamla"
        )
        self.fill.setChecked(self.plan["fill_from_other_source"])
        layout.addWidget(self.fill)
        text = QLabel(
            "Tamamlama kapalıyken seçilen değer politikasındaki kaynağın NULL değeri korunur. Açıkken yalnız aynı parselin birleştirilen kaynaklarına bakılır; ilk NULL olmayan değer alınır (boş metin bir değerdir). Farklı değerler raporda saklanır. Kimlik, ölçüm ve izleme sütunları her zaman korunur."
        )
        text.setWordWrap(True)
        layout.addWidget(text)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        if review.get("mode") == "vector":
            for label in self.findChildren(QLabel):
                label.setText(
                    label.text()
                    .replace("parseller", "kayıtlar")
                    .replace("parsel", "kayıt")
                )
            self.fill.setText(self.fill.text().replace("parsel", "kayıt"))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Şemayı uygula"
        )
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            "Vazgeç"
        )
        buttons.accepted.connect(self.apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.table.currentCellChanged.connect(self.show_detail)
        self.populate()

    def capture(self):
        for i, f in enumerate(self.plan["fields"]):
            f["enabled"] = self.table.item(i, 0).checkState() == CHECKED
            f["name"] = self.table.item(i, 1).text().strip()
            f["type"] = self.table.cellWidget(i, 2).currentData()
        self.plan["allow_missing"] = self.allow_missing.isChecked()
        self.plan["fill_from_other_source"] = self.fill.isChecked()

    def populate(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.plan["fields"]))
        for i, f in enumerate(self.plan["fields"]):
            tick = QTableWidgetItem()
            tick.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            tick.setCheckState(
                CHECKED if f["enabled"] else Qt.CheckState.Unchecked
            )
            self.table.setItem(i, 0, tick)
            self.table.setItem(i, 1, QTableWidgetItem(f["name"]))
            combo = QComboBox()
            for label, key in [
                ("Metin", "text"),
                ("Tam sayı (32 bit)", "integer"),
                ("Ondalık sayı", "real"),
            ]:
                combo.addItem(label, key)
            combo.setCurrentIndex(combo.findData(f["type"]))
            self.table.setCellWidget(i, 2, combo)
            matches = [
                x for x in self.items if x["source_name"] in f["sources"]
            ]
            found = sum(
                any(n in r.get("raw", {}) for n in f["sources"])
                for r in self.records
            )
            total = len(self.records)
            per_source = {}
            for x in matches:
                for file in x["files"]:
                    per_source[file["source_index"]] = max(
                        per_source.get(file["source_index"], 0),
                        file["present"],
                    )
            present_sources = sum(v > 0 for v in per_source.values())
            for column, value in [
                (3, ", ".join(f["sources"])),
                (4, f"{found} / {total}"),
                (5, f"{present_sources} / {len(per_source)} dosya"),
            ]:
                item = QTableWidgetItem(value)
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                )
                self.table.setItem(i, column, item)
        self.table.blockSignals(False)
        if self.table.rowCount():
            self.table.setCurrentCell(0, 1)
            self.show_detail()

    def show_detail(self, *args):
        i = self.table.currentRow()
        if i < 0 or i >= len(self.plan["fields"]):
            return
        names = self.plan["fields"][i]["sources"]
        lines = []
        for item in self.items:
            if item["source_name"] not in names:
                continue
            lines.append(
                f"KAYNAK ALANI: {item['source_name']} — {item['present']}/{item['records']} kayıtta var; {item['null']} NULL"
            )
            for file in item["files"]:
                status = (
                    "YOK"
                    if not file["present"]
                    else f"{file['present']}/{file['records']} kayıtta var; {file['null']} NULL"
                )
                lines.append(
                    f"  {file['format']} | {file['path']}"
                    + (f" [{file['member']}]" if file["member"] else "")
                    + f"\n    {status}; örnekler: "
                    + " | ".join(file["examples"])
                )
        self.detail.setPlainText(
            "\n".join(lines)
            or "Eşlenen alan bu seçimde bulunmuyor. Eşlemeyi düzenleyin veya otomatik şemaya dönün."
        )

    def edit_mapping(self):
        i = self.table.currentRow()
        if i < 0:
            return
        self.capture()
        field = self.plan["fields"][i]
        d = QDialog(self)
        d.setWindowTitle("Kaynak alanlarını aynı çıktı sütununa eşle")
        d.resize(600, 420)
        layout = QVBoxLayout(d)
        note = QLabel(
            "Birden çok ad seçebilirsiniz. Bu alanları kullanan diğer çıktı sütunlarının tikini kaldırın. Aynı kayıtta eşlenen alanlar farklıysa işlem durur."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        table = QTableWidget(len(self.items), 2)
        table.setHorizontalHeaderLabels(
            ["Kaynak alanı", "Bulunan / seçili kayıt"]
        )
        layout.addWidget(table)
        for row, item in enumerate(self.items):
            cell = QTableWidgetItem(item["source_name"])
            cell.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
            )
            cell.setCheckState(
                CHECKED
                if item["source_name"] in field["sources"]
                else Qt.CheckState.Unchecked
            )
            table.setItem(row, 0, cell)
            table.setItem(
                row,
                1,
                QTableWidgetItem(f"{item['present']} / {item['records']}"),
            )
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(d.accept)
        buttons.rejected.connect(d.reject)
        layout.addWidget(buttons)
        if d.exec() == QDialog.DialogCode.Accepted:
            field["sources"] = [
                self.items[row]["source_name"]
                for row in range(len(self.items))
                if table.item(row, 0).checkState() == CHECKED
            ]
            self.populate()
            self.table.setCurrentCell(i, 1)

    def reset(self):
        self.plan = schema.default_schema(self.items, self.sources)
        self.allow_missing.setChecked(True)
        self.fill.setChecked(False)
        self.error.clear()
        self.populate()

    def apply(self):
        try:
            self.capture()
            schema.validate(self.plan, self.sources)
            for field in self.plan["fields"]:
                if not field["enabled"]:
                    continue
                for record in self.records:
                    schema.pick(
                        field,
                        [
                            {
                                "source_uid": str(record["source_index"]),
                                "source_fid": record["fid"],
                                "format": record["format"],
                                "raw": record.get("raw", {}),
                            }
                        ],
                        "preserve",
                        False,
                    )
        except ValueError as error:
            self.error.setText(str(error))
            return
        self.accept()
