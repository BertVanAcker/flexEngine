import hashlib, json, sys
from pathlib import Path
from flexengine2.drivers.arduino_ota import compose_command
d = Path(sys.argv[1])
key = bytes(range(0xa0, 0xc0))
img = d / "img.bin"
img.write_bytes(bytes(range(256)) * 300)
sha = hashlib.sha256(img.read_bytes()).hexdigest()
params = {"board": "arduino:mbed_portenta:envie_m7", "sha256": sha, "size": img.stat().st_size,
          "url": "http://192.168.1.50:8470/ota/Abc_dEF-123xyz.ota", "version": "0.2.0"}
c = compose_command(key, params)
(d / "ok.json").write_text(json.dumps(c, separators=(",", ":")))
bad = dict(c, params=dict(params, url="http://evil/x.ota"))
(d / "tampered.json").write_text(json.dumps(bad))
extra = compose_command(key, dict(params, extra="x"))
(d / "extra.json").write_text(json.dumps(extra))
expired = compose_command(key, params, expiry_seconds=-10)
(d / "expired.json").write_text(json.dumps(expired))
print(key.hex(), sha)
