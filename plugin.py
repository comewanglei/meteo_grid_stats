# SPDX-License-Identifier: GPL-3.0-or-later
"""QGIS action lifecycle."""
from pathlib import Path

from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMessageBox


class MeteoGridStatsPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.dialog = None
        self.action = None

    def initGui(self):
        self.action = QAction(
            QIcon(str(Path(__file__).with_name("icon.svg"))),
            "气象格点分区统计",
            self.iface.mainWindow(),
        )
        self.action.triggered.connect(self.show)
        self.iface.addPluginToRasterMenu("气象格点分区统计", self.action)
        self.iface.addToolBarIcon(self.action)

    def show(self):
        if self.dialog is None:
            try:
                from .dialog import StatisticsDialog
            except ImportError as error:
                QMessageBox.warning(
                    self.iface.mainWindow(),
                    "Meteo Grid Statistics",
                    "Missing QGIS Python dependency (GDAL/OGR or NumPy). "
                    "Please repair your QGIS installation.\n"
                    "缺少 QGIS 自带依赖，请修复 QGIS 安装。\n" + str(error),
                )
                return
            self.dialog = StatisticsDialog(self.iface)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def unload(self):
        if self.dialog:
            self.dialog.shutdown()
            self.dialog = None
        if self.action:
            self.iface.removePluginRasterMenu("气象格点分区统计", self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action.deleteLater()
            self.action = None
