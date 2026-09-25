#!/usr/bin/env python3
"""Linux-friendly icon extraction for Windows installers (Cimian / cimiimport parity).

Mirrors windowsadmins/cimian ``IconExtractor`` flows (EXE / MSI / MSIX / NUPKG)
without Shell32: PE icons via ``icoextract``, MSI Icon streams via ``msiinfo``,
MSIX/NUPKG via zip + manifest/nuspec paths. PNG output matches Cimian client
expectations under ``icons/<name>.png``.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

DESIRED_ICON_SIZE = 256
# Bound PIL decoding and MSI unpack work from untrusted installers.
MAX_IMAGE_PIXELS = 25_000_000
MAX_ICON_SOURCE_BYTES = 200 * 1024 * 1024


def _path_under(base: Path, candidate: Path) -> bool:
    """Return True when *candidate* resolves inside *base*."""
    try:
        candidate.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False


def _safe_child(base: Path, rel: str) -> Optional[Path]:
    """Join *rel* under *base* only when the result stays inside *base*."""
    text = str(rel or "").replace("\\", "/").strip()
    if not text or text.startswith("/") or ".." in Path(text).parts:
        return None
    candidate = (base / text).resolve()
    if not _path_under(base, candidate):
        return None
    return candidate


def extract_installer_icon(installer_path: Path, dest_png: Path) -> Optional[str]:
    """Extract a product icon to *dest_png*.

    Returns the icon filename (e.g. ``GoogleChrome.png``) on success, else None.
    Failures are non-fatal for the caller — return None instead of raising.
    """
    path = Path(installer_path)
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_ICON_SOURCE_BYTES:
            return None
    except OSError:
        return None

    dest_png = Path(dest_png)
    dest_png.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()

    try:
        if suffix == ".exe":
            ok = _extract_from_pe(path, dest_png)
        elif suffix == ".msi":
            ok = _extract_from_msi(path, dest_png)
        elif suffix in {".msix", ".appx"}:
            ok = _extract_from_msix(path, dest_png)
        elif suffix == ".nupkg":
            ok = _extract_from_nupkg(path, dest_png)
        elif suffix in {".ico", ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}:
            ok = _image_to_png(path, dest_png)
        else:
            # Unknown container — try PE parse in case of mislabeled EXE.
            ok = _extract_from_pe(path, dest_png)
    except Exception:  # noqa: BLE001 — match cimiimport: warn-and-continue
        return None

    if ok and dest_png.is_file() and dest_png.stat().st_size > 0:
        return dest_png.name
    return None


def _extract_from_pe(pe_path: Path, dest_png: Path) -> bool:
    """Extract the largest group icon from a PE (.exe/.dll) via icoextract."""
    try:
        from icoextract import IconExtractor, IconExtractorError
    except ImportError:
        return False

    try:
        extractor = IconExtractor(filename=str(pe_path))
        ico_buf = extractor.get_icon(num=0)
    except IconExtractorError:
        return False
    except Exception:  # noqa: BLE001
        return False

    return _ico_bytes_to_png(ico_buf.getvalue(), dest_png)


def _extract_from_msi(msi_path: Path, dest_png: Path) -> bool:
    """Prefer MSI Icon table streams (msiinfo); fall back to embedded PE icons."""
    if _extract_msi_icon_streams(msi_path, dest_png):
        return True
    return _extract_msi_embedded_pe_icons(msi_path, dest_png)


def _msiinfo(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["msiinfo", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _extract_msi_icon_streams(msi_path: Path, dest_png: Path) -> bool:
    """Export Icon.* streams with msitools ``msiinfo`` (cimiimport Icon table)."""
    if shutil.which("msiinfo") is None:
        return False

    listed = _msiinfo("streams", str(msi_path))
    if listed.returncode != 0:
        return False

    # Prefer ARPPRODUCTICON / ProductIcon-style names, then any Icon.* stream.
    streams = [
        line.strip()
        for line in listed.stdout.splitlines()
        if line.strip().startswith("Icon.")
    ]
    if not streams:
        return False

    preferred = sorted(
        streams,
        key=lambda name: (
            0 if "product" in name.lower() else 1,
            0 if "arp" in name.lower() else 1,
            name.lower(),
        ),
    )

    with tempfile.TemporaryDirectory(prefix="cimian-msi-icon-") as tmp:
        tmp_path = Path(tmp)
        for stream in preferred:
            out = tmp_path / f"{stream.replace('.', '_')}.bin"
            extracted = subprocess.run(
                ["msiinfo", "extract", str(msi_path), stream],
                check=False,
                capture_output=True,
            )
            if extracted.returncode != 0 or not extracted.stdout:
                continue
            out.write_bytes(extracted.stdout)
            if _ico_bytes_to_png(extracted.stdout, dest_png):
                return True
            # Some streams are PE resources — try icoextract on the blob.
            pe_tmp = tmp_path / f"{out.stem}.exe"
            pe_tmp.write_bytes(extracted.stdout)
            if _extract_from_pe(pe_tmp, dest_png):
                return True
            if _image_to_png(out, dest_png):
                return True
    return False


def _extract_msi_embedded_pe_icons(msi_path: Path, dest_png: Path) -> bool:
    """Fall back: unpack MSI payload and harvest icons from embedded EXE/DLL."""
    with tempfile.TemporaryDirectory(prefix="cimian-msi-pe-") as tmp:
        tmp_path = Path(tmp)
        if not _unpack_msi(msi_path, tmp_path):
            return False

        candidates: list[Path] = []
        for pattern in ("*.exe", "*.dll", "*.ico", "*.png"):
            candidates.extend(tmp_path.rglob(pattern))

        # Prefer larger EXEs (product binary over stubs) then ICO/PNG.
        def _rank(path: Path) -> tuple:
            suffix = path.suffix.lower()
            size = path.stat().st_size if path.is_file() else 0
            type_rank = {".exe": 0, ".dll": 1, ".ico": 2, ".png": 3}.get(suffix, 9)
            return (type_rank, -size, path.name.lower())

        for candidate in sorted(candidates, key=_rank):
            if candidate.suffix.lower() in {".ico", ".png"}:
                if _image_to_png(candidate, dest_png):
                    return True
            elif _extract_from_pe(candidate, dest_png):
                return True
    return False


def _unpack_msi(msi_path: Path, dest_dir: Path) -> bool:
    """Extract MSI contents with msiextract or 7z."""
    if shutil.which("msiextract"):
        proc = subprocess.run(
            ["msiextract", "-C", str(dest_dir), str(msi_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and any(dest_dir.iterdir()):
            return True

    seven = shutil.which("7z") or shutil.which("7zz")
    if seven:
        proc = subprocess.run(
            [seven, "x", f"-o{dest_dir}", "-y", str(msi_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        return proc.returncode == 0 and any(dest_dir.iterdir())
    return False


def _extract_from_msix(msix_path: Path, dest_png: Path) -> bool:
    """Read AppxManifest logo paths (same priority order as cimiimport)."""
    with tempfile.TemporaryDirectory(prefix="cimian-msix-") as tmp:
        tmp_path = Path(tmp)
        try:
            with zipfile.ZipFile(msix_path) as archive:
                archive.extractall(tmp_path)
        except (OSError, zipfile.BadZipFile):
            return False

        manifest = tmp_path / "AppxManifest.xml"
        if not manifest.is_file():
            return False
        text = manifest.read_text(encoding="utf-8", errors="ignore")
        patterns = (
            r'Square310x310Logo="([^"]+)"',
            r'Square150x150Logo="([^"]+)"',
            r'Square44x44Logo="([^"]+)"',
            r'Logo="([^"]+)"',
        )
        scales = ("scale-400", "scale-200", "scale-150", "scale-125", "scale-100", "")
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            rel = match.group(1).replace("\\", "/")
            logo_path = _safe_child(tmp_path, rel)
            if logo_path is None:
                continue
            logo_dir = logo_path.parent
            if not _path_under(tmp_path, logo_dir):
                continue
            base = logo_path.stem
            ext = logo_path.suffix
            for scale in scales:
                name = f"{base}.{scale}{ext}" if scale else f"{base}{ext}"
                candidate = logo_dir / name
                if (
                    _path_under(tmp_path, candidate)
                    and candidate.is_file()
                    and _image_to_png(candidate, dest_png)
                ):
                    return True
            if logo_path.is_file() and _image_to_png(logo_path, dest_png):
                return True
    return False


def _extract_from_nupkg(nupkg_path: Path, dest_png: Path) -> bool:
    """Read nuspec <icon> or common icon filenames (cimiimport parity)."""
    with tempfile.TemporaryDirectory(prefix="cimian-nupkg-") as tmp:
        tmp_path = Path(tmp)
        try:
            with zipfile.ZipFile(nupkg_path) as archive:
                archive.extractall(tmp_path)
        except (OSError, zipfile.BadZipFile):
            return False

        for nuspec in tmp_path.rglob("*.nuspec"):
            if not _path_under(tmp_path, nuspec):
                continue
            text = nuspec.read_text(encoding="utf-8", errors="ignore")
            match = re.search(r"<icon>([^<]+)</icon>", text, re.IGNORECASE)
            if match:
                icon_rel = match.group(1).strip().replace("\\", "/")
                candidate = _safe_child(tmp_path, icon_rel)
                if (
                    candidate is not None
                    and candidate.is_file()
                    and _image_to_png(candidate, dest_png)
                ):
                    return True

        for pattern in ("icon.png", "icon.ico", "images/icon.png"):
            for candidate in tmp_path.rglob(pattern):
                if (
                    _path_under(tmp_path, candidate)
                    and candidate.is_file()
                    and _image_to_png(candidate, dest_png)
                ):
                    return True
    return False


def _ico_bytes_to_png(data: bytes, dest_png: Path) -> bool:
    if not data:
        return False
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        image = Image.open(io.BytesIO(data))
        return _save_best_png(image, dest_png)
    except Exception:  # noqa: BLE001
        return False


def _image_to_png(source: Path, dest_png: Path) -> bool:
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
        image = Image.open(source)
        return _save_best_png(image, dest_png)
    except Exception:  # noqa: BLE001
        return False


def _save_best_png(image, dest_png: Path) -> bool:
    """Pick the largest frame (ICO multi-size) and write PNG ≤ DESIRED_ICON_SIZE."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    frames: list = []
    try:
        index = 0
        while True:
            image.seek(index)
            frames.append(image.copy())
            index += 1
            # Bound multi-frame ICO decoding.
            if index > 64:
                break
    except EOFError:
        pass
    if not frames:
        frames = [image]

    best = max(frames, key=lambda frame: frame.size[0] * frame.size[1])
    if best.size[0] * best.size[1] > MAX_IMAGE_PIXELS:
        return False
    if max(best.size) > DESIRED_ICON_SIZE:
        best = best.resize(
            (
                DESIRED_ICON_SIZE
                if best.size[0] >= best.size[1]
                else int(best.size[0] * DESIRED_ICON_SIZE / best.size[1]),
                DESIRED_ICON_SIZE
                if best.size[1] >= best.size[0]
                else int(best.size[1] * DESIRED_ICON_SIZE / best.size[0]),
            ),
            Image.Resampling.LANCZOS,
        )
    if best.mode not in ("RGB", "RGBA"):
        best = best.convert("RGBA")
    dest_png.parent.mkdir(parents=True, exist_ok=True)
    best.save(dest_png, format="PNG")
    return dest_png.is_file() and dest_png.stat().st_size > 0
