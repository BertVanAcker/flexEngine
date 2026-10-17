# flexEngineV2 alpha security (EV-06, DL-4)

Alpha = **TLS to the control plane with a pinned lab CA + the existing Ed25519 request signatures**.
mTLS device certificates and TPM-backed keys come in beta (EV-21).

## What alpha guarantees

| Area | Guarantee | Code |
|---|---|---|
| Transport (V-46) | `https://` apiUrl: TLS ≥ 1.2, certificate must chain to the configured CA file **only** (no system store), hostname verified. Unknown CA, wrong hostname, expired cert → `TlsError` (an `Offline`: the engine keeps retrying, status/log shows the TLS reason). `https` without `caFile` is refused when the client is built. | `cloud/client.py` |
| Plain HTTP | refused unless the config sets `allowPlainHttp: true` (or the engine runs with `FLEX_ALLOW_PLAIN_HTTP=1`, as the test suites do); then it logs `plain HTTP: lab/test only` once per client (lead decision 2026-10-17). | `cloud/client.py` |
| Request auth (V-45) | unchanged: every device request carries an Ed25519 signature over method, path, time, nonce, body hash; onboarding checks the cloud key against the provisioned trust anchor. | `identity/crypto.py` |
| Key storage (V-48) | device key created 0600 in a 0700 directory; a key that is group/other accessible, a symlink, or owned by another uid is refused (`KeyPermissionError`). `fixPermissions: true` repairs the mode (never ownership). | `identity/keys.py` |
| Secrets (E11) | secrets come from files only (`read_secret_file`: 0600, own uid), never from the environment. The onboarding token is read, and deleted (overwritten, unlinked) only after the cloud confirmed onboarding; reuse is impossible. Child processes get an environment without `FLEX_*` and `*TOKEN*/*SECRET*/*KEY*/*PASSWORD*` variables (`scrub_env`). | `identity/secrets.py` |
| Egress | the engine only talks to `apiUrl`; no third-party lookups (`ipinfo.io`-style) by default. | — |

## Config keys (per device or under `cloud:`)

| Key | Meaning |
|---|---|
| `apiUrl` | `https://host:port` for the lab; `http://` only with `allowPlainHttp: true` (tests, loopback TLS proxy) |
| `allowPlainHttp` | `false` by default; `true` allows a plain `http://` apiUrl (lab/test only) |
| `caFile` | PEM file of the lab CA (the only trust anchor for TLS) |
| `tlsServerName` | name checked against the server certificate when `apiUrl` uses an IP address that is not in the SAN (e.g. `lattepanda.lab`) |
| `allowSystemTrust` | `true` = use the OS trust store instead of a pinned CA; off by default, not for the lab |
| `fixPermissions` | `true` = chmod an over-permissive device key to 0600 instead of refusing to start |
| `onboardingTokenFile` | one-time onboarding token file (0600); deleted after successful onboarding |

## Creating the lab CA

With the demo PKI (EC P-256 root, 10 years; server leaf with SANs, 2 years; keys 0600):

```sh
cd demo/cloud && python -m democloud.pki --dir DATA/tls --name lattepanda.lab --ip 192.168.50.10
# -> DATA/tls/ca.crt (give this to devices as caFile), DATA/tls/server.crt + server.key (cloud side)
```

Or with openssl:

```sh
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
  -subj "/CN=flex lab CA" -addext basicConstraints=critical,CA:TRUE,pathlen:0 \
  -addext keyUsage=critical,keyCertSign,cRLSign -keyout ca.key -out ca.crt
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=lattepanda.lab" \
  -keyout server.key -out server.csr
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 730 -out server.crt \
  -extfile <(printf "subjectAltName=DNS:lattepanda.lab,IP:192.168.50.10\nextendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE")
chmod 600 ca.key server.key
```

Keep `ca.key` off the devices. Device config: `apiUrl: https://192.168.50.10:8443`, `caFile: /etc/flexengine/ca.crt`,
`tlsServerName: lattepanda.lab` (only if the IP is not in the SAN). The device-side `tlsproxy.py` client mode is no
longer needed.

## Deferred to beta

* mTLS with device certificates (SPIFFE-shaped URI SAN), enrolment, rotation, revocation (EV-21)
* TPM 2.0-backed device key on the LattePanda (EV-21)
* cosign/Notation-compatible artefact signatures, signed SBOM (EV-22)
* CRL/OCSP for the lab CA (none in alpha: short-lived lab material)

## Tests

`tests/security/` (layer 4, push tier): valid CA ok, wrong CA / wrong hostname / expired refused, https without
`caFile` refused, plain-HTTP warning, `TlsError` is `Offline`; key perms and other-uid refusal; secrets never from
env, perms, one-time token, `scrub_env`. Throwaway CA generated at test time.
