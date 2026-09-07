# SPDX-License-Identifier: GPL-3.0-or-later
"""Recover regular NetCDF coordinates without editing source files."""
import re

import numpy as np
from osgeo import gdal, osr


def regular_axis(values, name):
    original = np.asarray(values)
    values = original.astype(np.float64)
    if values.ndim != 1 or values.size < 2 or not np.all(np.isfinite(values)):
        raise ValueError(name + " 必须是一维、至少两个点的有限坐标数组。")
    steps = np.diff(values)
    if not (np.all(steps > 0) or np.all(steps < 0)):
        raise ValueError(name + " 坐标不严格单调，不能恢复规则格点。")
    step = float((values[-1] - values[0]) / (values.size - 1))
    precision = (
        np.finfo(original.dtype).eps
        if original.dtype.kind == "f"
        else np.finfo(float).eps
    )
    tolerance = max(
        abs(step) * 1e-5, 4 * precision * max(1, float(np.max(np.abs(values))))
    )
    if tolerance > abs(step) * 0.05:
        raise ValueError(name + " 坐标精度不足以确定规则间隔。")
    expected = values[0] + np.arange(values.size) * step
    if np.max(np.abs(values - expected)) > tolerance:
        raise ValueError(name + " 坐标不等间隔，不支持按仿射规则格点定位。")
    bounds = (-90, 90) if name == "latitude" else (-180, 360)
    if (
        values.min() < bounds[0]
        or values.max() > bounds[1]
        or (name == "longitude" and abs(values[-1] - values[0]) > 360)
    ):
        raise ValueError(name + " 超出经纬度范围。")
    return float(values[0] - step / 2), step


def prepare_raster(path, original=None):
    """Return a dataset and assumptions; retain MD sources on the view."""
    original = original or gdal.OpenEx(
        str(path), gdal.OF_RASTER | gdal.OF_READONLY
    )
    if original is None:
        raise ValueError("无法打开格点数据：" + str(path))
    if (
        original.GetGeoTransform(can_return_null=True) is not None
        and original.GetProjection()
    ):
        return original, []
    if original.GetDriver().ShortName != "netCDF":
        return original, []
    match = re.match(r'^NETCDF:"(.*)":(.+)$', str(path), re.I)
    filename = match[1] if match else str(path)
    variable = (
        match[2]
        if match
        else original.GetRasterBand(1).GetMetadataItem("NETCDF_VARNAME")
    )
    if not variable:
        raise ValueError("无法确定需要恢复定位的 NetCDF 变量。")
    multidim = gdal.OpenEx(
        filename, gdal.OF_MULTIDIM_RASTER | gdal.OF_READONLY
    )
    if multidim is None:
        raise ValueError("当前 GDAL 无法读取 NetCDF 经纬度数组。")
    group = multidim.GetRootGroup()
    components = variable.strip("/").split("/")
    for part in components[:-1]:
        group = group.OpenGroup(part)
        if group is None:
            raise ValueError("找不到 NetCDF 变量所在分组。")
    array = group.OpenMDArray(components[-1])
    if array is None:
        raise ValueError("找不到 NetCDF 数据变量：" + variable)
    axes = {}
    for index, dimension in enumerate(array.GetDimensions()):
        coordinate = dimension.GetIndexingVariable()
        if coordinate is None:
            continue
        attrs = {
            a.GetName(): a.ReadAsString() for a in coordinate.GetAttributes()
        }
        name = coordinate.GetName().lower()
        standard = attrs.get("standard_name", "").lower()
        units = (coordinate.GetUnit() or attrs.get("units", "")).lower()
        kind = (
            "latitude"
            if name in ("lat", "latitude")
            or standard == "latitude"
            or units in ("degrees_north", "degree_north")
            else (
                "longitude"
                if name in ("lon", "longitude")
                or standard == "longitude"
                or units in ("degrees_east", "degree_east")
                else None
            )
        )
        if kind is None:
            continue
        accepted_units = (
            ("", "degrees", "degree", "degrees_north", "degree_north")
            if kind == "latitude"
            else ("", "degrees", "degree", "degrees_east", "degree_east")
        )
        if units not in accepted_units or standard not in ("", kind):
            raise ValueError(
                "经纬度单位或标准名称不兼容，不能推定 WGS84：" + name
            )
        if kind in axes:
            raise ValueError("经纬度维度不唯一。")
        dimensions = coordinate.GetDimensions()
        if (
            len(dimensions) != 1
            or dimensions[0].GetFullName() != dimension.GetFullName()
        ):
            raise ValueError("仅支持与数据维度对应的一维经纬度数组。")
        values = coordinate.ReadAsArray()
        edge, step = regular_axis(values, kind)
        axes[kind] = (index, edge, step)
    if set(axes) != {"latitude", "longitude"}:
        raise ValueError("缺少可识别的一维 lat/lon 坐标数组，无法恢复定位。")
    xi, xedge, dx = axes["longitude"]
    yi, yedge, dy = axes["latitude"]
    # Limit to the ordinary (..., lat, lon) / (..., lon, lat) layout so band
    # metadata stays aligned with the driver's non-spatial dimensions.
    if {xi, yi} != {
        len(array.GetDimensions()) - 2,
        len(array.GetDimensions()) - 1,
    }:
        raise ValueError("经纬度必须是 NetCDF 最后两个维度，请先转置数据。")
    classic = array.AsClassicDataset(xi, yi)
    if classic is None or classic.RasterCount != original.RasterCount:
        raise ValueError("恢复定位后的波段与原始波段不一致。")
    view = gdal.Translate("", classic, format="VRT")
    if view is None:
        raise ValueError("无法创建定位视图。")
    view.SetGeoTransform((xedge, dx, 0, yedge, 0, dy))
    messages = [
        (
            "GEOLOCATION_RECOVERED",
            "按一维经纬度数组恢复定位；坐标视为像元中心，保留原数组行列方向。",
        )
    ]
    projection = original.GetProjection()
    if projection:
        srs = osr.SpatialReference()
        srs.ImportFromWkt(projection)
        if not srs.IsGeographic():
            raise ValueError("已有投影坐标系与经纬度数组冲突，不能自动定位。")
    else:
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        projection = srs.ExportToWkt()
        messages.append(
            (
                "CRS_ASSUMED_WGS84",
                "原数据缺少 CRS；根据经纬度数组推定 WGS84（EPSG:4326），请以数据提供方说明为准。",
            )
        )
    view.SetProjection(projection)
    view.SetMetadata(original.GetMetadata())
    for i in range(1, view.RasterCount + 1):
        src, dst = original.GetRasterBand(i), view.GetRasterBand(i)
        dst.SetMetadata(src.GetMetadata())
        dst.SetDescription(src.GetDescription())
        dst.SetUnitType(src.GetUnitType())
        for getter, setter in (
            ("GetNoDataValue", "SetNoDataValue"),
            ("GetScale", "SetScale"),
            ("GetOffset", "SetOffset"),
        ):
            value = getattr(src, getter)()
            if value is not None:
                getattr(dst, setter)(value)
    # VRT has a live anonymous MD source, not a serializable filename. Never
    # persist this VRT: materialize a GeoTIFF when adding it to the QGIS map.
    view._source_owners = (multidim, group, array, classic, original)
    return view, messages
