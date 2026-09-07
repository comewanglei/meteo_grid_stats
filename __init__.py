# SPDX-License-Identifier: GPL-3.0-or-later
"""QGIS plugin entry point."""


def classFactory(iface):
    from .plugin import MeteoGridStatsPlugin

    return MeteoGridStatsPlugin(iface)
