#!/usr/local/autopkg/python
#
# Copyright 2026 moojomoore
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
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
import subprocess
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
    "product_code",
    "upgrade_code",
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
        "display_name": {
            "required": False,
            "description": "User-facing display name; defaults to item_name.",
        },
        "version": {"required": True, "description": "Package version string."},
        "catalogs": {
            "required": False,
            "default": ["import"],
            "description": "Cimian catalogs list (default: import).",
        },
        "installer_type": {
            "required": True,
            "description": "Cimian installer type (msi, exe, msix, ...).",
        },
        "supported_architectures": {
            "required": False,
            "default": ["x64"],
            "description": "supported_architectures list.",
        },
        "pkginfo_subdir": {
            "required": False,
            "default": "apps",
            "description": "Directory under pkgsinfo/ and pkgs/ (e.g. apps).",
        },
        "expected_sha256": {
            "required": False,
            "description": "Optional reviewed SHA-256 for the downloaded installer.",
        },
        "developer": {"required": False, "description": "Optional developer string."},
        "category": {"required": False, "description": "Optional category string."},
        "description": {
            "required": False,
            "description": "Optional pkgsinfo description.",
        },
        "pkgsinfo": {
            "required": False,
            "description": (
                "Optional dict of pkgsinfo keys to overlay onto the generated "
                "item (MunkiImporter pkginfo equivalent). Nested installer keys "
                "are deep-merged. Unknown top-level keys raise ProcessorError."
            ),
        },
        "installer_flags": {
            "required": False,
            "default": [],
            "description": "Optional installer.flags list. Cimian prefixes each with --.",
        },
        "installer_switches": {
            "required": False,
            "default": [],
            "description": "Optional installer.switches list. Cimian prefixes each with /.",
        },
        "installer_args": {
            "required": False,
            "default": [],
            "description": (
                "Optional installer.args list, passed through without a prefix. "
                "Use this for values such as CID=... that must not become flags."
            ),
        },
        "installer_subcommand": {
            "required": False,
            "description": "Optional installer.subcommand placed before switches.",
        },
        "unattended_install": {
            "required": False,
            "default": True,
            "description": "pkgsinfo unattended_install.",
        },
        "unattended_uninstall": {
            "required": False,
            "default": True,
            "description": "pkgsinfo unattended_uninstall.",
        },
        "force_cimianimport": {
            "required": False,
            "default": False,
            "description": (
                "When true, import even if an existing pkgsinfo already has the "
                "same installer hash (mirrors force_munkiimport)."
            ),
        },
        "manifest_assignment": {
            "required": False,
            "description": (
                "Optional dict recorded under pkgsinfo "
                "(e.g. managed_installs / managed_updates / optional_installs). "
                "May also be supplied via the pkgsinfo overlay."
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
                "(Linux-friendly; mirrors AutoPkg Munki extract_icon / "
                "cimiimport IconExtractor). Reuses an existing icon when present. "
                "Failures are non-fatal."
            ),
        },
        "icon_name": {
            "required": False,
            "description": (
                "Optional icon filename override (e.g. GoogleChrome.png). "
                "Defaults to <item_name>.png when extraction succeeds."
            ),
        },
        "msiinfo_path": {
            "required": False,
            "description": (
                "Optional path to msiinfo for MSI ProductCode/UpgradeCode "
                "extraction. Defaults to msiinfo on PATH."
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
        """Load pkgsinfo written as JSON-in-YAML (or dict-shaped YAML)."""
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
            if path.suffix.lower() not in {".yaml", ".yml", ".json"}:
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

    def _read_msi_identity(self, source):
        """Return (product_code, upgrade_code) from MSI Property table, or (None, None)."""
        requested = str(self.env.get("msiinfo_path") or "").strip()
        msiinfo = requested or shutil.which("msiinfo")
        if not msiinfo:
            self.output(
                "msiinfo not found; skipping MSI ProductCode/UpgradeCode extraction"
            )
            return None, None
        try:
            completed = subprocess.run(
                [msiinfo, "export", str(source), "Property"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            self.output(f"Unable to read MSI Property table: {error}")
            return None, None

        properties = {}
        for line in completed.stdout.splitlines():
            fields = line.rstrip("\r").split("\t", 1)
            if len(fields) == 2 and fields[0] in ("ProductCode", "UpgradeCode"):
                properties[fields[0]] = fields[1].strip()
        product_code = properties.get("ProductCode") or None
        upgrade_code = properties.get("UpgradeCode") or None
        if product_code or upgrade_code:
            self.output(
                f"MSI identity ProductCode={product_code} UpgradeCode={upgrade_code}"
            )
        return product_code, upgrade_code

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

    def _apply_pkgsinfo_overlay(self, item):
        overlay = self.env.get("pkgsinfo")
        if not overlay:
            return item
        if not isinstance(overlay, dict):
            raise ProcessorError("pkgsinfo must be a dict when set")
        merged = _deep_merge(item, overlay)
        self._validate_pkgsinfo_keys(merged)
        return merged

    def _maybe_extract_icon(self, source, repo, item_name):
        """Extract or reuse icon; return (icon_path, icon_name) or (None, None)."""
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

        override = str(self.env.get("icon_name") or "").strip()
        if override:
            icon_filename = override if override.endswith(".png") else f"{override}.png"
        else:
            icon_filename = f"{item_name}.png"
        if "/" in icon_filename or "\\" in icon_filename or ".." in icon_filename:
            self.output(f"Ignoring unsafe icon_name: {icon_filename}")
            return None, None

        icon_path = repo / "icons" / icon_filename
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

    def main(self):
        self._clear_summary()

        source = Path(self.env["pathname"]).resolve()
        repo = Path(self.env["cimian_repo"]).resolve()
        item_name = str(self.env.get("item_name") or self.env.get("NAME") or "").strip()
        version = str(self.env["version"]).strip()
        installer_type = str(self.env["installer_type"]).strip().lower()
        pkginfo_subdir = str(self.env.get("pkginfo_subdir") or "apps").strip().strip("/\\")
        catalogs = self._as_list(self.env.get("catalogs") or ["import"]) or ["import"]
        architectures = self._as_list(self.env.get("supported_architectures") or ["x64"]) or [
            "x64"
        ]

        if not source.is_file():
            raise ProcessorError(f"Downloaded installer does not exist: {source}")
        if not SAFE_ITEM_NAME.fullmatch(item_name):
            raise ProcessorError(f"Invalid Cimian item_name: {item_name}")
        if not version or any(character in version for character in "/\\"):
            raise ProcessorError(f"Invalid Cimian version: {version}")
        if installer_type not in SUPPORTED_INSTALLER_TYPES:
            raise ProcessorError(f"Unsupported Cimian installer type: {installer_type}")
        if not SAFE_ITEM_NAME.fullmatch(pkginfo_subdir.replace("/", "").replace("\\", "")):
            # Allow a single path segment only.
            if "/" in pkginfo_subdir or "\\" in pkginfo_subdir or ".." in pkginfo_subdir:
                raise ProcessorError(f"Invalid pkginfo_subdir: {pkginfo_subdir}")

        package_hash = self._sha256(source)
        expected_hash = str(self.env.get("expected_sha256") or "").strip().lower()
        if expected_hash and package_hash != expected_hash:
            raise ProcessorError(
                f"SHA-256 mismatch for {source.name}: expected {expected_hash}, got {package_hash}"
            )

        suffix = source.suffix.lower() or f".{installer_type}"
        rel_location = f"{pkginfo_subdir}/{item_name}/{item_name}-{version}{suffix}"
        package_path = repo / "pkgs" / rel_location
        pkgsinfo_dir = repo / "pkgsinfo" / pkginfo_subdir / item_name
        pkgsinfo_path = pkgsinfo_dir / f"{item_name}-{version}.yaml"

        force = self._env_bool("force_cimianimport", False)
        if not force:
            matched_path, matched_item = self._find_matching_pkginfo(
                repo, package_hash, architectures
            )
            path_match = self._existing_match(
                pkgsinfo_path, item_name, version, package_hash, architectures
            )
            if matched_path is not None or path_match:
                existing = matched_item or self._load_pkgsinfo(pkgsinfo_path) or {}
                existing_installer = existing.get("installer") or {}
                existing_rel = str(
                    existing_installer.get("location") or rel_location
                )
                existing_pkg = repo / "pkgs" / existing_rel
                existing_info = matched_path or pkgsinfo_path
                self.output(
                    f"Item already exists in the Cimian repo as pkgs/{existing_rel} "
                    f"(matching installer hash)."
                )
                self._set_skip_outputs(
                    existing_info, existing_pkg, existing_rel, package_hash
                )
                return

        package_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, package_path)

        size = package_path.stat().st_size
        installer = {
            "type": installer_type,
            "location": rel_location,
            # Bare hex — Cimian DownloadService compares without stripping a
            # "sha256:" prefix (cimiimport writes the same format).
            "hash": package_hash,
            "size": size,
        }
        flags = self._as_list(self.env.get("installer_flags"))
        switches = self._as_list(self.env.get("installer_switches"))
        args = self._as_list(self.env.get("installer_args"))
        subcommand = str(self.env.get("installer_subcommand") or "").strip()
        # Reject leftover %VAR% so unresolved recipe substitutions are not written.
        self._reject_unresolved(flags, "installer_flags")
        self._reject_unresolved(switches, "installer_switches")
        self._reject_unresolved(args, "installer_args")
        if "%" in subcommand:
            raise ProcessorError("Unresolved substitution in installer_subcommand")
        if flags:
            installer["flags"] = flags
        if switches:
            installer["switches"] = switches
        if args:
            installer["args"] = [str(value) for value in args]
        if subcommand:
            installer["subcommand"] = subcommand

        # MSI identity: recipe/overlay wins; otherwise read from the payload.
        if installer_type == "msi":
            product_code, upgrade_code = self._read_msi_identity(source)
            if product_code:
                installer["product_code"] = product_code
            if upgrade_code:
                installer["upgrade_code"] = upgrade_code

        display_name = str(self.env.get("display_name") or item_name).strip()
        unattended_install = self._env_bool("unattended_install", True)
        unattended_uninstall = self._env_bool("unattended_uninstall", True)

        item = {
            "name": item_name,
            "display_name": display_name,
            "version": version,
            "catalogs": catalogs,
            "supported_architectures": architectures,
            "installer": installer,
            "unattended_install": unattended_install,
            "unattended_uninstall": unattended_uninstall,
        }
        developer = str(self.env.get("developer") or "").strip()
        category = str(self.env.get("category") or "").strip()
        description = str(self.env.get("description") or "").strip()
        if developer:
            item["developer"] = developer
        if category:
            item["category"] = category
        if description:
            item["description"] = description

        manifest_assignment = self.env.get("manifest_assignment")
        if manifest_assignment:
            if not isinstance(manifest_assignment, dict):
                raise ProcessorError("manifest_assignment must be a dict when set")
            item["manifest_assignment"] = manifest_assignment

        item = self._apply_pkgsinfo_overlay(item)
        # Overlay may replace installer entirely; re-assert required identity fields.
        if not isinstance(item.get("installer"), dict):
            raise ProcessorError("pkgsinfo.installer must be a dict when set")
        item["installer"].setdefault("type", installer_type)
        item["installer"].setdefault("location", rel_location)
        item["installer"].setdefault("hash", package_hash)
        item["installer"].setdefault("size", size)

        icon_path, icon_filename = self._maybe_extract_icon(source, repo, item_name)
        if icon_filename:
            item["icon_name"] = icon_filename
            self.env["cimian_icon_path"] = str(icon_path)
            self.env["cimian_icon_name"] = icon_filename

        uninstaller_src = str(self.env.get("uninstaller_pathname") or "").strip()
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
            un_dest = repo / "pkgs" / un_rel
            un_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(uninstaller_path, un_dest)
            un_type = un_suffix.lstrip(".") or "exe"
            if un_type not in SUPPORTED_INSTALLER_TYPES:
                un_type = "exe"
            item["uninstaller"] = {
                "type": un_type,
                "location": un_rel,
                "hash": self._sha256(un_dest),
                "size": un_dest.stat().st_size,
            }
            item["uninstallable"] = True
            self.output(f"Copied uninstaller → {un_rel}")

        pkgsinfo_dir.mkdir(parents=True, exist_ok=True)
        # JSON is a strict subset of YAML; keeps this processor dependency-free.
        pkgsinfo_path.write_text(json.dumps(item, indent=2) + "\n", encoding="utf-8")

        self.env["cimian_pkgsinfo_path"] = str(pkgsinfo_path)
        self.env["cimian_package_path"] = str(package_path)
        self.env["cimian_package_sha256"] = package_hash
        self.env["cimian_rel_location"] = rel_location
        self.env["cimian_repo_changed"] = True
        self._set_summary(item, pkgsinfo_path, package_path, repo, icon_filename)
        self.output(f"Imported {item_name} {version} → {rel_location}")


