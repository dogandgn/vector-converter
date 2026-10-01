"""Profile-local preferences and platform-native defaults; no developer paths."""

from pathlib import Path
from datetime import datetime
import uuid
from qgis.PyQt.QtCore import QSettings, QStandardPaths
from qgis.core import QgsApplication

PACKAGE_DIR = Path(__file__).resolve().parent


def documents_folder():
    location = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.DocumentsLocation
    )
    return Path(location) if location else Path.home()


def absolute_path(text, label):
    if not str(text).strip():
        raise ValueError(label + " seçin.")
    path = Path(str(text).strip()).expanduser()
    if not path.is_absolute():
        raise ValueError(label + " tam bir dosya yolu olmalıdır.")
    return path.resolve()


def validate_output(text, input_folder):
    output = absolute_path(text, "Çıktı yolu")
    source = absolute_path(input_folder, "Girdi klasörü")
    if output.is_relative_to(source):
        raise ValueError(
            "Ham kaynakları korumak için çıktıyı girdi klasörünün dışında kaydedin."
        )
    if output.is_relative_to(PACKAGE_DIR):
        raise ValueError(
            "Eklenti kurulum klasörüne veri kaydedilmez; başka bir çıktı klasörü seçin."
        )
    if output.exists():
        raise ValueError("Çıktı zaten var; yeni bir ad seçin.")
    companions = [".rapor.json", ".rapor.html"]
    if output.suffix.lower() in (".geojson", ".kml"):
        companions.append(".veri.json")
    if any(output.with_suffix(ext).exists() for ext in companions):
        raise ValueError(
            "Bu adla bir rapor veya kaynak bilgi dosyası zaten var; çıktı için yeni bir ad seçin."
        )
    return output


class Preferences:
    def __init__(self, path=None):
        self.path = (
            Path(path)
            if path
            else Path(QgsApplication.qgisSettingsDirPath())
            / "parsel_donusturucu.ini"
        )
        self.settings = QSettings(str(self.path), QSettings.Format.IniFormat)

    def input_folder(self):
        value = str(self.settings.value("paths/input", "") or "")
        return (
            value
            if value and Path(value).is_absolute() and Path(value).is_dir()
            else ""
        )

    def output_folder(self):
        value = str(self.settings.value("paths/output", "") or "")
        if value and Path(value).is_absolute() and Path(value).is_dir():
            return Path(value)
        return documents_folder() / "ParselDonusturucu" / "outputs"

    def fresh_output(self, suffix):
        # Microseconds and a random suffix avoid collisions even on rapid clicks.
        return str(
            self.output_folder()
            / (
                "parseller_"
                + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                + "_"
                + uuid.uuid4().hex[:6]
                + suffix
            )
        )

    def remember(self, input_folder=None, output_folder=None):
        for key, value in [("input", input_folder), ("output", output_folder)]:
            if value is not None:
                path = absolute_path(value, "Klasör")
                if path.is_dir():
                    self.settings.setValue("paths/" + key, str(path))
        self.settings.sync()
        return self.settings.status() == QSettings.Status.NoError
