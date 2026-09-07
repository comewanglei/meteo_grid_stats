# SPDX-License-Identifier: GPL-3.0-or-later
"""Qt UI; worker opens its own GDAL datasets and never accesses UI layers."""
from pathlib import Path

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    QgsApplication,
    QgsProject,
    QgsRasterLayer,
    QgsTask,
    QgsVectorLayer,
    QgsProcessingUtils,
)
from osgeo import gdal
from .geolocation import prepare_raster

from .engine import (
    BASE_FIELDS,
    STATISTICS,
    Cancelled,
    Options,
    inspect_bands,
    raster_sources,
    run,
    vector_layers,
)


class StatisticsTask(QgsTask):
    def __init__(self, options, callback):
        super().__init__("气象格点分区统计", QgsTask.Flag.CanCancel)
        self.options, self.callback = options, callback
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = run(self.options, self.setProgress, self.isCanceled)
            return True
        except Exception as error:
            self.error = error
            return False

    def finished(self, success):
        if self.callback:
            self.callback(self.result, self.error)


class StatisticsDialog(QDialog):
    def __init__(self, iface):
        super().__init__(iface.mainWindow())
        self.iface, self.task = iface, None
        self.layer_fields = {}
        self.setWindowTitle("气象格点分区统计")
        self.resize(880, 780)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.inputs = QWidget()
        form = QFormLayout(self.inputs)
        self.tabs.addTab(self.inputs, "数据与统计")
        self.raster_path = QLineEdit()
        form.addRow(
            "格点文件", self.path_row(self.raster_path, self.browse_raster)
        )
        self.raster_path.editingFinished.connect(self.load_raster)
        self.sources = QComboBox()
        self.sources.setMinimumContentsLength(30)
        self.sources.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.sources.currentIndexChanged.connect(self.load_bands)
        form.addRow("变量 / 子数据集", self.sources)
        self.bands = QListWidget()
        self.bands.setMaximumHeight(155)
        form.addRow("时间 / 波段", self.bands)
        self.location_note = QLabel()
        self.location_note.setWordWrap(True)
        form.addRow("定位说明", self.location_note)
        choices = QHBoxLayout()
        for text, callback in (
            ("全选", lambda: self.check_bands(True)),
            ("清空", lambda: self.check_bands(False)),
            ("添加格点到地图", self.add_raster),
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            choices.addWidget(button)
        form.addRow("", choices)
        self.vector_path = QLineEdit()
        form.addRow(
            "面矢量文件", self.path_row(self.vector_path, self.browse_vector)
        )
        self.vector_path.editingFinished.connect(self.load_vector)
        self.layers = QComboBox()
        self.layers.currentTextChanged.connect(self.load_fields)
        form.addRow("面图层", self.layers)
        self.id_field = QComboBox()
        form.addRow("标识字段", self.id_field)
        add_vector = QPushButton("添加矢量到地图")
        add_vector.clicked.connect(self.add_vector)
        form.addRow("", add_vector)
        stat_layout = QHBoxLayout()
        self.stats = {}
        labels = (
            "计数",
            "最小值",
            "最大值",
            "平均值",
            "中位数",
            "总和",
            "标准差",
            "方差",
            "极差",
        )
        for name, label in zip(STATISTICS, labels):
            checkbox = QCheckBox(label)
            checkbox.setChecked(
                name in ("count", "min", "max", "mean", "median")
            )
            self.stats[name] = checkbox
            stat_layout.addWidget(checkbox)
        form.addRow("统计指标", stat_layout)
        bounds = QHBoxLayout()
        self.lower, self.upper = QLineEdit(), QLineEdit()
        self.lower.setPlaceholderText("下限（可空，解码后单位）")
        self.upper.setPlaceholderText("上限（可空，解码后单位）")
        bounds.addWidget(self.lower)
        bounds.addWidget(QLabel("～"))
        bounds.addWidget(self.upper)
        form.addRow("合理值范围", bounds)
        self.exclude_bad = QCheckBox("排除范围外异常值（非有限值始终排除）")
        self.exclude_bad.setChecked(True)
        form.addRow("", self.exclude_bad)
        self.all_touched = QCheckBox("包含所有接触面边界的像元")
        form.addRow("", self.all_touched)
        note = QLabel(
            "默认按像元中心归属，非面积加权；输出 EPSG:4326。\n"
            "NoData 不参与统计；异常范围由气象变量的合理取值设定。"
        )
        note.setWordWrap(True)
        form.addRow("", note)
        self.format = QComboBox()
        for label, value in (
            ("GeoJSON", "GeoJSON"),
            ("Shapefile", "ESRI Shapefile"),
            ("CSV（含 WKT）", "CSV"),
        ):
            self.format.addItem(label, value)
        self.format.currentIndexChanged.connect(self.update_extension)
        form.addRow("导出格式", self.format)
        self.output = QLineEdit()
        form.addRow("输出路径", self.path_row(self.output, self.browse_output))

        results = QWidget()
        result_layout = QVBoxLayout(results)
        self.summary = QLabel(
            "尚未运行。预览最多显示 500 行，完整结果导出到文件。"
        )
        self.summary.setWordWrap(True)
        result_layout.addWidget(self.summary)
        self.table = QTableWidget()
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        result_layout.addWidget(self.table, 2)
        result_layout.addWidget(QLabel("提醒（完整记录见 .warnings.json）"))
        self.warnings = QTextEdit()
        self.warnings.setReadOnly(True)
        result_layout.addWidget(self.warnings, 1)
        self.tabs.addTab(results, "结果与提醒")
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        buttons = QHBoxLayout()
        help_button = QPushButton("使用帮助 / Help")
        help_button.clicked.connect(self.show_help)
        buttons.addWidget(help_button)
        self.start = QPushButton("开始统计")
        self.start.clicked.connect(self.start_task)
        self.cancel = QPushButton("取消任务")
        self.cancel.setEnabled(False)
        self.cancel.clicked.connect(self.cancel_task)
        buttons.addStretch()
        buttons.addWidget(self.start)
        buttons.addWidget(self.cancel)
        layout.addLayout(buttons)

    def path_row(self, field, callback):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(field)
        button = QPushButton("浏览…")
        button.clicked.connect(callback)
        layout.addWidget(button)
        return row

    def error(self, error):
        QMessageBox.warning(self, "气象格点分区统计", str(error))

    def show_help(self):
        help_dialog = QDialog(self)
        help_dialog.setWindowTitle("Meteo Grid Statistics — Help")
        help_dialog.resize(720, 580)
        layout = QVBoxLayout(help_dialog)
        browser = QTextBrowser(help_dialog)
        browser.setOpenExternalLinks(False)
        browser.setHtml(
            Path(__file__).with_name("help.html").read_text(encoding="utf-8")
        )
        layout.addWidget(browser)
        help_dialog.exec()

    def browse_raster(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择格点文件",
            "",
            "气象格点 (*.nc *.nc4 *.cdf *.tif *.tiff *.grb "
            "*.grib *.grb2 *.grib2 *.asc *.vrt);;所有文件 (*)",
        )
        if path:
            self.raster_path.setText(path)
            self.load_raster()

    def load_raster(self):
        self.sources.blockSignals(True)
        self.sources.clear()
        self.bands.clear()
        try:
            if self.raster_path.text().strip():
                for uri, label in raster_sources(
                    self.raster_path.text().strip()
                ):
                    self.sources.addItem(label, uri)
        except Exception as error:
            self.error(error)
        finally:
            self.sources.blockSignals(False)
        self.load_bands()

    def load_bands(self):
        self.bands.clear()
        self.location_note.clear()
        if not self.sources.currentData():
            return
        try:
            located, assumptions = prepare_raster(self.sources.currentData())
            self.location_note.setText(
                "\n".join(message for _, message in assumptions)
                or "使用文件自带空间定位。"
            )
            del located
            for info in inspect_bands(self.sources.currentData()):
                label = "{} · {} · {} · {} · {}".format(
                    info["band"],
                    info["time"] or "无时间标记",
                    info["variable"],
                    info["dims"],
                    info["unit"],
                )
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, info["band"])
                item.setCheckState(Qt.CheckState.Checked)
                item.setToolTip(
                    label + ("\n" + info["warning"] if info["warning"] else "")
                )
                self.bands.addItem(item)
        except Exception as error:
            self.error(error)

    def check_bands(self, checked):
        for i in range(self.bands.count()):
            self.bands.item(i).setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )

    def browse_vector(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择面矢量文件",
            "",
            "矢量 (*.json *.geojson *.shp *.kml *.gpkg);;所有文件 (*)",
        )
        if path:
            self.vector_path.setText(path)
            self.load_vector()

    def load_vector(self):
        self.layers.clear()
        self.id_field.clear()
        self.layer_fields = {}
        try:
            if self.vector_path.text().strip():
                self.layer_fields = dict(
                    vector_layers(self.vector_path.text().strip())
                )
                self.layers.addItems(list(self.layer_fields))
                self.load_fields()
        except Exception as error:
            self.error(error)

    def load_fields(self):
        self.id_field.clear()
        self.id_field.addItem("FID（源要素 ID）", "")
        for name in self.layer_fields.get(self.layers.currentText(), []):
            self.id_field.addItem(name, name)

    def add_raster(self):
        uri = self.sources.currentData()
        if uri:
            try:
                located, assumptions = prepare_raster(uri)
                if assumptions:
                    uri = QgsProcessingUtils.generateTempFilename(
                        "meteo_located.tif"
                    )
                    translated = gdal.Translate(
                        uri,
                        located,
                        format="GTiff",
                        creationOptions=[
                            "COMPRESS=LZW",
                            "TILED=YES",
                            "BIGTIFF=IF_SAFER",
                        ],
                    )
                    if translated is None:
                        raise ValueError("无法生成已定位地图栅格。")
                    translated = None
                located = None
            except Exception as error:
                self.error(error)
                return
            layer = QgsRasterLayer(
                uri, Path(self.raster_path.text()).name, "gdal"
            )
            if assumptions:
                layer.setAbstract(
                    "\n".join(message for _, message in assumptions)
                    + "\n地图使用临时 GeoTIFF；持久保存项目时请另存该栅格。"
                )
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
            else:
                self.error("无法在地图中加载格点。")

    def add_vector(self):
        if self.layers.currentText():
            layer = QgsVectorLayer(
                self.vector_path.text().strip()
                + "|layername="
                + self.layers.currentText(),
                self.layers.currentText(),
                "ogr",
            )
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
            else:
                self.error("无法在地图中加载矢量。")

    def extension(self):
        return {
            "GeoJSON": ".geojson",
            "ESRI Shapefile": ".shp",
            "CSV": ".csv",
        }[self.format.currentData()]

    def update_extension(self):
        if self.output.text():
            self.output.setText(
                str(Path(self.output.text()).with_suffix(self.extension()))
            )

    def browse_output(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "选择新输出文件（不覆盖已有文件）",
            self.output.text(),
            "统计结果 (*{})".format(self.extension()),
        )
        if path:
            self.output.setText(str(Path(path).with_suffix(self.extension())))

    def start_task(self):
        try:
            bands = [
                self.bands.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.bands.count())
                if self.bands.item(i).checkState() == Qt.CheckState.Checked
            ]
            stats = tuple(
                name
                for name, checkbox in self.stats.items()
                if checkbox.isChecked()
            )
            if (
                not self.sources.currentData()
                or not self.layers.currentText()
                or not self.output.text().strip()
            ):
                raise ValueError("请先选择格点、面矢量和输出路径。")
            if not bands or not stats:
                raise ValueError("至少选择一个时间/波段和一个统计指标。")
            options = Options(
                raster=self.sources.currentData(),
                vector=self.vector_path.text().strip(),
                layer=self.layers.currentText(),
                output=self.output.text().strip(),
                format=self.format.currentData(),
                bands=bands,
                id_field=self.id_field.currentData() or "",
                statistics=stats,
                lower=(
                    float(self.lower.text())
                    if self.lower.text().strip()
                    else None
                ),
                upper=(
                    float(self.upper.text())
                    if self.upper.text().strip()
                    else None
                ),
                exclude_bad=self.exclude_bad.isChecked(),
                all_touched=self.all_touched.isChecked(),
            )
            self.task = StatisticsTask(options, self.completed)
            self.task.progressChanged.connect(self.progress_changed)
            self.inputs.setEnabled(False)
            self.start.setEnabled(False)
            self.cancel.setEnabled(True)
            self.progress.setValue(0)
            self.summary.setText("正在后台计算…")
            QgsApplication.taskManager().addTask(self.task)
        except Exception as error:
            self.error(error)

    def progress_changed(self, value):
        self.progress.setValue(int(value))

    def cancel_task(self):
        if self.task:
            self.task.cancel()
            self.cancel.setEnabled(False)

    def completed(self, result, error):
        self.task = None
        self.inputs.setEnabled(True)
        self.start.setEnabled(True)
        self.cancel.setEnabled(False)
        if error:
            self.summary.setText(str(error))
            if not isinstance(error, Cancelled):
                self.error(error)
            return
        self.progress.setValue(100)
        self.tabs.setCurrentIndex(1)
        self.summary.setText(
            "已导出 {} 行：{}\n{} 条提醒：{}".format(
                result["rows"],
                result["output"],
                len(result["warnings"]),
                result["warnings_file"],
            )
        )
        rows = result["preview"]
        fields = ["src_fid", "zone_id", "band", "time"] + [
            key for key in STATISTICS if rows and key in rows[0]
        ]
        fields += [key for key in BASE_FIELDS if key not in fields]
        self.table.clear()
        self.table.setColumnCount(len(fields))
        self.table.setHorizontalHeaderLabels(fields)
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, key in enumerate(fields):
                value = row.get(key)
                self.table.setItem(
                    i, j, QTableWidgetItem("" if value is None else str(value))
                )
        self.warnings.setPlainText(
            "\n".join(
                "[{code}] 面={src_fid} 波段={band}：{message}".format(**w)
                for w in result["warnings"][:500]
            )
            or "无提醒。"
        )
        if (
            Path(result["output"]).suffix.lower() in (".shp", ".geojson")
            and result["rows"]
        ):
            layer = QgsVectorLayer(result["output"], "气象分区统计", "ogr")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)

    def closeEvent(self, event):
        # Closing hides the dialog; a running task can still complete safely.
        event.accept()

    def shutdown(self):
        if self.task:
            self.task.callback = None
            self.task.progressChanged.disconnect(self.progress_changed)
            self.task.cancel()
        self.close()
        self.deleteLater()
