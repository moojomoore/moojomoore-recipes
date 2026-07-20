# moojomoore-recipes

AutoPkg recipes maintained by [moojomoore](https://github.com/moojomoore).

## Recipes

| App | Recipes | Source |
| --- | --- | --- |
| BitBox | `BitBox.download`, `BitBox.pkg`, `BitBox.munki` | GitHub releases from `BitBoxSwiss/bitbox-wallet-app` |
| Ledger Live | `LedgerLive.munki` | Parents `com.github.andredb90.download.LedgerLive` from `autopkg/andredb90-recipes` |
| Sparrow | `Sparrow.download`, `Sparrow.pkg`, `Sparrow.munki` | GitHub releases from `sparrowwallet/sparrow` |
| Trezor Suite | `TrezorSuite.munki` | Parents `com.rderewianko.download.TrezorSuite` from `autopkg/rderewianko-recipes` |

## Parent Repos

Ledger Live and Trezor Suite already have working public download/pkg recipes, so this repo only carries Munki children for those apps. Add the parent repos before running those Munki recipes:

```sh
autopkg repo-add https://github.com/autopkg/andredb90-recipes
autopkg repo-add https://github.com/autopkg/rderewianko-recipes
```
