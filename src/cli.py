#!/usr/bin/env python3
import argparse
import getpass
import json
import time
from pathlib import Path

from .ios_backup import (
    iOSBackupParser,
    iOSBackupDecryptor,
    is_image_file,
    check_encryption_status,
    get_backup_device_name,
)

DEFAULT_OUTPUT = "output_images"


def decrypt_backup(backup_path: Path, password: str) -> tuple:
    decryptor = iOSBackupDecryptor(str(backup_path))
    result = decryptor.decrypt_with_password(password)

    if not result.success:
        print(f"Decryption failed: {result.message}")
        return None, None

    return result.manifest_key, result.protection_classes


def cmd_extract(args):
    """Extract images from iOS backup."""
    backup_path = Path(args.backup_path)
    base_output = Path(args.output)

    if not (backup_path / "Manifest.plist").exists():
        print(f"Error: No Manifest.plist found at {backup_path}")
        print("Make sure this is a valid iOS backup directory.")
        return

    # Create per-device output directory
    device_name = get_backup_device_name(backup_path)
    output_path = base_output / device_name
    output_path.mkdir(parents=True, exist_ok=True)

    is_encrypted = check_encryption_status(backup_path)

    if is_encrypted:
        password = args.password
        if not password:
            password = getpass.getpass("Enter backup password: ")

        unwrapped_manifest_key, protection_classes = decrypt_backup(backup_path, password)
        if unwrapped_manifest_key is None:
            return

        parser = iOSBackupParser(
            str(backup_path),
            unwrapped_manifest_key=unwrapped_manifest_key,
            protection_classes=protection_classes
        )

        all_files = parser.get_all_files(filter_images=False)
        image_files = [f for f in all_files if is_image_file(f, parser)]

    else:
        parser = iOSBackupParser(
            str(backup_path),
        )

        image_files = parser.get_all_files(filter_images=True)

    if not image_files:
        print("No images found in backup.")
        parser.cleanup()
        return

    print(f"Found {len(image_files)} image(s).")
    print(f"Extracting to: {output_path}")

    def progress_callback(current: int, total: int, filename: str):
        percent = int((current / total) * 100) if total > 0 else 0
        print(f"\r  Extracting: {current}/{total} ({percent}%) - {filename[:50]}", end='', flush=True)

    parser.extract_files(
        str(output_path),
        backup_files=image_files,
        progress_callback=progress_callback
    )
    print()

    # Save file manifest for semantic search metadata
    manifest = {}
    for f in image_files:
        manifest[f.file_id] = {
            "relative_path": f.relative_path,
            "domain": f.domain,
        }

    print(f"\nExtraction complete!")
    print(f"  Extracted: {len(image_files)} image(s)")
    print(f"  Output: {output_path}")

    # Deep extraction — images from app databases and BLOBs
    print(f"\nRunning deep extraction (databases, BLOBs, app caches)...")
    deep_manifest = parser.extract_deep_images(str(output_path))
    if deep_manifest:
        manifest.update(deep_manifest)
        print(f"  Deep extraction: {len(deep_manifest)} additional image(s)")
    else:
        print(f"  Deep extraction: no additional images found")

    manifest_path = output_path / "file_manifest.json"
    with open(manifest_path, "w") as mf:
        json.dump(manifest, mf)

    parser.cleanup()

    # Build CLIP search index
    from .semantic import SemanticIndex

    index_dir = str(output_path / ".search_index")
    index = SemanticIndex(index_dir)

    print(f"\nBuilding search index...")

    def index_progress(current: int, total: int):
        percent = int((current / total) * 100) if total > 0 else 0
        print(f"\r  Indexing: {current}/{total} ({percent}%)", end='', flush=True)

    index.build_index(
        str(output_path),
        file_manifest=manifest,
        progress_callback=index_progress,
    )
    print(f"\nIndexing complete!")


def cmd_index(args):
    """Build semantic search index from extracted images."""
    from .semantic import SemanticIndex

    output_path = Path(args.output)
    index_dir = str(output_path / ".search_index")

    if not output_path.exists():
        print(f"No extracted images found at {output_path}. Run 'extract' first.")
        return

    # Load file manifest if available
    manifest_path = output_path / "file_manifest.json"
    file_manifest = None
    if manifest_path.exists():
        with open(manifest_path) as f:
            file_manifest = json.load(f)

    index = SemanticIndex(index_dir)

    start = time.time()

    def progress_callback(current: int, total: int):
        percent = int((current / total) * 100) if total > 0 else 0
        print(f"\r  {current}/{total} ({percent}%)", end='', flush=True)

    index.build_index(
        str(output_path),
        file_manifest=file_manifest,
        progress_callback=progress_callback,
    )

    elapsed = time.time() - start
    print(f"\nIndexing complete in {elapsed:.1f}s")


def cmd_search(args):
    """Search indexed images by text query."""
    from .semantic import SemanticIndex

    output_path = Path(args.output)
    index_dir = output_path / ".search_index"

    if not (index_dir / "image_index.faiss").exists():
        print("No search index found. Run 'index' first.")
        return

    index = SemanticIndex(str(index_dir))
    results = index.search(args.query, threshold=args.threshold)

    if not results:
        print("No results found.")
        return

    print(f"\nResults for: \"{args.query}\"\n")
    print(f"{'Rank':<6}{'Score':<10}{'File'}")
    print("-" * 50)

    for i, r in enumerate(results, 1):
        filename = Path(r.file_path).name
        print(f"{i:<6}{r.score:<10.4f}{filename}")


def cmd_gui(args):
    """Launch the photo gallery UI."""
    from .gui import launch

    launch(args.output)


def main():
    parser = argparse.ArgumentParser(
        description="iOS Backup Image Extractor with Semantic Search"
    )
    subparsers = parser.add_subparsers(dest="command")

    # extract
    extract_parser = subparsers.add_parser("extract", help="Extract images from iOS backup")
    extract_parser.add_argument("--backup-path", required=True, help="Path to iOS backup directory")
    extract_parser.add_argument("--password", default=None, help="Backup password (prompted if encrypted)")
    extract_parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"Base output directory (default: {DEFAULT_OUTPUT})")

    # index
    index_parser = subparsers.add_parser("index", help="Build semantic search index")
    index_parser.add_argument("--output", required=True, help="Device image directory to index (e.g., output_images/Jacobs_iPhone)")

    # search
    search_parser = subparsers.add_parser("search", help="Search images by text query")
    search_parser.add_argument("query", help="Text query (e.g., 'sunset', 'weapon')")
    search_parser.add_argument("--threshold", type=float, default=0.20, help="Minimum similarity score (default: 0.20)")
    search_parser.add_argument("--output", required=True, help="Device image directory with index")

    # gui
    gui_parser = subparsers.add_parser("gui", help="Launch photo gallery UI")
    gui_parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"Base output directory (default: {DEFAULT_OUTPUT})")

    args = parser.parse_args()

    if args.command == "extract":
        cmd_extract(args)
    elif args.command == "index":
        cmd_index(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "gui":
        cmd_gui(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
