from pathlib import Path
import traceback

from qgis.PyQt.QtCore import Qt, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QIcon, QDesktopServices
from .compat import QAction, environment_status
from qgis.PyQt.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QFileDialog,
    QComboBox,
    QCheckBox,
    QProgressBar,
    QPlainTextEdit,
    QGroupBox,
    QFormLayout,
    QScrollArea,
    QWidget,
)
from qgis.core import (
    QgsApplication,
    QgsTask,
    QgsVectorLayer,
    QgsProject,
    Qgis,
    QgsCoordinateReferenceSystem,
)
from . import core, input_review, source_crs
from .review_widget import ReviewWidget
from .attribute_widget import AttributeDialog
from .comparison_widget import ComparisonDialog
from . import comparison
from . import attribute_schema
from .preferences import (
    Preferences,
    PACKAGE_DIR,
    absolute_path,
    validate_output,
)


class Worker(QgsTask):
    message = pyqtSignal(str)

    def __init__(self, description, job, callback):
        super().__init__(description, QgsTask.Flag.CanCancel)
        self.job = job
        self.callback = callback
        self.result = None
        self.error = ""
        self.cancelled = False

    def run(self):
        try:
            self.result = self.job(self.isCanceled, self.update)
            return True
        except core.Cancelled as error:
            self.cancelled = True
            self.error = str(error)
            return False
        except Exception as error:
            self.error = str(error)
            self.traceback = traceback.format_exc()
            return False

    def update(self, percent, text):
        self.setProgress(percent)
        self.message.emit(text)

    def finished(self, success):
        self.callback(self, success)


class OutputPathEdit(QLineEdit):
    clicked = pyqtSignal()

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setReadOnly(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("Çıktı dosyasını seç")
        self.setPlaceholderText("Kaydedilecek dosyayı seçmek için tıklayın")
        self.textChanged.connect(self.setToolTip)
        self.setToolTip(text)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def keyPressEvent(self, event):
        if event.key() in (
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        ):
            self.clicked.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


class ParselDialog(QDialog):
    def __init__(self, iface):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.sources = []
        self.task = None
        self.report_path = None
        self.close_when_done = False
        self.pending_selection = None
        self.last_report = None
        self.result_dialog = None
        self.input_dialog = None
        self.run_snapshot = None
        self.attribute_plan = None
        self.preferences = Preferences()
        self.setWindowTitle("Parsel Dönüştürücü · 0.9")
        self.resize(1180, 850)
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        title = QLabel("Parselleri birleştir ve seçtiğin biçimde kaydet")
        self.title = title
        title.setStyleSheet("font-size:20px;font-weight:600;margin:4px 0;")
        layout.addWidget(title)
        intro = QLabel(
            "SHP/ZIP, KML/KMZ ve GeoJSON dosyaları taranır. Seçtiğiniz kaynakların özgün alanları ve farklı değerleri saklanır."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.data_mode = QComboBox()
        self.data_mode.addItem("Çalışma türünü seçin", None)
        self.data_mode.addItem("Genel vektör", "vector")
        self.data_mode.addItem("Parsel dönüşümü", "parcel")
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Çalışma türü"))
        mode_row.addWidget(self.data_mode, 1)
        layout.addLayout(mode_row)
        source_group = QGroupBox("1 · Girdi ve parsel seçimi")
        source_form = QVBoxLayout(source_group)
        self.source_group = source_group
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText(
            "Parsel dosyalarının bulunduğu klasörü seçin"
        )
        self.browse_input = QPushButton("Klasör seç")
        self.browse_input.clicked.connect(self.pick_input)
        self.scan_button = QPushButton("Tara ve karşılaştır")
        self.scan_button.clicked.connect(self.start_scan)
        row.addWidget(self.input, 1)
        row.addWidget(self.browse_input)
        row.addWidget(self.scan_button)
        source_form.addLayout(row)
        self.review = ReviewWidget()
        self.table = self.review.files
        self.formats = self.review.formats
        self.merge = self.review.mode
        source_form.addWidget(self.review, 1)
        layout.addWidget(source_group, 1)
        options = QHBoxLayout()
        coords = QGroupBox("2 · Koordinat sistemleri")
        self.coordinate_group = coords
        coordinate_layout = QVBoxLayout(coords)
        self.input_coords = QGroupBox("Girdi koordinatları")
        form = QFormLayout(self.input_coords)
        coordinate_layout.addWidget(self.input_coords)
        self.source_summary = QLabel(
            "Kaynak koordinat bilgisi: klasörü tarayın."
        )
        self.source_summary.setWordWrap(True)
        self.source_summary.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        form.addRow(self.source_summary)
        self.source_list = QPushButton("Girdi koordinat dökümü")
        self.source_list.clicked.connect(self.show_inputs)
        self.source_list.setEnabled(False)
        form.addRow(self.source_list)
        self.output_coords = QGroupBox("Çıktı koordinatları")
        target_form = QFormLayout(self.output_coords)
        coordinate_layout.addWidget(self.output_coords)
        self.profile = QComboBox()
        for key, profile in core.PROFILES.items():
            self.profile.addItem(profile["name"], key)
        target_form.addRow("Kaydedilecek sistem", self.profile)
        self.target_note = QLabel()
        self.target_note.setWordWrap(True)
        target_form.addRow(self.target_note)
        self.policy = QComboBox()
        self.policy.addItem(
            "İlk kaynağı koru; farklı değerleri raporla", "preserve"
        )
        self.policy.addItem(
            "Birleştirmede KML alanlarını tercih et", "prefer_kml"
        )
        self.policy.addItem(
            "Birleştirmede GeoJSON alanlarını tercih et", "prefer_geojson"
        )
        self.policy_label = QLabel("Birleştirmede öznitelik değeri")
        policy_row = QHBoxLayout()
        policy_row.addWidget(self.policy_label)
        policy_row.addWidget(self.policy, 1)
        source_form.addLayout(policy_row)
        attribute_row = QHBoxLayout()
        self.attribute_button = QPushButton(
            "Öznitelikleri incele / çıktı sütunlarını düzenle"
        )
        self.attribute_button.clicked.connect(self.edit_attributes)
        attribute_row.addWidget(self.attribute_button)
        self.attribute_note = QLabel("Kaynak sütunları ve geometri ölçümleri.")
        self.attribute_note.setWordWrap(True)
        attribute_row.addWidget(self.attribute_note, 1)
        source_form.addLayout(attribute_row)
        options.addWidget(coords, 1)
        dest = QGroupBox("3 · Çıktı")
        self.output_group = dest
        df = QFormLayout(dest)
        self.output_format = QComboBox()
        self.output_format.addItem("GeoPackage (.gpkg) — tek dosya", ".gpkg")
        self.output_format.addItem(
            "FileGDB (.gdb) — veritabanı klasörü", ".gdb"
        )
        self.output_format.addItem(
            "GeoJSON (.geojson) — WGS84 derece", ".geojson"
        )
        self.output_format.addItem("KML (.kml) — WGS84 derece", ".kml")
        df.addRow("Çıktı biçimi", self.output_format)
        self.kml_name_label = QLabel("KML kayıt adı")
        self.kml_name_field = QComboBox()
        self.kml_name_field.addItem("Mevcut ad; yoksa katman / kayıt", None)
        df.addRow(self.kml_name_label, self.kml_name_field)
        self.output = OutputPathEdit()
        self.output.clicked.connect(self.pick_output)
        df.addRow("Çıktı yolu", self.output)
        self.output_warning = QLabel()
        self.output_warning.setWordWrap(True)
        df.addRow(self.output_warning)
        self.output_format.currentIndexChanged.connect(
            self.change_output_format
        )
        self.format_note = QLabel(
            "GeoPackage / GDB: seçilen koordinat şablonu kullanılır."
        )
        self.format_note.setWordWrap(True)
        df.addRow(self.format_note)
        self.add_to_qgis = QCheckBox("Sonucu QGIS’e ekle")
        self.add_to_qgis.setChecked(True)
        df.addRow(self.add_to_qgis)
        df.addRow(QLabel("Yanına HTML ve JSON kontrol raporu kaydedilir."))
        df.addRow(QLabel("Mevcut dosyaların üzerine yazılmaz."))
        env = environment_status()
        if not env["gdb_write"]:
            self.output_format.model().item(1).setEnabled(False)
            self.output_format.setItemData(
                1,
                "Bu ortamda FileGDB yazma desteklenmiyor (Windows + OpenFileGDB gerekir).",
                Qt.ItemDataRole.ToolTipRole,
            )
        options.addWidget(dest, 1)
        layout.addLayout(options)
        self.readiness = QLabel()
        self.readiness.setWordWrap(True)
        outer.addWidget(self.readiness)
        self.exclude_blocked = QPushButton(
            "Tanım / düzeltme bekleyen girdilerin seçimini kaldır"
        )
        self.exclude_blocked.setToolTip(
            "Aktarılamayan girdilerin seçimini kaldırır; dosyalar silinmez."
        )
        self.exclude_blocked.clicked.connect(self.review.exclude_unresolved)
        outer.addWidget(self.exclude_blocked)
        row = QHBoxLayout()
        self.run_button = QPushButton("Çıktı oluştur")
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self.start_convert)
        self.cancel_button = QPushButton("İptal")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        self.open_report = QPushButton("Raporu aç")
        self.open_report.setEnabled(False)
        self.open_report.clicked.connect(self.show_report)
        self.result_button = QPushButton("Sonuç tablosu")
        self.result_button.setEnabled(False)
        self.result_button.clicked.connect(self.show_results)
        self.help_button = QPushButton("Kullanım / kurulum")
        self.help_button.clicked.connect(self.show_help)
        row.addWidget(self.run_button)
        row.addWidget(self.cancel_button)
        row.addStretch()
        row.addWidget(self.help_button)
        row.addWidget(self.result_button)
        row.addWidget(self.open_report)
        outer.addLayout(row)
        self.progress = QProgressBar()
        outer.addWidget(self.progress)
        self.status = QLabel("Önce girdi klasörünü tarayın.")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(60)
        self.log.setVisible(False)
        outer.addWidget(self.log)
        self.input.textChanged.connect(self.input_changed)
        self.formats.formatsChanged.connect(self.update_source_summary)
        self.profile.currentIndexChanged.connect(self.selection_changed)
        self.output.textChanged.connect(self.selection_changed)
        self.review.selectionChanged.connect(self.selection_changed)
        self.merge.currentIndexChanged.connect(self.selection_changed)
        self.data_mode.currentIndexChanged.connect(self.mode_changed)
        self.mode_changed()

    def mode_changed(self):
        chosen = self.data_mode.currentData() in ("vector", "parcel")
        general = self.data_mode.currentData() == "vector"
        self.title.setText(
            (
                "Vektörleri toparla ve seçtiğin biçimde kaydet"
                if general
                else "Parselleri birleştir ve seçtiğin biçimde kaydet"
            )
            if chosen
            else "Vektör ve parsel dönüşümü"
        )
        self.source_group.setTitle(
            "1 · Girdi ve kayıt seçimi"
            if general or not chosen
            else "1 · Girdi ve parsel seçimi"
        )
        self.input.setPlaceholderText(
            (
                "Vektör dosyalarının bulunduğu klasörü seçin"
                if general
                else "Parsel dosyalarının bulunduğu klasörü seçin"
            )
            if chosen
            else "Girdi klasörünü seçin"
        )
        self.review.set_general(general)
        self.input_changed()
        self.selection_changed()
        self.status.setText(
            (
                "Klasörü yeniden tarayın."
                if self.input.text()
                else "Girdi klasörünü seçin."
            )
            if chosen
            else "Önce çalışma türünü seçin."
        )

    def fresh_output(self):
        value = self.preferences.fresh_output(self.output_format.currentData())
        if self.data_mode.currentData() == "vector":
            value = str(
                Path(value).with_name(
                    Path(value).name.replace("parseller_", "vektorler_", 1)
                )
            )
        return value

    def change_output_format(self):
        current = self.output.text().strip()
        self.output.setText(
            str(Path(current).with_suffix(self.output_format.currentData()))
            if current
            else ""
        )
        exchange = self.output_format.currentData() in (".geojson", ".kml")
        if exchange:
            if self.profile.isEnabled():
                self.previous_profile = self.profile.currentData()
            self.profile.setCurrentIndex(self.profile.findData("wgs84"))
        elif not self.profile.isEnabled():
            self.profile.setCurrentIndex(
                self.profile.findData(
                    getattr(self, "previous_profile", "wgs84")
                )
            )
        self.profile.setEnabled(not exchange)
        self.format_note.setText(
            "KML / standart GeoJSON: WGS84 enlem-boylam (derece) kullanılır; bu nedenle çıktı koordinat seçimi kilitlidir. Kaynak bilgileri .veri.json dosyasında korunur; birlikte paylaşın."
            if exchange
            else "GeoPackage / GDB: seçilen koordinat şablonu kullanılır."
        )
        self.selection_changed()

    def pick_input(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Kaynak klasörü", self.input.text()
        )
        if folder:
            self.input.setText(folder)
            self.start_scan()

    def pick_output(self):
        filter = {
            ".gpkg": "GeoPackage (*.gpkg)",
            ".gdb": "FileGDB (*.gdb)",
            ".geojson": "GeoJSON (*.geojson)",
            ".kml": "KML (*.kml)",
        }[self.output_format.currentData()]
        path, _ = QFileDialog.getSaveFileName(
            self, "Çıktı dosyasını seç", self.output.text(), filter
        )
        if path:
            self.output.setText(path)

    def invalidate_scan(self, *args):
        if self.review.review:
            self.pending_selection = self.review.snapshot()
        self.review.clear()
        self.run_button.setEnabled(False)
        self.update_source_summary()

    def input_changed(self, *args):
        self.sources = []
        self.pending_selection = None
        self.attribute_plan = None
        self.review.clear()
        self.run_button.setEnabled(False)
        self.update_source_summary()

    def format_filter_changed(self, *args):
        self.invalidate_scan()
        if not self.task and Path(self.input.text().strip()).is_dir():
            self.start_scan()

    def selection_changed(self, *args):
        self.refresh_kml_names()
        chosen = self.data_mode.currentData() in ("vector", "parcel")
        for group in (
            self.source_group,
            self.coordinate_group,
            self.output_group,
        ):
            group.setEnabled(chosen and not self.task)
        ready = chosen and not self.task and self.review.ready()
        count = self.review.expected_count(self.merge.currentData() == "merge")
        show = self.merge.currentData() == "merge"
        self.policy.setVisible(show)
        self.policy_label.setVisible(show)
        self.attribute_button.setEnabled(ready)
        reasons = []
        if not chosen:
            reasons.append("Önce çalışma türünü seçin.")
        elif self.task:
            reasons.append(
                "İşlem sürüyor; tamamlanmasını bekleyin veya İptal düğmesini kullanın."
            )
        elif self.review.review is None:
            reasons.append(
                "Klasörü seçip Tara ve karşılaştır düğmesine basın."
                if not self.sources
                else "Girdi veya koordinat tanımı değişti. Tara ve karşılaştır düğmesine yeniden basın."
            )
        else:
            if not self.formats.currentData():
                reasons.append(
                    "İşleme katılacak en az bir dosya türünü işaretleyin."
                )
            elif not self.review.selected():
                reasons.append(
                    "Hiç kayıt seçili değil. "
                    + self.review.all_button.text()
                    + " düğmesine basın veya listeden kayıt seçin."
                )
            unresolved = self.review.unresolved()
            missing_count = sum(
                not self.sources[i].wkt
                and not self.sources[i].assigned_crs
                and not self.review.review.get("missing_crs")
                for i in unresolved
            )
            if missing_count:
                remedy = (
                    "Koordinat tanım dosyasını girdi klasörüne ekleyip yeniden tarayın; ya da bu girdilerin seçimini kaldırın."
                    if self.data_mode.currentData() == "vector"
                    else "Koordinat tanım dosyasını veya eş KML/GeoJSON kaydını girdi klasörüne ekleyip yeniden tarayın; ya da bu girdilerin seçimini kaldırın."
                )
                reasons.append(
                    f"⚠ {missing_count} seçili girdinin kaynak koordinatı belirlenemedi. "
                    + remedy
                )
            if len(unresolved) > missing_count:
                reasons.append(
                    f"⚠ {len(unresolved)-missing_count} seçili girdi için düzeltme gerekiyor. Listedeki uyarıyı inceleyin veya seçimini kaldırın."
                )
            if self.profile.currentData() not in core.PROFILES:
                reasons.append("⚠ Çıktı koordinat sistemini seçin.")
        self.exclude_blocked.setVisible(
            chosen and bool(self.review.unresolved()) and not self.task
        )
        if self.attribute_plan and ready:
            try:
                attribute_schema.validate(
                    self.attribute_plan, self.review.selected_sources()
                )
            except ValueError as error:
                self.attribute_note.setText(str(error))
                reasons.append(
                    "Seçim değişti; Öznitelikleri incele düğmesinden çıktı sütunlarını yeniden onaylayın. "
                    + str(error)
                )
            else:
                self.attribute_note.setText(
                    f"{sum(f['enabled'] for f in self.attribute_plan['fields'])} kaynak sütunu + geometri ölçümleri."
                )
        elif not self.attribute_plan:
            self.attribute_note.setText(
                "Kaynak sütunları korunur; ortak alanları düzenleyebilirsiniz."
            )
        output_error = ""
        if chosen and not self.task:
            try:
                output = validate_output(self.output.text(), self.input.text())
                if output.suffix.lower() != self.output_format.currentData():
                    raise ValueError(
                        "Çıktı dosyasının uzantısı "
                        + self.output_format.currentData()
                        + " olmalıdır."
                    )
            except ValueError as error:
                output_error = "⚠ " + str(error)
                reasons.append(output_error)
        self.output_warning.setText(output_error)
        self.output_warning.setVisible(bool(output_error))
        self.output.setStyleSheet(
            "border:1px solid #b26a00;" if output_error else ""
        )
        self.target_note.setText(
            "KML / GeoJSON için WGS84 derece zorunludur."
            if self.output_format.currentData() in (".kml", ".geojson")
            else ""
        )
        self.target_note.setVisible(bool(self.target_note.text()))
        enabled = ready and not reasons
        self.run_button.setEnabled(enabled)
        self.run_button.setText(
            f"Çıktı oluştur ({count} kayıt)" if enabled else "Çıktı oluştur"
        )
        message = (
            "Çıktıya hazır: "
            + str(count)
            + " kayıt. Koordinat dönüşümü ve yazma kontrolleri işlem sırasında yapılır."
            if enabled
            else "\n".join(reasons)
        )
        self.readiness.setText(message)
        self.run_button.setToolTip(message)

    def refresh_kml_names(self):
        visible = (
            self.data_mode.currentData() == "vector"
            and self.output_format.currentData() == ".kml"
        )
        self.kml_name_label.setVisible(visible)
        self.kml_name_field.setVisible(visible)
        fields = []
        if visible and self.review.ready():
            plan = self.attribute_plan
            if plan is None:
                selected = self.review.selected()
                names = {
                    key
                    for group in self.review.review["groups"]
                    for row in group["records"]
                    if (row["source_index"], row["fid"]) in selected
                    for key in row["raw"]
                }
                items = [
                    {"source_name": key, "canonical": key, "values": []}
                    for key in sorted(names)
                ]
                plan = attribute_schema.default_schema(
                    items, self.review.selected_sources(), general=True
                )
            fields = [f["name"] for f in plan["fields"] if f["enabled"]]
        previous = self.kml_name_field.currentData()
        current = [
            self.kml_name_field.itemData(i)
            for i in range(1, self.kml_name_field.count())
        ]
        if current != fields:
            self.kml_name_field.clear()
            self.kml_name_field.addItem(
                "Mevcut ad; yoksa katman / kayıt", None
            )
            for name in fields:
                self.kml_name_field.addItem("Sütun: " + name, name)
            self.kml_name_field.setCurrentIndex(
                max(0, self.kml_name_field.findData(previous))
            )
        self.kml_name_field.setEnabled(bool(fields) and not self.task)

    def edit_attributes(self):
        if self.task or not self.review.ready():
            return
        dialog = AttributeDialog(
            self.review.selected_sources(),
            self.review.review,
            self.review.selected(),
            self.attribute_plan,
            self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.attribute_plan = dialog.plan
        self.selection_changed()

    def busy(self, active):
        for w in [
            self.data_mode,
            self.input,
            self.browse_input,
            self.scan_button,
            self.profile,
            self.output,
            self.output_format,
            self.kml_name_field,
            self.add_to_qgis,
            self.formats,
            self.merge,
            self.policy,
            self.review,
            self.attribute_button,
            self.exclude_blocked,
            self.source_list,
        ]:
            w.setEnabled(not active)
        self.run_button.setEnabled(not active and self.review.ready())
        self.cancel_button.setEnabled(active)
        if self.output_format.currentData() in (".geojson", ".kml"):
            self.profile.setEnabled(False)

    def launch(self, description, job, callback):
        # A previous close request applies only to that task, not the next run.
        self.close_when_done = False
        self.busy(True)
        self.progress.setValue(0)
        self.task = Worker(description, job, callback)
        self.selection_changed()
        self.task.progressChanged.connect(
            lambda value: self.progress.setValue(int(value))
        )
        self.task.message.connect(self.status.setText)
        QgsApplication.taskManager().addTask(self.task)

    def failed(self, task):
        self.log.show()
        self.status.setText(task.error)
        self.log.appendPlainText(task.error)
        if getattr(task, "traceback", ""):
            from qgis.core import QgsMessageLog

            QgsMessageLog.logMessage(
                task.traceback, "Parsel Dönüştürücü", Qgis.MessageLevel.Warning
            )

    def done(self):
        self.task = None
        self.busy(False)
        self.selection_changed()
        self.update_source_summary()
        if self.close_when_done:
            self.close()

    def start_scan(self):
        if self.task:
            return
        if self.data_mode.currentData() not in ("vector", "parcel"):
            self.status.setText("Önce çalışma türünü seçin.")
            return
        try:
            folder = absolute_path(self.input.text(), "Girdi klasörü")
            if not folder.is_dir():
                raise ValueError("Girdi klasörü bulunamadı.")
        except ValueError as error:
            self.status.setText(str(error))
            return
        self.invalidate_scan()
        general = self.data_mode.currentData() == "vector"

        def job(cancel, progress):
            core.preflight()
            sources = core.scan(
                folder,
                cancel=cancel,
                progress=lambda n, s: progress(n * 0.35, s),
                general=general,
            )
            if general:
                from . import vector_review

                review = vector_review.build(
                    sources,
                    cancel=cancel,
                    progress=lambda n, s: progress(35 + n * 0.65, s),
                )
            else:
                sources = source_crs.resolve_counterparts(
                    sources, cancel=cancel
                )
                review = input_review.build(
                    sources,
                    cancel=cancel,
                    progress=lambda n, s: progress(35 + n * 0.65, s),
                )
            return sources, review

        self.launch("Parsel dosyalarını tara", job, self.scanned)

    def scanned(self, task, success):
        if success:
            self.sources, review = task.result
            self.review.populate(self.sources, review)
            if not self.preferences.remember(input_folder=self.input.text()):
                self.log.appendPlainText(
                    "Son klasör tercihi kaydedilemedi; bu işlem devam edebilir."
                )
            self.review.restore(self.pending_selection)
            self.pending_selection = None
            self.update_source_summary()
            self.status.setText(
                "Girdi önizlemesi hazır. Dosya veya kayıt kutularıyla eleme yapın; çıktı koordinat sisteminin uygunluğu aktarımda ayrıca kontrol edilir."
            )
            self.log.appendPlainText(self.status.text())
        else:
            self.failed(task)
        self.done()

    def start_convert(self):
        if self.task:
            return
        self.selection_changed()
        if not self.run_button.isEnabled():
            self.status.setText(self.readiness.text())
            return
        if not self.review.ready():
            self.status.setText(
                "Önce girdileri karşılaştırın; çözümlenmemiş seçili kayıtları düzeltin veya hariç tutun."
            )
            return
        try:
            output = validate_output(self.output.text(), self.input.text())
        except ValueError as error:
            self.status.setText(str(error))
            return
        if output.suffix.lower() != self.output_format.currentData():
            self.status.setText(
                "Çıktı uzantısı seçili biçimle aynı olmalıdır: "
                + self.output_format.currentData()
            )
            return
        sources = self.review.selected_sources()
        profile = self.profile.currentData()
        input_folder = self.input.text().strip()
        try:
            if self.attribute_plan is not None:
                attribute_schema.validate(self.attribute_plan, sources)
        except ValueError as error:
            self.status.setText(str(error))
            return
        import copy

        plan = copy.deepcopy(self.attribute_plan)
        kml_name_field = (
            self.kml_name_field.currentData()
            if self.data_mode.currentData() == "vector"
            and self.output_format.currentData() == ".kml"
            else None
        )
        merge_mode = (
            "merge" if self.merge.currentData() == "merge" else "separate"
        )
        attribute_policy = self.policy.currentData()
        self.result_add_to_qgis = self.add_to_qgis.isChecked()
        self.run_snapshot = (sources, output, dict(core.PROFILES[profile]))
        self.result_input_folder = input_folder
        self.run_data_mode = self.data_mode.currentData()
        engine = core.convert
        if self.run_data_mode == "vector":
            from .vector_export import convert

            engine = convert
        self.launch(
            "Parsel veritabanını oluştur",
            lambda cancel, progress: engine(
                sources,
                output,
                profile,
                cancel=cancel,
                progress=progress,
                protected=[input_folder, PACKAGE_DIR],
                merge_mode=merge_mode,
                attribute_policy=attribute_policy,
                attribute_schema=plan,
                kml_name_field=kml_name_field,
            ),
            self.converted,
        )

    def update_source_summary(self, *args):
        active = [
            s for s in self.sources if s.format in self.formats.currentData()
        ]
        text = source_crs.summary(active)
        self.source_summary.setText(text)
        self.source_summary.setToolTip(text)
        self.source_list.setEnabled(bool(self.sources) and not self.task)
        self.refresh_target_profiles(active)

    def refresh_target_profiles(self, sources):
        current = self.profile.currentData()
        self.profile.blockSignals(True)
        self.profile.clear()
        declared = {s.wkt for s in sources if s.status == "ready" and s.wkt}

        def heading(text):
            self.profile.addItem(text, None)
            self.profile.model().item(self.profile.count() - 1).setEnabled(
                False
            )

        heading("— Veriden okunan koordinat sistemleri —")
        for wkt in sorted(declared):
            crs = QgsCoordinateReferenceSystem.fromWkt(wkt)
            matching = next(
                (
                    k
                    for k, p in core.PROFILES.items()
                    if crs == QgsCoordinateReferenceSystem(p["crs"])
                ),
                None,
            )
            self.profile.addItem(
                crs.description()
                + " ("
                + (crs.authid() or "özel tanım")
                + ")",
                matching,
            )
            if matching is None:
                self.profile.model().item(self.profile.count() - 1).setEnabled(
                    False
                )
                self.profile.setItemData(
                    self.profile.count() - 1,
                    "Bu kaynak tanımı mevcut çıktı şablonlarında yok; aşağıdan bir hedef seçin.",
                    Qt.ItemDataRole.ToolTipRole,
                )
        if not declared:
            heading("Veride kayıtlı koordinat tanımı bulunamadı")
        heading("— Kullanıcı seçimi: hazır çıktı sistemleri —")
        for key, profile in core.PROFILES.items():
            self.profile.addItem(profile["name"], key)
        self.profile.setCurrentIndex(
            max(0, self.profile.findData(current or "wgs84"))
        )
        self.profile.blockSignals(False)

    def converted(self, task, success):
        if success:
            r = task.result
            self.last_report = r
            s = r["summary"]
            self.report_path = Path(r["output"]).with_suffix(".rapor.html")
            if not self.preferences.remember(
                output_folder=Path(r["output"]).parent
            ):
                self.log.appendPlainText(
                    "Son çıktı klasörü tercihi kaydedilemedi; çıktı dosyası oluşturuldu."
                )
            self.open_report.setEnabled(True)
            self.status.setText(
                f"{s['aktarilan_parsel']} çıktı kaydı / {s['kaynak_baglantisi']} kaynak bağlantısı; {s['oznitelik_farki']} alan farkı; {s['aktarilamayan_kayit']} kayıt aktarılamadı."
            )
            self.log.appendPlainText(self.status.text() + "\n" + r["output"])
            if self.result_add_to_qgis:
                try:
                    self.load_output(r["output"])
                except Exception as error:
                    self.log.show()
                    self.log.appendPlainText(
                        "Çıktı kaydedildi; QGIS’e eklenemedi: " + str(error)
                    )
            self.output.setText(self.fresh_output())
        else:
            self.failed(task)
            if self.run_snapshot:
                sources, output, profile = self.run_snapshot
                self.last_report = comparison.failed_report(
                    sources,
                    output,
                    profile,
                    task.error,
                    getattr(task, "cancelled", False),
                )
                self.last_report["mode"] = getattr(
                    self, "run_data_mode", "parcel"
                )
            self.report_path = None
            self.open_report.setEnabled(False)
        self.done()
        self.result_button.setEnabled(self.last_report is not None)
        if self.last_report and not self.close_when_done:
            self.show_results()

    def show_inputs(self):
        if self.input_dialog:
            self.input_dialog.close()
            self.input_dialog.deleteLater()
        pending = {
            issue["source_index"]: {
                **issue,
                "selected": issue["source_index"] in self.review.unresolved(),
            }
            for issue in (self.review.review or {}).get("blocked", [])
        }
        model = comparison.input_model(
            self.sources, self.review.selected(), pending
        )
        self.input_dialog = ComparisonDialog(
            model, self, protected=[self.input.text(), PACKAGE_DIR]
        )
        self.input_dialog.show()
        self.input_dialog.raise_()

    def show_results(self):
        if not self.last_report:
            return
        if self.result_dialog:
            self.result_dialog.close()
            self.result_dialog.deleteLater()
        self.result_dialog = ComparisonDialog(
            comparison.result_model(self.last_report),
            self,
            protected=[
                getattr(self, "result_input_folder", self.input.text()),
                PACKAGE_DIR,
            ],
        )
        self.result_dialog.show()
        self.result_dialog.raise_()

    def load_output(self, path):
        import sqlite3

        if (
            self.last_report
            and (
                self.last_report.get("mode") == "vector"
                or self.last_report.get("public_schema")
            )
            and Path(path).suffix.lower() == ".kml"
        ):
            from osgeo import ogr

            ds = ogr.Open(str(path))
            if ds is None:
                raise ValueError("Çıktı QGIS tarafından açılamadı.")
            names = [
                ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())
            ]
            core.close_dataset(ds)
            layers = [
                QgsVectorLayer(str(path) + "|layername=" + name, name, "ogr")
                for name in names
            ]
            if not layers or not all(layer.isValid() for layer in layers):
                raise ValueError("KML katmanları açılamadı.")
            project = QgsProject.instance()
            group = project.layerTreeRoot().addGroup(Path(path).stem)
            for layer in layers:
                layer.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
                project.addMapLayer(layer, False)
                group.addLayer(layer)
            self.iface.mapCanvas().refresh()
            return
        if (
            self.last_report
            and (
                self.last_report.get("mode") == "vector"
                or self.last_report.get("public_schema")
            )
            and Path(path).suffix.lower() in (".gdb", ".gpkg")
        ):
            project = QgsProject.instance()
            group = project.layerTreeRoot().addGroup(Path(path).stem)
            names = list(self.last_report["output_layers"])
            if not self.last_report.get("public_schema"):
                names += [
                    "kaynaklar",
                    "kayit_kaynaklari",
                    "kontroller",
                    "islem_ozeti",
                ]
            for name in names:
                layer = QgsVectorLayer(
                    str(path) + "|layername=" + name, name, "ogr"
                )
                if not layer.isValid():
                    raise ValueError("Katman açılamadı: " + name)
                project.addMapLayer(layer, False)
                group.addLayer(layer)
            self.iface.mapCanvas().refresh()
            return
        if Path(path).suffix.lower() in (".geojson", ".kml"):
            layer = QgsVectorLayer(str(path), Path(path).stem, "ogr")
            if not layer.isValid():
                raise ValueError("Çıktı QGIS tarafından açılamadı.")
            layer.setCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
            QgsProject.instance().addMapLayer(layer)
            self.iface.mapCanvas().refresh()
            return
        registration = None
        if Path(path).suffix.lower() == ".gpkg":
            conn = sqlite3.connect(
                "file:" + Path(path).as_posix() + "?mode=ro", uri=True
            )
            try:
                registration = conn.execute(
                    "SELECT s.organization,s.organization_coordsys_id FROM gpkg_geometry_columns g JOIN gpkg_spatial_ref_sys s USING(srs_id) WHERE g.table_name='parseller'"
                ).fetchone()
            finally:
                conn.close()
        else:
            from .gdb_writer import spatial_reference

            srs = spatial_reference(path)
            # Identify against our two supported EPSG definitions, not against an external report.
            from osgeo import osr

            for profile in core.PROFILES.values():
                expected = osr.SpatialReference()
                code = int(profile["crs"].split(":")[1])
                expected.ImportFromEPSG(code)
                if srs.IsSame(
                    expected,
                    [
                        "IGNORE_DATA_AXIS_TO_SRS_AXIS_MAPPING=YES",
                        "CRITERION=EQUIVALENT_EXCEPT_AXIS_ORDER_GEOGCRS",
                    ],
                ):
                    registration = ("EPSG", code)
                    break
            if not registration:
                raise ValueError(
                    "FileGDB koordinat tanımı desteklenen şablonlarla eşleşmedi."
                )
        project = QgsProject.instance()
        group = project.layerTreeRoot().insertGroup(0, Path(path).stem)
        for name in [
            "parseller",
            "kaynaklar",
            "kayit_kaynaklari",
            "kontroller",
            "islem_ozeti",
        ]:
            layer = QgsVectorLayer(
                str(path) + "|layername=" + name, name, "ogr"
            )
            if not layer.isValid():
                self.log.appendPlainText("Katman yüklenemedi: " + name)
                continue
            if (
                name == "parseller"
                and registration
                and registration[0] == "EPSG"
            ):
                # This QGIS build can omit authid when identifying WKT. Use the verified GPKG registry.
                layer.setCrs(
                    QgsCoordinateReferenceSystem(
                        "EPSG:" + str(registration[1])
                    )
                )
            project.addMapLayer(layer, False)
            group.addLayer(layer)
        self.iface.mapCanvas().refresh()

    def cancel(self):
        if self.task:
            self.task.cancel()
            self.status.setText(
                "İptal isteği alındı; geçici çıktı kapatılıyor…"
            )

    def show_report(self):
        if self.report_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.report_path)))

    def show_help(self):
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(PACKAGE_DIR / "help.html"))
        )

    def closeEvent(self, event):
        if self.task:
            self.close_when_done = True
            self.cancel()
            event.ignore()
        else:
            event.accept()


class ParselPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.dialog = None
        self.action = None

    def initGui(self):
        self.action = QAction(
            QIcon(str(Path(__file__).with_name("icon.svg"))),
            "Parsel Dönüştürücü",
            self.iface.mainWindow(),
        )
        self.action.setObjectName("parsel_donusturucu_open")
        self.action.triggered.connect(self.show)
        self.iface.addPluginToVectorMenu("Parsel Dönüştürücü", self.action)
        self.iface.addToolBarIcon(self.action)

    def show(self):
        if self.dialog is None:
            self.dialog = ParselDialog(self.iface)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def unload(self):
        if self.dialog and self.dialog.task:
            self.dialog.task.cancel()
        if self.dialog:
            self.dialog.close()
        if self.action:
            self.iface.removePluginVectorMenu(
                "Parsel Dönüştürücü", self.action
            )
            self.iface.removeToolBarIcon(self.action)
