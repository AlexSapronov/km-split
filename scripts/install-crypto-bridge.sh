#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p static/vendor
# Official CryptoPro bridge; keep upstream code out of the public source tree.
curl --fail --location --proto '=https' --tlsv1.2 --max-time 45 \
  https://www.cryptopro.ru/sites/default/files/products/cades/cadesplugin_api.js \
  --output static/vendor/cadesplugin_api.js.new
test "$(wc -c < static/vendor/cadesplugin_api.js.new)" -gt 10000
mv static/vendor/cadesplugin_api.js.new static/vendor/cadesplugin_api.js
printf 'CryptoPro bridge installed. Restart is not required.\n'
