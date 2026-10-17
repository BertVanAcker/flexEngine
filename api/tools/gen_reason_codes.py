"""Generate flexengine2/api/reason-codes.v1alpha1.json (CP-2 reason-code registry) from the table below.

    flexengine/.venv/bin/python flexengine/api/tools/gen_reason_codes.py [--check]

The engine reports the text and, since 2026-10-17, its code (`reasonCode`); the registry maps every text the engine can report to a
stable code. The table must cover exactly the templates found in the sources by reasonscan.py (dynamic entries
excepted); the script refuses to write otherwise, and tests/api/test_reason_codes.py checks the same.

Fields: reason = status.reason; failure = status.failure; detail = text embedded in another entry's {field}
(e.g. FE-REC-002 "artefact rejected: {detail}"); log = not reported, logged (and counted) only.
Codes are stable: never reuse or renumber a code; a changed text keeps its code (pattern updated), a removed text
is marked "retired": true instead of being deleted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reasonscan  # noqa: E402

OUT = Path(__file__).resolve().parents[2] / "src" / "flexengine2" / "api" / "reason-codes.v1alpha1.json"

# (code, field, stage, profile, template, meaning[, extra])   extra: {"pattern", "example", "dynamic"}
T = [
    # ---- desired-state acceptance (refused as a whole, the previous state is kept; reported once as a device event) ----
    ("FE-DS-001", "event", "accept", "engine", "desired state with bad/missing cloud signature ignored", "X-Flex-Manifest-Signature / desired.sig does not verify with cloudPublicKey"),
    ("FE-DS-002", "event", "accept", "engine", "desired state for another device ignored", "DesiredState.deviceId differs from this engine's deviceId"),
    ("FE-DS-003", "event", "accept", "engine", "manifestVersion {mv} <= accepted {accepted}: rollback attack? ignored", "lower manifestVersion, or equal with a different body (counted in state.downgradesRefused)"),
    ("FE-DS-004", "event", "accept", "engine", "desired state is not valid JSON: {error}", "the signed body does not parse as JSON"),
    ("FE-DS-005", "event", "accept", "engine", "desired state does not match the CP-1 schema: {errors}", "the signed document fails desired-state.v1alpha1 (first three violations, '; '-joined)"),
    ("FE-ONB-001", "log", "onboarding", "engine", "cloud key at onboarding does not match the provisioned trust anchor", "onboarding response cloudPublicKey differs from cfg cloudPublicKey; the device stays un-onboarded"),
    ("FE-BUN-001", "log", "bundle", "engine", "bundle without desired state", "offline bundle lacks desired.json or desired.sig"),
    ("FE-BUN-002", "log", "bundle", "engine", "bundle desired state is not signed by the cloud key", "the bundle's desired.sig does not verify with cloudPublicKey; nothing from the bundle is stored"),
    # ---- reconciler: top-level reasons ----
    ("FE-REC-001", "reason", "prepare", "engine", "no driver for {profile}", "x-flex-driver names a profile with no driver on this device; manifest version rejected"),
    ("FE-REC-002", "reason", "prepare", "engine", "artefact rejected: {detail}", "release verification or driver preflight refused the artefact; the whole manifest version is rejected and the previous state keeps running (detail = an FE-ART/-CLD/-*-PRE code)"),
    ("FE-REC-003", "reason", "apply", "engine", "apply failed", "driver apply raised DriverError; rolled back (detail in failure, FE-REC-005)"),
    ("FE-REC-004", "reason", "health-gate", "engine", "health check failed", "health gate failed; rolled back (detail in failure: FE-HG-*, or the driver's failed() text)"),
    ("FE-REC-005", "failure", "apply", "engine", "apply failed: {error}", "the DriverError text of the failed apply (an FE-CTR/-NAT/-SYD/-APL/-MCU/-PLC code)"),
    ("FE-REC-006", "reason", "drift", "engine", "drift repaired (workload was missing)", "a committed workload disappeared and was re-created"),
    ("FE-REC-007", "reason", "supervise", "engine", "supervisor: {event}", "local supervision event: restarted (state Installed) or start-limit-hit (state Failed)",
     {"pattern": "^supervisor: (restarted|start-limit-hit)$", "example": "supervisor: start-limit-hit"}),
    ("FE-REC-008", "reason", "drift", "engine", "drift repaired (executor ran another release)", "supervise(): a committed workload's executor ran another digest than the committed one (drift by digest); the committed release was re-applied (EV-13)"),
    ("FE-REC-009", "reason", "resume", "engine", "interrupted install of {version} abandoned (no longer desired)", "restart after a crash during an install the accepted desired state no longer asks for: the uncommitted release was taken off the executor -- the committed version restored (state Installed) or the deployment removed (state Removed) (EV-13)"),
    # ---- activation gate (state Pending) ----
    ("FE-GATE-001", "reason", "gate", "engine", "awaiting safe state: {signals}", "required safe-state signals are not true (FE-S6; implicit machine_parked, operator_confirmed for safety-relevant)"),
    ("FE-GATE-002", "reason", "gate", "engine", "outside activation window {start}-{end} UTC", "x-flex-activation.window is closed"),
    ("FE-GATE-003", "reason", "gate", "engine", "held: PLC not verified: {requires}", "plc:<dep>[@hash] requirement not yet verified by plc-verify"),
    # ---- health gate (status.failure) ----
    ("FE-HG-001", "failure", "health-gate", "engine", "timeout after {timeoutSeconds} s", "no successThreshold consecutive healthy probes within timeoutSeconds"),
    ("FE-HG-002", "failure", "commit", "engine", "commit failed: {error}", "two-phase driver commit raised DriverError after a passed gate"),
    # ---- cloud answers ----
    ("FE-CLD-001", "detail", "prepare", "engine", "{status}: {body}", "definite 4xx from flexCloud while fetching an artefact (cloud.client.Rejected)",
     {"pattern": "^[4][0-9]{2}: (.*)$", "example": '404: {"type": "about:blank", "title": "Blob unknown"}'}),
    # ---- artefact store (CP-3/CP-4) ----
    ("FE-ART-DIG-001", "detail", "prepare", "engine", "digest mismatch for {digest}", "blob put into the store (bundle import) does not hash to its name"),
    ("FE-ART-DIG-002", "detail", "prepare", "engine", "digest mismatch: {repo} {kind}/{digest} hashes to {actual}", "downloaded manifest/blob does not hash to the requested digest (tampered)"),
    ("FE-ART-STO-001", "detail", "prepare", "engine", "{digest} not in local store and no cloud client (offline bundle incomplete)", "offline operation: blob missing"),
    ("FE-ART-STO-002", "detail", "prepare", "engine", "no referrers cached and no cloud client", "offline operation: no signature index"),
    ("FE-ART-SIG-001", "detail", "prepare", "engine", "release not signed by a trusted key (no signature)", "no signature referrer"),
    ("FE-ART-SIG-002", "detail", "prepare", "engine", "release not signed by a trusted key ({reasons})", "signature referrers exist, none verifies; reasons = '; '-joined FE-ART-SIG-003..006"),
    ("FE-ART-SIG-003", "detail", "prepare", "engine", "signature subject mismatch", "signature manifest subject is another digest"),
    ("FE-ART-SIG-004", "detail", "prepare", "engine", "untrusted key {keyid}", "keyid not among releasePublicKeys"),
    ("FE-ART-SIG-005", "detail", "prepare", "engine", "bad signature", "Ed25519 signature does not verify (tampered payload or wrong key under a trusted keyid)"),
    ("FE-ART-SIG-006", "detail", "prepare", "engine", "signed payload names a different artefact", "critical.image.manifest-digest or identity.repository differs"),
    ("FE-ART-SAF-001", "detail", "prepare", "engine", "safety-relevant release lacks a two-person approval in its signed payload", "safetyApproval not approved or < 2 distinct approvers"),
    ("FE-ART-SAF-002", "detail", "prepare", "engine", "safety class mismatch: release descriptor says {release}, desired state says {desired}", "descriptor.safetyClass != x-flex-safetyClass"),
    ("FE-ART-REL-001", "detail", "prepare", "engine", "not a flexNode release", "config mediaType is not the flexNode descriptor"),
    ("FE-ART-REL-002", "detail", "prepare", "engine", "release has no {profile} payload for platform {platform} (has {have})", "multi-platform release without a layer for the executor's platform()"),
    ("FE-ART-REL-003", "detail", "prepare", "engine", "release has no payload for profile {profile}", "no layer annotated dev.flex.profile = x-flex-driver"),
    ("FE-ART-REL-098", "detail", "prepare", "engine", "'{key}'", "malformed release document (KeyError text)",
     {"pattern": "^'[^']*'$", "example": "'layers'", "dynamic": True}),
    ("FE-ART-REL-099", "detail", "prepare", "engine", "{json error}", "release document is not JSON (ValueError text)",
     {"pattern": "^(Expecting|Extra data|Unterminated string|Invalid control character|Invalid \\\\escape).*$",
      "example": "Expecting value: line 1 column 1 (char 0)", "dynamic": True}),
    # ---- container (docker/podman/ankaios) ----
    ("FE-CTR-001", "detail", "apply", "container", "{cmd}...: rc={rc} {stderr}", "runtime CLI call failed"),
    ("FE-CTR-002", "detail", "apply", "container", "payload did not contain image {ref}", "docker load did not provide dev.flex.imageRef"),
    ("FE-CTR-003", "detail", "apply", "container", "runOptions.{key} is not supported (supported: {keys})", "descriptor runOptions key unknown"),
    ("FE-CTR-004", "detail", "apply", "container", "runOptions.{key}={value} is not in this device's allow-list", "runOptions value not in allowedRunOptions"),
    ("FE-CTR-005", "detail", "apply", "container", "runOptions are not supported by the Ankaios driver (use docker/podman)", "Ankaios with runOptions"),
    ("FE-CTR-006", "detail", "apply", "container", "ank apply failed: {stderr}", "ank CLI failed"),
    ("FE-CTR-007", "detail", "apply", "container", "ankaios did not start the workload within 60 s", "Ankaios start timeout"),
    ("FE-CTR-008", "detail", "apply", "container", "{cmd}: executable not found ({error})", "runtime CLI (docker/podman/ank) not installed; capabilities() reports available: false"),
    ("FE-CTR-009", "detail", "apply", "container", "{cmd}...: no answer within {timeout} s", "runtime CLI call hung (daemon unresponsive): executor unreachable"),
    ("FE-CTR-012", "detail", "apply", "container", "{cmd}: cannot run ({error})", "runtime CLI not executable (OSError other than not found)"),
    ("FE-CTR-013", "detail", "apply", "container", "{cmd}...: unreadable output ({error})", "runtime CLI answered with output that is not JSON"),
    ("FE-CTR-010", "failure", "health-gate", "container", "container dead", "fail-fast: container state dead"),
    ("FE-CTR-011", "failure", "health-gate", "container", "container exited (code {code}, {restarts} restarts, restart policy {policy}:{max})", "fail-fast: exited and the restart policy will not restart it"),
    # ---- native (process) ----
    ("FE-NAT-001", "detail", "apply", "native", "process did not start", "unit process not alive within 5 s"),
    ("FE-NAT-010", "failure", "health-gate", "native", "process exited (Restart=never)", "fail-fast"),
    ("FE-NAT-011", "failure", "health-gate", "native", "process exited, start limit hit after {restarts} restarts", "fail-fast"),
    # ---- native (systemd, EV-19) ----
    ("FE-SYD-001", "detail", "apply", "native", "systemctl: executable not found ({error})", "systemctl not installed; capabilities() reports available: false"),
    ("FE-SYD-002", "detail", "apply", "native", "systemctl {cmd}: no answer within {timeout} s", "systemctl call hung (manager or D-Bus unresponsive): executor unreachable"),
    ("FE-SYD-003", "detail", "apply", "native", "systemctl: cannot run ({error})", "systemctl not executable (OSError other than not found)"),
    ("FE-SYD-004", "detail", "apply", "native", "systemctl {cmd}: {stderr} (exit {rc})", "systemctl call failed (e.g. 'Failed to connect to bus', 'Interactive authentication required' = no polkit rule / not root)"),
    ("FE-SYD-005", "detail", "apply", "native", "systemctl show {unit}: unreadable output", "systemctl show answered without ActiveState"),
    ("FE-SYD-006", "detail", "apply", "native", "native payload unusable: {error}", "the verified payload could not be unpacked (tar error, disk full, unsafe member refused by the data filter)"),
    ("FE-SYD-007", "detail", "apply", "native", "cannot hand the data directory to user {user}: {error}", "configured drivers.native.user does not exist on the device"),
    ("FE-SYD-008", "detail", "apply", "native", "unit {unit} did not start ({state}/{substate}, result {result})", "unit not active within 10 s after systemctl restart (exec failed, crashed at once)"),
    ("FE-SYD-009", "detail", "remove", "native", "systemctl stop {unit}: {stderr} (exit {rc})", "stopping the unit failed during remove"),
    ("FE-SYD-PRE-001", "detail", "preflight", "native", "native payload is not a tar.gz archive: {error}", ""),
    ("FE-SYD-PRE-002", "detail", "preflight", "native", "native payload member {name} escapes the payload directory", "absolute path or '..' in the archive"),
    ("FE-SYD-PRE-003", "detail", "preflight", "native", "native payload has neither app.py nor unit.json", "no entry point (also raised by apply)"),
    ("FE-SYD-PRE-004", "detail", "preflight", "native", "unit.json is not valid JSON: {error}", ""),
    ("FE-SYD-PRE-005", "detail", "preflight", "native", "unit.json: ExecStart must be a non-empty list of strings", ""),
    ("FE-SYD-PRE-006", "detail", "preflight", "native", "restartPolicy.policy {policy} is not supported by the systemd driver (on-failure, always, never)", ""),
    ("FE-SYD-010", "failure", "health-gate", "native", "unit {unit} failed: start limit hit after {restarts} restarts", "fail-fast: ActiveState=failed, Result=start-limit-hit (StartLimitBurst = maxRetries + 1 within 60 s)"),
    ("FE-SYD-011", "failure", "health-gate", "native", "unit {unit} failed ({result}, exit status {code}) and will not be restarted", "fail-fast: ActiveState=failed (Restart=no, or a result Restart= does not cover)"),
    ("FE-SYD-012", "failure", "health-gate", "native", "unit {unit} exited (status {code}) and will not be restarted", "fail-fast: clean exit with Restart=on-failure / no"),
    ("FE-SYD-020", "log", "config", "native", "scope must be system or user, got {scope}", "drivers.native.scope invalid (the config schema refuses it first)"),
    # ---- applab ----
    ("FE-APL-001", "detail", "apply", "applab", "arduino-app-cli {cmd}: {error} (exit {rc})", "arduino-app-cli call failed"),
    ("FE-APL-002", "detail", "apply", "applab", "{cmd}: {output} (exit {rc})", "ssh/scp call failed (remote mode)"),
    ("FE-APL-003", "detail", "apply", "applab", "payload is not an App Lab release (no dev.flex.releaseName)", "layer annotation missing"),
    ("FE-APL-004", "detail", "apply", "applab", "bad release archive: {error}", "archive check failed (FE-APL-ARD-*)"),
    ("FE-APL-005", "detail", "apply", "applab", "archive root {root} != annotation {release}", "archive root folder differs from dev.flex.releaseName"),
    ("FE-APL-006", "detail", "apply", "applab", "release built for {target}, this board is a {board}", "wrong board"),
    ("FE-APL-007", "detail", "apply", "applab", "contract hash mismatch in the archive sources: descriptor {want}, found {got}", "FLEX01 contract hash in the sources differs from dev.flex.contractHash"),
    ("FE-APL-008", "detail", "apply", "applab", "one app per board: {releases} already deployed on this board", "exclusive executor already used by another deployment"),
    ("FE-APL-009", "detail", "apply", "applab", "one app per board: foreign app running: {apps}", "an app this engine does not own runs (takeoverForeignApps false)"),
    ("FE-APL-010", "failure", "health-gate", "applab", "app release:{release} failed", "fail-fast: app state failed"),
    ("FE-APL-011", "failure", "health-gate", "applab", "app release:{release} not installed", "fail-fast: release vanished"),
    ("FE-APL-012", "detail", "apply", "applab", "arduino-app-cli {cmd}: cannot run {binary} ({error})", "arduino-app-cli (or ssh in remote mode) is not installed or not executable; capabilities() reports available: false"),
    ("FE-APL-013", "detail", "apply", "applab", "arduino-app-cli {cmd}: no answer within {timeout} s", "arduino-app-cli call hung (cliTimeout)"),
    ("FE-APL-ARD-001", "detail", "apply", "applab", "{file} is not a release archive: expected '.ard'", ""),
    ("FE-APL-ARD-002", "detail", "apply", "applab", "{file} is not a release archive: {error}", ""),
    ("FE-APL-ARD-003", "detail", "apply", "applab", "{file} is not rooted at a single release folder", ""),
    ("FE-APL-ARD-004", "detail", "apply", "applab", "{file} no release manifest, it is not a release", ""),
    ("FE-APL-ARD-005", "detail", "apply", "applab", "its release manifest states no name or no target", ""),
    ("FE-APL-ARD-006", "detail", "apply", "applab", "{file} is schema {schema} and needs a newer cli", ""),
    # ---- mcu-module / mcu-firmware ----
    ("FE-MCU-PRE-001", "detail", "preflight", "mcu-module", "not an MCU module: {error}", "payload is no FLXM container (FE-MCU-FLXM-001..003)"),
    ("FE-MCU-PRE-002", "detail", "preflight", "mcu-module", "module container: {why}", "FLXM integrity/signature (FE-MCU-FLXM-004..006)"),
    ("FE-MCU-PRE-003", "detail", "preflight", "mcu-module", "module ABI {major}.{minor} incompatible with base ABI {bmajor}.{bminor} on {child}", ""),
    ("FE-MCU-PRE-004", "detail", "preflight", "mcu-module", "module built for {arch}/{fabi}-float, {child} is {barch}/{bfabi}-float", ""),
    ("FE-MCU-PRE-005", "detail", "preflight", "mcu-module", "module needs capabilities {missing} that {child} does not offer", ""),
    ("FE-MCU-PRE-006", "detail", "preflight", "mcu-module", "interface hash in module ({module}…) differs from its release descriptor ({descriptor}…)", ""),
    ("FE-MCU-PRE-007", "detail", "preflight", "mcu-module", "interface hash mismatch: deployment expects {expect}…, module has {module}…", "parameters._interfaceHash differs"),
    ("FE-MCU-PRE-008", "detail", "preflight", "mcu-module", "module imports {symbols} outside the flex API allow-list", ""),
    ("FE-MCU-PRE-009", "detail", "preflight", "mcu-module", "module of {size} B exceeds the {slot} B slot", ""),
    ("FE-MCU-PRE-010", "detail", "preflight", "mcu-module", "module needs ~{ram} B of LLEXT heap; {heap} B heap must hold two modules", ""),
    ("FE-MCU-FLXM-001", "detail", "preflight", "mcu-module", "shorter than an FLXM header", ""),
    ("FE-MCU-FLXM-002", "detail", "preflight", "mcu-module", "not an FLXM v1 container", "magic / header size / format version"),
    ("FE-MCU-FLXM-003", "detail", "preflight", "mcu-module", "truncated ELF", ""),
    ("FE-MCU-FLXM-004", "detail", "preflight", "mcu-module", "ELF SHA-256 mismatch (tampered)", ""),
    ("FE-MCU-FLXM-005", "detail", "preflight", "mcu-module", "module not signed", ""),
    ("FE-MCU-FLXM-006", "detail", "preflight", "mcu-module", "bad module signature", "ECDSA P-256 over header||ELF does not verify with modulePublicKey"),
    ("FE-MCU-FLXM-007", "detail", "release", "mcu-module", "unknown MCU capability {name}", "release-side helper (flxm.caps_mask)"),
    ("FE-MCU-FLXM-008", "detail", "release", "mcu-module", "not a FLEX01 hash: {value}", "release-side helper (flxm.iface_bytes)"),
    ("FE-MCU-001", "detail", "apply", "mcu-module", "MCU unreachable: {error}", "SMP link down (also mcu-firmware)"),
    ("FE-MCU-002", "detail", "apply", "mcu-firmware", "MCU did not come back: {error}", "no SMP answer after reset"),
    ("FE-MCU-003", "detail", "apply", "mcu-module", "no free module socket on {child} ({n} sockets)", ""),
    ("FE-MCU-004", "detail", "apply", "mcu-module", "upload failed at offset {off}/{len} after {s}s: {error}", ""),
    ("FE-MCU-005", "detail", "apply", "mcu-module", "device refused the upload: {reason}", ""),
    ("FE-MCU-006", "detail", "apply", "mcu-module", "device rejected activation: {reason}", ""),
    ("FE-MCU-007", "detail", "commit", "mcu-module", "confirm failed: {reason}", ""),
    ("FE-MCU-008", "detail", "apply", "mcu-module", "flexmod {cmd}: SMP error {response}", "SMP error answer of the flexmod group"),
    ("FE-MCU-009", "detail", "apply", "mcu-firmware", "{request}: {response}", "SMP error answer of a standard group (img_mgmt/os_mgmt)",
     {"pattern": "^([A-Za-z]+(Request|Response)): (.*)$", "example": "ImageUploadWriteRequest: {'rc': 3}"}),
    ("FE-MCU-010", "failure", "health-gate", "mcu-module", "module fault on {child}: {reason}", "fail-fast: base image reverted the module under test"),
    ("FE-MCU-011", "failure", "health-gate", "mcu-module", "module under test fails its steps on {child} (last rc {rc}, {errors} errors)", "fail-fast"),
    ("FE-MCUFW-PRE-001", "detail", "preflight", "mcu-firmware", "not an MCUboot image", "payload lacks the MCUboot magic"),
    ("FE-MCUFW-001", "detail", "apply", "mcu-firmware", "uploaded image not visible in slot1: {images}", ""),
    # ---- plc-verify ----
    ("FE-PLC-PRE-001", "detail", "preflight", "plc-verify", "not a PLC verify artefact (flex-plc.json): {error}", ""),
    ("FE-PLC-PRE-002", "detail", "preflight", "plc-verify", "PLC {target} is not configured on {device} (known: {known})", ""),
    ("FE-PLC-PRE-003", "detail", "preflight", "plc-verify", "verify spec hash {spec} differs from the release descriptor {descriptor}", ""),
    ("FE-PLC-PRE-004", "detail", "preflight", "plc-verify", "payload annotation dev.flex.ifaceHash differs from the release descriptor", ""),
    ("FE-PLC-PRE-005", "detail", "preflight", "plc-verify", "interface hash mismatch: deployment expects {expect}…, release has {descriptor}…", "parameters._interfaceHash differs"),
    ("FE-PLC-PRE-006", "detail", "preflight", "plc-verify", "this engine never downloads PLC programs (decision 2026-10-16): release must say download: false", ""),
    ("FE-PLC-001", "detail", "health-gate", "plc-verify", "PLC {target} is not configured on {device}", ""),
    ("FE-PLC-002", "detail", "health-gate", "plc-verify", "unknown verify transport {via}", ""),
    ("FE-PLC-010", "failure", "health-gate", "plc-verify", "{target} runs interface {running}…, the release expects {expect}… ({detail}): program not provisioned for this release", "fail-fast: the PLC answers with another hash"),
]


def build() -> dict:
    codes = []
    for row in T:
        code, field, stage, profile, template, meaning = row[:6]
        extra = row[6] if len(row) > 6 else {}
        e = {"code": code, "field": field, "stage": stage, "profile": profile, "template": template,
             "pattern": extra.get("pattern") or reasonscan.pattern_of(template), "meaning": meaning}
        if extra.get("example"):
            e["example"] = extra["example"]
        if extra.get("dynamic"):
            e["dynamic"] = True
        codes.append(e)
    return {
        "$comment": "Generated by flexengine/api/tools/gen_reason_codes.py; do not edit by hand. CP-2 reason-code registry.",
        "apiVersion": "flex.dev/v1alpha1", "kind": "ReasonCodeRegistry",
        "fields": {"reason": "status.reason", "failure": "status.failure",
                   "detail": "embedded in another entry's field (e.g. FE-REC-002 'artefact rejected: {detail}')",
                   "event": "DeviceEvent.reason (POST /devices/{id}/events; desired state refused)",
                   "log": "not reported; engine log only"},
        "rules": "Codes are stable (never reused or renumbered). Since 2026-10-17 the engine adds "
                 "the code as `reasonCode` next to every reported reason (status records and device events). A text matches the entry whose "
                 "pattern fullmatches it; when several do, the most specific one (most literal characters) wins "
                 "(flexengine2.contracts.reason_code).",
        "codes": codes,
    }


def problems(reg: dict) -> list[str]:
    scanned = {reasonscan.normalize(t) for t in reasonscan.scan()}
    out = []
    seen = set()
    for e in reg["codes"]:
        if e["code"] in seen:
            out.append(f"duplicate code {e['code']}")
        seen.add(e["code"])
        if not e.get("dynamic") and reasonscan.normalize(e["template"]) not in scanned:
            out.append(f"{e['code']}: template not found in the engine sources: {e['template']!r}")
    have = {reasonscan.normalize(e["template"]) for e in reg["codes"]}
    out += [f"unregistered reason text in the engine sources: {t!r}" for t in sorted(scanned - have)]
    return out


if __name__ == "__main__":
    reg = build()
    bad = problems(reg)
    if bad:
        print("\n".join(bad), file=sys.stderr)
        raise SystemExit(1)
    text = json.dumps(reg, indent=1, ensure_ascii=False) + "\n"
    if "--check" in sys.argv:
        raise SystemExit(0 if OUT.read_text(encoding="utf-8") == text else "reason-codes.v1alpha1.json is stale")
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT} ({len(reg['codes'])} codes)")
