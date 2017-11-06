#!/usr/bin/env python
# -*- coding: utf-8 -*-
# ******************************************************************************
#  $Id$
#
# Project:  Google Summer of Code 2007, 2008 (http://code.google.com/soc/)
# Support:  BRGM (http://www.brgm.fr)
# Purpose:  Convert a raster into TMS (Tile Map Service) tiles in a directory.
#           - generate Google Earth metadata (KML SuperOverlay)
#           - generate simple HTML viewer based on Google Maps and OpenLayers
#           - support of global tiles (Spherical Mercator) for compatibility
#               with interactive web maps a la Google Maps
# Author:   Klokan Petr Pridal, klokan at klokan dot cz
# Web:      http://www.klokan.cz/projects/GDAL2Mbtiles/
# GUI:      http://www.maptiler.org/
#
###############################################################################
# Copyright (c) 2008, Klokan Petr Pridal
#
#  Permission is hereby granted, free of charge, to any person obtaining a
#  copy of this software and associated documentation files (the "Software"),
#  to deal in the Software without restriction, including without limitation
#  the rights to use, copy, modify, merge, publish, distribute, sublicense,
#  and/or sell copies of the Software, and to permit persons to whom the
#  Software is furnished to do so, subject to the following conditions:
#
#  The above copyright notice and this permission notice shall be included
#  in all copies or substantial portions of the Software.
#
#  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
#  OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
#  THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
#  FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
#  DEALINGS IN THE SOFTWARE.
# ******************************************************************************
import signal, sys
import time
import io
import os
import json
from PyQt4.QtCore import pyqtSlot
from PyQt4 import QtCore

if getattr(sys, 'frozen', False):
    app_path = os.path.dirname(sys.executable)
    lib_dir = os.path.join(os.path.dirname(sys.executable), 'lib').decode('cp1251').encode('utf-8')
elif __file__:
    app_path = os.path.dirname(__file__)
    lib_dir = os.path.normpath(os.path.join(os.path.abspath(os.path.dirname(__file__)), '..', 'lib'))

# Uncomment to use local GDAL
environ_list = os.environ['PATH'].split(';')
environ_list.insert(0, os.path.join(lib_dir, 'GDAL'))
environ_list.insert(0, os.path.join(os.getcwd(), 'lib/Python27/Scripts'))
environ_list.insert(0, os.path.join(os.getcwd(), 'lib/Python27'))
os.environ['PATH'] = ';'.join(environ_list)
os.environ['GDAL_DRIVER_PATH'] = os.path.join(lib_dir, '/GDAL', 'gdalplugins')
os.environ['GDAL_DATA'] = os.path.join(lib_dir, 'GDAL', 'gdal-data')
os.environ['PROJ_LIB'] = os.path.join(lib_dir, 'GDAL', 'projlib')
os.environ['PROJ_DEBUG'] = 'ON'

try:
    from osgeo import gdal
    from osgeo import osr
except:
    import gdal

    print('You are using "old gen" bindings. GDAL2Mbtiles needs "new gen" bindings.')
    sys.exit(1)

import sqlite3
import math

try:
    from PIL import Image
    import numpy
    import osgeo.gdal_array as gdalarray
except:
    # 'antialias' resampling is not available
    pass

import multiprocessing
import traceback
import tempfile
from optparse import OptionParser, OptionGroup

__version__ = "$Id$"

resampling_list = ('average', 'near', 'bilinear', 'cubic', 'cubicspline', 'lanczos', 'antialias')
profile_list = ('mercator', 'geodetic', 'raster')  # ,'zoomify')
webviewer_list = ('all', 'google', 'openlayers', 'leaflet', 'index', 'metadata', 'none')
tcount = 0
# =============================================================================
# =============================================================================
# =============================================================================

__doc__globalmaptiles = """
globalmaptiles.py

Global Map Tiles as defined in Tile Map Service (TMS) Profiles
==============================================================

Functions necessary for generation of global tiles used on the web.
It contains classes implementing coordinate conversions for:

  - GlobalMercator (based on EPSG:900913 = EPSG:3785)
       for Google Maps, Yahoo Maps, Microsoft Maps compatible tiles
  - GlobalGeodetic (based on EPSG:4326)
       for OpenLayers Base Map and Google Earth compatible tiles

More info at:

http://wiki.osgeo.org/wiki/Tile_Map_Service_Specification
http://wiki.osgeo.org/wiki/WMS_Tiling_Client_Recommendation
http://msdn.microsoft.com/en-us/library/bb259689.aspx
http://code.google.com/apis/maps/documentation/overlays.html#Google_Maps_Coordinates

Created by Klokan Petr Pridal on 2008-07-03.
Google Summer of Code 2008, project GDAL2Mbtiles for OSGEO.

In case you use this class in your product, translate it to another language
or find it usefull for your project please let me know.
My email: klokan at klokan dot cz.
I would like to know where it was used.

Class is available under the open-source GDAL license (www.gdal.org).
"""

MAXZOOMLEVEL = 32


class GlobalMercator(object):
    """
    TMS Global Mercator Profile
    ---------------------------

    Functions necessary for generation of tiles in Spherical Mercator projection,
    EPSG:900913 (EPSG:gOOglE, Google Maps Global Mercator), EPSG:3785, OSGEO:41001.

    Such tiles are compatible with Google Maps, Microsoft Virtual Earth, Yahoo Maps,
    UK Ordnance Survey OpenSpace API, ...
    and you can overlay them on top of base maps of those web mapping applications.

    Pixel and tile coordinates are in TMS notation (origin [0,0] in bottom-left).

    What coordinate conversions do we need for TMS Global Mercator tiles::

         LatLon      <->       Meters      <->     Pixels    <->       Tile

     WGS84 coordinates   Spherical Mercator  Pixels in pyramid  Tiles in pyramid
         lat/lon            XY in metres     XY pixels Z zoom      XYZ from TMS
        EPSG:4326           EPSG:900913
         .----.              ---------               --                TMS
        /      \     <->     |       |     <->     /----/    <->      Google
        \      /             |       |           /--------/          QuadTree
         -----               ---------         /------------/
       KML, public         WebMapService         Web Clients      TileMapService

    What is the coordinate extent of Earth in EPSG:900913?

      [-20037508.342789244, -20037508.342789244, 20037508.342789244, 20037508.342789244]
      Constant 20037508.342789244 comes from the circumference of the Earth in meters,
      which is 40 thousand kilometers, the coordinate origin is in the middle of extent.
      In fact you can calculate the constant as: 2 * math.pi * 6378137 / 2.0
      $ echo 180 85 | gdaltransform -s_srs EPSG:4326 -t_srs EPSG:900913
      Polar areas with abs(latitude) bigger then 85.05112878 are clipped off.

    What are zoom level constants (pixels/meter) for pyramid with EPSG:900913?

      whole region is on top of pyramid (zoom=0) covered by 256x256 pixels tile,
      every lower zoom level resolution is always divided by two
      initialResolution = 20037508.342789244 * 2 / 256 = 156543.03392804062

    What is the difference between TMS and Google Maps/QuadTree tile name convention?

      The tile raster itself is the same (equal extent, projection, pixel size),
      there is just different identification of the same raster tile.
      Tiles in TMS are counted from [0,0] in the bottom-left corner, id is XYZ.
      Google placed the origin [0,0] to the top-left corner, reference is XYZ.
      Microsoft is referencing tiles by a QuadTree name, defined on the website:
      http://msdn2.microsoft.com/en-us/library/bb259689.aspx

    The lat/lon coordinates are using WGS84 datum, yeh?

      Yes, all lat/lon we are mentioning should use WGS84 Geodetic Datum.
      Well, the web clients like Google Maps are projecting those coordinates by
      Spherical Mercator, so in fact lat/lon coordinates on sphere are treated as if
      the were on the WGS84 ellipsoid.

      From MSDN documentation:
      To simplify the calculations, we use the spherical form of projection, not
      the ellipsoidal form. Since the projection is used only for map display,
      and not for displaying numeric coordinates, we don't need the extra precision
      of an ellipsoidal projection. The spherical projection causes approximately
      0.33 percent scale distortion in the Y direction, which is not visually noticable.

    How do I create a raster in EPSG:900913 and convert coordinates with PROJ.4?

      You can use standard GIS tools like gdalwarp, cs2cs or gdaltransform.
      All of the tools supports -t_srs 'epsg:900913'.

      For other GIS programs check the exact definition of the projection:
      More info at http://spatialreference.org/ref/user/google-projection/
      The same projection is degined as EPSG:3785. WKT definition is in the official
      EPSG database.

      Proj4 Text:
        +proj=merc +a=6378137 +b=6378137 +lat_ts=0.0 +lon_0=0.0 +x_0=0.0 +y_0=0
        +k=1.0 +units=m +nadgrids=@null +no_defs

      Human readable WKT format of EPGS:900913:
         PROJCS["Google Maps Global Mercator",
             GEOGCS["WGS 84",
                 DATUM["WGS_1984",
                     SPHEROID["WGS 84",6378137,298.257223563,
                         AUTHORITY["EPSG","7030"]],
                     AUTHORITY["EPSG","6326"]],
                 PRIMEM["Greenwich",0],
                 UNIT["degree",0.0174532925199433],
                 AUTHORITY["EPSG","4326"]],
             PROJECTION["Mercator_1SP"],
             PARAMETER["central_meridian",0],
             PARAMETER["scale_factor",1],
             PARAMETER["false_easting",0],
             PARAMETER["false_northing",0],
             UNIT["metre",1,
                 AUTHORITY["EPSG","9001"]]]
    """

    def __init__(self, tileSize=256):
        "Initialize the TMS Global Mercator pyramid"
        self.tileSize = tileSize
        self.initialResolution = 2 * math.pi * 6378137 / self.tileSize
        # 156543.03392804062 for tileSize 256 pixels
        self.originShift = 2 * math.pi * 6378137 / 2.0

    # 20037508.342789244

    def LatLonToMeters(self, lat, lon):
        "Converts given lat/lon in WGS84 Datum to XY in Spherical Mercator EPSG:900913"

        mx = lon * self.originShift / 180.0
        my = math.log(math.tan((90 + lat) * math.pi / 360.0)) / (math.pi / 180.0)

        my = my * self.originShift / 180.0
        return mx, my

    def MetersToLatLon(self, mx, my):
        "Converts XY point from Spherical Mercator EPSG:900913 to lat/lon in WGS84 Datum"

        lon = (mx / self.originShift) * 180.0
        lat = (my / self.originShift) * 180.0

        lat = 180 / math.pi * (2 * math.atan(math.exp(lat * math.pi / 180.0)) - math.pi / 2.0)
        return lat, lon

    def PixelsToMeters(self, px, py, zoom):
        "Converts pixel coordinates in given zoom level of pyramid to EPSG:900913"

        res = self.Resolution(zoom)
        mx = px * res - self.originShift
        my = py * res - self.originShift
        return mx, my

    def MetersToPixels(self, mx, my, zoom):
        "Converts EPSG:900913 to pyramid pixel coordinates in given zoom level"

        res = self.Resolution(zoom)
        px = (mx + self.originShift) / res
        py = (my + self.originShift) / res
        return px, py

    def PixelsToTile(self, px, py):
        "Returns a tile covering region in given pixel coordinates"

        tx = int(math.ceil(px / float(self.tileSize)) - 1)
        ty = int(math.ceil(py / float(self.tileSize)) - 1)
        return tx, ty

    def PixelsToRaster(self, px, py, zoom):
        "Move the origin of pixel coordinates to top-left corner"

        mapSize = self.tileSize << zoom
        return px, mapSize - py

    def MetersToTile(self, mx, my, zoom):
        "Returns tile for given mercator coordinates"

        px, py = self.MetersToPixels(mx, my, zoom)
        return self.PixelsToTile(px, py)

    def TileBounds(self, tx, ty, zoom):
        "Returns bounds of the given tile in EPSG:900913 coordinates"

        minx, miny = self.PixelsToMeters(tx * self.tileSize, ty * self.tileSize, zoom)
        maxx, maxy = self.PixelsToMeters((tx + 1) * self.tileSize, (ty + 1) * self.tileSize, zoom)
        return (minx, miny, maxx, maxy)

    def TileLatLonBounds(self, tx, ty, zoom):
        "Returns bounds of the given tile in latutude/longitude using WGS84 datum"

        bounds = self.TileBounds(tx, ty, zoom)
        minLat, minLon = self.MetersToLatLon(bounds[0], bounds[1])
        maxLat, maxLon = self.MetersToLatLon(bounds[2], bounds[3])

        return (minLat, minLon, maxLat, maxLon)

    def Resolution(self, zoom):
        "Resolution (meters/pixel) for given zoom level (measured at Equator)"

        # return (2 * math.pi * 6378137) / (self.tileSize * 2**zoom)
        return self.initialResolution / (2 ** zoom)

    def ZoomForPixelSize(self, pixelSize):
        "Maximal scaledown zoom of the pyramid closest to the pixelSize."

        for i in range(MAXZOOMLEVEL):
            if pixelSize > self.Resolution(i):
                if i != 0:
                    return i - 1
                else:
                    return 0  # We don't want to scale up

    def GoogleTile(self, tx, ty, zoom):
        "Converts TMS tile coordinates to Google Tile coordinates"

        # coordinate origin is moved from bottom-left to top-left corner of the extent
        return tx, (2 ** zoom - 1) - ty

    def QuadTree(self, tx, ty, zoom):
        "Converts TMS tile coordinates to Microsoft QuadTree"

        quadKey = ""
        ty = (2 ** zoom - 1) - ty
        for i in range(zoom, 0, -1):
            digit = 0
            mask = 1 << (i - 1)
            if (tx & mask) != 0:
                digit += 1
            if (ty & mask) != 0:
                digit += 2
            quadKey += str(digit)

        return quadKey


# ---------------------

class GlobalGeodetic(object):
    """
    TMS Global Geodetic Profile
    ---------------------------

    Functions necessary for generation of global tiles in Plate Carre projection,
    EPSG:4326, "unprojected profile".

    Such tiles are compatible with Google Earth (as any other EPSG:4326 rasters)
    and you can overlay the tiles on top of OpenLayers base map.

    Pixel and tile coordinates are in TMS notation (origin [0,0] in bottom-left).

    What coordinate conversions do we need for TMS Global Geodetic tiles?

      Global Geodetic tiles are using geodetic coordinates (latitude,longitude)
      directly as planar coordinates XY (it is also called Unprojected or Plate
      Carre). We need only scaling to pixel pyramid and cutting to tiles.
      Pyramid has on top level two tiles, so it is not square but rectangle.
      Area [-180,-90,180,90] is scaled to 512x256 pixels.
      TMS has coordinate origin (for pixels and tiles) in bottom-left corner.
      Rasters are in EPSG:4326 and therefore are compatible with Google Earth.

         LatLon      <->      Pixels      <->     Tiles

     WGS84 coordinates   Pixels in pyramid  Tiles in pyramid
         lat/lon         XY pixels Z zoom      XYZ from TMS
        EPSG:4326
         .----.                ----
        /      \     <->    /--------/    <->      TMS
        \      /         /--------------/
         -----        /--------------------/
       WMS, KML    Web Clients, Google Earth  TileMapService
    """

    def __init__(self, tileSize=256):
        self.tileSize = tileSize

    def LatLonToPixels(self, lat, lon, zoom):
        "Converts lat/lon to pixel coordinates in given zoom of the EPSG:4326 pyramid"

        res = 180.0 / self.tileSize / 2 ** zoom
        px = (180 + lat) / res
        py = (90 + lon) / res
        return px, py

    def PixelsToTile(self, px, py):
        "Returns coordinates of the tile covering region in pixel coordinates"

        tx = int(math.ceil(px / float(self.tileSize)) - 1)
        ty = int(math.ceil(py / float(self.tileSize)) - 1)
        return tx, ty

    def LatLonToTile(self, lat, lon, zoom):
        "Returns the tile for zoom which covers given lat/lon coordinates"

        px, py = self.LatLonToPixels(lat, lon, zoom)
        return self.PixelsToTile(px, py)

    def Resolution(self, zoom):
        "Resolution (arc/pixel) for given zoom level (measured at Equator)"

        return 180.0 / self.tileSize / 2 ** zoom

    # return 180 / float( 1 << (8+zoom) )

    def ZoomForPixelSize(self, pixelSize):
        "Maximal scaledown zoom of the pyramid closest to the pixelSize."

        for i in range(MAXZOOMLEVEL):
            if pixelSize > self.Resolution(i):
                if i != 0:
                    return i - 1
                else:
                    return 0  # We don't want to scale up

    def TileBounds(self, tx, ty, zoom):
        "Returns bounds of the given tile"
        res = 180.0 / self.tileSize / 2 ** zoom
        return (
            tx * self.tileSize * res - 180,
            ty * self.tileSize * res - 90,
            (tx + 1) * self.tileSize * res - 180,
            (ty + 1) * self.tileSize * res - 90
        )

    def TileLatLonBounds(self, tx, ty, zoom):
        "Returns bounds of the given tile in the SWNE form"
        b = self.TileBounds(tx, ty, zoom)
        return (b[1], b[0], b[3], b[2])


# ---------------------
# TODO: Finish Zoomify implemtentation!!!
class Zoomify(object):
    """
    Tiles compatible with the Zoomify viewer
    ----------------------------------------
    """

    def __init__(self, width, height, tilesize=256, tileformat='jpg'):
        """Initialization of the Zoomify tile tree"""

        self.tilesize = tilesize
        self.tileformat = tileformat
        imagesize = (width, height)
        tiles = (math.ceil(width / tilesize), math.ceil(height / tilesize))

        # Size (in tiles) for each tier of pyramid.
        self.tierSizeInTiles = []
        self.tierSizeInTiles.push(tiles)

        # Image size in pixels for each pyramid tierself
        self.tierImageSize = []
        self.tierImageSize.append(imagesize);

        while (imagesize[0] > tilesize or imagesize[1] > tilesize):
            imagesize = (math.floor(imagesize[0] / 2), math.floor(imagesize[1] / 2))
            tiles = (math.ceil(imagesize[0] / tilesize), math.ceil(imagesize[1] / tilesize))
            self.tierSizeInTiles.append(tiles)
            self.tierImageSize.append(imagesize)

        self.tierSizeInTiles.reverse()
        self.tierImageSize.reverse()

        # Depth of the Zoomify pyramid, number of tiers (zoom levels)
        self.numberOfTiers = len(self.tierSizeInTiles)

        # Number of tiles up to the given tier of pyramid.
        self.tileCountUpToTier = []
        self.tileCountUpToTier[0] = 0
        for i in range(1, self.numberOfTiers + 1):
            self.tileCountUpToTier.append(
                self.tierSizeInTiles[i - 1][0] * self.tierSizeInTiles[i - 1][1] + self.tileCountUpToTier[i - 1]
            )

    def tilefilename(self, x, y, z):
        """Returns filename for tile with given coordinates"""

        tileIndex = x + y * self.tierSizeInTiles[z][0] + self.tileCountUpToTier[z]
        return os.path.join("TileGroup%.0f" % math.floor(tileIndex / 256),
                            "%s-%s-%s.%s" % (z, x, y, self.tileformat))


# =============================================================================
# =============================================================================
# =============================================================================

class GDAL2Mbtiles(object):
    """Class for generating .mbtiles form raster based on GDAl, sqlite3
    order of main steps:
        open_input()
        generate_metadata()
        generate_base_tiles()
        generate_overview_tiles()"""

    # -------------------------------------------------------------------------
    def error(self, msg, details=""):
        """Print an error message and stop the processing"""

        if details:
            self.parser.error(msg + "\n\n" + details)
        else:
            self.parser.error(msg)

    # -------------------------------------------------------------------------
    def progressbar(self, complete=0.0):
        """Print progressbar for float value 0..1"""

        gdal.TermProgress_nocb(complete)

    # -------------------------------------------------------------------------


    # -------------------------------------------------------------------------
    def stop(self):
        """Stop the rendering immediately"""
        self.stopped = True

    # -------------------------------------------------------------------------
    def __init__(self, arguments):
        """Constructor function - initialization"""

        self.stopped = False
        self.input = None
        self.output = None

        # Tile format

        self.tilesize = 256

        # Should we read bigger window of the input raster and scale it down?
        # Note: Modified leter by open_input()
        # Not for 'near' resampling
        # Not for Wavelet based drivers (JPEG2000, ECW, MrSID)
        # Not for 'raster' profile
        self.scaledquery = True
        # How big should be query window be for scaling down
        # Later on reset according the chosen resampling algorightm
        self.querysize = 4 * self.tilesize

        # Should we use Read on the input file for generating overview tiles?
        # Note: Modified later by open_input()
        # Otherwise the overview tiles are generated from existing underlying tiles
        self.overviewquery = False

        # RUN THE ARGUMENT PARSER:

        self.optparse_init()
        self.options, self.args = self.parser.parse_args(args=arguments)
        if not self.args:
            self.error("No input file specified")

        # POSTPROCESSING OF PARSED ARGUMENTS:

        if self.options.output_format == 'JPEG':
            self.tiledriver = 'JPEG'
            self.tileext = 'jpg'

        elif self.options.output_format == 'PNG':
            self.tiledriver = 'PNG'
            self.tileext = 'png'

        else:
            self.error("Output formats allowed are PNG and JPEG")

        if self.options.output_cache not in ('tms', 'xyz'):
            self.error("Accepted formats for output cache are 'xyz' or 'tms'")

        # Workaround for old versions of GDAL
        try:
            if (self.options.verbose and self.options.resampling == 'near') or gdal.TermProgress_nocb:
                pass
        except:
            self.error("This version of GDAL is not supported. Please upgrade to 1.6+.")
        # ,"You can try run crippled version of GDAL2Mbtiles with parameters: -v -r 'near'")

        # Is output directory the last argument?

        # Test output directory, if it doesn't exist
        # if os.path.isdir(self.args[-1]) or (len(self.args) > 1 and not os.path.exists(self.args[-1])):
        self.output = self.args[-1]
        self.args = self.args[:-1]
        # More files on the input not directly supported yet

        if (len(self.args) > 1):
            self.error("Processing of several input files is not supported.",
                       """Please first use a tool like gdal_vrtmerge.py or gdal_merge.py on the files:
           gdal_vrtmerge.py -o merged.vrt %s""" % " ".join(self.args))
        # TODO: Call functions from gdal_vrtmerge.py directly

        self.input = self.args[0]

        # Default values for not given options

        if not self.output:
            # Directory with input filename without extension in actual directory
            self.output = os.path.splitext(os.path.basename(self.input))[0]

        if not self.options.title:
            self.options.title = os.path.basename(self.input)

        if self.options.url and not self.options.url.endswith('/'):
            self.options.url += '/'
        if self.options.url:
            self.options.url += os.path.basename(self.output) + '/'

        # Supported options

        self.resampling = None

        if self.options.resampling == 'average':
            try:
                if gdal.RegenerateOverview:
                    pass
            except:
                self.error("'average' resampling algorithm is not available.",
                           "Please use -r 'near' argument or upgrade to newer version of GDAL.")

        elif self.options.resampling == 'antialias':
            try:
                if numpy:
                    pass
            except:
                self.error("'antialias' resampling algorithm is not available.",
                           "Install PIL (Python Imaging Library) and numpy.")

        elif self.options.resampling == 'near':
            self.resampling = gdal.GRA_NearestNeighbour
            self.querysize = self.tilesize

        elif self.options.resampling == 'bilinear':
            self.resampling = gdal.GRA_Bilinear
            self.querysize = self.tilesize * 2

        elif self.options.resampling == 'cubic':
            self.resampling = gdal.GRA_Cubic

        elif self.options.resampling == 'cubicspline':
            self.resampling = gdal.GRA_CubicSpline

        elif self.options.resampling == 'lanczos':
            self.resampling = gdal.GRA_Lanczos

        # User specified zoom levels
        self.tminz = None
        self.tmaxz = None
        if self.options.zoom:
            minmax = self.options.zoom.split('-', 1)
            minmax.extend([''])
            min, max = minmax[:2]
            self.tminz = int(min)
            if max:
                self.tmaxz = int(max)
            else:
                self.tmaxz = int(min)

        # KML generation
        self.kml = self.options.kml

        # Output the results

        if self.options.verbose:
            print("Options:", self.options)
            print("Input:", self.input)
            print("Output:", self.output)
            print("Cache: %s MB" % (gdal.GetCacheMax() / 1024 / 1024))
            print('')

    # -------------------------------------------------------------------------
    def optparse_init(self):
        """Prepare the option parser for input (argv)"""

        usage = "Usage: %prog [options] input_file(s) [output]"
        p = OptionParser(usage, version="%prog " + __version__)
        p.add_option("-p", "--profile", dest='profile', type='choice', choices=profile_list,
                     help="Tile cutting profile (%s) - default 'mercator' (Google Maps compatible)" % ",".join(
                         profile_list))
        p.add_option("-r", "--resampling", dest="resampling", type='choice', choices=resampling_list,
                     help="Resampling method (%s) - default 'average'" % ",".join(resampling_list))
        p.add_option('-s', '--s_srs', dest="s_srs", metavar="SRS",
                     help="The spatial reference system used for the source input data")
        p.add_option('-z', '--zoom', dest="zoom",
                     help="Zoom levels to render (format:'2-5' or '10').")
        p.add_option('-e', '--resume', dest="resume", action="store_true",
                     help="Resume mode. Generate only missing files.")
        p.add_option('-a', '--srcnodata', dest="srcnodata", metavar="NODATA",
                     help="NODATA transparency value to assign to the input data")
        p.add_option('--processes', dest='processes', type='int', default=multiprocessing.cpu_count(),
                     help='Number of concurrent processes (defaults to the number of cores in the system)')
        p.add_option("-v", "--verbose",
                     action="store_true", dest="verbose",
                     help="Print status messages to stdout")

        # KML options
        g = OptionGroup(p, "KML (Google Earth) options", "Options for generated Google Earth SuperOverlay metadata")
        g.add_option("-k", "--force-kml", dest='kml', action="store_true",
                     help="Generate KML for Google Earth - default for 'geodetic' profile and 'raster' in EPSG:4326. For a dataset with different projection use with caution!")
        g.add_option("-n", "--no-kml", dest='kml', action="store_false",
                     help="Avoid automatic generation of KML files for EPSG:4326")
        g.add_option("-u", "--url", dest='url',
                     help="URL address where the generated tiles are going to be published")
        p.add_option_group(g)

        # HTML options
        g = OptionGroup(p, "Web viewer options", "Options for generated HTML viewers a la Google Maps")
        g.add_option("-w", "--webviewer", dest='webviewer', type='choice', choices=webviewer_list,
                     help="Web viewer to generate (%s) - default 'all'" % ",".join(webviewer_list))
        g.add_option("-t", "--title", dest='title',
                     help="Title of the map")
        g.add_option("-c", "--copyright", dest='copyright',
                     help="Copyright for the map")
        g.add_option("-g", "--googlekey", dest='googlekey',
                     help="Google Maps API key from http://code.google.com/apis/maps/signup.html")
        g.add_option("-y", "--yahookey", dest='yahookey',
                     help="Yahoo Application ID from http://developer.yahoo.com/wsregapp/")
        p.add_option_group(g)

        # Config options
        g = OptionGroup(p, "Config options", "Options for config parameters")
        g.add_option("-x", "--auxfiles", dest='aux_files', action='store_true',
                     help="Generate aux.xml files.")
        g.add_option("-f", "--format", dest="output_format",
                     help="Image format for output tiles. Just PNG and JPEG allowed. PNG is selected by default")
        g.add_option("-o", "--output", dest="output_cache",
                     help="Format for output cache. Values allowed are tms and xyz, being xyz the default value")
        p.add_option_group(g)

        # TODO: MapFile + TileIndexes per zoom level for efficient MapServer WMS
        # g = OptionGroup(p, "WMS MapServer metadata", "Options for generated mapfile and tileindexes for MapServer")
        # g.add_option("-i", "--tileindex", dest='wms', action="store_true"
        #                 help="Generate tileindex and mapfile for MapServer (WMS)")
        # p.add_option_group(g)

        p.set_defaults(verbose=False, profile="mercator", kml=False, url='',
                       webviewer='all', copyright='', resampling='average', resume=False,
                       googlekey='INSERT_YOUR_KEY_HERE', yahookey='INSERT_YOUR_YAHOO_APP_ID_HERE', aux_files=False,
                       output_format="PNG", output_cache="xyz")

        self.parser = p

    # -------------------------------------------------------------------------
    def open_input(self):
        """Initialization of the input raster, reprojection if necessary"""

        gdal.UseExceptions()
        gdal.AllRegister()
        if not self.options.verbose:
            gdal.PushErrorHandler('CPLQuietErrorHandler')

        # Initialize necessary GDAL drivers

        self.out_drv = gdal.GetDriverByName(self.tiledriver)
        self.mem_drv = gdal.GetDriverByName('MEM')

        if not self.out_drv:
            raise Exception("The '%s' driver was not found, is it available in this GDAL build?", self.tiledriver)
        if not self.mem_drv:
            raise Exception("The 'MEM' driver was not found, is it available in this GDAL build?")

        # Open the input file

        if self.input:
            self.in_ds = gdal.Open(self.input, gdal.GA_ReadOnly)
        else:
            raise Exception("No input file was specified")

        if self.options.verbose:
            print("Input file:",
                  "( %sP x %sL - %s bands)" % (self.in_ds.RasterXSize, self.in_ds.RasterYSize, self.in_ds.RasterCount))

        if not self.in_ds:
            # Note: GDAL prints the ERROR message too
            self.error("It is not possible to open the input file '%s'." % self.input)

        # Read metadata from the input file
        if self.in_ds.RasterCount == 0:
            self.error("Input file '%s' has no raster band" % self.input)

        if self.in_ds.GetRasterBand(1).GetRasterColorTable():
            # TODO: Process directly paletted dataset by generating VRT in memory
            self.error("Please convert this file to RGB/RGBA and run GDAL2Mbtiles on the result.",
                       """From paletted file you can create RGBA file (temp.vrt) by:
           gdal_translate -of vrt -expand rgba %s temp.vrt
           then run:
           GDAL2Mbtiles temp.vrt""" % self.input)

        # Get NODATA value
        self.in_nodata = []
        for i in range(1, self.in_ds.RasterCount + 1):
            if self.in_ds.GetRasterBand(i).GetNoDataValue() != None:
                self.in_nodata.append(self.in_ds.GetRasterBand(i).GetNoDataValue())
        if self.options.srcnodata:
            nds = list(map(float, self.options.srcnodata.split(',')))
            if len(nds) < self.in_ds.RasterCount:
                self.in_nodata = (nds * self.in_ds.RasterCount)[:self.in_ds.RasterCount]
            else:
                self.in_nodata = nds

        if self.options.verbose:
            print("NODATA: %s" % self.in_nodata)

        #
        # Here we should have RGBA input dataset opened in self.in_ds
        #

        if self.options.verbose:
            print("Preprocessed file:",
                  "( %sP x %sL - %s bands)" % (self.in_ds.RasterXSize, self.in_ds.RasterYSize, self.in_ds.RasterCount))

        # Spatial Reference System of the input raster


        self.in_srs = None
        # self.in_srs = 'EPSG:32641'

        if self.options.s_srs:
            self.in_srs = osr.SpatialReference()
            self.in_srs.SetFromUserInput(self.options.s_srs)
            self.in_srs_wkt = self.in_srs.ExportToWkt()
        else:
            self.in_srs_wkt = self.in_ds.GetProjection()
            if not self.in_srs_wkt and self.in_ds.GetGCPCount() != 0:
                self.in_srs_wkt = self.in_ds.GetGCPProjection()
            if self.in_srs_wkt:
                self.in_srs = osr.SpatialReference()
                self.in_srs.ImportFromWkt(self.in_srs_wkt)
                # elif self.options.profile != 'raster':
                #   self.error("There is no spatial reference system info included in the input file.","You should run GDAL2Mbtiles with --s_srs EPSG:XXXX or similar.")

        # Spatial Reference System of tiles

        self.out_srs = osr.SpatialReference()

        if self.options.profile == 'mercator':
            self.out_srs.ImportFromEPSG(3857)
        elif self.options.profile == 'geodetic':
            self.out_srs.ImportFromEPSG(4326)
        else:
            self.out_srs = self.in_srs

        # Are the reference systems the same? Reproject if necessary.

        self.out_ds = None

        if self.options.profile in ('mercator', 'geodetic'):

            if (self.in_ds.GetGeoTransform() == (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)) and (self.in_ds.GetGCPCount() == 0):
                self.error(
                    "There is no georeference - neither affine transformation (worldfile) nor GCPs. You can generate only 'raster' profile tiles.",
                    "Either GDAL2Mbtiles with parameter -p 'raster' or use another GIS software for georeference e.g. gdal_transform -gcp / -a_ullr / -a_srs")

            if self.in_srs:

                if (self.in_srs.ExportToProj4() != self.out_srs.ExportToProj4()) or (self.in_ds.GetGCPCount() != 0):

                    # Generation of VRT dataset in tile projection, default 'nearest neighbour' warping
                    self.out_ds = gdal.AutoCreateWarpedVRT(self.in_ds, self.in_srs_wkt, self.out_srs.ExportToWkt())

                    # TODO: HIGH PRIORITY: Correction of AutoCreateWarpedVRT according the max zoomlevel for correct direct warping!!!

                    if self.options.verbose:
                        print("Warping of the raster by AutoCreateWarpedVRT (result saved into 'tiles.vrt')")
                        self.out_ds.GetDriver().CreateCopy("tiles.vrt", self.out_ds)

                    # Note: self.in_srs and self.in_srs_wkt contain still the non-warped reference system!!!

                    # Correction of AutoCreateWarpedVRT for NODATA values
                    if self.in_nodata != []:
                        fd, tempfilename = tempfile.mkstemp('-GDAL2Mbtiles.vrt')
                        fptr = os.fdopen(fd)
                        self.out_ds.GetDriver().CreateCopy(tempfilename, self.out_ds)
                        # open as a text file
                        s = open(tempfilename).read()
                        # Add the warping options
                        s = s.replace("""<GDALWarpOptions>""", """<GDALWarpOptions>
      <Option name="INIT_DEST">NO_DATA</Option>
      <Option name="UNIFIED_SRC_NODATA">YES</Option>""")
                        # replace BandMapping tag for NODATA bands....
                        for i in range(len(self.in_nodata)):
                            s = s.replace("""<BandMapping src="%i" dst="%i"/>""" % ((i + 1), (i + 1)), """<BandMapping src="%i" dst="%i">
          <SrcNoDataReal>%i</SrcNoDataReal>
          <SrcNoDataImag>0</SrcNoDataImag>
          <DstNoDataReal>%i</DstNoDataReal>
          <DstNoDataImag>0</DstNoDataImag>
        </BandMapping>""" % (
                                (i + 1), (i + 1), self.in_nodata[i],
                                self.in_nodata[i]))  # Or rewrite to white by: , 255 ))
                        # save the corrected VRT
                        open(tempfilename, "w").write(s)
                        # open by GDAL as self.out_ds
                        self.out_ds = gdal.Open(tempfilename)  # , gdal.GA_ReadOnly)
                        # delete the temporary file
                        fptr.flush()
                        fptr.close()
                        os.unlink(tempfilename)

                        # set NODATA_VALUE metadata
                        self.out_ds.SetMetadataItem('NODATA_VALUES', '%i %i %i' % (
                            self.in_nodata[0], self.in_nodata[1], self.in_nodata[2]))

                        if self.options.verbose:
                            print("Modified warping result saved into 'tiles1.vrt'")
                            open("tiles1.vrt", "w").write(s)

                    # -----------------------------------
                    # Correction of AutoCreateWarpedVRT for Mono (1 band) and RGB (3 bands) files without NODATA:
                    # equivalent of gdalwarp -dstalpha
                    if self.in_nodata == [] and self.out_ds.RasterCount in [1, 3]:
                        fd, tempfilename = tempfile.mkstemp('-GDAL2Mbtiles.vrt')
                        fptr = os.fdopen(fd)
                        self.out_ds.GetDriver().CreateCopy(tempfilename, self.out_ds)
                        # open as a text file
                        s = open(tempfilename).read()
                        # Add the warping options
                        s = s.replace("""<BlockXSize>""", """<VRTRasterBand dataType="Byte" band="%i" subClass="VRTWarpedRasterBand">
    <ColorInterp>Alpha</ColorInterp>
  </VRTRasterBand>
  <BlockXSize>""" % (self.out_ds.RasterCount + 1))
                        s = s.replace("""</GDALWarpOptions>""", """<DstAlphaBand>%i</DstAlphaBand>
  </GDALWarpOptions>""" % (self.out_ds.RasterCount + 1))
                        s = s.replace("""</WorkingDataType>""", """</WorkingDataType>
    <Option name="INIT_DEST">0</Option>""")
                        # save the corrected VRT
                        open(tempfilename, "w").write(s)
                        # open by GDAL as self.out_ds
                        self.out_ds = gdal.Open(tempfilename)  # , gdal.GA_ReadOnly)
                        # delete the temporary file
                        fptr.flush()
                        fptr.close()
                        os.unlink(tempfilename)

                        if self.options.verbose:
                            print("Modified -dstalpha warping result saved into 'tiles1.vrt'")
                            open("tiles1.vrt", "w").write(s)
                    s = '''
                    '''

            else:
                self.error("Input file has unknown SRS.",
                           "Use --s_srs ESPG:xyz (or similar) to provide source reference system.")

            if self.out_ds and self.options.verbose:
                print("Projected file:", "tiles.vrt", "( %sP x %sL - %s bands)" % (
                    self.out_ds.RasterXSize, self.out_ds.RasterYSize, self.out_ds.RasterCount))

        if not self.out_ds:
            self.out_ds = self.in_ds

        #
        # Here we should have a raster (out_ds) in the correct Spatial Reference system
        #

        # Get alpha band (either directly or from NODATA value)
        self.alphaband = self.out_ds.GetRasterBand(1).GetMaskBand()
        if (
                    self.alphaband.GetMaskFlags() & gdal.GMF_ALPHA) or self.out_ds.RasterCount == 4 or self.out_ds.RasterCount == 2:
            # TODO: Better test for alpha band in the dataset
            self.dataBandsCount = self.out_ds.RasterCount - 1
        else:
            self.dataBandsCount = self.out_ds.RasterCount

        # KML test
        self.isepsg4326 = False
        srs4326 = osr.SpatialReference()
        srs4326.ImportFromEPSG(4326)
        if self.out_srs and srs4326.ExportToProj4() == self.out_srs.ExportToProj4():
            # self.kml = True
            self.kml = False
            self.isepsg4326 = True
            if self.options.verbose:
                print("KML autotest OK!")

        # Read the georeference

        self.out_gt = self.out_ds.GetGeoTransform()

        # originX, originY = self.out_gt[0], self.out_gt[3]
        # pixelSize = self.out_gt[1] # = self.out_gt[5]

        # Test the size of the pixel

        # MAPTILER - COMMENTED
        # if self.out_gt[1] != (-1 * self.out_gt[5]) and self.options.profile != 'raster':
        # TODO: Process corectly coordinates with are have swichted Y axis (display in OpenLayers too)
        # self.error("Size of the pixel in the output differ for X and Y axes.")

        # Report error in case rotation/skew is in geotransform (possible only in 'raster' profile)
        if (self.out_gt[2], self.out_gt[4]) != (0, 0):
            self.error(
                "Georeference of the raster contains rotation or skew. Such raster is not supported. Please use gdalwarp first.")
        # TODO: Do the warping in this case automaticaly

        #
        # Here we expect: pixel is square, no rotation on the raster
        #

        # Output Bounds - coordinates in the output SRS
        self.ominx = self.out_gt[0]
        self.omaxx = self.out_gt[0] + self.out_ds.RasterXSize * self.out_gt[1]
        self.omaxy = self.out_gt[3]
        self.ominy = self.out_gt[3] - self.out_ds.RasterYSize * self.out_gt[1]
        # Note: maybe round(x, 14) to avoid the gdal_translate behaviour, when 0 becomes -1e-15

        if self.options.verbose:
            print("Bounds (output srs):", round(self.ominx, 13), self.ominy, self.omaxx, self.omaxy)

        #
        # Calculating ranges for tiles in different zoom levels
        #

        if self.options.profile == 'mercator':

            self.mercator = GlobalMercator()  # from globalmaptiles.py

            # Function which generates SWNE in LatLong for given tile
            self.tileswne = self.mercator.TileLatLonBounds

            # Generate table with min max tile coordinates for all zoomlevels
            self.tminmax = list(range(0, 32))
            for tz in range(0, 32):
                tminx, tminy = self.mercator.MetersToTile(self.ominx, self.ominy, tz)
                tmaxx, tmaxy = self.mercator.MetersToTile(self.omaxx, self.omaxy, tz)
                # crop tiles extending world limits (+-180,+-90)
                tminx, tminy = max(0, tminx), max(0, tminy)
                tmaxx, tmaxy = min(2 ** tz - 1, tmaxx), min(2 ** tz - 1, tmaxy)
                self.tminmax[tz] = (tminx, tminy, tmaxx, tmaxy)

            # TODO: Maps crossing 180E (Alaska?)

            # Get the minimal zoom level (map covers area equivalent to one tile)
            if self.tminz == None:
                self.tminz = self.mercator.ZoomForPixelSize(
                    self.out_gt[1] * max(self.out_ds.RasterXSize, self.out_ds.RasterYSize) / float(self.tilesize))

            # Get the maximal zoom level (closest possible zoom level up on the resolution of raster)
            if self.tmaxz == None:
                self.tmaxz = self.mercator.ZoomForPixelSize(self.out_gt[1])

            if self.options.verbose:
                print("Bounds (latlong):", self.mercator.MetersToLatLon(self.ominx, self.ominy),
                      self.mercator.MetersToLatLon(self.omaxx, self.omaxy))
                print('MinZoomLevel:', self.tminz)
                print("MaxZoomLevel:", self.tmaxz, "(", self.mercator.Resolution(self.tmaxz), ")")

        if self.options.profile == 'geodetic':

            self.geodetic = GlobalGeodetic()  # from globalmaptiles.py

            # Function which generates SWNE in LatLong for given tile
            self.tileswne = self.geodetic.TileLatLonBounds

            # Generate table with min max tile coordinates for all zoomlevels
            self.tminmax = list(range(0, 32))
            for tz in range(0, 32):
                tminx, tminy = self.geodetic.LatLonToTile(self.ominx, self.ominy, tz)
                tmaxx, tmaxy = self.geodetic.LatLonToTile(self.omaxx, self.omaxy, tz)
                # crop tiles extending world limits (+-180,+-90)
                tminx, tminy = max(0, tminx), max(0, tminy)
                tmaxx, tmaxy = min(2 ** (tz + 1) - 1, tmaxx), min(2 ** tz - 1, tmaxy)
                self.tminmax[tz] = (tminx, tminy, tmaxx, tmaxy)

            # TODO: Maps crossing 180E (Alaska?)

            # Get the maximal zoom level (closest possible zoom level up on the resolution of raster)
            if self.tminz == None:
                self.tminz = self.geodetic.ZoomForPixelSize(
                    self.out_gt[1] * max(self.out_ds.RasterXSize, self.out_ds.RasterYSize) / float(self.tilesize))

            # Get the maximal zoom level (closest possible zoom level up on the resolution of raster)
            if self.tmaxz == None:
                self.tmaxz = self.geodetic.ZoomForPixelSize(self.out_gt[1])

            if self.options.verbose:
                print("Bounds (latlong):", self.ominx, self.ominy, self.omaxx, self.omaxy)

        if self.options.profile == 'raster':

            log2 = lambda x: math.log10(x) / math.log10(2)  # log2 (base 2 logarithm)

            self.nativezoom = int(max(math.ceil(log2(self.out_ds.RasterXSize / float(self.tilesize))),
                                      math.ceil(log2(self.out_ds.RasterYSize / float(self.tilesize)))))

            if self.options.verbose:
                print("Native zoom of the raster:", self.nativezoom)

            # Get the minimal zoom level (whole raster in one tile)
            if self.tminz == None:
                self.tminz = 0

            # Get the maximal zoom level (native resolution of the raster)
            if self.tmaxz == None:
                self.tmaxz = self.nativezoom

            # Generate table with min max tile coordinates for all zoomlevels
            self.tminmax = list(range(0, self.tmaxz + 1))
            self.tsize = list(range(0, self.tmaxz + 1))
            for tz in range(0, self.tmaxz + 1):
                tsize = 2.0 ** (self.nativezoom - tz) * self.tilesize
                tminx, tminy = 0, 0
                tmaxx = int(math.ceil(self.out_ds.RasterXSize / tsize)) - 1
                tmaxy = int(math.ceil(self.out_ds.RasterYSize / tsize)) - 1
                self.tsize[tz] = math.ceil(tsize)
                self.tminmax[tz] = (tminx, tminy, tmaxx, tmaxy)

            # Function which generates SWNE in LatLong for given tile
            if self.kml and self.in_srs_wkt:
                self.ct = osr.CoordinateTransformation(self.in_srs, srs4326)

                def rastertileswne(x, y, z):
                    pixelsizex = (2 ** (self.tmaxz - z) * self.out_gt[1])  # X-pixel size in level
                    pixelsizey = (
                        2 ** (self.tmaxz - z) * self.out_gt[1])  # Y-pixel size in level (usually -1*pixelsizex)
                    west = self.out_gt[0] + x * self.tilesize * pixelsizex
                    east = west + self.tilesize * pixelsizex
                    south = self.ominy + y * self.tilesize * pixelsizex
                    north = south + self.tilesize * pixelsizex
                    if not self.isepsg4326:
                        # Transformation to EPSG:4326 (WGS84 datum)
                        west, south = self.ct.TransformPoint(west, south)[:2]
                        east, north = self.ct.TransformPoint(east, north)[:2]
                    return south, west, north, east

                self.tileswne = rastertileswne
            else:
                self.tileswne = lambda x, y, z: (0, 0, 0, 0)
    # -------------------------------------------------------------------------
    def generate_metadata(self, cur):
        """Generation of main metadata files and HTML viewers (metadata related to particular tiles are generated during the tile processing)."""

        output_dir = os.path.dirname(os.path.abspath(self.output))
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        if self.options.profile == 'mercator':

            south, west = self.mercator.MetersToLatLon(self.ominx, self.ominy)
            north, east = self.mercator.MetersToLatLon(self.omaxx, self.omaxy)
            south, west = max(-85.05112878, south), max(-180.0, west)
            north, east = min(85.05112878, north), min(180.0, east)
            self.swne = (south, west, north, east)

            # Generate googlemaps.html
            if self.options.webviewer in ('all', 'google') and self.options.profile == 'mercator':
                if not self.options.resume or not os.path.exists(os.path.join(output_dir, 'googlemaps.html')):
                    f = open(os.path.join(output_dir, 'googlemaps.html'), 'w')
                    f.write(self.generate_googlemaps())
                    f.close()

            # Generate openlayers.html
            if self.options.webviewer in ('all', 'openlayers'):
                if not self.options.resume or not os.path.exists(os.path.join(output_dir, 'openlayers.html')):
                    f = open(os.path.join(output_dir, 'openlayers.html'), 'w')
                    f.write(self.generate_openlayers())
                    f.close()

            # Generate leaflet.html
            if self.options.webviewer in ('all', 'leaflet'):
                if not self.options.resume or not os.path.exists(os.path.join(output_dir, 'leaflet.html')):
                    f = open(os.path.join(output_dir, 'leaflet.html'), 'w')
                    f.write(self.generate_leaflet())
                    f.close()

            # Generate index.html
            if self.options.webviewer in ('all', 'index'):
                if not self.options.resume or not os.path.exists(os.path.join(output_dir, 'index.html')):
                    f = open(os.path.join(output_dir, 'index.html'), 'w')
                    f.write(self.generate_index())
                    f.close()

            # Generate metadata.json
            if self.options.webviewer in ('all', 'metadata'):
                if not self.options.resume or not os.path.exists(os.path.join(output_dir, 'metadata.json')):
                    metadata_dict = self.generate_metadatajson()
                    f = open(os.path.join(output_dir, 'metadata.json'), 'w')
                    f.write(json.dumps(metadata_dict))
                    f.close()
                    for n, v in metadata_dict.items():
                        cur.execute("INSERT INTO metadata (name,value) values (?,?)", (n, v))


        elif self.options.profile == 'geodetic':

            west, south = self.ominx, self.ominy
            east, north = self.omaxx, self.omaxy
            south, west = max(-90.0, south), max(-180.0, west)
            north, east = min(90.0, north), min(180.0, east)
            self.swne = (south, west, north, east)

            # Generate openlayers.html
            if self.options.webviewer in ('all', 'openlayers'):
                if not self.options.resume or not os.path.exists(os.path.join(output_dir, 'openlayers.html')):
                    f = open(os.path.join(output_dir, 'openlayers.html'), 'w')
                    f.write(self.generate_openlayers())
                    f.close()

        elif self.options.profile == 'raster':

            west, south = self.ominx, self.ominy
            east, north = self.omaxx, self.omaxy

            self.swne = (south, west, north, east)

            # Generate openlayers.html
            if self.options.webviewer in ('all', 'openlayers'):
                if not self.options.resume or not os.path.exists(os.path.join(output_dir, 'openlayers.html')):
                    f = open(os.path.join(output_dir, 'openlayers.html'), 'w')
                    f.write(self.generate_openlayers())
                    f.close()

        # Generate tilemapresource.xml.
        if not self.options.resume or not os.path.exists(os.path.join(output_dir, 'tilemapresource.xml')):
            f = open(os.path.join(output_dir, 'tilemapresource.xml'), 'w')
            f.write(self.generate_tilemapresource())
            f.close()

        if self.kml:
            # TODO: Maybe problem for not automatically generated tminz
            # The root KML should contain links to all tiles in the tminz level
            children = []
            xmin, ymin, xmax, ymax = self.tminmax[self.tminz]
            for x in range(xmin, xmax + 1):
                for y in range(ymin, ymax + 1):
                    children.append([x, y, self.tminz])
            # Generate Root KML
            if self.kml:
                if not self.options.resume or not os.path.exists(os.path.join(output_dir, 'doc.kml')):
                    f = open(os.path.join(output_dir, 'doc.kml'), 'w')
                    f.write(self.generate_kml(None, None, None, children))
                    f.close()

    # -------------------------------------------------------------------------
    def generate_base_tiles(self, cpu, queue, con):
        """Generation of the base tiles (the lowest in the pyramid) directly from the input raster"""
        cur = con.cursor()
        if self.options.verbose:
            # mx, my = self.out_gt[0], self.out_gt[3] # OriginX, OriginY
            # px, py = self.mercator.MetersToPixels( mx, my, self.tmaxz)
            # print "Pixel coordinates:", px, py, (mx, my)
            print('')
            print("Tiles generated from the max zoom level:")
            print("----------------------------------------")
            print('')

        # Set the bounds
        tminx, tminy, tmaxx, tmaxy = self.tminmax[self.tmaxz]

        # Just the center tile
        # tminx = tminx+ (tmaxx - tminx)/2
        # tminy = tminy+ (tmaxy - tminy)/2
        # tmaxx = tminx
        # tmaxy = tminy

        ds = self.out_ds
        tilebands = self.dataBandsCount + 1
        querysize = self.querysize

        if self.options.verbose:
            print("dataBandsCount: ", self.dataBandsCount)
            print("tilebands: ", tilebands)

        # print tminx, tminy, tmaxx, tmaxy
        tcount = (1 + abs(tmaxx - tminx)) * (1 + abs(tmaxy - tminy))

        queue.put(tcount)

        ti = 0
        j = 0
        msg = ''
        tz = self.tmaxz
        count = (tmaxy - tminy + 1) * (tmaxx + 1 - tminx)

        for ty in range(tmaxy, tminy - 1, -1):  # range(tminy, tmaxy+1):
            for tx in range(tminx, tmaxx + 1):
                if self.stopped:
                    break
                ti += 1
                if (ti - 1) % self.options.processes != cpu:
                    continue

                if self.options.output_cache == 'xyz':
                    ty_final = (2 ** tz - 1) - ty
                else:
                    ty_final = ty
                # my addons
                tilefilename = os.path.join(self.output, str(tz), str(tx), "%s.%s" % (ty_final, self.tileext))
                if os.path.exists(os.path.abspath(tilefilename)):
                    # already exist
                    continue
                if self.options.verbose:
                    print(ti, '/', tcount, tilefilename)  # , "( TileMapService: z / x / y )"

                if self.options.resume:
                    cur.execute("""SELECT 1 FROM tiles WHERE zoom_level = ?
                                   AND tile_column = ? AND tile_row = ?""", (tz, tx, ty))
                    exists = cur.fetchone()
                    if exists is not None:
                        if self.options.verbose:
                            print("Tile generation skiped because of --resume")
                        else:
                            queue.put(tcount)
                        continue

                if self.options.profile == 'mercator':
                    # Tile bounds in EPSG:900913
                    b = self.mercator.TileBounds(tx, ty, tz)
                elif self.options.profile == 'geodetic':
                    b = self.geodetic.TileBounds(tx, ty, tz)

                # print "\tgdalwarp -ts 256 256 -te %s %s %s %s %s %s_%s_%s.tif" % ( b[0], b[1], b[2], b[3], "tiles.vrt", tz, tx, ty)

                # Don't scale up by nearest neighbour, better change the querysize
                # to the native resolution (and return smaller query tile) for scaling

                if self.options.profile in ('mercator', 'geodetic'):
                    rb, wb = self.geo_query(ds, b[0], b[3], b[2], b[1])
                    nativesize = wb[0] + wb[2]  # Pixel size in the raster covering query geo extent
                    if self.options.verbose:
                        print("\tNative Extent (querysize", nativesize, "): ", rb, wb)

                    # Tile bounds in raster coordinates for ReadRaster query
                    rb, wb = self.geo_query(ds, b[0], b[3], b[2], b[1], querysize=querysize)

                    rx, ry, rxsize, rysize = rb
                    wx, wy, wxsize, wysize = wb

                else:  # 'raster' profile:

                    tsize = int(self.tsize[tz])  # tilesize in raster coordinates for actual zoom
                    xsize = self.out_ds.RasterXSize  # size of the raster in pixels
                    ysize = self.out_ds.RasterYSize
                    if tz >= self.nativezoom:
                        querysize = self.tilesize  # int(2**(self.nativezoom-tz) * self.tilesize)

                    rx = (tx) * tsize
                    rxsize = 0
                    if tx == tmaxx:
                        rxsize = xsize % tsize
                    if rxsize == 0:
                        rxsize = tsize

                    rysize = 0
                    if ty == tmaxy:
                        rysize = ysize % tsize
                    if rysize == 0:
                        rysize = tsize
                    ry = ysize - (ty * tsize) - rysize

                    wx, wy = 0, 0
                    wxsize, wysize = int(rxsize / float(tsize) * self.tilesize), int(
                        rysize / float(tsize) * self.tilesize)
                    if wysize != self.tilesize:
                        wy = self.tilesize - wysize

                if self.options.verbose:
                    print("\tReadRaster Extent: ", (rx, ry, rxsize, rysize), (wx, wy, wxsize, wysize))

                # Query is in 'nearest neighbour' but can be bigger in then the tilesize
                # We scale down the query to the tilesize by supplied algorithm.

                # Tile dataset in memory
                dstile = self.mem_drv.Create('', self.tilesize, self.tilesize, tilebands)
                # print 'dest', dstile
                data = ds.ReadRaster(rx, ry, rxsize, rysize, wxsize, wysize,
                                     band_list=list(range(1, self.dataBandsCount + 1)))
                alpha = self.alphaband.ReadRaster(rx, ry, rxsize, rysize, wxsize, wysize)

                if self.tilesize == querysize:
                    # Use the ReadRaster result directly in tiles ('nearest neighbour' query)
                    dstile.WriteRaster(wx, wy, wxsize, wysize, data, band_list=list(range(1, self.dataBandsCount + 1)))
                    dstile.WriteRaster(wx, wy, wxsize, wysize, alpha, band_list=[tilebands])

                # Note: For source drivers based on WaveLet compression (JPEG2000, ECW, MrSID)
                # the ReadRaster function returns high-quality raster (not ugly nearest neighbour)
                # TODO: Use directly 'near' for WaveLet files
                else:
                    # Big ReadRaster query in memory scaled to the tilesize - all but 'near' algo
                    dsquery = self.mem_drv.Create('', querysize, querysize, tilebands)
                    # TODO: fill the null value in case a tile without alpha is produced (now only png tiles are supported)
                    # for i in range(1, tilebands+1):
                    #   dsquery.GetRasterBand(1).Fill(tilenodata)
                    dsquery.WriteRaster(wx, wy, wxsize, wysize, data, band_list=list(range(1, self.dataBandsCount + 1)))
                    dsquery.WriteRaster(wx, wy, wxsize, wysize, alpha, band_list=[tilebands])

                    self.scale_query_to_tile(dsquery, dstile, tilefilename)
                    del dsquery

                del data

                if self.options.resampling != 'antialias':
                    dstile_array = dstile.ReadAsArray()
                    binary = io.BytesIO()
                    img = Image.fromarray(numpy.rollaxis(dstile_array, 0, 3))  # rotate from (256,256,3) to (3,256,256)
                    img.save(binary, format=self.tiledriver)

                    cur.execute("""insert into tiles (zoom_level,
                                            tile_column, tile_row, tile_data) values
                                            (?, ?, ?, ?);""",
                                (tz, tx, ty, sqlite3.Binary(binary.getvalue())))

                    del img
                    binary.flush()
                    binary.close()
                    del dstile_array
                    del dstile
                if not self.options.verbose:
                    con.commit()
                    queue.put(tcount)

    # -------------------------------------------------------------------------
    def generate_overview_tiles(self, cpu, tz, queue, con):
        """Generation of the overview tiles (higher in the pyramid) based on existing tiles"""
        cur = con.cursor()
        tilebands = self.dataBandsCount + 1

        # Usage of existing tiles: from 4 underlying tiles generate one as overview.

        tcount = 0
        for z in range(self.tmaxz - 1, self.tminz - 1, -1):
            tminx, tminy, tmaxx, tmaxy = self.tminmax[z]
            tcount += (1 + abs(tmaxx - tminx)) * (1 + abs(tmaxy - tminy))

        ti = 0

        # querysize = tilesize * 2

        msg = ''
        tminx, tminy, tmaxx, tmaxy = self.tminmax[tz]
        count = (tmaxy - tminy + 1) * (tmaxx + 1 - tminx)
        for ty in range(tmaxy, tminy - 1, -1):  # range(tminy, tmaxy+1):
            for tx in range(tminx, tmaxx + 1):

                if self.stopped:
                    break

                ti += 1
                if (ti - 1) % self.options.processes != cpu:
                    continue

                if self.options.output_cache == 'xyz':
                    ty_final = (2 ** tz - 1) - ty
                else:
                    ty_final = ty

                tilefilename = os.path.join(self.output, str(tz), str(tx), "%s.%s" % (ty_final, self.tileext))

                if self.options.verbose:
                    print(ti, '/', tcount, tilefilename)  # , "( TileMapService: z / x / y )"

                if self.options.resume and os.path.exists(tilefilename):
                    if self.options.verbose:
                        print("Tile generation skipped because of --resume")
                    else:
                        queue.put(tcount)
                    continue

                # TODO: improve that
                if self.out_drv.ShortName == 'JPEG' and tilebands == 4:
                    tilebands = 3

                dsquery = self.mem_drv.Create('', 2 * self.tilesize, 2 * self.tilesize, tilebands)
                # TODO: fill the null value
                # for i in range(1, tilebands+1):
                #   dsquery.GetRasterBand(1).Fill(tilenodata)
                dstile = self.mem_drv.Create('', self.tilesize, self.tilesize, tilebands)

                # TODO: Implement more clever walking on the tiles with cache functionality
                # probably walk should start with reading of four tiles from top left corner
                # Hilbert curve...


                # Read the tiles and write them to query window
                for y in range(2 * ty, 2 * ty + 2):
                    for x in range(2 * tx, 2 * tx + 2):
                        minx, miny, maxx, maxy = self.tminmax[tz + 1]
                        if x >= minx and x <= maxx and y >= miny and y <= maxy:

                            if self.options.output_cache == 'xyz':
                                y_final = (2 ** (tz + 1) - 1) - y
                            else:
                                y_final = y

                            tiles = cur.execute('''select  tile_data from tiles
                                where zoom_level = (?) AND tile_column = (?) AND tile_row = (?) ;''', [tz + 1, x, y])
                            blob_tile = tiles.fetchone()
                            pil_tile = Image.open(io.BytesIO(blob_tile[0]))
                            np_tile = numpy.array(pil_tile)

                            if (ty == 0 and y == 1) or (ty != 0 and (y % (2 * ty)) != 0):
                                tileposy = 0
                            else:
                                tileposy = self.tilesize
                            if tx:
                                tileposx = x % (2 * tx) * self.tilesize
                            elif tx == 0 and x == 1:
                                tileposx = self.tilesize
                            else:
                                tileposx = 0
                                # Write Array each band of size (256L,256L)
                            for i in range(tilebands):
                                dsquery.GetRasterBand(i + 1).WriteArray(np_tile[:, :, i], tileposx, tileposy)

                self.scale_query_to_tile(dsquery, dstile, tilefilename)
                # Write a copy of tile to png/jpg
                #
                if self.options.resampling != 'antialias':
                    # Write a copy of tile to png/jpg
                    dstile_array = dstile.ReadAsArray()
                    binary = io.BytesIO()
                    img = Image.fromarray(numpy.rollaxis(dstile_array, 0, 3))
                    img.save(binary, format=self.tiledriver)
                    cur.execute("""insert into tiles (zoom_level,
                                                                tile_column, tile_row, tile_data) values
                                                                (?, ?, ?, ?);""",
                                (tz, tx, ty, sqlite3.Binary(binary.getvalue())))

                    del binary
                    del img
                del dstile

                if self.options.verbose:
                    print("\tbuild from zoom", tz + 1, " tiles:", (2 * tx, 2 * ty), (2 * tx + 1, 2 * ty),
                          (2 * tx, 2 * ty + 1), (2 * tx + 1, 2 * ty + 1))

                if not self.options.verbose:
                    queue.put(tcount)
                    con.commit()
                    pass

    # -------------------------------------------------------------------------
    def geo_query(self, ds, ulx, uly, lrx, lry, querysize=0):
        """For given dataset and query in cartographic coordinates
        returns parameters for ReadRaster() in raster coordinates and
        x/y shifts (for border tiles). If the querysize is not given, the
        extent is returned in the native resolution of dataset ds."""

        geotran = ds.GetGeoTransform()
        rx = int((ulx - geotran[0]) / geotran[1] + 0.001)
        ry = int((uly - geotran[3]) / geotran[5] + 0.001)
        rxsize = int((lrx - ulx) / geotran[1] + 0.5)
        rysize = int((lry - uly) / geotran[5] + 0.5)

        if not querysize:
            wxsize, wysize = rxsize, rysize
        else:
            wxsize, wysize = querysize, querysize

        # Coordinates should not go out of the bounds of the raster
        wx = 0
        if rx < 0:
            rxshift = abs(rx)
            wx = int(wxsize * (float(rxshift) / rxsize))
            wxsize = wxsize - wx
            rxsize = rxsize - int(rxsize * (float(rxshift) / rxsize))
            rx = 0
        if rx + rxsize > ds.RasterXSize:
            wxsize = int(wxsize * (float(ds.RasterXSize - rx) / rxsize))
            rxsize = ds.RasterXSize - rx

        wy = 0
        if ry < 0:
            ryshift = abs(ry)
            wy = int(wysize * (float(ryshift) / rysize))
            wysize = wysize - wy
            rysize = rysize - int(rysize * (float(ryshift) / rysize))
            ry = 0
        if ry + rysize > ds.RasterYSize:
            wysize = int(wysize * (float(ds.RasterYSize - ry) / rysize))
            rysize = ds.RasterYSize - ry

        return (rx, ry, rxsize, rysize), (wx, wy, wxsize, wysize)

    # -------------------------------------------------------------------------
    def scale_query_to_tile(self, dsquery, dstile, tilefilename=''):
        """Scales down query dataset to the tile dataset"""

        querysize = dsquery.RasterXSize
        tilesize = dstile.RasterXSize
        tilebands = dstile.RasterCount

        if self.options.resampling == 'average':

            # Function: gdal.RegenerateOverview()
            for i in range(1, tilebands + 1):
                # Black border around NODATA
                # if i != 4:
                #   dsquery.GetRasterBand(i).SetNoDataValue(0)
                res = gdal.RegenerateOverview(dsquery.GetRasterBand(i),
                                              dstile.GetRasterBand(i), 'average')
                if res != 0:
                    self.error("RegenerateOverview() failed on %s, error %d" % (tilefilename, res))

        elif self.options.resampling == 'antialias':

            # Scaling by PIL (Python Imaging Library) - improved Lanczos
            array = numpy.zeros((querysize, querysize, tilebands), numpy.uint8)
            for i in range(tilebands):
                array[:, :, i] = gdalarray.BandReadAsArray(dsquery.GetRasterBand(i + 1), 0, 0, querysize, querysize)
            im = Image.fromarray(array, 'RGBA')  # Always four bands
            im1 = im.resize((tilesize, tilesize), Image.ANTIALIAS)
            if os.path.exists(tilefilename):
                im0 = Image.open(tilefilename)
                im1 = Image.composite(im1, im0, im1)
            im1.save(tilefilename, self.tiledriver)

        else:

            # Other algorithms are implemented by gdal.ReprojectImage().
            dsquery.SetGeoTransform((0.0, tilesize / float(querysize), 0.0, 0.0, 0.0, tilesize / float(querysize)))
            dstile.SetGeoTransform((0.0, 1.0, 0.0, 0.0, 0.0, 1.0))

            res = gdal.ReprojectImage(dsquery, dstile, None, None, self.resampling)
            if res != 0:
                self.error("ReprojectImage() failed on %s, error %d" % (tilefilename, res))

    # -------------------------------------------------------------------------
    def generate_tilemapresource(self):
        """
        Template for tilemapresource.xml. Returns filled string. Expected variables:
          title, north, south, east, west, isepsg4326, projection, publishurl,
          zoompixels, tilesize, tileformat, profile
        """

        args = {}
        args['title'] = self.options.title
        args['south'], args['west'], args['north'], args['east'] = self.swne
        args['tilesize'] = self.tilesize
        args['tileformat'] = self.tileext
        args['publishurl'] = self.options.url
        args['profile'] = self.options.profile

        if self.options.profile == 'mercator':
            args['srs'] = "EPSG:900913"
        elif self.options.profile == 'geodetic':
            args['srs'] = "EPSG:4326"
        elif self.options.s_srs:
            args['srs'] = self.options.s_srs
        elif self.out_srs:
            args['srs'] = self.out_srs.ExportToWkt()
        else:
            args['srs'] = ""

        s = """<?xml version="1.0" encoding="utf-8"?>
    <TileMap version="1.0.0" tilemapservice="http://tms.osgeo.org/1.0.0">
      <Title>%(title)s</Title>
      <Abstract></Abstract>
      <SRS>%(srs)s</SRS>
      <BoundingBox minx="%(south).14f" miny="%(west).14f" maxx="%(north).14f" maxy="%(east).14f"/>
      <Origin x="%(south).14f" y="%(west).14f"/>
      <TileFormat width="%(tilesize)d" height="%(tilesize)d" mime-type="image/%(tileformat)s" extension="%(tileformat)s"/>
      <TileSets profile="%(profile)s">
""" % args
        for z in range(self.tminz, self.tmaxz + 1):
            if self.options.profile == 'raster':
                s += """        <TileSet href="%s%d" units-per-pixel="%.14f" order="%d"/>\n""" % (
                    args['publishurl'], z, (2 ** (self.nativezoom - z) * self.out_gt[1]), z)
            elif self.options.profile == 'mercator':
                s += """        <TileSet href="%s%d" units-per-pixel="%.14f" order="%d"/>\n""" % (
                    args['publishurl'], z, 156543.0339 / 2 ** z, z)
            elif self.options.profile == 'geodetic':
                s += """        <TileSet href="%s%d" units-per-pixel="%.14f" order="%d"/>\n""" % (
                    args['publishurl'], z, 0.703125 / 2 ** z, z)
        s += """      </TileSets>
    </TileMap>
    """
        return s

    # -------------------------------------------------------------------------
    def generate_kml(self, tx, ty, tz, children=[], **args):
        """
        Template for the KML. Returns filled string.
        """
        args['tx'], args['ty'], args['tz'] = tx, ty, tz
        args['tileformat'] = self.tileext
        if 'tilesize' not in args:
            args['tilesize'] = self.tilesize

        if 'minlodpixels' not in args:
            args['minlodpixels'] = int(args['tilesize'] / 2)  # / 2.56) # default 128
        if 'maxlodpixels' not in args:
            args['maxlodpixels'] = int(args['tilesize'] * 8)  # 1.7) # default 2048 (used to be -1)
        if children == []:
            args['maxlodpixels'] = -1

        if tx == None:
            tilekml = False
            args['title'] = self.options.title
        else:
            tilekml = True
            args['title'] = "%d/%d/%d.kml" % (tz, tx, ty)
            args['south'], args['west'], args['north'], args['east'] = self.tileswne(tx, ty, tz)

        if tx == 0:
            args['drawOrder'] = 2 * tz + 1
        elif tx != None:
            args['drawOrder'] = 2 * tz
        else:
            args['drawOrder'] = 0

        url = self.options.url
        if not url:
            if tilekml:
                url = "../../"
            else:
                url = ""

        s = """<?xml version="1.0" encoding="utf-8"?>
    <kml xmlns="http://earth.google.com/kml/2.1">
      <Document>
        <Name>%(title)s</Name>
        <Description></Description>
        <Style>
          <ListStyle id="hideChildren">
            <listItemType>checkHideChildren</listItemType>
          </ListStyle>
        </Style>""" % args
        if tilekml:
            s += """
        <Region>
          <Lod>
            <minLodPixels>%(minlodpixels)d</minLodPixels>
            <maxLodPixels>%(maxlodpixels)d</maxLodPixels>
          </Lod>
          <LatLonAltBox>
            <north>%(north).14f</north>
            <south>%(south).14f</south>
            <east>%(east).14f</east>
            <west>%(west).14f</west>
          </LatLonAltBox>
        </Region>
        <GroundOverlay>
          <drawOrder>%(drawOrder)d</drawOrder>
          <Icon>
            <href>%(ty)d.%(tileformat)s</href>
          </Icon>
          <LatLonBox>
            <north>%(north).14f</north>
            <south>%(south).14f</south>
            <east>%(east).14f</east>
            <west>%(west).14f</west>
          </LatLonBox>
        </GroundOverlay>
    """ % args

        for cx, cy, cz in children:
            csouth, cwest, cnorth, ceast = self.tileswne(cx, cy, cz)
            s += """
        <NetworkLink>
          <name>%d/%d/%d.%s</name>
          <Region>
            <Lod>
              <minLodPixels>%d</minLodPixels>
              <maxLodPixels>-1</maxLodPixels>
            </Lod>
            <LatLonAltBox>
              <north>%.14f</north>
              <south>%.14f</south>
              <east>%.14f</east>
              <west>%.14f</west>
            </LatLonAltBox>
          </Region>
          <Link>
            <href>%s%d/%d/%d.kml</href>
            <viewRefreshMode>onRegion</viewRefreshMode>
            <viewFormat/>
          </Link>
        </NetworkLink>
    """ % (cz, cx, cy, args['tileformat'], args['minlodpixels'], cnorth, csouth, ceast, cwest, url, cz, cx, cy)

        s += """      </Document>
    </kml>
    """
        return s

    # -------------------------------------------------------------------------
    def generate_googlemaps(self):
        """
        Template for googlemaps.html implementing Overlay of tiles for 'mercator' profile.
        It returns filled string. Expected variables:
        title, googlemapskey, north, south, east, west, minzoom, maxzoom, tilesize, tileformat, publishurl
        """
        args = {}
        args['title'] = self.options.title
        args['googlemapskey'] = self.options.googlekey
        args['south'], args['west'], args['north'], args['east'] = self.swne
        args['minzoom'] = self.tminz
        args['maxzoom'] = self.tmaxz
        args['tilesize'] = self.tilesize
        args['tileformat'] = self.tileext
        args['publishurl'] = self.options.url
        args['copyright'] = self.options.copyright

        s = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">
            <html xmlns="http://www.w3.org/1999/xhtml" xmlns:v="urn:schemas-microsoft-com:vml">
              <head>
                <title>%(title)s</title>
                <meta http-equiv="content-type" content="text/html; charset=utf-8"/>
                <meta http-equiv='imagetoolbar' content='no'/>
                <style type="text/css"> v\:* {behavior:url(#default#VML);}
                    html, body { overflow: hidden; padding: 0; height: 100%%; width: 100%%; font-family: 'Lucida Grande',Geneva,Arial,Verdana,sans-serif; }
                    body { margin: 10px; background: #fff; }
                    h1 { margin: 0; padding: 6px; border:0; font-size: 20pt; }
                    #header { height: 43px; padding: 0; background-color: #eee; border: 1px solid #888; }
                    #subheader { height: 12px; text-align: right; font-size: 10px; color: #555;}
                    #map { height: 95%%; border: 1px solid #888; }
                </style>
                <script src='http://maps.google.com/maps?file=api&amp;v=2&amp;key=%(googlemapskey)s' type='text/javascript'></script>
                <script type="text/javascript">
                //<![CDATA[

                /*
                 * Constants for given map
                 * TODO: read it from tilemapresource.xml
                 */

                var mapBounds = new GLatLngBounds(new GLatLng(%(south)s, %(west)s), new GLatLng(%(north)s, %(east)s));
                var mapMinZoom = %(minzoom)s;
                var mapMaxZoom = %(maxzoom)s;

                var opacity = 0.75;
                var map;
                var hybridOverlay;

                /*
                 * Create a Custom Opacity GControl
                 * http://www.maptiler.org/google-maps-overlay-opacity-control/
                 */

                var CTransparencyLENGTH = 58;
                // maximum width that the knob can move (slide width minus knob width)

                function CTransparencyControl( overlay ) {
                    this.overlay = overlay;
                    this.opacity = overlay.getTileLayer().getOpacity();
                }
                CTransparencyControl.prototype = new GControl();

                // This function positions the slider to match the specified opacity
                CTransparencyControl.prototype.setSlider = function(pos) {
                    var left = Math.round((CTransparencyLENGTH*pos));
                    this.slide.left = left;
                    this.knob.style.left = left+"px";
                    this.knob.style.top = "0px";
                }

                // This function reads the slider and sets the overlay opacity level
                CTransparencyControl.prototype.setOpacity = function() {
                    // set the global variable
                    opacity = this.slide.left/CTransparencyLENGTH;
                    this.map.clearOverlays();
                    this.map.addOverlay(this.overlay, { zPriority: 0 });
                    if (this.map.getCurrentMapType() == G_HYBRID_MAP) {
                        this.map.addOverlay(hybridOverlay);
                    }
                }

                // This gets called by the API when addControl(new CTransparencyControl())
                CTransparencyControl.prototype.initialize = function(map) {
                    var that=this;
                    this.map = map;

                    // Is this MSIE, if so we need to use AlphaImageLoader
                    var agent = navigator.userAgent.toLowerCase();
                    if ((agent.indexOf("msie") > -1) && (agent.indexOf("opera") < 1)){this.ie = true} else {this.ie = false}

                    // create the background graphic as a <div> containing an image
                    var container = document.createElement("div");
                    container.style.width="70px";
                    container.style.height="21px";

                    // Handle transparent PNG files in MSIE
                    if (this.ie) {
                      var loader = "filter:progid:DXImageTransform.Microsoft.AlphaImageLoader(src='http://www.maptiler.org/img/opacity-slider.png', sizingMethod='crop');";
                      container.innerHTML = '<div style="height:21px; width:70px; ' +loader+ '" ></div>';
                    } else {
                      container.innerHTML = '<div style="height:21px; width:70px; background-image: url(http://www.maptiler.org/img/opacity-slider.png)" ></div>';
                    }

                    // create the knob as a GDraggableObject
                    // Handle transparent PNG files in MSIE
                    if (this.ie) {
                      var loader = "progid:DXImageTransform.Microsoft.AlphaImageLoader(src='http://www.maptiler.org/img/opacity-slider.png', sizingMethod='crop');";
                      this.knob = document.createElement("div");
                      this.knob.style.height="21px";
                      this.knob.style.width="13px";
                  this.knob.style.overflow="hidden";
                      this.knob_img = document.createElement("div");
                      this.knob_img.style.height="21px";
                      this.knob_img.style.width="83px";
                      this.knob_img.style.filter=loader;
                  this.knob_img.style.position="relative";
                  this.knob_img.style.left="-70px";
                      this.knob.appendChild(this.knob_img);
                    } else {
                      this.knob = document.createElement("div");
                      this.knob.style.height="21px";
                      this.knob.style.width="13px";
                      this.knob.style.backgroundImage="url(http://www.maptiler.org/img/opacity-slider.png)";
                      this.knob.style.backgroundPosition="-70px 0px";
                    }
                    container.appendChild(this.knob);
                    this.slide=new GDraggableObject(this.knob, {container:container});
                    this.slide.setDraggableCursor('pointer');
                    this.slide.setDraggingCursor('pointer');
                    this.container = container;

                    // attach the control to the map
                    map.getContainer().appendChild(container);

                    // init slider
                    this.setSlider(this.opacity);

                    // Listen for the slider being moved and set the opacity
                    GEvent.addListener(this.slide, "dragend", function() {that.setOpacity()});
                    //GEvent.addListener(this.container, "click", function( x, y ) { alert(x, y) });

                    return container;
                  }

                  // Set the default position for the control
                  CTransparencyControl.prototype.getDefaultPosition = function() {
                    return new GControlPosition(G_ANCHOR_TOP_RIGHT, new GSize(7, 47));
                  }

                /*
                 * Full-screen Window Resize
                 */

                function getWindowHeight() {
                    if (self.innerHeight) return self.innerHeight;
                    if (document.documentElement && document.documentElement.clientHeight)
                        return document.documentElement.clientHeight;
                    if (document.body) return document.body.clientHeight;
                    return 0;
                }

                function getWindowWidth() {
                    if (self.innerWidth) return self.innerWidth;
                    if (document.documentElement && document.documentElement.clientWidth)
                        return document.documentElement.clientWidth;
                    if (document.body) return document.body.clientWidth;
                    return 0;
                }

                function resize() {
                    var map = document.getElementById("map");
                    var header = document.getElementById("header");
                    var subheader = document.getElementById("subheader");
                    map.style.height = (getWindowHeight()-80) + "px";
                    map.style.width = (getWindowWidth()-20) + "px";
                    header.style.width = (getWindowWidth()-20) + "px";
                    subheader.style.width = (getWindowWidth()-20) + "px";
                    // map.checkResize();
                }


                /*
                 * Main load function:
                 */

                function load() {

                   if (GBrowserIsCompatible()) {

                      // Bug in the Google Maps: Copyright for Overlay is not correctly displayed
                      var gcr = GMapType.prototype.getCopyrights;
                      GMapType.prototype.getCopyrights = function(bounds,zoom) {
                          return ["%(copyright)s"].concat(gcr.call(this,bounds,zoom));
                      }

                      map = new GMap2( document.getElementById("map"), { backgroundColor: '#fff' } );

                      map.addMapType(G_PHYSICAL_MAP);
                      map.setMapType(G_PHYSICAL_MAP);

                      map.setCenter( mapBounds.getCenter(), map.getBoundsZoomLevel( mapBounds ));

                      hybridOverlay = new GTileLayerOverlay( G_HYBRID_MAP.getTileLayers()[1] );
                      GEvent.addListener(map, "maptypechanged", function() {
                        if (map.getCurrentMapType() == G_HYBRID_MAP) {
                            map.addOverlay(hybridOverlay);
                        } else {
                           map.removeOverlay(hybridOverlay);
                        }
                      } );

                      var tilelayer = new GTileLayer(GCopyrightCollection(''), mapMinZoom, mapMaxZoom);
                      var mercator = new GMercatorProjection(mapMaxZoom+1);
                      tilelayer.getTileUrl = function(tile,zoom) {
                          if ((zoom < mapMinZoom) || (zoom > mapMaxZoom)) {
                              return "http://www.maptiler.org/img/none.png";
                          }
                          var ymax = 1 << zoom;
                          var y = ymax - tile.y -1;
                          var tileBounds = new GLatLngBounds(
                              mercator.fromPixelToLatLng( new GPoint( (tile.x)*256, (tile.y+1)*256 ) , zoom ),
                              mercator.fromPixelToLatLng( new GPoint( (tile.x+1)*256, (tile.y)*256 ) , zoom )
                          );
                          if (mapBounds.intersects(tileBounds)) {
                              return zoom+"/"+tile.x+"/"+y+".png";
                          } else {
                              return "http://www.maptiler.org/img/none.png";
                          }
                      }
                      // IE 7-: support for PNG alpha channel
                      // Unfortunately, the opacity for whole overlay is then not changeable, either or...
                      tilelayer.isPng = function() { return true;};
                      tilelayer.getOpacity = function() { return opacity; }

                      overlay = new GTileLayerOverlay( tilelayer );
                      map.addOverlay(overlay);

                      map.addControl(new GLargeMapControl());
                      map.addControl(new GHierarchicalMapTypeControl());
                      map.addControl(new CTransparencyControl( overlay ));
        """ % args
        if self.kml:
            s += """
                      map.addMapType(G_SATELLITE_3D_MAP);
                      map.getEarthInstance(getEarthInstanceCB);
        """
        s += """

                      map.enableContinuousZoom();
                      map.enableScrollWheelZoom();

                      map.setMapType(G_HYBRID_MAP);
                   }
                   resize();
                }
        """
        if self.kml:
            s += """
                function getEarthInstanceCB(object) {
                   var ge = object;

                   if (ge) {
                       var url = document.location.toString();
                       url = url.substr(0,url.lastIndexOf('/'))+'/doc.kml';
                       var link = ge.createLink("");
                       if ("%(publishurl)s") { link.setHref("%(publishurl)s/doc.kml") }
                       else { link.setHref(url) };
                       var networkLink = ge.createNetworkLink("");
                       networkLink.setName("TMS Map Overlay");
                       networkLink.setFlyToView(true);
                       networkLink.setLink(link);
                       ge.getFeatures().appendChild(networkLink);
                   } else {
                       // alert("You should open a KML in Google Earth");
                       // add div with the link to generated KML... - maybe JavaScript redirect to the URL of KML?
                   }
                }
        """ % args
        s += """
                onresize=function(){ resize(); };

                //]]>
                </script>
              </head>
              <body onload="load()">
                  <div id="header"><h1>%(title)s</h1></div>
                  <div id="subheader">Generated by <a href="http://www.maptiler.org/">MapTiler</a>/<a href="http://www.klokan.cz/projects/GDAL2Mbtiles/">GDAL2Mbtiles</a>, Copyright &copy; 2008 <a href="http://www.klokan.cz/">Klokan Petr Pridal</a>,  <a href="http://www.gdal.org/">GDAL</a> &amp; <a href="http://www.osgeo.org/">OSGeo</a> <a href="http://code.google.com/soc/">GSoC</a>
            <!-- PLEASE, LET THIS NOTE ABOUT AUTHOR AND PROJECT SOMEWHERE ON YOUR WEBSITE, OR AT LEAST IN THE COMMENT IN HTML. THANK YOU -->
                  </div>
                   <div id="map"></div>
              </body>
            </html>
        """ % args

        return s

    # -------------------------------------------------------------------------
    def generate_leaflet(self):
        """
        Template for leaflet.html implementing overlay of tiles for 'mercator' profile.
        It returns filled string. Expected variables:
        title, north, south, east, west, minzoom, maxzoom, tilesize, tileformat, publishurl
        """

        args = {}
        args['title'] = self.options.title.replace('"', '\\"')
        args['htmltitle'] = self.options.title
        args['south'], args['west'], args['north'], args['east'] = self.swne
        args['centerlon'] = (args['north'] + args['south']) / 2.
        args['centerlat'] = (args['west'] + args['east']) / 2.
        args['minzoom'] = self.tminz
        args['maxzoom'] = self.tmaxz
        args['beginzoom'] = self.tmaxz
        args['tilesize'] = self.tilesize  # not used
        args['tileformat'] = self.tileext
        args['publishurl'] = self.options.url  # not used
        args['copyright'] = self.options.copyright.replace('"', '\\"')

        s = """<!DOCTYPE html>
        <html lang="en">
          <head>
            <meta charset="utf-8">
            <meta name='viewport' content='width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no' />
            <title>%(htmltitle)s</title>

            <!-- Leaflet -->
            <link rel="stylesheet" href="https://npmcdn.com/leaflet@0.7.7/dist/leaflet.css" />
            <script src="https://npmcdn.com/leaflet@0.7.7/dist/leaflet.js"></script>

            <style>
                body { margin:0; padding:0; }
                body, table, tr, td, th, div, h1, h2, input { font-family: "Calibri", "Trebuchet MS", "Ubuntu", Serif; font-size: 11pt; }
                #map { position:absolute; top:0; bottom:0; width:100%%; } /* full size */
                .ctl {
                    padding: 2px 10px 2px 10px;
                    background: white;
                    background: rgba(255,255,255,0.9);
                    box-shadow: 0 0 15px rgba(0,0,0,0.2);
                    border-radius: 5px;
                    text-align: right;
                }
                .title {
                    font-size: 18pt;
                    font-weight: bold;
                }
                .src {
                    font-size: 10pt;
                }

            </style>

        </head>
        <body>

        <div id="map"></div>

        <script>
        /* **** Leaflet **** */

        // Base layers
        //  .. OpenStreetMap
        var osm = L.tileLayer('http://{s}.tile.osm.org/{z}/{x}/{y}.png', {attribution: '&copy; <a href="http://osm.org/copyright">OpenStreetMap</a> contributors'});

        //  .. CartoDB Positron
        var cartodb = L.tileLayer('http://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png', {attribution: '&copy; <a href="http://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, &copy; <a href="http://cartodb.com/attributions">CartoDB</a>'});

        //  .. OSM Toner
        var toner = L.tileLayer('http://{s}.tile.stamen.com/toner/{z}/{x}/{y}.png', {attribution: 'Map tiles by <a href="http://stamen.com">Stamen Design</a>, under <a href="http://creativecommons.org/licenses/by/3.0">CC BY 3.0</a>. Data by <a href="http://openstreetmap.org">OpenStreetMap</a>, under <a href="http://www.openstreetmap.org/copyright">ODbL</a>.'});

        //  .. White background
        var white = L.tileLayer("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAQAAAAEAAQMAAABmvDolAAAAA1BMVEX///+nxBvIAAAAH0lEQVQYGe3BAQ0AAADCIPunfg43YAAAAAAAAAAA5wIhAAAB9aK9BAAAAABJRU5ErkJggg==");

        // Overlay layers (XYZ)
        var lyr = L.tileLayer('./{z}/{x}/{y}.%(tileformat)s', {tms: false, opacity: 1.0, attribution: "%(copyright)s"});

        // Map
        var map = L.map('map', {
            center: [%(centerlon)s, %(centerlat)s],
            zoom: %(beginzoom)s,
            minZoom: %(minzoom)s,
            maxZoom: %(maxzoom)s,
            layers: [osm]
        });

        var basemaps = {"OpenStreetMap": osm, "CartoDB Positron": cartodb, "Stamen Toner": toner, "Without background": white}
        var overlaymaps = {"Layer": lyr}

        // Title
        var title = L.control();
        title.onAdd = function(map) {
            this._div = L.DomUtil.create('div', 'ctl title');
            this.update();
            return this._div;
        };
        title.update = function(props) {
            this._div.innerHTML = "%(title)s";
        };
        title.addTo(map);

        // Note
        var src = 'Generated by <a href="http://www.klokan.cz/projects/GDAL2Mbtiles/">GDAL2Mbtiles</a>, Copyright &copy; 2008 <a href="http://www.klokan.cz/">Klokan Petr Pridal</a>,  <a href="http://www.gdal.org/">GDAL</a> &amp; <a href="http://www.osgeo.org/">OSGeo</a> <a href="http://code.google.com/soc/">GSoC</a>';
        var title = L.control({position: 'bottomleft'});
        title.onAdd = function(map) {
            this._div = L.DomUtil.create('div', 'ctl src');
            this.update();
            return this._div;
        };
        title.update = function(props) {
            this._div.innerHTML = src;
        };
        title.addTo(map);


        // Add base layers
        L.control.layers(basemaps, overlaymaps, {collapsed: false}).addTo(map);

        // Fit to overlay bounds (SW and NE points with (lat, lon))
        map.fitBounds([[%(south)s, %(east)s], [%(north)s, %(west)s]]);

        </script>

        </body>
        </html>

        """ % args

        return s

    # -------------------------------------------------------------------------
    def generate_index(self):
        """
        Template for leaflet.html implementing overlay of tiles for 'mercator' profile.
        It returns filled string. Expected variables:
        title, north, south, east, west, minzoom, maxzoom, tilesize, tileformat, publishurl
        """

        args = {}
        args['title'] = self.options.title.replace('"', '\\"')
        args['htmltitle'] = self.options.title
        args['south'], args['west'], args['north'], args['east'] = self.swne
        args['centerlat'] = (args['north'] + args['south']) / 2.
        args['centerlon'] = (args['west'] + args['east']) / 2.
        args['minzoom'] = self.tminz
        args['maxzoom'] = self.tmaxz
        args['beginzoom'] = self.tmaxz
        args['tilesize'] = self.tilesize  # not used
        args['tileformat'] = self.tileext
        args['publishurl'] = self.options.url  # not used
        args['copyright'] = self.options.copyright.replace('"', '\\"')

        s = """<!DOCTYPE html>
        <html>
          <head>
            <title>%(title)s</title>
            <meta charset="utf-8"/>
            <link rel="stylesheet" href="http://cdn.klokantech.com/tileviewer/v1/index.css" type="text/css"/>
            <script src="http://cdn.klokantech.com/tileviewer/v1/index.js"></script>
          </head>
          <body>
            <script type="text/javascript">
              // tilejson based on https://github.com/mapbox/tilejson-spec/tree/master/2.1.0
              var data = {
                    "tilejson": "2.1.0",
                    "name": "%(title)s",
                    "description": "%(htmltitle)s",
                    "version": "1.0.0",
                    "attribution": "%(copyright)s",
                    "template": "",
                    "legend": "",
                    "scheme": "xyz",
                    "tiles": ["./{z}/{x}/{y}.%(tileformat)s"],
                    "grids": [],
                    "data": [],
                    "type": "overlay",
                    "format": "%(tileformat)s",
                    "center": [%(centerlon)s, %(centerlat)s, %(beginzoom)s],
                    "minzoom": "%(minzoom)s",
                    "maxzoom": "%(maxzoom)s",
                    "bounds": "%(west)s,%(south)s,%(east)s,%(north)s",
                    "scale": "1.000000",
                    "profile": "mercator"
              };
              tileserver(data);
            </script>
          </body>
        </html>

        """ % args

        return s

    # -------------------------------------------------------------------------
    def generate_metadatajson(self):
        """
        Template for metadata.json implementing overlay of tiles for 'mercator' profile.
        It returns filled string. Expected variables:

        """

        args = {}
        args['title'] = self.options.title.replace('"', '\\"')
        args['htmltitle'] = self.options.title
        args['south'], args['west'], args['north'], args['east'] = self.swne
        args['centerlat'] = (args['north'] + args['south']) / 2.
        args['centerlon'] = (args['west'] + args['east']) / 2.
        args['minzoom'] = self.tminz
        args['maxzoom'] = self.tmaxz
        args['beginzoom'] = self.tmaxz
        args['tilesize'] = self.tilesize  # not used
        args['tileformat'] = self.tileext
        args['publishurl'] = self.options.url  # not used
        args['copyright'] = self.options.copyright.replace('"', '\\"')

        s = {
            "name": args['title'],
            "description": args['htmltitle'],
            "version": "1.0.0",
            "attribution": args['copyright'],
            "type": "overlay",
            "format": args['tileformat'],
            "minzoom": args['minzoom'],
            "maxzoom": args['maxzoom'],
            "bounds": str(args['south']) + " " + str(args['west']) + " " + str(args['north']) + " " + str(args['east']),
            "scale": "1",
            "profile": "mercator"
        }
        return s

    # -------------------------------------------------------------------------
    def generate_openlayers(self):
        """
        Template for openlayers.html implementing overlay of available Spherical Mercator layers.

        It returns filled string. Expected variables:
        title, googlemapskey, yahooappid, north, south, east, west, minzoom, maxzoom, tilesize, tileformat, publishurl
        """

        args = {}
        args['title'] = self.options.title
        args['googlemapskey'] = self.options.googlekey
        args['yahooappid'] = self.options.yahookey
        args['south'], args['west'], args['north'], args['east'] = self.swne
        args['minzoom'] = self.tminz
        args['maxzoom'] = self.tmaxz
        args['tilesize'] = self.tilesize
        args['tileformat'] = self.tileext
        args['publishurl'] = self.options.url
        args['copyright'] = self.options.copyright
        if self.options.profile == 'raster':
            args['rasterzoomlevels'] = self.tmaxz + 1
            args['rastermaxresolution'] = 2 ** (self.nativezoom) * self.out_gt[1]

        s = """<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">
        <html xmlns="http://www.w3.org/1999/xhtml"
          <head>
            <title>%(title)s</title>
            <meta http-equiv='imagetoolbar' content='no'/>
            <style type="text/css"> v\:* {behavior:url(#default#VML);}
                html, body { overflow: hidden; padding: 0; height: 100%%; width: 100%%; font-family: 'Lucida Grande',Geneva,Arial,Verdana,sans-serif; }
                body { margin: 10px; background: #fff; }
                h1 { margin: 0; padding: 6px; border:0; font-size: 20pt; }
                #header { height: 43px; padding: 0; background-color: #eee; border: 1px solid #888; }
                #subheader { height: 12px; text-align: right; font-size: 10px; color: #555;}
                #map { height: 95%%; border: 1px solid #888; }
            </style>""" % args

        if self.options.profile == 'mercator':
            s += """
            <script src='http://dev.virtualearth.net/mapcontrol/mapcontrol.ashx?v=6.1'></script>
            <script src='http://maps.google.com/maps?file=api&amp;v=2&amp;key=%(googlemapskey)s' type='text/javascript'></script>
            <script src="http://api.maps.yahoo.com/ajaxymap?v=3.0&amp;appid=%(yahooappid)s"></script>""" % args

        s += """
            <script src="http://www.openlayers.org/api/2.7/OpenLayers.js" type="text/javascript"></script>
            <script type="text/javascript">
                var map;
                var mapBounds = new OpenLayers.Bounds( %(west)s, %(south)s, %(east)s, %(north)s);
                var mapMinZoom = %(minzoom)s;
                var mapMaxZoom = %(maxzoom)s;

                // avoid pink tiles
                OpenLayers.IMAGE_RELOAD_ATTEMPTS = 3;
                OpenLayers.Util.onImageLoadErrorColor = "transparent";

                function init(){""" % args

        if self.options.profile == 'mercator':
            s += """
                var options = {
                    controls: [],
                    projection: new OpenLayers.Projection("EPSG:900913"),
                    displayProjection: new OpenLayers.Projection("EPSG:4326"),
                    units: "m",
                    maxResolution: 156543.0339,
                    maxExtent: new OpenLayers.Bounds(-20037508, -20037508, 20037508, 20037508.34)
                    };
                map = new OpenLayers.Map('map', options);

                // create Google Mercator layers
                var gmap = new OpenLayers.Layer.Google("Google Streets",
                    { sphericalMercator: true, numZoomLevels: 20} );
                var gsat = new OpenLayers.Layer.Google("Google Satellite",
                    {type: G_SATELLITE_MAP, sphericalMercator: true, numZoomLevels: 20} );
                var ghyb = new OpenLayers.Layer.Google("Google Hybrid",
                    {type: G_HYBRID_MAP, sphericalMercator: true, numZoomLevels: 20});
                var gter = new OpenLayers.Layer.Google("Google Terrain",
                    {type: G_PHYSICAL_MAP, sphericalMercator: true, numZoomLevels: 20 });

                // create Virtual Earth layers
                OpenLayers.Layer.VirtualEarth.prototype.MAX_ZOOM_LEVEL=19;
                OpenLayers.Layer.VirtualEarth.prototype.RESOLUTIONS=OpenLayers.Layer.Google.prototype.RESOLUTIONS
                var veroad = new OpenLayers.Layer.VirtualEarth("Virtual Earth Roads",
                    {'type': VEMapStyle.Road, 'sphericalMercator': true, numZoomLevels: 20});
                var veaer = new OpenLayers.Layer.VirtualEarth("Virtual Earth Aerial",
                    {'type': VEMapStyle.Aerial, 'sphericalMercator': true, numZoomLevels: 20 });
                var vehyb = new OpenLayers.Layer.VirtualEarth("Virtual Earth Hybrid",
                    {'type': VEMapStyle.Hybrid, 'sphericalMercator': true});

                // create Yahoo layer
                var yahoo = new OpenLayers.Layer.Yahoo("Yahoo Street",
                    {'sphericalMercator': true});
                var yahoosat = new OpenLayers.Layer.Yahoo("Yahoo Satellite",
                    {'type': YAHOO_MAP_SAT, 'sphericalMercator': true});
                var yahoohyb = new OpenLayers.Layer.Yahoo("Yahoo Hybrid",
                    {'type': YAHOO_MAP_HYB, 'sphericalMercator': true});

                // create OSM/OAM layer
                var osm = new OpenLayers.Layer.TMS( "OpenStreetMap",
                    "http://tile.openstreetmap.org/",
                    { type: 'png', getURL: osm_getTileURL, displayOutsideMaxExtent: true, attribution: '<a href="http://www.openstreetmap.org/">OpenStreetMap</a>'} );
                var oam = new OpenLayers.Layer.TMS( "OpenAerialMap",
                    "http://tile.openaerialmap.org/tiles/1.0.0/openaerialmap-900913/",
                    { type: 'png', getURL: osm_getTileURL } );

                // create TMS Overlay layer
                var tmsoverlay = new OpenLayers.Layer.TMS( "TMS Overlay", "",
                    {   // url: '', serviceVersion: '.', layername: '.',
                        type: 'png', getURL: overlay_getTileURL, alpha: true,
                        isBaseLayer: false
                    });
                if (OpenLayers.Util.alphaHack() == false) { tmsoverlay.setOpacity(0.7); }

                map.addLayers([gmap, gsat, ghyb, gter, veroad, veaer, vehyb,
                               yahoo, yahoosat, yahoohyb, osm, oam,
                               tmsoverlay]);

                var switcherControl = new OpenLayers.Control.LayerSwitcher();
                map.addControl(switcherControl);
                switcherControl.maximizeControl();

                map.zoomToExtent( mapBounds.transform(map.displayProjection, map.projection ) );
            """ % args

        elif self.options.profile == 'geodetic':
            s += """
                var options = {
                    controls: [],
                    projection: new OpenLayers.Projection("EPSG:4326"),
                    maxResolution: 0.703125,
                    maxExtent: new OpenLayers.Bounds(-180, -90, 180, 90)
                    };
                map = new OpenLayers.Map('map', options);

                layer = new OpenLayers.Layer.WMS( "Blue Marble",
                        "http://labs.metacarta.com/wms-c/Basic.py?", {layers: 'satellite' } );
                map.addLayer(layer);
                wms = new OpenLayers.Layer.WMS( "VMap0",
                        "http://labs.metacarta.com/wms-c/Basic.py?", {layers: 'basic', format: 'image/png' } );
                map.addLayer(wms);

                var tmsoverlay = new OpenLayers.Layer.TMS( "TMS Overlay", "",
                    {
                        serviceVersion: '.', layername: '.', alpha: true,
                        type: 'png', getURL: overlay_getTileURL,
                        isBaseLayer: false
                    });
                map.addLayer(tmsoverlay);
                if (OpenLayers.Util.alphaHack() == false) { tmsoverlay.setOpacity(0.7); }

                var switcherControl = new OpenLayers.Control.LayerSwitcher();
                map.addControl(switcherControl);
                switcherControl.maximizeControl();

                map.zoomToExtent( mapBounds );
            """

        elif self.options.profile == 'raster':
            s += """
                var options = {
                    controls: [],
                    maxExtent: new OpenLayers.Bounds(  %(west)s, %(south)s, %(east)s, %(north)s ),
                    maxResolution: %(rastermaxresolution)f,
                    numZoomLevels: %(rasterzoomlevels)d
                    };
                map = new OpenLayers.Map('map', options);

                var layer = new OpenLayers.Layer.TMS( "TMS Layer","",
                    {  url: '', serviceVersion: '.', layername: '.', alpha: true,
                        type: 'png', getURL: overlay_getTileURL
                    });
                map.addLayer(layer);
                map.zoomToExtent( mapBounds );
        """ % args

        s += """
                map.addControl(new OpenLayers.Control.PanZoomBar());
                map.addControl(new OpenLayers.Control.MousePosition());
                map.addControl(new OpenLayers.Control.MouseDefaults());
                map.addControl(new OpenLayers.Control.KeyboardDefaults());
            }
            """ % args

        if self.options.profile == 'mercator':
            s += """
            function osm_getTileURL(bounds) {
                var res = this.map.getResolution();
                var x = Math.round((bounds.left - this.maxExtent.left) / (res * this.tileSize.w));
                var y = Math.round((this.maxExtent.top - bounds.top) / (res * this.tileSize.h));
                var z = this.map.getZoom();
                var limit = Math.pow(2, z);

                if (y < 0 || y >= limit) {
                    return "http://www.maptiler.org/img/none.png";
                } else {
                    x = ((x %% limit) + limit) %% limit;
                    return this.url + z + "/" + x + "/" + y + "." + this.type;
                }
            }

            function overlay_getTileURL(bounds) {
                var res = this.map.getResolution();
                var x = Math.round((bounds.left - this.maxExtent.left) / (res * this.tileSize.w));
                var y = Math.round((bounds.bottom - this.tileOrigin.lat) / (res * this.tileSize.h));
                var z = this.map.getZoom();
                if (this.map.baseLayer.name == 'Virtual Earth Roads' || this.map.baseLayer.name == 'Virtual Earth Aerial' || this.map.baseLayer.name == 'Virtual Earth Hybrid') {
                   z = z + 1;
                }
                if (mapBounds.intersectsBounds( bounds ) && z >= mapMinZoom && z <= mapMaxZoom ) {
                   //console.log( this.url + z + "/" + x + "/" + y + "." + this.type);
                   return this.url + z + "/" + x + "/" + y + "." + this.type;
                } else {
                   return "http://www.maptiler.org/img/none.png";
                }
            }
            """ % args

        elif self.options.profile == 'geodetic':
            s += """
            function overlay_getTileURL(bounds) {
                bounds = this.adjustBounds(bounds);
                var res = this.map.getResolution();
                var x = Math.round((bounds.left - this.tileOrigin.lon) / (res * this.tileSize.w));
                var y = Math.round((bounds.bottom - this.tileOrigin.lat) / (res * this.tileSize.h));
                var z = this.map.getZoom();
                var path = this.serviceVersion + "/" + this.layername + "/" + z + "/" + x + "/" + y + "." + this.type;
                var url = this.url;
                if (mapBounds.intersectsBounds( bounds ) && z >= mapMinZoom && z <= mapMaxZoom) {
                   // console.log( this.url + z + "/" + x + "/" + y + "." + this.type);
                   return this.url + z + "/" + x + "/" + y + "." + this.type;
                } else {
                   return "http://www.maptiler.org/img/none.png";
                }
            }
            """ % args

        elif self.options.profile == 'raster':
            s += """
            function overlay_getTileURL(bounds) {
                var res = this.map.getResolution();
                var x = Math.round((bounds.left - this.maxExtent.left) / (res * this.tileSize.w));
                var y = Math.round((bounds.bottom - this.maxExtent.bottom) / (res * this.tileSize.h));
                var z = this.map.getZoom();
                if (x >= 0 && y >= 0) {
                    return this.url + z + "/" + x + "/" + y + "." + this.type;
                } else {
                    return "http://www.maptiler.org/img/none.png";
                }
            }
            """ % args

        s += """
           function getWindowHeight() {
                if (self.innerHeight) return self.innerHeight;
                if (document.documentElement && document.documentElement.clientHeight)
                    return document.documentElement.clientHeight;
                if (document.body) return document.body.clientHeight;
                    return 0;
            }

            function getWindowWidth() {
                if (self.innerWidth) return self.innerWidth;
                if (document.documentElement && document.documentElement.clientWidth)
                    return document.documentElement.clientWidth;
                if (document.body) return document.body.clientWidth;
                    return 0;
            }

            function resize() {
                var map = document.getElementById("map");
                var header = document.getElementById("header");
                var subheader = document.getElementById("subheader");
                map.style.height = (getWindowHeight()-80) + "px";
                map.style.width = (getWindowWidth()-20) + "px";
                header.style.width = (getWindowWidth()-20) + "px";
                subheader.style.width = (getWindowWidth()-20) + "px";
                if (map.updateSize) { map.updateSize(); };
            }

            onresize=function(){ resize(); };

            </script>
          </head>
          <body onload="init()">
            <div id="header"><h1>%(title)s</h1></div>
            <div id="subheader">Generated by <a href="http://www.maptiler.org/">MapTiler</a>/<a href="http://www.klokan.cz/projects/GDAL2Mbtiles/">GDAL2Mbtiles</a>, Copyright &copy; 2008 <a href="http://www.klokan.cz/">Klokan Petr Pridal</a>,  <a href="http://www.gdal.org/">GDAL</a> &amp; <a href="http://www.osgeo.org/">OSGeo</a> <a href="http://code.google.com/soc/">GSoC</a>
            <!-- PLEASE, LET THIS NOTE ABOUT AUTHOR AND PROJECT SOMEWHERE ON YOUR WEBSITE, OR AT LEAST IN THE COMMENT IN HTML. THANK YOU -->
            </div>
            <div id="map"></div>
            <script type="text/javascript" >resize()</script>
          </body>
        </html>""" % args

        return s

    # -------------------------------------------------------
    """Methods for work with mbtiles"""

    # -------------------------------------------------------


    def mbtiles_connect(self):
        try:
            con = sqlite3.connect(self.output, timeout=30)
            self.optimize_connection(con.cursor())
            return con
        except Exception as e:
            sys.exit(1)

    def mbtiles_setup(self, cur):
        cur.execute("""
            CREATE TABLE tiles (
                zoom_level integer,
                tile_column integer,
                tile_row integer,
                tile_data blob);
                """)
        cur.execute("""CREATE TABLE metadata
            (name text, value text);""")
        cur.execute("""CREATE TABLE grids (zoom_level integer, tile_column integer,
        tile_row integer, grid blob);""")
        cur.execute("""CREATE TABLE grid_data (zoom_level integer, tile_column
        integer, tile_row integer, key_name text, key_json text);""")

    def create_index(slef, cur):
        cur.execute("""create unique index name on metadata (name);""")
        cur.execute("""create unique index tile_index on tiles
            (zoom_level, tile_column, tile_row);""")

    def optimize_connection(self, cur):
        cur.execute("""PRAGMA synchronous=OFF;""")
        # cur.execute("""PRAGMA journal_mode=DELETE""")
        # cur.execute("""PRAGMA journal_mode=WAL""")
        cur.execute("""PRAGMA journal_mode=OFF;""")
        cur.execute("""PRAGMA cache_size=-2000;""")
        cur.execute("""PRAGMA page_size=65536;""")
        cur.execute("""PRAGMA foreign_keys=1;""")

    # -------------------------------------------------------
    """Methods for work with Progressbar"""

    # -------------------------------------------------------
class ProgressBar(QtCore.QObject):

    # pbar_signal variable which will emit count of processed tiles to GUI
    pbar_signal = QtCore.pyqtSignal(int)

    def progress_emiter(self, maxz, minz, processed_tiles, total, overview=False):
        base_level = float(100) / (maxz - minz+1)
        if not overview:
            multiplyer = (base_level)/float(total)
            self.pbar_signal.emit(int(processed_tiles * multiplyer))
        else:  # overview tiles
            level_percent =100- base_level
            # level_percent = base_level
            multiplyer = (level_percent)/float(total)
            self.pbar_signal.emit(int(base_level + processed_tiles * multiplyer))

# =============================================================================
# =============================================================================
# =============================================================================

def worker_metadata(gdal2mbtiles):
    gdal2mbtiles.open_input()
    con = gdal2mbtiles.mbtiles_connect()
    if not gdal2mbtiles.options.resume:
        cur = con.cursor()
        gdal2mbtiles.mbtiles_setup(cur)
        gdal2mbtiles.generate_metadata(cur)
        con.commit()
    con.close()
    sys.stdout.flush()


def worker_base_tiles(argv, cpu, queue):
    gdal2mbtiles = GDAL2Mbtiles(argv[1:])
    gdal2mbtiles.open_input()
    con = gdal2mbtiles.mbtiles_connect()
    gdal2mbtiles.generate_base_tiles(cpu, queue, con)
    con.close()


def worker_overview_tiles(argv, cpu, tz, queue):
    gdal2mbtiles = GDAL2Mbtiles(argv[1:])
    gdal2mbtiles.open_input()
    con = gdal2mbtiles.mbtiles_connect()
    gdal2mbtiles.generate_overview_tiles(cpu, tz, queue, con)
    con.close()


def timing_val(func):
    def wrapper(*arg, **kw):
        t1 = time.time()
        func(*arg, **kw)
        t2 = time.time()
        return (t2 - t1)

    return wrapper


@timing_val
def main(progress,argv = None):
    queue = multiprocessing.Queue()
    # progress = ProgressBar()
    if not argv:
        argv = gdal.GeneralCmdLineProcessor(sys.argv)
        # progress = ProgressBar()

    gdal2mbtiles = GDAL2Mbtiles(argv[1:])  # handle command line options
    proc_count = gdal2mbtiles.options.processes
    if gdal2mbtiles.options.aux_files:
        gdal.SetConfigOption("GDAL_PAM_ENABLED", "YES")
    else:
        gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")
    p = multiprocessing.Process(target=worker_metadata, args=[gdal2mbtiles])
    p.start()
    p.join()
    print("Generating Base Tiles:")
    tminz = gdal2mbtiles.tminz
    tmaxz = gdal2mbtiles.tmaxz
    procs = []
    for cpu in range(proc_count):
        proc = multiprocessing.Process(target=worker_base_tiles, args=(argv, cpu, queue,))
        proc.daemon = True
        proc.start()
        procs.append(proc)
    processed_tiles = 0
    while len(multiprocessing.active_children()):
        try:
            total = queue.get(timeout=1)
            processed_tiles += 1
            progress.progress_emiter(tmaxz,tminz,processed_tiles,total)
            gdal2mbtiles.progressbar(processed_tiles / float(total))
            sys.stdout.flush()
        except:
            pass
    [p.join(timeout=1) for p in procs]
    print("\n")
    print("Generating Overview Tiles:")
    #  Values generated after base tiles creation

    processed_tiles = 0
    for tz in range(tmaxz - 1, tminz - 1, -1):
        for cpu in range(proc_count):
            proc = multiprocessing.Process(target=worker_overview_tiles, args=(argv, cpu % proc_count, tz, queue))
            proc.daemon = True
            proc.start()
            procs.append(proc)
        while len(multiprocessing.active_children()):
            try:
                total = queue.get(timeout=1)
                processed_tiles += 1
                progress.progress_emiter(tmaxz, tminz, processed_tiles, total, overview=True)
                gdal2mbtiles.progressbar(processed_tiles / float(total))
                sys.stdout.flush()
            except:
                pass
        [p.join(timeout=1) for p in procs]

    con = gdal2mbtiles.mbtiles_connect()
    if not gdal2mbtiles.options.resume:
        print('Indexing tiles')
        gdal2mbtiles.create_index(con.cursor())
    con.execute('''PRAGMA journal_mode=DELETE''')


if __name__ == '__main__':
    progress = ProgressBar()
    t = main(progress)
    print ('Tiling took: {:.2f} seconds '.format(t))
