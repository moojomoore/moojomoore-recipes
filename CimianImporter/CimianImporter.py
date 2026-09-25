#!/usr/local/autopkg/python
"""Import a downloaded Windows installer into a Cimian repository.

Cimian counterpart to AutoPkg's MunkiImporter. See CimianImporter/README.md
(in autopkg/moojomoore-recipes) for Munki vs Cimian behavior differences.
"""

from __future__ import absolute_import

import copy
import hashlib
import json
import re
import shutil
from pathlib import Path

from autopkglib import Processor, ProcessorError

__all__ = ["CimianImporter"]

SAFE_ITEM_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
SUPPORTED_INSTALLER_TYPES = {"exe", "msi", "msix", "nupkg", "ps1", "script", "nopkg"}

# Subset of Cimian pkgsinfo schema (windowsadmins/cimian-gitops + estate lint).
VALID_TOP_KEYS = {
    "name",
    "display_name",
    "identifier",
    "version",
    "description",
    "category",
    "developer",
    "notes",
    "icon_name",
    "_metadata",
    "catalogs",
    "installer",
    "installer_type",
    "uninstaller",
    "installer_item_location",
    "installer_item_hash",
    "uninstallable",
    "unattended_install",
    "unattended_uninstall",
    "autoremove",
    "OnDemand",
    "install_window",
    "restart_action",
    "conditional_items",
    "minimum_os_version",
    "maximum_os_version",
    "minimum_cimian_version",
    "supported_architectures",
    "installs",
    "blocking_applications",
    "days_untouched_before_uninstall",
    "usage_tracked_paths",
    "minimum_usage_history_days",
    "unused_software_removal_info",
    "preinstall_script",
    "postinstall_script",
    "preuninstall_script",
    "postuninstall_script",
    "install_script",
    "uninstall_script",
    "installcheck_script",
    "uninstallcheck_script",
    "version_script",
    "requires",
    "update_for",
    "force_install_after_date",
    "precache",
    "check",
    "installer_timeout",
    "recurring",
    "loop_fingerprint",
    "channel",
    "manifest_assignment",
}

VALID_INSTALLER_KEYS = {
    "location",
    "hash",
    "type",
    "size",
    "switches",
    "flags",
    "subcommand",
    "arguments",
    "args",
    "temp_dir",
    "installer_item_location",
    "success_codes",
    "identity_name",
}


def _truthy(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _deep_merge(base, overlay):
    """Recursively merge *overlay* into a copy of *base* (dict values only)."""
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class CimianImporter(Processor):
    """Stage an installer under cimian/pkgs and write pkgsinfo YAML."""

    description = __doc__
    input_variables = {
        "pathname": {"required": True, "description": "Downloaded installer path."},
        "cimian_repo": {"required": True, "description": "Cimian repository root."},
        "NAME": {
            "required": False,
            "description": "Fallback item name when item_name is unset.",
        },
        "item_name": {
            "required": False,
            "description": "Stable Cimian package name (pkgsinfo name).",
        },
        "version": {"required": True, "description": "Package version string."},
        "installer_type": {
            "required": True,
            "description": "Cimian installer type (msi, exe, msix, ...).",
        },
        "pkginfo_subdir": {
            "required": False,
            "default": "apps",
            "description": (
                "The subdirectory under pkgs to which the item will be copied, "
                "and under pkgsinfo where the pkgsinfo will be created "
                "(MunkiImporter repo_subdirectory counterpart)."
            ),
        },
        "pkgsinfo": {
            "required": False,
            "description": (
                "Optional dict of pkgsinfo keys to overlay onto the generated "
                "item (MunkiImporter pkginfo equivalent). Nested installer keys "
                "are deep-merged. Unknown top-level keys raise ProcessorError. "
                "Put catalogs, category, developer, description, display_name, "
                "unattended_*, supported_architectures, manifest_assignment, and "
                "installer.flags/switches/args/subcommand here."
            ),
        },
        "force_cimianimport": {
            "required": False,
            "default": False,
            "description": (
                "When true, import even if an existing pkgsinfo already has the "
                "same installer hash (mirrors force_munkiimport)."
            ),
        },
        "uninstaller_pathname": {
            "required": False,
            "description": (
                "Optional path to an uninstaller to copy into pkgs/ beside the "
                "installer (MunkiImporter uninstaller_pkg_path counterpart)."
            ),
        },
        "extract_icon": {
            "required": False,
            "default": True,
            "description": (
                "Extract a product icon into cimian/icons/<item_name>.png "
                "(Linux-friendly; mirrors AutoPkg Munki extract_icon). "
                "Reuses an existing icon when present. Failures are non-fatal."
            ),
        },
        "icon_name": {
            "required": False,
            "description": (
                "Optional icon filename override (e.g. GoogleChrome.png). "
                "Defaults to <item_name>.png when extraction succeeds."
            ),
        },
        "cimianimport_pkgname": {
            "required": False,
            "description": (
                "Optional staged installer basename under pkgs/ "
                "(MunkiImporter munkiimport_pkgname / --pkgname counterpart). "
                "When omitted, defaults to <item_name>-<version><suffix>."
            ),
        },
        "cimianimport_appname": {
            "required": False,
            "description": (
                "Optional default display_name before the pkgsinfo overlay "
                "(weak Cimian stand-in for munkiimport_appname / --appname; "
                "no payload scan). Overlay display_name wins when set."
            ),
        },
        "version_comparison_key": {
            "required": False,
            "description": (
                "String to set 'version_comparison_key' for any installs items "
                "(same behavior as MunkiImporter)."
            ),
        },
        "metadata_additions": {
            "required": False,
            "description": (
                "A dictionary that will be merged with the pkgsinfo _metadata. "
                "Unique keys will be added, but overlapping keys will replace "
                "existing values (MunkiImporter metadata_additions counterpart)."
            ),
        },
        "CIMIAN_PKGSINFO_FILE_EXTENSION": {
            "required": False,
            "default": "yaml",
            "description": (
                "Extension for output pkgsinfo files. Default is 'yaml' "
                "(MunkiImporter MUNKI_PKGINFO_FILE_EXTENSION counterpart)."
            ),
        },
    }
    output_variables = {
        "cimian_pkgsinfo_path": {"description": "Generated pkgsinfo path."},
        "cimian_package_path": {"description": "Staged installer path."},
        "cimian_package_sha256": {"description": "Installer SHA-256 (no prefix)."},
        "cimian_rel_location": {"description": "Installer path relative to pkgs/."},
        "cimian_icon_path": {"description": "Extracted icon path when present."},
        "cimian_icon_name": {"description": "pkgsinfo icon_name when present."},
        "cimian_repo_changed": {
            "description": "True when a new item was imported; False when skipped."
        },
        "cimian_importer_summary_result": {
            "description": "AutoPkg run-summary block (mirrors munki_importer_summary_result)."
        },
    }

    @staticmethod
    def _sha256(path):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _env_bool(self, key, default):
        """Read a recipe boolean. The string \"false\" must not count as true."""
        if key not in self.env or self.env.get(key) is None or self.env.get(key) == "":
            return default
        return _truthy(self.env.get(key))

    @staticmethod
    def _reject_unresolved(values, label):
        for value in values:
            if "%" in str(value):
                raise ProcessorError(f"Unresolved substitution in {label}")

    @staticmethod
    def _as_list(value):
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return list(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("[") and text.endswith("]"):
                # AutoPkg may pass JSON-ish list strings from YAML recipes.
                try:
                    parsed = json.loads(text.replace("'", '"'))
                    if isinstance(parsed, list):
                        return parsed
                except Exception:
                    pass
            return [part.strip() for part in text.split(",") if part.strip()]
        return [value]

    @staticmethod
    def _load_pkgsinfo(path):
        """Load pkgsinfo YAML (JSON still accepted for older staged files)."""
        text = path.read_text(encoding="utf-8")
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
        try:
            import yaml
        except ImportError:
            return None
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else None

    @staticmethod
    def _dump_pkgsinfo(item):
        """Serialize pkgsinfo as block-style YAML (Cimian gitops convention)."""
        try:
            import yaml
        except ImportError as err:
            raise ProcessorError(
                "PyYAML is required to write Cimian pkgsinfo (.yaml). "
                "Install with: pip install pyyaml"
            ) from err
        return yaml.safe_dump(
            item,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )

    def _pkgsinfo_overlay(self):
        overlay = self.env.get("pkgsinfo")
        if not overlay:
            return {}
        if not isinstance(overlay, dict):
            raise ProcessorError("pkgsinfo must be a dict when set")
        return overlay

    def _arch_compatible(self, existing_arch, architectures):
        if existing_arch is None:
            return True
        return self._as_list(existing_arch) == list(architectures)

    def _find_matching_pkginfo(self, repo, package_hash, architectures):
        """Find an existing pkgsinfo with the same installer hash (Munki-like).

        Returns (pkgsinfo_path, item_dict) or (None, None).
        """
        pkgsinfo_root = repo / "pkgsinfo"
        if not pkgsinfo_root.is_dir():
            return None, None
        needle = package_hash.lower()
        for path in sorted(pkgsinfo_root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix.lower() not in {".yaml", ".yml", ".json", ".plist"}:
                continue
            existing = self._load_pkgsinfo(path)
            if not existing:
                continue
            installer = existing.get("installer") or {}
            if not isinstance(installer, dict):
                continue
            if str(installer.get("hash") or "").lower() != needle:
                continue
            if not self._arch_compatible(
                existing.get("supported_architectures"), architectures
            ):
                continue
            return path, existing
        return None, None

    def _existing_match(self, pkgsinfo_path, item_name, version, package_hash, architectures):
        """Return True when the expected pkgsinfo path already matches."""
        if not pkgsinfo_path.is_file():
            return False
        existing = self._load_pkgsinfo(pkgsinfo_path)
        if not existing:
            return False
        if str(existing.get("name") or "") != item_name:
            return False
        if str(existing.get("version") or "") != version:
            return False
        installer = existing.get("installer") or {}
        if not isinstance(installer, dict):
            return False
        if str(installer.get("hash") or "").lower() != package_hash.lower():
            return False
        return self._arch_compatible(
            existing.get("supported_architectures"), architectures
        )

    @staticmethod
    def _validate_single_subdir(subdir, label="pkginfo_subdir"):
        """Require one safe path segment (rejects '.', '..', and separators)."""
        if (
            not subdir
            or subdir in (".", "..")
            or "/" in subdir
            or "\\" in subdir
            or not SAFE_ITEM_NAME.fullmatch(subdir)
        ):
            raise ProcessorError(f"Invalid {label}: {subdir}")

    @staticmethod
    def _resolve_under(base, *parts):
        """Resolve *parts under *base*; raise if the result escapes *base*."""
        base_resolved = Path(base).resolve()
        candidate = base_resolved.joinpath(*parts).resolve()
        try:
            candidate.relative_to(base_resolved)
        except ValueError as error:
            raise ProcessorError(
                f"Path escapes {base_resolved}: {'/'.join(str(p) for p in parts)}"
            ) from error
        return candidate

    @staticmethod
    def _validate_repo_rel_location(rel_location, label="installer.location"):
        """Ensure a pkgs-relative location has no traversal and is non-empty."""
        text = str(rel_location or "").strip()
        if not text or text.startswith("/") or text.startswith("\\"):
            raise ProcessorError(f"Invalid {label}: {rel_location}")
        parts = Path(text.replace("\\", "/")).parts
        if not parts or any(part in ("", ".", "..") for part in parts):
            raise ProcessorError(f"Invalid {label}: {rel_location}")
        return text.replace("\\", "/")

    @staticmethod
    def _safe_icon_filename(name):
        """Return a basename under icons/ or None if unsafe."""
        text = str(name or "").strip()
        if not text:
            return None
        filename = text if text.endswith(".png") else f"{text}.png"
        if (
            "/" in filename
            or "\\" in filename
            or ".." in filename
            or filename in (".png",)
            or not SAFE_ITEM_NAME.fullmatch(Path(filename).stem)
        ):
            return None
        return filename

    def _validate_pkgsinfo_keys(self, item):
        unknown = sorted(set(item) - VALID_TOP_KEYS)
        if unknown:
            raise ProcessorError(
                "Unknown pkgsinfo top-level key(s): " + ", ".join(unknown)
            )
        installer = item.get("installer")
        if isinstance(installer, dict):
            bad = sorted(set(installer) - VALID_INSTALLER_KEYS)
            if bad:
                raise ProcessorError(
                    "Unknown installer key(s): " + ", ".join(bad)
                )
            # location/hash/size are forced from the staged artifact in main();
            # do not reject overlay values here (they are overwritten).
            if "temp_dir" in installer and installer.get("temp_dir") is not None:
                temp_dir = str(installer.get("temp_dir"))
                if (
                    ".." in Path(temp_dir.replace("\\", "/")).parts
                    or temp_dir.startswith("/")
                    or temp_dir.startswith("\\")
                ):
                    raise ProcessorError(f"Invalid installer.temp_dir: {temp_dir}")
        icon_name = item.get("icon_name")
        if icon_name is not None and icon_name != "":
            if self._safe_icon_filename(icon_name) is None:
                raise ProcessorError(f"Invalid pkgsinfo icon_name: {icon_name}")

    def _apply_pkgsinfo_overlay(self, item):
        overlay = self._pkgsinfo_overlay()
        if not overlay:
            return item
        merged = _deep_merge(item, overlay)
        self._validate_pkgsinfo_keys(merged)
        return merged

    def _apply_generated_defaults(self, item, item_name):
        """Fill keys Munki recipes put in pkginfo when the overlay omitted them."""
        if not self._as_list(item.get("catalogs")):
            item["catalogs"] = ["import"]
        if not self._as_list(item.get("supported_architectures")):
            item["supported_architectures"] = ["x64"]
        if "unattended_install" not in item:
            item["unattended_install"] = True
        if "unattended_uninstall" not in item:
            item["unattended_uninstall"] = True
        if not str(item.get("display_name") or "").strip():
            item["display_name"] = item_name
        return item

    def _reject_installer_unresolved(self, installer):
        """Reject leftover %VAR% in installer list/string fields from pkgsinfo."""
        if not isinstance(installer, dict):
            return
        for key in ("flags", "switches", "args", "arguments"):
            values = installer.get(key)
            if values is None:
                continue
            self._reject_unresolved(self._as_list(values), f"installer.{key}")
        subcommand = installer.get("subcommand")
        if subcommand is not None and "%" in str(subcommand):
            raise ProcessorError("Unresolved substitution in installer.subcommand")

    def _apply_version_comparison_key(self, item):
        key = self.env.get("version_comparison_key")
        if not key or "installs" not in item:
            return
        if not isinstance(item["installs"], list):
            raise ProcessorError("pkgsinfo.installs must be a list when set")
        for install_item in item["installs"]:
            if not isinstance(install_item, dict):
                raise ProcessorError("pkgsinfo.installs entries must be dicts")
            if key not in install_item:
                path = install_item.get("path") or install_item.get("file") or "?"
                raise ProcessorError(
                    "version_comparison_key "
                    f"'{key}' could not be found in the installs item for path '{path}'"
                )
            install_item["version_comparison_key"] = key

    def _apply_metadata_additions(self, item):
        if "metadata_additions" not in self.env:
            return
        additions = self.env["metadata_additions"]
        if additions is None or additions == "":
            return
        if not isinstance(additions, dict):
            raise ProcessorError("metadata_additions must be a dict when set")
        metadata = item.setdefault("_metadata", {})
        if not isinstance(metadata, dict):
            raise ProcessorError("pkgsinfo._metadata must be a dict when set")
        metadata.update(additions)

    def _maybe_extract_icon(self, source, repo, item_name, preferred_icon_name=None):
        """Extract or reuse icon; return (icon_path, icon_name) or (None, None).

        Preference order for the filename: env ``icon_name``, then
        *preferred_icon_name* (typically pkgsinfo overlay), then
        ``<item_name>.png``.
        """
        # Default on — match AutoPkg Munki extract_icon pref behavior.
        if "extract_icon" in self.env and self.env.get("extract_icon") not in (
            None,
            "",
        ):
            extract = _truthy(self.env.get("extract_icon"))
        else:
            extract = True
        if not extract:
            return None, None

        candidates = (
            str(self.env.get("icon_name") or "").strip(),
            str(preferred_icon_name or "").strip(),
            f"{item_name}.png",
        )
        icon_filename = None
        for candidate in candidates:
            if not candidate:
                continue
            safe = self._safe_icon_filename(candidate)
            if safe:
                icon_filename = safe
                break
            self.output(f"Ignoring unsafe icon_name: {candidate}")
        if not icon_filename:
            return None, None

        icon_path = self._resolve_under(repo / "icons", icon_filename)
        # Munki-style reuse: keep an existing icon instead of re-extracting.
        if icon_path.is_file() and icon_path.stat().st_size > 0:
            self.output(f"Reusing existing icon → {icon_path}")
            return icon_path, icon_filename

        try:
            # AutoPkg loads processors via load_source and does not put the
            # recipe directory on sys.path; ensure the sibling module resolves.
            import sys

            _processor_dir = str(Path(__file__).resolve().parent)
            if _processor_dir not in sys.path:
                sys.path.insert(0, _processor_dir)
            from CimianIconExtractor import extract_installer_icon
        except ImportError:
            self.output("CimianIconExtractor unavailable; skipping icon extraction")
            return None, None

        written = extract_installer_icon(source, icon_path)
        if not written or not icon_path.is_file():
            self.output(f"No icon extracted for {item_name}")
            return None, None

        self.output(f"Extracted icon → {icon_path}")
        return icon_path, icon_filename

    def _clear_summary(self):
        if "cimian_importer_summary_result" in self.env:
            del self.env["cimian_importer_summary_result"]

    def _set_skip_outputs(self, pkgsinfo_path, package_path, rel_location, package_hash):
        self.env["cimian_pkgsinfo_path"] = str(pkgsinfo_path)
        self.env["cimian_package_path"] = str(package_path)
        self.env["cimian_package_sha256"] = package_hash
        self.env["cimian_rel_location"] = rel_location
        self.env["cimian_repo_changed"] = False

    def _set_summary(self, item, pkgsinfo_path, package_path, repo, icon_filename):
        pkgsinfo_prefix = repo / "pkgsinfo"
        pkgs_prefix = repo / "pkgs"
        try:
            pkgsinfo_rel = str(pkgsinfo_path.relative_to(pkgsinfo_prefix))
        except ValueError:
            pkgsinfo_rel = str(pkgsinfo_path)
        try:
            pkg_rel = str(package_path.relative_to(pkgs_prefix))
        except ValueError:
            pkg_rel = str(package_path)
        self.env["cimian_importer_summary_result"] = {
            "summary_text": "The following new items were imported into Cimian:",
            "report_fields": [
                "name",
                "version",
                "catalogs",
                "pkgsinfo_path",
                "pkg_repo_path",
                "icon_repo_path",
            ],
            "data": {
                "name": item["name"],
                "version": item["version"],
                "catalogs": ",".join(self._as_list(item.get("catalogs"))),
                "pkgsinfo_path": pkgsinfo_rel,
                "pkg_repo_path": pkg_rel,
                "icon_repo_path": f"icons/{icon_filename}" if icon_filename else "",
            },
        }

    def _staged_rel_location(self, pkginfo_subdir, item_name, version, suffix):
        """Build pkgs/ relative path; honor cimianimport_pkgname like --pkgname."""
        override = str(self.env.get("cimianimport_pkgname") or "").strip()
        if override:
            if "/" in override or "\\" in override or ".." in override:
                raise ProcessorError(f"Invalid cimianimport_pkgname: {override}")
            basename = Path(override).name
            if not Path(basename).suffix:
                basename = f"{basename}{suffix}"
            if not SAFE_ITEM_NAME.fullmatch(Path(basename).stem.replace(" ", "")):
                # Allow common versioned names (dots, hyphens) via SAFE on full stem
                # after normalizing spaces — reject path traversal already handled.
                stem = Path(basename).stem
                if not re.fullmatch(r"[A-Za-z0-9._ -]+", stem):
                    raise ProcessorError(f"Invalid cimianimport_pkgname: {override}")
            return f"{pkginfo_subdir}/{item_name}/{basename}"
        return f"{pkginfo_subdir}/{item_name}/{item_name}-{version}{suffix}"

    def main(self):
        self._clear_summary()

        source = Path(self.env["pathname"]).resolve()
        repo = Path(self.env["cimian_repo"]).resolve()
        item_name = str(self.env.get("item_name") or self.env.get("NAME") or "").strip()
        version = str(self.env["version"]).strip()
        installer_type = str(self.env["installer_type"]).strip().lower()
        pkginfo_subdir = str(self.env.get("pkginfo_subdir") or "apps").strip().strip(
            "/\\"
        )
        overlay = self._pkgsinfo_overlay()
        architectures = self._as_list(
            overlay.get("supported_architectures") or ["x64"]
        ) or ["x64"]

        extension = str(
            self.env.get("CIMIAN_PKGSINFO_FILE_EXTENSION") or "yaml"
        ).strip().lstrip(".")
        if (
            not extension
            or "/" in extension
            or "\\" in extension
            or ".." in extension
            or not re.fullmatch(r"[A-Za-z0-9]+", extension)
        ):
            raise ProcessorError(
                f"Invalid CIMIAN_PKGSINFO_FILE_EXTENSION: {extension}"
            )

        if not source.is_file():
            raise ProcessorError(f"Downloaded installer does not exist: {source}")
        if not SAFE_ITEM_NAME.fullmatch(item_name):
            raise ProcessorError(f"Invalid Cimian item_name: {item_name}")
        if not version or any(character in version for character in "/\\"):
            raise ProcessorError(f"Invalid Cimian version: {version}")
        if installer_type not in SUPPORTED_INSTALLER_TYPES:
            raise ProcessorError(f"Unsupported Cimian installer type: {installer_type}")
        self._validate_single_subdir(pkginfo_subdir)

        package_hash = self._sha256(source)
        size = source.stat().st_size
        suffix = source.suffix.lower() or f".{installer_type}"
        rel_location = self._staged_rel_location(
            pkginfo_subdir, item_name, version, suffix
        )
        self._validate_repo_rel_location(rel_location)
        package_path = self._resolve_under(repo / "pkgs", *Path(rel_location).parts)
        pkgsinfo_dir = self._resolve_under(
            repo / "pkgsinfo", pkginfo_subdir, item_name
        )
        pkgsinfo_path = pkgsinfo_dir / f"{item_name}-{version}.{extension}"

        force = self._env_bool("force_cimianimport", False)
        if not force:
            matched_path, matched_item = self._find_matching_pkginfo(
                repo, package_hash, architectures
            )
            path_match = self._existing_match(
                pkgsinfo_path, item_name, version, package_hash, architectures
            )
            if matched_path is not None or path_match:
                # Match MunkiImporter: hash (+ arch) match skips import entirely.
                # Do not rewrite pkgsinfo when pkgs/ is missing (common in CI
                # where binaries live in object storage, not the git checkout).
                existing = matched_item or self._load_pkgsinfo(pkgsinfo_path) or {}
                existing_installer = existing.get("installer") or {}
                existing_rel = str(
                    existing_installer.get("location") or rel_location
                )
                try:
                    existing_rel = self._validate_repo_rel_location(existing_rel)
                    existing_pkg = self._resolve_under(
                        repo / "pkgs", *Path(existing_rel).parts
                    )
                except ProcessorError:
                    existing_rel = rel_location
                    existing_pkg = package_path
                existing_info = matched_path or pkgsinfo_path
                self.output(
                    f"Item already exists in the Cimian repo as "
                    f"pkgs/{existing_rel} (matching installer hash)."
                )
                self._set_skip_outputs(
                    existing_info, existing_pkg, existing_rel, package_hash
                )
                return

        # Build and validate pkgsinfo before staging so a failed validation
        # cannot leave an orphan installer under pkgs/.
        installer = {
            "type": installer_type,
            "location": rel_location,
            # Bare hex — Cimian DownloadService compares without stripping a
            # "sha256:" prefix (cimiimport writes the same format).
            "hash": package_hash,
            "size": size,
        }

        appname = str(self.env.get("cimianimport_appname") or "").strip()
        display_name = appname or item_name

        item = {
            "name": item_name,
            "display_name": display_name,
            "version": version,
            "catalogs": ["import"],
            "supported_architectures": architectures,
            "installer": installer,
            "unattended_install": True,
            "unattended_uninstall": True,
        }

        item = self._apply_pkgsinfo_overlay(item)
        item = self._apply_generated_defaults(item, item_name)

        if not isinstance(item.get("installer"), dict):
            raise ProcessorError("pkgsinfo.installer must be a dict when set")
        # Force identity fields from the staged artifact (overlay cannot spoof).
        item["installer"]["type"] = installer_type
        item["installer"]["location"] = rel_location
        item["installer"]["hash"] = package_hash
        item["installer"]["size"] = size
        self._reject_installer_unresolved(item["installer"])

        self._apply_metadata_additions(item)
        self._apply_version_comparison_key(item)
        self._validate_pkgsinfo_keys(item)

        overlay_icon = item.get("icon_name")
        icon_path, icon_filename = self._maybe_extract_icon(
            source, repo, item_name, preferred_icon_name=overlay_icon
        )
        if icon_filename:
            item["icon_name"] = icon_filename
            self.env["cimian_icon_path"] = str(icon_path)
            self.env["cimian_icon_name"] = icon_filename
        elif overlay_icon:
            safe_overlay_icon = self._safe_icon_filename(overlay_icon)
            if safe_overlay_icon:
                item["icon_name"] = safe_overlay_icon

        uninstaller_src = str(self.env.get("uninstaller_pathname") or "").strip()
        un_dest = None
        if uninstaller_src:
            uninstaller_path = Path(uninstaller_src).resolve()
            if not uninstaller_path.is_file():
                raise ProcessorError(
                    f"uninstaller_pathname does not exist: {uninstaller_path}"
                )
            un_suffix = uninstaller_path.suffix.lower() or ".exe"
            un_rel = (
                f"{pkginfo_subdir}/{item_name}/"
                f"{item_name}-{version}-uninstall{un_suffix}"
            )
            self._validate_repo_rel_location(un_rel, "uninstaller.location")
            un_dest = self._resolve_under(repo / "pkgs", *Path(un_rel).parts)
            un_type = un_suffix.lstrip(".") or "exe"
            if un_type not in SUPPORTED_INSTALLER_TYPES:
                un_type = "exe"
            item["uninstaller"] = {
                "type": un_type,
                "location": un_rel,
                # Hash/size filled after copy below.
                "hash": "",
                "size": 0,
            }
            item["uninstallable"] = True

        package_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source, package_path)
            if un_dest is not None:
                un_dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(Path(uninstaller_src).resolve(), un_dest)
                item["uninstaller"]["hash"] = self._sha256(un_dest)
                item["uninstaller"]["size"] = un_dest.stat().st_size
                self.output(f"Copied uninstaller → {item['uninstaller']['location']}")

            pkgsinfo_dir.mkdir(parents=True, exist_ok=True)
            pkgsinfo_path.write_text(self._dump_pkgsinfo(item), encoding="utf-8")
        except Exception:
            # Roll back staged artifacts if metadata write fails mid-import.
            if package_path.is_file():
                try:
                    package_path.unlink()
                except OSError:
                    pass
            if un_dest is not None and un_dest.is_file():
                try:
                    un_dest.unlink()
                except OSError:
                    pass
            raise

        self.env["cimian_pkgsinfo_path"] = str(pkgsinfo_path)
        self.env["cimian_package_path"] = str(package_path)
        self.env["cimian_package_sha256"] = package_hash
        self.env["cimian_rel_location"] = rel_location
        self.env["cimian_repo_changed"] = True
        self._set_summary(item, pkgsinfo_path, package_path, repo, icon_filename)
        self.output(f"Imported {item_name} {version} → {rel_location}")
