# Offline campaign prototype

This repository contains a non-sending campaign state-machine prototype and a selection-only device observation harness. `Runner` accepts `FakeDriver` only; the recipient-selection port protocol exposes device listing, package inspection, screen capture, and selection of a candidate from a capture-bound picker—no message composer or dispatch method.

## Run tests

From this directory:

```sh
python -m unittest discover -s tests -v
```

## Recipient probe boundaries

- `DeviceObserver` requires exactly one allowlisted serial and pinned device fingerprint; it separately verifies the probe package/version/certificate and the phase-specific foreground package/version/certificate. The probe must have zero declared and zero granted permissions, and the temporary seeder must be absent. Captures require valid screenshot/hierarchy hashes, exactly one expected app surface, no external-surface leak, and no send affordance.
- `RecipientSelectionProbe` uses bounded passive polling, unique normalized recipient-key matching, capture-bound selection, and post-selection readback. It rejects duplicates, mismatches, missing evidence, layout changes, and process/session changes.
- `SelectionOnlyPort` remains the only port exposed to `RecipientSelectionProbe`. Offline adapter tests use a mock process runner and do not contact an emulator; hosted live AVD validation is documented below.
- `AdbSelectionAdapter` uses the exact serial allowlist, and `DeviceObserver` distinguishes `PICKER_ACTIVE` from `PROBE_RETURNED`; returning to the probe requires the pinned probe app, one probe-owned surface, visible readback, and no external-surface flag.

## Android selection fixture

- `android/probe` launches one `ACTION_PICK` flow for phone contacts and exposes the selected row ID, display name, and normalized key in an accessibility readback view. Its source manifest declares no permissions.
- `android/seeder` creates one deterministic synthetic contact and can remove the stored fixture; its source manifest declares only `android.permission.WRITE_CONTACTS`. Grant that permission only to the seeder, seed the contact, then uninstall the seeder before probe testing.
- `docs/adb-selection-adapter-contract.md` fixes the adapter allowlist to `emulator-5554` and prohibits general shell, text entry, installation, messaging, and dispatch capabilities.
- Static contract checks run with the Python suite. Hosted workflow run `43063042033` built the Android packages and exercised live synthetic-contact selection; see [the launch-provenance verification record](docs/aosp-api35-launch-provenance-verification.md).

## AOSP API 35 CI spike

- `.github/workflows/avd-selection-spike.yml` provisions CLI tools and an `android-35;default;x86_64` AOSP image, requires `/dev/kvm`, boots the emulator as a dedicated OS account, and applies verified IPv4/IPv6 non-loopback egress blocks scoped to that account (the Actions runner remains unrestricted).
- The workflow supports `discover` and `verify`; verify requires KVM, configured AOSP identity variables, and an exact `com.android.contacts` phone-picker resolution. Discovery records the image fingerprint, Contacts version and certificate, and resolver output but does not claim the target picker has been verified.
- The verified path builds the APKs, runs the offline regression suite, seeds one synthetic contact, uninstalls the seeder, and runs live selection/readback on the hosted AVD. The latest successful A/B check is run `43063042033`; its observed caller/result outcomes and the Data-row-ID caveat are recorded in [the launch-provenance verification report](docs/aosp-api35-launch-provenance-verification.md).

## Audit and campaign safety

- A roster snapshot is immutable and keyed by normalized unique US phone numbers.
- Events are append-only JSONL with a SHA-256 hash chain. The campaign prototype assumes a single writer; it is tamper-evident, not WORM storage.
- A lost acknowledgment or recovered in-flight dispatch becomes `NEEDS_REVIEW` and is never automatically retried.
- No real message can be sent by this package.

