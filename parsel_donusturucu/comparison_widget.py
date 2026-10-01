"""Read-only, sortable input/result tables with explicit CSV export."""

from pathlib import Path
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QPushButton,
    QFileDialog,
)
from . import comparison


class ComparisonDialog(QDialog):
    def __init__(self, model, parent=None, protected=()):
        super().__init__(parent)
        self.model = model
        self.protected = protected
        self.setWindowTitle(model["title"])
        self.resize(1180, 680)
        layout = QVBoxLayout(self)
        parts = [
            f'{label}: {model["totals"][key]}'
            for key, label in comparison.summary_labels(model)
            if key in model["totals"]
        ]
        self.summary = QLabel(
            "\n".join(
                " · ".join(parts[i : i + 4]) for i in range(0, len(parts), 4)
            )
        )
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.summary)
        if model["result"]:
            info = QLabel(
                model.get("state", "")
                + "\n"
                + model.get("output", "")
                + "\n"
                + model.get("target", "")
            )
            info.setWordWrap(True)
            info.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(info)
        columns = comparison.columns(model)
        if model["result"]:
            labels = dict(columns)
            columns = [
                (key, labels[key])
                for key in [
                    "file",
                    "format",
                    "input_status",
                    "output_status",
                    "count",
                    "selected",
                    "exported",
                    "rejected",
                    "output_records",
                    "source_crs",
                    "target_crs",
                    "basis",
                    "notes",
                    "path",
                    "member",
                    "output",
                ]
            ]
        self.table = QTableWidget(len(model["rows"]), len(columns))
        self.table.setHorizontalHeaderLabels([label for key, label in columns])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        for i, row in enumerate(model["rows"]):
            for j, (key, label) in enumerate(columns):
                value = row.get(key, "")
                item = QTableWidgetItem()
                item.setData(Qt.ItemDataRole.DisplayRole, value)
                item.setToolTip(str(value))
                self.table.setItem(i, j, item)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive
        )
        for j, (key, label) in enumerate(columns):
            widths = {
                "file": 200,
                "format": 75,
                "source_crs": 230,
                "target_crs": 230,
                "basis": 230,
                "count": 105,
                "selected": 90,
                "exported": 115,
                "rejected": 135,
                "output_records": 140,
                "path": 300,
                "output": 300,
                "notes": 280,
            }
            self.table.setColumnWidth(j, widths.get(key, 110))
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table, 1)
        if model["result"]:
            note = QLabel(comparison.wording(comparison.MERGE_NOTE, model))
            note.setWordWrap(True)
            layout.addWidget(note)
        self.feedback = QLabel()
        self.feedback.setWordWrap(True)
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.feedback)
        row = QHBoxLayout()
        self.export = QPushButton("CSV olarak kaydet")
        self.export.clicked.connect(self.save_csv)
        row.addWidget(self.export)
        row.addStretch()
        close = QPushButton("Kapat")
        close.clicked.connect(self.close)
        row.addWidget(close)
        layout.addLayout(row)

    def save_csv(self):
        proposed = (
            str(Path(self.model["output"]).with_suffix(".karsilastirma.csv"))
            if self.model.get("output")
            else "girdi_koordinatlari.csv"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "Tabloyu CSV olarak kaydet", proposed, "CSV (*.csv)"
        )
        if not path:
            return
        try:
            comparison.csv_export(path, self.model, self.protected)
        except (OSError, ValueError) as error:
            self.feedback.setText("Kaydedilemedi: " + str(error))
        else:
            self.feedback.setText("Kaydedildi: " + path)
