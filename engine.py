# SPDX-License-Identifier: GPL-3.0-or-later
"""GDAL/OGR engine, independent of QGIS and Qt."""
import csv
import datetime as dt
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from osgeo import gdal, ogr, osr
from .geolocation import prepare_raster


STATISTICS = (
    "count",
    "min",
    "max",
    "mean",
    "median",
    "sum",
    "std",
    "var",
    "range",
)
BASE_FIELDS = (
    "src_fid",
    "zone_id",
    "band",
    "variable",
    "time",
    "dims",
    "unit",
    "n_valid",
    "n_nodata",
    "n_bad",
)


class Cancelled(Exception):
    pass


@dataclass
class Options:
    raster: str
    vector: str
    layer: str
    output: str
    format: str = "GeoJSON"
    bands: list = field(default_factory=list)
    id_field: str = ""
    statistics: tuple = ("count", "min", "max", "mean", "median")
    lower: float = None
    upper: float = None
    exclude_bad: bool = True
    all_touched: bool = False
    median_limit: int = 5_000_000


def _open_raster(path):
    ds = gdal.OpenEx(str(path), gdal.OF_RASTER | gdal.OF_READONLY)
    if ds is None:
        raise ValueError("无法打开格点数据：" + str(path))
    return ds


def _open_vector(path):
    ds = ogr.Open(str(path), 0)
    if ds is None:
        raise ValueError("无法打开矢量数据：" + str(path))
    return ds


def raster_sources(path):
    ds = _open_raster(path)
    sources = ds.GetSubDatasets()
    if sources:
        return [(uri, description) for uri, description in sources]
    if not ds.RasterCount:
        raise ValueError("没有可读取的二维格点/波段。")
    return [(str(path), Path(path).name)]


def vector_layers(path):
    ds = _open_vector(path)
    result = []
    for i in range(ds.GetLayerCount()):
        layer = ds.GetLayerByIndex(i)
        # Validate unknown geometry layers feature by feature.
        if ogr.GT_Flatten(layer.GetGeomType()) not in (
            ogr.wkbPolygon,
            ogr.wkbMultiPolygon,
            ogr.wkbUnknown,
        ):
            continue
        definition = layer.GetLayerDefn()
        result.append(
            (
                layer.GetName(),
                [
                    definition.GetFieldDefn(j).GetName()
                    for j in range(definition.GetFieldCount())
                ],
            )
        )
    if not result:
        raise ValueError("文件中没有面图层。")
    return result


def _time_label(value, units, calendar):
    """Decode ordinary CF time; preserve non-Gregorian calendars losslessly."""
    raw = "{} [{}; calendar={}]".format(
        value, units or "unit unknown", calendar
    )
    if calendar not in ("standard", "gregorian", "proleptic_gregorian"):
        return raw, "非公历时间保留原始值、单位与日历：" + raw
    match = re.match(
        r"^(seconds?|minutes?|hours?|days?) since (.+)$", units or "", re.I
    )
    if not match:
        return raw, "无法解码时间单位，保留原始时间：" + raw
    try:
        origin = dt.datetime.fromisoformat(
            match[2].strip().replace("Z", "+00:00")
        )
        if origin.year < 1583 and calendar != "proleptic_gregorian":
            return raw, "早期混合历时间保留原始值：" + raw
        factor = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[1][0].lower()]
        date = origin + dt.timedelta(seconds=float(value) * factor)
        if date.tzinfo is None:
            date = date.replace(tzinfo=dt.timezone.utc)
        return (
            date.astimezone(dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "",
        )
    except (ValueError, OverflowError):
        return raw, "无法解码时间，保留原始时间：" + raw


def band_info(ds):
    metadata = ds.GetMetadata() or {}
    result = []
    for i in range(1, ds.RasterCount + 1):
        band = ds.GetRasterBand(i)
        md = band.GetMetadata() or {}
        prefix_length = len("NETCDF_DIM_")
        dims = {
            k[prefix_length:]: v
            for k, v in md.items()
            if k.startswith("NETCDF_DIM_")
        }
        time, warning = "", ""
        for key, value in dims.items():
            units = metadata.get(key + "#units", "")
            if key.lower() == "time" or " since " in units:
                time, warning = _time_label(
                    value, units, metadata.get(key + "#calendar", "standard")
                )
                break
        if "GRIB_VALID_TIME" in md:
            try:
                time = (
                    dt.datetime.fromtimestamp(
                        float(md["GRIB_VALID_TIME"].split()[0]),
                        dt.timezone.utc,
                    )
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            except (ValueError, OverflowError, OSError):
                time, warning = (
                    md["GRIB_VALID_TIME"],
                    "GRIB 时间无法解码，保留原值。",
                )
        for key in (
            "GRIB_REF_TIME",
            "GRIB_FORECAST_SECONDS",
            "GRIB_SHORT_NAME",
        ):
            if key in md:
                dims[key] = md[key]
        variable = md.get(
            "NETCDF_VARNAME",
            md.get("GRIB_ELEMENT", band.GetDescription() or "band"),
        )
        unit = band.GetUnitType() or md.get(
            "units", md.get("GRIB_UNIT", metadata.get(variable + "#units", ""))
        )
        result.append(
            dict(
                band=i,
                variable=variable,
                time=time,
                dims=json.dumps(dims, ensure_ascii=False, sort_keys=True),
                unit=unit,
                warning=warning,
            )
        )
    return result


def inspect_bands(path):
    return band_info(_open_raster(path))


def _srs(wkt):
    if not wkt:
        raise ValueError(
            "数据缺少坐标系，请先在 QGIS 中指定正确坐标系并另存。"
        )
    ref = osr.SpatialReference()
    if ref.SetFromUserInput(wkt) != 0:
        raise ValueError("无法识别坐标系。")
    ref.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return ref


def _check_cancel(cancel):
    if cancel():
        raise Cancelled("任务已取消，未写入结果。")


class Accumulator:
    """Stable mergeable population moments; bounded optional exact median."""

    def __init__(self, median=False, limit=5_000_000):
        self.n = 0
        self.mean = self.m2 = self.total = 0.0
        self.minimum, self.maximum = math.inf, -math.inf
        self.parts = [] if median else None
        self.limit = limit

    def add(self, values):
        n = len(values)
        if not n:
            return
        values = np.asarray(values, dtype=np.float64)
        mean = float(np.mean(values))
        delta = mean - self.mean
        total_n = self.n + n
        self.m2 += (
            float(np.sum((values - mean) ** 2))
            + delta**2 * self.n * n / total_n
        )
        self.mean += delta * n / total_n
        self.n = total_n
        self.total += float(np.sum(values))
        self.minimum = min(self.minimum, float(np.min(values)))
        self.maximum = max(self.maximum, float(np.max(values)))
        if self.parts is not None:
            if self.n > self.limit:
                raise ValueError(
                    "单面有效像元超过精确中位数内存预算，请取消中位数或缩小统计区域。"
                )
            self.parts.append(values.copy())

    def result(self, names):
        if not self.n:
            return {name: 0 if name == "count" else None for name in names}
        variance = max(0.0, self.m2 / self.n)
        values = dict(
            count=self.n,
            min=self.minimum,
            max=self.maximum,
            mean=self.mean,
            sum=self.total,
            std=math.sqrt(variance),
            var=variance,
            range=self.maximum - self.minimum,
        )
        if "median" in names:
            values["median"] = float(np.median(np.concatenate(self.parts)))
        if any(not math.isfinite(float(values[k])) for k in names):
            raise ValueError(
                "统计发生数值溢出，请检查异常值或设置合理值范围。"
            )
        return {key: values[key] for key in names}


def _window(geom, gt, width, height):
    inv = gdal.InvGeoTransform(gt)
    if inv is None:
        raise ValueError("格点仿射变换不可逆。")
    xmin, xmax, ymin, ymax = geom.GetEnvelope()
    corners = [
        gdal.ApplyGeoTransform(inv, x, y)
        for x in (xmin, xmax)
        for y in (ymin, ymax)
    ]
    x0 = max(0, math.floor(min(p[0] for p in corners)))
    y0 = max(0, math.floor(min(p[1] for p in corners)))
    x1 = min(width, math.ceil(max(p[0] for p in corners)))
    y1 = min(height, math.ceil(max(p[1] for p in corners)))
    return x0, y0, max(x0, x1), max(y0, y1)


def _statistics(ds, geom, index, options, cancel, tick):
    gt = ds.GetGeoTransform()
    band = ds.GetRasterBand(index)
    if gdal.DataTypeIsComplex(band.DataType):
        raise ValueError("不支持复数格点。")
    nodata = band.GetNoDataValue()
    scale = band.GetScale()
    offset = band.GetOffset()
    scale, offset = (1.0 if scale is None else scale), (
        0.0 if offset is None else offset
    )
    acc = Accumulator("median" in options.statistics, options.median_limit)
    n_missing = n_bad = n_inside = 0
    x0, y0, x1, y1 = _window(geom, gt, ds.RasterXSize, ds.RasterYSize)
    mem = ogr.GetDriverByName("Memory").CreateDataSource("")
    layer = mem.CreateLayer(
        "zone", srs=_srs(ds.GetProjection()), geom_type=ogr.wkbUnknown
    )
    feat = ogr.Feature(layer.GetLayerDefn())
    feat.SetGeometry(geom)
    layer.CreateFeature(feat)
    block_count = max(
        1, math.ceil((x1 - x0) / 512) * math.ceil((y1 - y0) / 512)
    )
    done = 0
    for y in range(y0, y1, 512):
        for x in range(x0, x1, 512):
            _check_cancel(cancel)
            w, h = min(512, x1 - x), min(512, y1 - y)
            mask_ds = gdal.GetDriverByName("MEM").Create(
                "", w, h, 1, gdal.GDT_Byte
            )
            ox, oy = gdal.ApplyGeoTransform(gt, x, y)
            mask_ds.SetGeoTransform((ox, gt[1], gt[2], oy, gt[4], gt[5]))
            mask_ds.SetProjection(ds.GetProjection())
            layer.ResetReading()
            if (
                gdal.RasterizeLayer(
                    mask_ds,
                    [1],
                    layer,
                    burn_values=[1],
                    options=[
                        "ALL_TOUCHED="
                        + ("TRUE" if options.all_touched else "FALSE")
                    ],
                )
                != 0
            ):
                raise ValueError("面栅格化失败。")
            inside = mask_ds.ReadAsArray().astype(bool)
            n_inside += int(np.count_nonzero(inside))
            if np.any(inside):
                raw = band.ReadAsArray(x, y, w, h)
                if raw is None:
                    raise ValueError("格点读取失败。")
                valid_mask = band.GetMaskBand().ReadAsArray(x, y, w, h)
                missing = valid_mask == 0
                if nodata is not None:
                    missing |= (
                        np.isnan(raw) if math.isnan(nodata) else raw == nodata
                    )
                with np.errstate(over="ignore", invalid="ignore"):
                    decoded = raw.astype(np.float64) * scale + offset
                finite = np.isfinite(decoded)
                bad = ~finite & ~missing
                out_of_range = np.zeros(raw.shape, dtype=bool)
                if options.lower is not None:
                    out_of_range |= decoded < options.lower
                if options.upper is not None:
                    out_of_range |= decoded > options.upper
                bad |= out_of_range & ~missing
                n_missing += int(np.count_nonzero(inside & missing))
                n_bad += int(np.count_nonzero(inside & bad))
                valid = inside & ~missing & finite
                if options.exclude_bad:
                    valid &= ~out_of_range
                acc.add(decoded[valid])
            done += 1
            tick(done / block_count)
    return acc.result(options.statistics), acc.n, n_missing, n_bad, n_inside


class Exporter:
    """Stage in a sibling directory and publish without overwriting."""

    def __init__(self, path, fmt, names):
        self.path, self.fmt = Path(path), fmt
        self.fields = list(BASE_FIELDS) + list(names)
        self.temp = None
        self.ds = self.stream = None
        self.warnings = []
        self.preview = []
        self.rows = 0

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if (
            self.path.exists()
            or self.path.with_suffix(".warnings.json").exists()
        ):
            raise ValueError("输出或提醒文件已存在，请选择新文件名。")
        self.temp = tempfile.TemporaryDirectory(
            prefix=".meteo-", dir=str(self.path.parent)
        )
        self.staged = Path(self.temp.name) / self.path.name
        try:
            if self.fmt == "CSV":
                self.stream = self.staged.open(
                    "w", encoding="utf-8-sig", newline=""
                )
                self.writer = csv.DictWriter(
                    self.stream, fieldnames=self.fields + ["wkt"]
                )
                self.writer.writeheader()
            else:
                driver = ogr.GetDriverByName(self.fmt)
                if driver is None:
                    raise ValueError("GDAL 未安装导出驱动：" + self.fmt)
                self.ds = driver.CreateDataSource(str(self.staged))
                if self.ds is None:
                    raise ValueError("无法创建输出文件。")
                self.layer = self.ds.CreateLayer(
                    "statistics",
                    srs=_srs("EPSG:4326"),
                    geom_type=ogr.wkbMultiPolygon,
                    options=(
                        ["ENCODING=UTF-8"]
                        if self.fmt == "ESRI Shapefile"
                        else ["RFC7946=YES"]
                    ),
                )
                for name in self.fields:
                    kind = (
                        ogr.OFTInteger64
                        if name
                        in ("band", "count", "n_valid", "n_nodata", "n_bad")
                        else (
                            ogr.OFTReal
                            if name in STATISTICS
                            else ogr.OFTString
                        )
                    )
                    definition = ogr.FieldDefn(name, kind)
                    if kind == ogr.OFTString and self.fmt == "ESRI Shapefile":
                        definition.SetWidth(254)
                    if self.layer.CreateField(definition) != 0:
                        raise ValueError("创建字段失败：" + name)
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def warn(self, code, message, fid=None, band=None):
        self.warnings.append(
            dict(code=code, message=message, src_fid=fid, band=band)
        )

    def write(self, row, geom):
        if self.fmt == "CSV":
            self.writer.writerow(dict(row, wkt=geom.ExportToWkt()))
        else:
            feat = ogr.Feature(self.layer.GetLayerDefn())
            for key, value in row.items():
                if value is not None:
                    if (
                        self.fmt == "ESRI Shapefile"
                        and isinstance(value, str)
                        and len(value.encode("utf-8")) > 254
                    ):
                        raise ValueError(
                            "Shapefile 属性超过 254 字节，请改用 GeoJSON 或 CSV："
                            + key
                        )
                    feat.SetField(key, value)
            feat.SetGeometry(ogr.ForceToMultiPolygon(geom.Clone()))
            if self.layer.CreateFeature(feat) != 0:
                raise ValueError("写入统计结果失败。")
        self.rows += 1
        if len(self.preview) < 500:
            self.preview.append(row)

    def commit(self, cancel):
        self._close()
        warnings_path = self.staged.with_suffix(".warnings.json")
        warnings_path.write_text(
            json.dumps(self.warnings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        linked = []
        try:
            for source in sorted(Path(self.temp.name).iterdir()):
                _check_cancel(cancel)
                dest = self.path.parent / source.name
                # Atomic no-replace; rollback only our newly created files.
                os.link(str(source), str(dest))
                linked.append(dest)
        except Exception:
            for dest in linked:
                dest.unlink()
            raise
        return dict(
            output=str(self.path),
            rows=self.rows,
            preview=self.preview,
            warnings=self.warnings,
            warnings_file=str(self.path.with_suffix(".warnings.json")),
        )

    def _close(self):
        self.layer = None
        self.ds = None
        if self.stream:
            self.stream.close()
            self.stream = None

    def __exit__(self, *args):
        self._close()
        if self.temp:
            self.temp.cleanup()


def run(options, progress=lambda value: None, cancel=lambda: False):
    if options.format not in ("GeoJSON", "ESRI Shapefile", "CSV"):
        raise ValueError("不支持的输出格式。")
    suffix = {"GeoJSON": ".geojson", "ESRI Shapefile": ".shp", "CSV": ".csv"}[
        options.format
    ]
    if Path(options.output).suffix.lower() != suffix:
        raise ValueError("输出扩展名必须是 " + suffix)
    if not options.statistics or set(options.statistics) - set(STATISTICS):
        raise ValueError("请选择有效统计指标。")
    if len(set(options.statistics)) != len(options.statistics):
        raise ValueError("统计指标不能重复。")
    for bound in (options.lower, options.upper):
        if bound is not None and not math.isfinite(bound):
            raise ValueError("合理值范围必须为有限数。")
    if (
        options.lower is not None
        and options.upper is not None
        and options.lower > options.upper
    ):
        raise ValueError("合理值范围下限不能大于上限。")
    ds, location_warnings = prepare_raster(
        options.raster, _open_raster(options.raster)
    )
    if ds.GetGeoTransform(can_return_null=True) is None or ds.GetMetadata(
        "GEOLOCATION"
    ):
        raise ValueError(
            "仅支持具有仿射定位的规则格点，请先用 GDAL/QGIS 将曲线网格转换为规则栅格。"
        )
    raster_srs = _srs(ds.GetProjection())
    info = band_info(ds)
    bands = options.bands or list(range(1, ds.RasterCount + 1))
    if not bands or any(
        not isinstance(b, int) or b < 1 or b > ds.RasterCount for b in bands
    ):
        raise ValueError("时间/波段选择无效。")
    vector = _open_vector(options.vector)
    layer = vector.GetLayerByName(options.layer)
    if layer is None:
        raise ValueError("找不到所选面图层。")
    if (
        options.id_field
        and layer.GetLayerDefn().GetFieldIndex(options.id_field) < 0
    ):
        raise ValueError("找不到标识字段。")
    source_srs = layer.GetSpatialRef()
    if source_srs is None:
        raise ValueError("矢量数据缺少坐标系，请先指定正确坐标系并另存。")
    source_srs = _srs(source_srs.ExportToWkt())
    to_grid = osr.CoordinateTransformation(source_srs, raster_srs)
    to_wgs = osr.CoordinateTransformation(source_srs, _srs("EPSG:4326"))
    count = layer.GetFeatureCount()
    if not count:
        raise ValueError("面图层为空。")
    count = max(1, count)
    with Exporter(options.output, options.format, options.statistics) as out:
        for code, message in location_warnings:
            out.warn(code, message)
        for b in bands:
            if info[b - 1]["warning"]:
                out.warn("TIME_METADATA", info[b - 1]["warning"], band=b)
        for index, feature in enumerate(layer):
            _check_cancel(cancel)
            fid = str(feature.GetFID())
            geom = feature.GetGeometryRef()
            if (
                geom is None
                or geom.IsEmpty()
                or ogr.GT_Flatten(geom.GetGeometryType())
                not in (ogr.wkbPolygon, ogr.wkbMultiPolygon)
                or not geom.IsValid()
            ):
                out.warn("INVALID_GEOMETRY", "空、非面或无效几何已跳过。", fid)
                continue
            grid_geom, wgs_geom = geom.Clone(), geom.Clone()
            if (
                grid_geom.Transform(to_grid) != 0
                or wgs_geom.Transform(to_wgs) != 0
            ):
                raise ValueError("要素 {} 坐标转换失败。".format(fid))
            envelope = wgs_geom.GetEnvelope()
            if (
                not all(math.isfinite(x) for x in envelope)
                or envelope[0] < -180.000001
                or envelope[1] > 180.000001
                or envelope[2] < -90.000001
                or envelope[3] > 90.000001
                or envelope[1] - envelope[0] > 180
            ):
                raise ValueError(
                    "要素 {} 跨日期变更线或超出经纬度范围，请先分割/规范化。".format(
                        fid
                    )
                )
            # Shift ordinary negative longitudes onto a global 0..360 grid.
            gt = ds.GetGeoTransform()
            grid_edges = [
                gdal.ApplyGeoTransform(gt, x, y)[0]
                for x in (0, ds.RasterXSize)
                for y in (0, ds.RasterYSize)
            ]
            if (
                raster_srs.IsGeographic()
                and min(grid_edges) >= 0
                and max(grid_edges) > 180
            ):
                gx0, gx1, _, _ = grid_geom.GetEnvelope()
                if gx0 < 0 < gx1:
                    raise ValueError(
                        "要素 {} 跨 0/360 格点接缝，请先分割该面。".format(fid)
                    )
                if gx1 <= 0:

                    def shift(g):
                        if g.GetGeometryCount():
                            for child in range(g.GetGeometryCount()):
                                shift(g.GetGeometryRef(child))
                        else:
                            for point in range(g.GetPointCount()):
                                x, y, z = g.GetPoint(point)
                                g.SetPoint(point, x + 360, y, z)

                    shift(grid_geom)
            for j, b in enumerate(bands):
                _check_cancel(cancel)

                def tick(fraction):
                    progress(
                        min(
                            99.0,
                            100
                            * (index + (j + fraction) / len(bands))
                            / count,
                        )
                    )

                stats, valid, missing, bad, inside = _statistics(
                    ds, grid_geom, b, options, cancel, tick
                )
                if not inside:
                    out.warn(
                        "NO_PIXELS",
                        "面内没有像元；可能不重叠或面小于像元。",
                        fid,
                        b,
                    )
                elif not valid:
                    out.warn(
                        "NO_VALID_DATA",
                        "面内没有有效数据，统计量为空。",
                        fid,
                        b,
                    )
                if bad:
                    out.warn(
                        "ABNORMAL_VALUES",
                        "发现 {} 个非有限或范围外值。".format(bad),
                        fid,
                        b,
                    )
                row = {
                    k: info[b - 1][k]
                    for k in ("band", "variable", "time", "dims", "unit")
                }
                row.update(
                    src_fid=fid,
                    zone_id=(
                        str(feature.GetField(options.id_field))
                        if options.id_field
                        else fid
                    ),
                    n_valid=valid,
                    n_nodata=missing,
                    n_bad=bad,
                )
                row.update(stats)
                out.write(row, wgs_geom)
        _check_cancel(cancel)
        result = out.commit(cancel)
    progress(100)
    return result
