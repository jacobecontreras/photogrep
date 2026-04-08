"""
Photo Metadata Extraction Module

Extracts GPS, date, media type, and other metadata from Photos.sqlite
and EXIF data for images in an iOS backup.
"""

import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS, IFD

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

logger = logging.getLogger(__name__)

# Core Data epoch: 2001-01-01 00:00:00 UTC
_CORE_DATA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_MIN_SANE_DATE = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MAX_SANE_DATE = datetime(2100, 1, 1, tzinfo=timezone.utc)


def _convert_core_data_timestamp(ts) -> Optional[str]:
    """Convert Core Data epoch (seconds since 2001-01-01) to ISO 8601 string.

    Returns None for invalid or out-of-range timestamps.
    """
    if ts is None:
        return None
    try:
        ts = float(ts)
        dt = _CORE_DATA_EPOCH + timedelta(seconds=ts)
        if dt < _MIN_SANE_DATE or dt > _MAX_SANE_DATE:
            return None
        return dt.isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def _map_media_type(kind, subtype, uti) -> Optional[str]:
    """Map ZKIND/ZKINDSUBTYPE integers to a human-readable media_type string."""
    if uti and "screenshot" in str(uti).lower():
        return "screenshot"
    if kind == 1:
        return "video"
    if subtype == 1:
        return "panorama"
    if subtype == 2:
        return "live_photo"
    if kind == 0:
        return "photo"
    return None


def _convert_gps_to_decimal(gps_info: dict) -> tuple:
    """Convert GPS IFD data to decimal (latitude, longitude).

    Returns (lat, lon) as floats, or (None, None) on failure.
    """
    try:
        def _to_degrees(value):
            """Convert GPS coordinate tuple (degrees, minutes, seconds) to decimal."""
            d, m, s = value
            return float(d) + float(m) / 60.0 + float(s) / 3600.0

        lat = _to_degrees(gps_info[2])
        lon = _to_degrees(gps_info[4])

        if gps_info.get(1) == "S":
            lat = -lat
        if gps_info.get(3) == "W":
            lon = -lon

        return lat, lon
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError):
        return None, None


def _extract_exif(image_path: str) -> dict:
    """Extract metadata from an image file using Pillow EXIF.

    Returns a metadata dict with the unified schema keys populated where available.
    """
    meta = {
        "latitude": None,
        "longitude": None,
        "date_created": None,
        "media_type": None,
        "favorite": None,
        "hidden": None,
        "trashed": None,
        "camera_make": None,
        "camera_model": None,
        "lens_model": None,
        "original_filename": None,
        "source_db": "exif",
    }

    try:
        img = Image.open(image_path)
        exif = img.getexif()
        if not exif:
            return meta

        # Camera make — tag 271, Camera model — tag 272
        make = exif.get(271)
        if make:
            meta["camera_make"] = str(make).strip()
        model = exif.get(272)
        if model:
            meta["camera_model"] = str(model).strip()

        # EXIF IFD — DateTimeOriginal (36867), LensModel (42036)
        try:
            exif_ifd = exif.get_ifd(IFD.Exif)
            date_str = exif_ifd.get(36867)
            if date_str:
                dt = datetime.strptime(str(date_str), "%Y:%m:%d %H:%M:%S")
                meta["date_created"] = dt.isoformat()
            lens = exif_ifd.get(42036)
            if lens:
                meta["lens_model"] = str(lens).strip()
        except Exception:
            pass

        # GPS IFD
        try:
            gps_ifd = exif.get_ifd(IFD.GPSInfo)
            if gps_ifd:
                lat, lon = _convert_gps_to_decimal(gps_ifd)
                if lat is not None and lon is not None:
                    meta["latitude"] = round(lat, 6)
                    meta["longitude"] = round(lon, 6)
        except Exception:
            pass

    except Exception as e:
        logger.debug(f"EXIF extraction failed for {image_path}: {e}")

    return meta


def _query_photos_sqlite(conn: sqlite3.Connection) -> list:
    """Query ZASSET joined with ZADDITIONALASSETATTRIBUTES, returning list of row dicts.

    Uses PRAGMA table_info to discover available columns before building
    the SELECT statement. Joins ZADDITIONALASSETATTRIBUTES for camera/lens info.
    """
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(ZASSET)")
        asset_cols = {row[1] for row in cursor.fetchall()}
    except Exception as e:
        logger.warning(f"Could not read ZASSET schema: {e}")
        return []

    if "ZFILENAME" not in asset_cols or "ZDIRECTORY" not in asset_cols:
        logger.warning("ZASSET missing ZFILENAME or ZDIRECTORY — cannot match photos")
        return []

    # Discover ZADDITIONALASSETATTRIBUTES columns
    addl_cols = set()
    has_addl_table = False
    try:
        cursor.execute("PRAGMA table_info(ZADDITIONALASSETATTRIBUTES)")
        addl_cols = {row[1] for row in cursor.fetchall()}
        has_addl_table = len(addl_cols) > 0
    except Exception:
        pass

    # Base ZASSET columns (always expected)
    asset_base = ["ZFILENAME", "ZDIRECTORY", "ZLATITUDE", "ZLONGITUDE", "ZDATECREATED"]
    # Optional ZASSET columns
    asset_optional = [
        "ZKIND", "ZKINDSUBTYPE", "ZFAVORITE", "ZHIDDEN",
        "ZTRASHEDSTATE", "ZUNIFORMTYPEIDENTIFIER", "ZDURATION",
    ]

    select_parts = []
    for col in asset_base:
        if col in asset_cols:
            select_parts.append(f"A.{col}")
        else:
            logger.debug(f"ZASSET missing expected column: {col}")

    for col in asset_optional:
        if col in asset_cols:
            select_parts.append(f"A.{col}")

    # ZADDITIONALASSETATTRIBUTES columns we care about
    addl_wanted = [
        "ZCAMERAMAKE", "ZCAMERAMODEL", "ZLENSMODEL",
        "ZORIGINALFILENAME", "ZEXIFTIMESTAMPSTRING",
    ]
    joined_addl = []
    for col in addl_wanted:
        if col in addl_cols:
            select_parts.append(f"AA.{col}")
            joined_addl.append(col)

    query = f"SELECT {', '.join(select_parts)} FROM ZASSET A"
    if has_addl_table and joined_addl:
        query += " LEFT JOIN ZADDITIONALASSETATTRIBUTES AA ON AA.ZASSET = A.Z_PK"

    try:
        cursor.execute(query)
        col_names = [desc[0] for desc in cursor.description]
        rows = []
        for row in cursor.fetchall():
            rows.append(dict(zip(col_names, row)))
        return rows
    except Exception as e:
        logger.warning(f"Failed to query ZASSET: {e}")
        return []


def extract_photo_metadata(parser, manifest: dict, output_dir) -> dict:
    """Extract photo metadata from Photos.sqlite and EXIF.

    Args:
        parser: iOSBackupParser instance.
        manifest: dict mapping file_id -> {relative_path, domain, ...}.
        output_dir: Path to the extraction output directory.

    Returns:
        dict mapping file_id -> metadata dict.
    """
    output_dir = Path(output_dir)
    result = {}

    # Step 1: Try to open Photos.sqlite and build directory+filename -> metadata mapping
    db_meta = {}
    try:
        conn = parser.open_backup_db("CameraRollDomain", "Media/PhotoData/Photos.sqlite")
        if conn:
            try:
                rows = _query_photos_sqlite(conn)
                for row in rows:
                    directory = row.get("ZDIRECTORY")
                    filename = row.get("ZFILENAME")
                    if not directory or not filename:
                        continue

                    key = f"{directory}/{filename}"

                    meta = {
                        "latitude": None,
                        "longitude": None,
                        "date_created": None,
                        "media_type": None,
                        "favorite": None,
                        "hidden": None,
                        "trashed": None,
                        "camera_make": None,
                        "camera_model": None,
                        "lens_model": None,
                        "original_filename": None,
                        "source_db": "photos.sqlite",
                    }

                    lat = row.get("ZLATITUDE")
                    lon = row.get("ZLONGITUDE")
                    if lat is not None and lon is not None and (lat != 0 or lon != 0):
                        meta["latitude"] = round(float(lat), 6)
                        meta["longitude"] = round(float(lon), 6)

                    meta["date_created"] = _convert_core_data_timestamp(row.get("ZDATECREATED"))

                    kind = row.get("ZKIND")
                    subtype = row.get("ZKINDSUBTYPE")
                    uti = row.get("ZUNIFORMTYPEIDENTIFIER")
                    meta["media_type"] = _map_media_type(kind, subtype, uti)

                    if "ZFAVORITE" in row:
                        val = row["ZFAVORITE"]
                        meta["favorite"] = bool(val) if val is not None else None
                    if "ZHIDDEN" in row:
                        val = row["ZHIDDEN"]
                        meta["hidden"] = bool(val) if val is not None else None
                    if "ZTRASHEDSTATE" in row:
                        val = row["ZTRASHEDSTATE"]
                        meta["trashed"] = bool(val) if val is not None else None

                    # ZADDITIONALASSETATTRIBUTES fields
                    camera_make = row.get("ZCAMERAMAKE")
                    camera_model = row.get("ZCAMERAMODEL")
                    if camera_make:
                        meta["camera_make"] = str(camera_make).strip()
                    if camera_model:
                        meta["camera_model"] = str(camera_model).strip()
                    lens = row.get("ZLENSMODEL")
                    if lens:
                        meta["lens_model"] = str(lens).strip()
                    orig_name = row.get("ZORIGINALFILENAME")
                    if orig_name:
                        meta["original_filename"] = str(orig_name).strip()

                    db_meta[key] = meta

                logger.info(f"Photos.sqlite: loaded metadata for {len(db_meta)} assets")
            finally:
                conn.close()
    except Exception as e:
        logger.warning(f"Could not open Photos.sqlite: {e}")

    # Step 2: Walk manifest and match
    for file_id, info in manifest.items():
        rel_path = info.get("relative_path", "")
        domain = info.get("domain", "")

        # Try to match Camera Roll images via ZDIRECTORY/ZFILENAME suffix
        matched = False
        if db_meta and rel_path:
            for db_key, meta in db_meta.items():
                if rel_path.endswith(db_key):
                    result[file_id] = meta
                    matched = True
                    break

        # For non-Camera Roll or unmatched images, try EXIF from extracted file
        if not matched:
            try:
                # Find the extracted file on disk by file_id
                candidates = list(output_dir.glob(f"{file_id}.*"))
                # Also check _deep subdirectory
                candidates.extend(output_dir.glob(f"_deep/{file_id}.*"))
                for candidate in candidates:
                    if candidate.is_file() and candidate.suffix.lower() in {
                        '.jpg', '.jpeg', '.png', '.heic', '.heif', '.tiff', '.tif', '.dng',
                    }:
                        meta = _extract_exif(str(candidate))
                        # Only include if we got at least some data
                        if meta.get("date_created") or meta.get("latitude") or meta.get("camera_model"):
                            result[file_id] = meta
                        break
            except Exception as e:
                logger.debug(f"EXIF fallback failed for {file_id}: {e}")

    logger.info(f"Photo metadata extracted: {len(result)} entries "
                f"({sum(1 for m in result.values() if m.get('source_db') == 'photos.sqlite')} from DB, "
                f"{sum(1 for m in result.values() if m.get('source_db') == 'exif')} from EXIF)")
    return result
