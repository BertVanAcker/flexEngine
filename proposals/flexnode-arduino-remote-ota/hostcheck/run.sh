#!/bin/sh
# PC check of the proposed src/commands/otaCommand.cpp against the flexEngineV2 driver's signer.
#   sh run.sh <flexNode-arduino checkout at b83b5fa>      (the checkout is only read; the patch is applied to a copy)
# Needs g++, the system libmbedcrypto (2.28, Ubuntu 24.04: libmbedcrypto7t64), curl, and flexengine2 importable
# (PYTHONPATH=<repo>/flexengine/src).
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
FNA=${1:?flexNode-arduino checkout}
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
cp -r "$FNA/src" "$FNA/README.adoc" "$W/"
mkdir -p "$W/examples/features"
patch -s -p1 -d "$W" < "$HERE/../remote-ota.patch"
curl -sSL -o "$W/ArduinoJson.h" https://github.com/bblanchon/ArduinoJson/releases/download/v6.21.5/ArduinoJson-v6.21.5.h
MBED=$(ls /usr/lib/*/libmbedcrypto.so.7 2>/dev/null | head -1)
g++ -std=c++14 -Wall -Wextra -DARDUINOJSON_ENABLE_ARDUINO_STRING=0 -I "$HERE/shim" -I "$W" -I "$W/src" \
    "$HERE/main.cpp" "$W/src/commands/otaCommand.cpp" "$MBED" -o "$W/check"
python3 "$HERE/gen.py" "$W" >/dev/null
K=a0a1a2a3a4a5a6a7a8a9aaabacadaeafb0b1b2b3b4b5b6b7b8b9babbbcbdbebf
"$W/check" $K "$W/ok.json" "$W/img.bin"
"$W/check" $K "$W/tampered.json"
"$W/check" $K "$W/extra.json"
"$W/check" $K "$W/expired.json"
"$W/check" 0000000000000000000000000000000000000000000000000000000000000000 "$W/ok.json"
python3 -c "import hashlib,sys; print('python sha256=' + hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$W/img.bin"
