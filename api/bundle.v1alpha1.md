# CP-3 Artefact access profile and offline bundle, v1alpha1

What the engine reads to get from a digest in the desired state to a verified payload on disk
(`artefacts/store.py`), and the offline bundle format (`ArtefactStore.import_bundle`). Producers: `flex release`
(flexmodel/release/pipeline.py), `testing/release.py`; server side: `testing/fakecloud.py`. Schemas:
`oci-profile.v1alpha1.schema.json`, `descriptor.v1alpha1.schema.json`. Vectors: `vectors/release/`, `vectors/bundle/`.

## 1. OCI distribution subset

Base URL = the engine's `apiUrl`; every request is device-signed (CP-4). The repository is the desired state's
`repository` without `oci://<host>/` (`flexnodes/<module>`; it may contain `/`).

| Request | Response | Engine check |
|---|---|---|
| `GET /v2/<repo>/manifests/<digest>` | release manifest bytes (`application/vnd.oci.image.manifest.v1+json`) | `sha256(bytes) == digest` else FE-ART-DIG-002 [V] |
| `GET /v2/<repo>/referrers/<digest>` | OCI image index; entries with `artifactType` `application/vnd.flex.signature.v1+json` | tried in order until one verifies (CP-4) [V] |
| `GET /v2/<repo>/manifests/<sig digest>` | signature manifest (`subject.digest` = release digest) | hash, subject [V] |
| `GET /v2/<repo>/blobs/<digest>` | blob bytes | hash [V] |

Not used: tags, `HEAD`, ranged GETs (EV-26), uploads, catalog. 404/4xx → `Rejected` → "artefact rejected: <status>:
<body>" (FE-CLD-001); 5xx/429/network → retry later, local state unchanged. Every blob is stored in a
content-addressed store `dataDir/cas/sha256/<hex>` after its hash check; a cached blob is re-hashed on read and
re-fetched when corrupt. A non-empty referrers answer is cached in `dataDir/cas/referrers/<hex>` ("no signature
yet" is never cached).

## 2. Release manifest (flexNode release)

```
manifest  mediaType  application/vnd.oci.image.manifest.v1+json, artifactType application/vnd.flex.node.v1+json
  config  application/vnd.flex.node.descriptor.v1+json   descriptor JSON (canonical), see descriptor schema
  layers  one or more payload layers, annotated dev.flex.profile = driver profile
          + application/spdx+json (dev.flex.role: sbom)   must be present and intact
referrer  signature manifest (artifactType application/vnd.flex.signature.v1+json, config application/vnd.oci.empty.v1+json,
          one layer = signature envelope, subject = the release manifest)
```

Layer selection [V]: layers whose `dev.flex.profile` equals the deployment's `x-flex-driver`; if the driver has a
`platform()` (mcu-module: `{dev.flex.arch, dev.flex.floatAbi}`), the layer whose annotations match it; a single
layer without platform annotations still fits. Required annotations per profile (schema): container
`dev.flex.imageRef` (`…@sha256:…`, run by digest); applab `dev.flex.releaseName`; mcu-module `dev.flex.arch`,
`dev.flex.floatAbi`; os-rauc `dev.flex.compatible` (matched against the driver's `platform()`); arduino-ota
`dev.flex.board` (FQBN, matched against `platform()` / the target board) and `dev.flex.firmwareVersion`. Payload media types:

| Profile | Payload mediaType |
|---|---|
| container | `application/vnd.flex.payload.image.docker-archive.v1.tar` (docker save / podman oci-archive) |
| native | `application/vnd.flex.payload.native.v1.tar+gzip` (app.py or unit.json, release.json) |
| applab | `application/vnd.flex.payload.applab-release.v1.tar+gzip` (`.ard`, CP-6) |
| mcu-module | `application/vnd.flex.payload.mcu-module.v1+flxm` (FLXM v1, CP-6) |
| mcu-firmware | `application/vnd.flex.payload.mcu-firmware.v1+mcuboot` (MCUboot image) |
| plc-verify | `application/vnd.flex.payload.plc-artefact.v1.tar+gzip` (`flex-plc.json` + generated sources, CP-6) |
| os-rauc | `application/vnd.flex.payload.os-rauc.v1+raucb` (signed RAUC bundle; annotation `dev.flex.compatible` = RAUC compatible, required; RAUC re-verifies the CMS signature with its keyring) |
| arduino-ota | `application/vnd.flex.payload.arduino-ota.v1+ota` (Arduino_Portenta_OTA image, `make_ota.py`; annotations `dev.flex.board`, `dev.flex.firmwareVersion` required; the board checks no signature, see CP-6 §5) |
| system | `application/vnd.flex.payload.system.v1+json` (record only; no driver deploys it) |

Descriptor checks [V]: config mediaType (FE-ART-REL-001), `safetyClass` = deployment class (FE-ART-SAF-002), payload
for the profile (FE-ART-REL-002/003). The descriptor supplies the workload's `health`, `restartPolicy`, `runOptions`.

## 3. Offline bundle (`bundle.tar`)

Served by `GET /api/v1/devices/{id}/bundle` (and the admin variant); imported with `Engine.import_bundle` (later
`flexengine bundle import`, CP-7). Plain POSIX tar (ustar/PAX, uncompressed) [V]:

| Member | Content |
|---|---|
| `desired.json` (first) | exact desired-state bytes as served online (canonical JSON) |
| `desired.sig` (second) | base64 Ed25519 signature of `desired.json` by the cloud key (= `X-Flex-Manifest-Signature`) |
| `blobs/sha256/<hex>` | every release manifest named in `desired.json`, each of its signature manifests (referrers), and every config and layer blob of those manifests; each member hashes to its name |

Import rules [V]: every `blobs/` member is hash-checked on the way in (any mismatch refuses the whole bundle:
FE-ART-DIG-001); missing `desired.json`/`desired.sig` → FE-BUN-001; referrer indexes are rebuilt from the signature
manifests found; then the desired state goes through the same acceptance as an online pull (signature,
device id, manifestVersion; CP-4). Other member names are ignored by the engine; producers must not add any (the contract test
`bundle_layout_problems` rejects them). A bundle may carry a superset of blobs.
Verification afterwards needs no network (`test_bundle_import_then_offline_verify`).

Limits (alpha) [I]: one signature referrer per release survives the index rebuild; no compression; the whole tar
is read into memory (multi-GB images: EV-26).

## 4. Test vectors

`api/tools/gen_vectors.py` (fixed keys and times; the test regenerates and compares byte for byte):
`vectors/release/oci/` = OCI layout with releases `vec_native`, `vec_unsigned`, `vec_rogue`, `vec_badsig`,
`vec_safety` 1.0.0 (two approvers) / 1.0.1 (one approver); `vectors/release/cases.json` = expected outcome and
reason code per case; `vectors/bundle/bundle.tar` (+ `bundle-tampered.tar`) for device `vec-dev`.
