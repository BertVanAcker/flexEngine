# CP-4 Trust, v1alpha1

All signatures are Ed25519 (raw 32-byte keys, 64-byte signatures, standard base64) except the MCU module container
(ECDSA P-256, CP-6). Code: `identity/crypto.py`, `artefacts/store.py`, `core/reconciler.py`, `cloud/client.py`.
Schema: `trust.v1alpha1.schema.json`. Vectors: `vectors/` (`keys.json` = **test keys only**, derived from public
seed strings). "Canonical JSON" = `util.canonical_json`: sorted keys, no whitespace, UTF-8, `ensure_ascii=False`.

## 1. Keys and trust anchors

| Key | Holder | Device side |
|---|---|---|
| cloud key | flexCloud | `cloudPublicKey` (trust anchor); must equal the onboarding response's `cloudPublicKey` (else FE-ONB-001) [V] |
| release key(s) | release pipeline (`flex release`, `release_key`) | `releasePublicKeys[]`, selected by `keyid` [V] |
| device key | generated on first start, `dataDir/device.key` (base64 raw private key, mode 0600) | public half registered at onboarding |
| module key (P-256) | MCU module signer | `devices[].mcu.modulePublicKey` (hex X‖Y), CP-6 |

`keyid` = `"ed25519:" + sha256(raw public key)[:16 hex]`. Trust-anchor view of one device engine:
`trust#TrustAnchors` = `{cloudPublicKey, releasePublicKeys, caFile?, tlsServerName?, allowSystemTrust?}` (where they
sit in the config file is the EV-04 config schema's business).

**Pinned TLS CA (alpha).** For an `https` `apiUrl` the client trusts only `caFile` (PEM), TLS ≥ 1.2, hostname
checked (or `tlsServerName`); `https` without `caFile` is refused unless `allowSystemTrust: true`
(`cloud/client.py`, EV-06 tests in `tests/security/test_tls.py`). Plain `http` is accepted for the lab/SIL [I: a
production config check should refuse it]. mTLS device identity is beta (EV-21).

**Onboarding (one-time token).** `POST /api/v1/onboarding` `{deviceId, publicKey, token}` (unsigned) registers the
device key; the answer `{deviceId, cloudPublicKey}` must name the provisioned cloud key (else FE-ONB-001). Device
rules [V]: the device key is created (atomic write + fsync) before the first onboarding request and never regenerated
on a retry; the token file (`onboardingTokenFile`) is destroyed only after the cloud confirmed and the `onboarded`
state is durably saved. **Requirement for flexCloud (EV-13): onboarding must be idempotent.** A repeated request
with an already used token, the same `deviceId` and the same `publicKey` the token was used with gets the same
answer as the first one (201, same body) — the device lost the reply, or was killed or lost power between the
cloud's acceptance and its own save, and retries with exactly the same request; without this rule such a device is
bricked (every retry 403). A used token with another `publicKey` or `deviceId`, an unknown token or a token issued
for another device is refused (403 "Invalid or used onboarding token"); a token presented with the wrong device id
is consumed. Reference: `testing/fakecloud.py` (`usedTokens`); tests `tests/property/test_prop_crash.py::
test_kill_during_onboarding_retries`, `tests/property/test_prop_link.py::test_onboarding_ack_lost_retries`.

## 2. Desired-state (manifest) signature

The cloud signs the **exact response body bytes** of `GET …/deployments` (canonical JSON of the DesiredState) and
sends `X-Flex-Manifest-Signature: base64(Ed25519(cloud key, body))` and `ETag: "sha256:<hex(sha256(body))>"`. The
bundle carries the same pair as `desired.json` / `desired.sig`. Device rules, in order [V]:

1. signature verifies with `cloudPublicKey` over the bytes as received (before parsing) — else ignored (FE-DS-001);
2. `deviceId` equals the engine's — else ignored (FE-DS-002);
3. `manifestVersion` ≥ accepted, and equal only with an identical document — else refused and counted in
   `state.downgradesRefused` (FE-DS-003).

Refusals are logged only (no status report) and the previous state keeps running. Vectors:
`vectors/manifest/cases.json` (good, tampered body, wrong key, missing).

## 3. Device request signature

Every southbound request except onboarding carries:

```
X-Flex-Device:    <deviceId>
X-Flex-Date:      <unix ms>                     cloud accepts ±300 s
X-Flex-Nonce:     <12 random bytes, hex>        cloud keeps a bounded replay cache (10 000)
X-Flex-Signature: base64(Ed25519(device key, string-to-sign))
string-to-sign = METHOD "\n" PATH[?QUERY] "\n" X-Flex-Date "\n" X-Flex-Nonce "\n" hex(sha256(body))
```

`PATH` is the request target exactly as sent (no normalisation); an empty body hashes as `sha256("")`. The cloud
scopes every request to the signing device (403 for another device's resources). Vectors:
`vectors/request/cases.json` (fixed date/nonce; Ed25519 is deterministic, so the engine's signer reproduces them [V]).

## 4. Release signature

A referrer manifest of the release (CP-3) whose single layer is the **signature envelope**
`{payload: base64(canonical JSON SignedPayload), signature: base64(Ed25519 over the decoded payload bytes), keyid}`:

```json
{"critical": {"type": "flex release signature v1", "identity": {"repository": "flexnodes/<module>"},
              "image": {"manifest-digest": "sha256:…"}},
 "optional": {"version": "1.2.0", "signedAt": "2026-10-17T00:00:00.000Z",
              "safetyApproval": {"approved": true, "approvers": ["alice", "bob"], "role": "safety-engineer"}}}
```

Device rules (first referrer that passes all wins) [V]: subject = release digest (else "signature subject
mismatch"); `keyid` trusted (else "untrusted key <id>"); signature verifies (else "bad signature");
`critical.image.manifest-digest` = release digest and `critical.identity.repository` = repository (else "signed
payload names a different artefact"). No referrer passes → FE-ART-SIG-001/002. The signature covers the manifest
digest, so the descriptor, payload layers and SBOM are covered through their digests.

## 5. Approval proof for safety-relevant releases

For a deployment with `x-flex-safetyClass: safety-relevant` the verified payload must carry
`optional.safetyApproval.approved == true` with ≥ 2 **distinct** `approvers` (FE-ART-SAF-001), and the descriptor's
`safetyClass` must equal the deployment's (FE-ART-SAF-002 — also refuses a desired state that downgrades a
safety-relevant release to `none`) [V]. The plan-level two-person rule (console-signed approvals, P6) is checked by
flexCloud at apply time and is not visible to the device in v1alpha1 [I: a device-side check of signed plan approvals
is open; FE-S7/EV-24].

## 6. Vectors

| File | Cases |
|---|---|
| `vectors/keys.json` | cloud, release, rogue, device (private, public, keyId, seed) |
| `vectors/manifest/cases.json` | good / tampered / wrong-key / missing |
| `vectors/request/cases.json` | good GET / good POST / tampered body / wrong key |
| `vectors/release/cases.json` | good, tampered blob, unsigned, wrong key, bad signature, safety two approvers, safety single approver, class downgrade |
| `vectors/bundle/*.tar` | good, tampered |
