#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
case "$project_root" in /srv/skew/*) ;; *) echo 'Run this build on the authorized remote server.' >&2; exit 1;; esac
mkdir -p "$project_root/tools/gas-browser" "$project_root/artifacts/fuel"
cp "$project_root/scripts/fuel-browser/package.json" "$project_root/scripts/fuel-browser/package-lock.json" "$project_root/tools/gas-browser/"
cd "$project_root/tools/gas-browser"
npm ci --ignore-scripts --no-audit --no-fund
./node_modules/.bin/esbuild ../../scripts/swap_crypto_entry.js --bundle --platform=browser --format=iife --global-name=SkewSwapCrypto --minify --outfile=../../web/swap-crypto.js --alias:ethers=./node_modules/ethers/lib.esm/index.js --metafile=../../artifacts/fuel/crypto-bundle.json
