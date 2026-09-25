# moojomoore-recipes

AutoPkg recipes maintained by [moojomoore](https://github.com/moojomoore).

## Recipes

| App | Recipes | Source |
| --- | --- | --- |
| BitBox | `BitBox.download`, `BitBox.pkg`, `BitBox.munki` | GitHub releases from `BitBoxSwiss/bitbox-wallet-app` |
| Blockstream | `Blockstream.download`, `Blockstream.pkg`, `Blockstream.munki` | GitHub releases from `Blockstream/green_qt` |
| Ledger Live | `LedgerLive.munki` | Parents `com.github.andredb90.download.LedgerLive` from `autopkg/andredb90-recipes` |
| Sparrow | `Sparrow.download`, `Sparrow.pkg`, `Sparrow.munki` | GitHub releases from `sparrowwallet/sparrow` |
| Trezor Suite | `TrezorSuite.munki` | Parents `com.rderewianko.download.TrezorSuite` from `autopkg/rderewianko-recipes` |

## Shared processors

| Processor | Identifier | Notes |
| --- | --- | --- |
| CimianImporter | `com.github.moojomoore.CimianImporter/CimianImporter` | Import Windows installers into a Cimian repo (MunkiImporter counterpart). See `CimianImporter/README.md`. |

Add with:

```bash
autopkg repo-add autopkg/moojomoore-recipes
```
