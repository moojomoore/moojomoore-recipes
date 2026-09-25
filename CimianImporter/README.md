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
| Metadata | Generated, then any `pkginfo` key overlaid | Built from recipe inputs, then optional `pkgsinfo` dict deeply overlaid |
| Install identity | Receipts / installs discovered from the payload | Recipe `pkgsinfo` overlay for `installs`, scripts, `requires`, etc.; for MSI, `ProductCode` / `UpgradeCode` read via `msiinfo` when unset |
| Already imported | Skip on installer hash, app version, receipt, or file checksum (same arch) | Skip when the pkgsinfo path already has the same `name`, `version`, and installer `hash` (and matching `supported_architectures` when present). Use `force_cimianimport` to override |
| Run summary | `munki_importer_summary_result` + `munki_repo_changed` | `cimian_importer_summary_result` + `cimian_repo_changed` |
| Icons | munkilib extract/reuse (macOS; needs Munki tools) | `CimianIconExtractor` (PE / MSI / MSIX / nupkg on Linux); reuses `icons/<name>.png` when present |
| Extra copy | Optional uninstaller pkg | Optional S3 upload (`upload_s3` / `CIMIAN_UPLOAD_S3`) with head-object check |
| Catalog rebuild | Not part of import (`makecatalogs` later) | Not part of import (Cimian / gitops rebuild later) |
| Silent install | Munki pkginfo keys | `installer.flags`, `installer.switches`, `installer.args` (unprefixed), `installer.subcommand` |
| Hash check | From makepkginfo | Optional `expected_sha256` before staging |

## Optional pipeline-local inputs

These are safe for any consumer but are not required for third-party use:

- `manifest_assignment` — autopromote metadata recorded in pkgsinfo
- `upload_s3` / `s3_bucket` / `CIMIAN_S3_BUCKET` / `GORILLA_S3_BUCKET` — package and icon upload

## Recipe `pkgsinfo` overlay

Same idea as a `.munki` recipe’s `pkginfo` dict. Example:

```xml
<key>pkgsinfo</key>
<dict>
  <key>blocking_applications</key>
  <array><string>chrome.exe</string></array>
  <key>installer</key>
  <dict>
    <key>product_code</key>
    <string>{YOUR-PRODUCT-CODE}</string>
    <key>success_codes</key>
    <array><integer>0</integer><integer>3010</integer></array>
  </dict>
  <key>installcheck_script</key>
  <string>...</string>
</dict>
```

Unknown top-level or installer keys raise `ProcessorError`.

## MSI identity

When `installer_type` is `msi` and the overlay does not supply codes, the processor runs `msiinfo export <file> Property` (or `msiinfo_path`) and writes `installer.product_code` / `installer.upgrade_code`. Missing `msiinfo` logs and continues without codes.

## Stub identifier

`CimianImporter.recipe` is an empty shared-processor stub with identifier `com.github.moojomoore.CimianImporter`.
