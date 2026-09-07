# Meteo Grid Statistics

QGIS 3 plugin for time-aware polygon statistics on meteorological NetCDF, GRIB
and GeoTIFF grids. The interface is Chinese; this English guide and the in-app
**使用帮助 / Help** button describe the complete workflow.

## Install and run

In QGIS, choose Plugins → Manage and Install Plugins → Install from ZIP.
Install the release ZIP and enable **Meteo Grid Statistics**. Open Raster →
气象格点分区统计 or use the navy sun/grid/statistical-ruler toolbar icon.

Select a raster, its variable/subdataset and the bands to process. Select a
polygon file (GeoJSON/JSON, Shapefile, KML or another OGR source), its layer and
an identifier field. Choose statistics, optional reasonable-value bounds,
output format and a new output path, then click **开始统计**. Input files are
read-only. Existing outputs are not overwritten. A running task is cancellable.

## Outputs and calculation

Each valid polygon and selected band produces a row. Fields include source FID,
zone identifier, band, variable, time, other dimension metadata, unit, valid,
missing and abnormal counts, and selected statistics. GeoJSON and Shapefile
outputs are added to the project. Output geometry is EPSG:4326; CSV contains WKT.
Warnings are saved to a companion `.warnings.json`. UI preview is limited to
500 rows/warnings; exported results are complete.

Statistics use cell centers by default; the all-touched option includes cells
touching polygon boundaries. Holes are excluded, overlapping polygons are
independent, and results are not area weighted. Standard deviation and variance
are population statistics. Scale/offset are applied, NoData/masked/NaN/Inf values
are excluded. User bounds apply to decoded values. Invalid polygons and lines
are skipped with warnings. Empty zones have count zero and null statistics.

Regular 1-D NetCDF geographic coordinates can recover missing georeferencing.
Coordinates are treated as cell centers and a missing CRS is assumed WGS84,
explicitly reported in the UI and warning file. Original arrays are not edited.
Time and vertical dimensions are retained; non-Gregorian time is preserved as
raw values with units/calendar instead of inventing Gregorian dates.

## Requirements and limitations

- QGIS 3.22–3.99, using QGIS Python, GDAL/OGR and NumPy. NetCDF recovery needs
  GDAL 3.1+ multidimensional APIs. No additional runtime pip install, web account,
  network service or bundled executable is required. Driver availability depends
  on the QGIS distribution. Repair QGIS if bundled dependencies are missing.
- Tested locally on macOS QGIS 3.40.5 / GDAL 3.3.2. Other QGIS 3 versions and
  Windows/Linux still need maintainer validation. QGIS 4 is not advertised.
- Regular affine grids only; recovered geographic axes must be the last two
  dimensions, monotonic and equally spaced. Curvilinear grids are unsupported.
- Exact median is limited to 5 million valid cells per polygon; disable it for
  larger zones. Other statistics use blockwise accumulation. Warning storage
  grows with feature and band counts, so divide very large jobs into batches.
- Split date-line or 0/360-seam polygons before processing. Non-NetCDF sources
  without CRS must be assigned the correct CRS before use.
- Recovered map rasters are temporary GeoTIFFs; save them to a permanent path
  before sharing or persisting a project.
- Shapefile strings over 254 UTF-8 bytes are rejected rather than truncated.
  CSV WKT may be large. Import untrusted CSV string attributes as text.

## Reproducible example

Use `samples/grid.asc` and `samples/zones.geojson`, choose the `name` field and
all statistics. The `full` polygon has count 16, min 1, max 16, mean/median 8.5,
sum 136 and population variance 21.25. The `outside` polygon has count 0 and a
`NO_PIXELS` warning. See `samples/README.md`; all sample files are text.

## License and support

Source, synthetic sample data and the original SVG icon are licensed under
GPL-3.0-or-later; see `LICENSE`. GDAL, NumPy and QGIS are used from the host
installation, not redistributed. The plugin performs no network requests.

Public releases must provide a genuine author, contactable email, public source
repository, usage homepage and issue tracker in `metadata.txt`. `UNSET` marks
local builds only; formal packaging refuses missing publishing information.
Report issues through the metadata tracker once configured, including version,
OS, QGIS/GDAL versions, a small reproducible input and the warning/error text.
