# CimianImporter

Shared AutoPkg processor that imports a Windows installer into a [Cimian](https://github.com/windowsadmins/cimian) repository.

Usage after `autopkg repo-add autopkg/moojomoore-recipes`:

```xml
<key>Processor</key>
<string>com.github.moojomoore.CimianImporter/CimianImporter</string>
```

This is the Windows counterpart to AutoPkg core’s `MunkiImporter`. Behaviors match where the platforms allow; the table below documents intentional differences.

## MunkiImporter vs CimianImporter

| Area | MunkiImporter | CimianImporter |
| --- | --- | --- |
| Payload | macOS pkg/dmg inspected by `makepkginfo` | Recipe-declared `exe` / `msi` / `msix` / `nupkg` / `ps1` / `script` / `nopkg` |
| Metadata | Generated, then any `pkginfo` key overlaid | Built from structural inputs, then optional `pkgsinfo` dict deeply overlaid |
| Install identity | Receipts / installs discovered from the payload | Recipe `pkgsinfo` overlay for `installs`, scripts, `requires`, etc. |
| Already imported | Skip on installer hash, app version, receipt, or file checksum (same arch); does not require `pkgs/` on disk | Skip when any pkgsinfo already has the same installer `hash` (and matching `supported_architectures` when present); does not require `pkgs/` on disk. Use `force_cimianimport` to override |
| Run summary | `munki_importer_summary_result` + `munki_repo_changed` | `cimian_importer_summary_result` + `cimian_repo_changed` |
| Icons | munkilib extract/reuse (macOS; needs Munki tools) | `CimianIconExtractor` (PE / MSI / MSIX / nupkg on Linux); reuses `icons/<name>.png` when present |
| Extra copy | Optional uninstaller pkg | Optional `uninstaller_pathname` copied into `pkgs/` |
| Object store | Not part of import (repo plugins or a later sync) | Not part of import (stage locally; sync separately if needed) |
| Catalog rebuild | Not part of import (`makecatalogs` later) | Not part of import (Cimian catalog rebuild / gitops later) |
| Silent install | Munki pkginfo keys | `pkgsinfo.installer.flags` / `switches` / `args` / `subcommand` |
| Hash check | From makepkginfo / other processors | Not part of import (use a download/verify processor upstream) |
| File extension | `MUNKI_PKGINFO_FILE_EXTENSION` (default `plist`) | `CIMIAN_PKGSINFO_FILE_EXTENSION` (default `yaml`) |
| `_metadata` merge | `metadata_additions` | `metadata_additions` |
| Installs version key | `version_comparison_key` | `version_comparison_key` |
| Staged basename | `munkiimport_pkgname` | `cimianimport_pkgname` |
| Display-name hint | `munkiimport_appname` (payload scan) | `cimianimport_appname` (default `display_name` only; no payload scan) |

### Not applicable on Cimian

- `additional_makepkginfo_options` — no `makepkginfo`
- `MUNKI_REPO_PLUGIN` / `MUNKILIB_DIR` / `force_munki_repo_lib` — filesystem repo only

## Structural inputs

Same role as MunkiImporter’s `pkg_path` / `MUNKI_REPO` / `repo_subdirectory`:

- `pathname`, `cimian_repo`, `version`, `installer_type` (required)
- `item_name` / `NAME`, `pkginfo_subdir`
- `pkgsinfo`, `force_cimianimport`, `extract_icon`, `icon_name`
- `uninstaller_pathname`
- `cimianimport_pkgname`, `cimianimport_appname`
- `metadata_additions`, `version_comparison_key`, `CIMIAN_PKGSINFO_FILE_EXTENSION`

## Recipe `pkgsinfo` overlay

Put catalogs, category, developer, description, display_name, unattended_*, supported_architectures, manifest_assignment, and installer silent-install keys here — the same place a `.munki` recipe puts them under `pkginfo`:

```xml
<key>pkgsinfo</key>
<dict>
  <key>catalogs</key>
  <array><string>import</string></array>
  <key>category</key>
  <string>Browsers</string>
  <key>developer</key>
  <string>Google</string>
  <key>display_name</key>
  <string>Google Chrome</string>
  <key>supported_architectures</key>
  <array><string>x64</string></array>
  <key>blocking_applications</key>
  <array><string>chrome.exe</string></array>
  <key>installer</key>
  <dict>
    <key>flags</key>
    <array><string>quiet</string></array>
    <key>success_codes</key>
    <array><integer>0</integer><integer>3010</integer></array>
  </dict>
  <key>manifest_assignment</key>
  <dict>
    <key>managed_installs</key>
    <array><string>import</string><string>release</string></array>
  </dict>
</dict>
```

Unknown top-level or installer keys are passed through with a warning (same as MunkiImporter). Structurally unsafe values still raise `ProcessorError`. Prefer `cimian_pkgsinfo_lint` in CI for schema gates.

New imports stamp `_metadata.creation_date` as `YYYY-MM-DDTHH:MM:SSZ` when absent (munkiimport parity). If the recipe sets `force_install_after_date`, it is normalized to the same ISO8601 `Z` string form used by estate pkgsinfo / `cimian_autopromote` (PyYAML datetime dumps are avoided).

When omitted after overlay, defaults are: `catalogs=["import"]`, `supported_architectures=["x64"]`, `unattended_install` / `unattended_uninstall` = true, `display_name` = `cimianimport_appname` or `item_name`.

## Stub identifier

`CimianImporter.recipe` is an empty shared-processor stub with identifier `com.github.moojomoore.CimianImporter`.
