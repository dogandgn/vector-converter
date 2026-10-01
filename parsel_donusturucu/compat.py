"""Small compatibility boundary for supported QGIS 3/4 installations."""

import os
import sys
from qgis.PyQt.QtCore import QT_VERSION_STR

try:
    from qgis.PyQt.QtGui import QAction as QAction
except ImportError:  # Qt5 keeps QAction in QtWidgets.
    from qgis.PyQt.QtWidgets import QAction as QAction

__all__ = ["QAction", "close_dataset", "environment_status", "check_runtime"]


def close_dataset(dataset):
    if dataset is None:
        return
    if hasattr(dataset, "Close"):
        dataset.Close()
    else:
        dataset.FlushCache()
        if hasattr(dataset, "Destroy"):
            dataset.Destroy()


def environment_status():
    from qgis.core import Qgis
    from osgeo import gdal, ogr

    driver = ogr.GetDriverByName("OpenFileGDB")
    return {
        "qgis": Qgis.QGIS_VERSION,
        "qt": QT_VERSION_STR,
        "python": ".".join(map(str, sys.version_info[:3])),
        "platform": sys.platform,
        "gdal": gdal.VersionInfo("--version"),
        "gdb_write": os.name == "nt"
        and driver is not None
        and driver.GetMetadataItem("DCAP_CREATE") == "YES",
    }


def check_runtime():
    from qgis.core import Qgis
    from osgeo import gdal

    if Qgis.QGIS_VERSION_INT < 33400 or sys.version_info < (3, 9):
        raise ValueError(
            "QGIS 3.34 veya üzeri ve Python 3.9 veya üzeri gerekir. QGIS 3.44 LTR / 4.x önerilir."
        )
    if int(gdal.VersionInfo("VERSION_NUM")) < 3060000:
        raise ValueError(
            "GDAL 3.6 veya üzeri gerekir; QGIS kurulumunu güncelleyin."
        )
