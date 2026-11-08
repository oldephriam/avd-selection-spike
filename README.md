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
- `SelectionOnlyPort` remains the only port exposed to `RecipientSelectionProbe`. The offline tests use a deterministic fake; `AdbSelectionAdapter` is now implemented separately with bounded argument-array subprocess calls, fresh hierarchy/screenshot capture, pinned package attestation, and capture-bound candidate taps. Its tests use a mock process runner; they do not contact an emulator. The host still has no Android SDK, Java, Gradle, or ADB, so live AVD validation is pending.
- `AdbSelectionAdapter` uses the exact serial allowlist, and `DeviceObserver` distinguishes `PICKER_ACTIVE` from `PROBE_RETURNED`; returning to the probe requires the pinned probe app, one probe-owned surface, visible readback, and no external-surface flag.

## Android selection fixture

- `android/probe` launches one `ACTION_PICK` flow for phone contacts and exposes the selected row ID, display name, and normalized key in an accessibility readback view. Its source manifest declares no permissions.
- `android/seeder` creates one deterministic synthetic contact and can remove the stored fixture; its source manifest declares only `android.permission.WRITE_CONTACTS`. Grant that permission only to the seeder, seed the contact, then uninstall the seeder before probe testing.
- `docs/adb-selection-adapter-contract.md` fixes the adapter allowlist to `emulator-5554` and prohibits general shell, text entry, installation, messaging, and dispatch capabilities.
- Static contract checks run with the Python suite. The Android packages have not been compiled or exercised on an emulator because Java, Gradle, ADB, and the Android SDK are absent here.

## AOSP API 35 CI spike

- `.github/workflows/avd-selection-spike.yml` provisions CLI tools and an `android-35;default;x86_64` AOSP image, requires `/dev/kvm`, boots the emulator as a dedicated OS account, and applies verified IPv4/IPv6 non-loopback egress blocks scoped to that account (the Actions runner remains unrestricted).
- The first manual `pin_mode=discover` run is the default. If any of the four AOSP repository variables are blank, verify mode automatically falls back to discovery: it logs and uploads the build fingerprint, contacts version name/code, signing certificate, and raw phone-picker resolver output, then skips APK build and probing. Discovery records identity but does not claim the contacts package is the active picker; inspect `pickerIntentMatchesPackage` and `pickerIntentResolverOutput` in the artifact. Verify mode fails closed unless the phone-picker intent resolves to `com.android.contacts` and all four configured variables match. Set `AOSP_35_BUILD_FINGERPRINT`, `AOSP_CONTACTS_VERSION_NAME`, `AOSP_CONTACTS_VERSION_CODE`, and `AOSP_CONTACTS_CERT_SHA256` from the artifact before rerunning.
- The verified path builds both APKs, runs the offline regression suite, seeds one synthetic contact, uninstalls the seeder, and executes the live selection/readback probe. This live path has not yet run; the target picker pins must first be discovered on the hosted AVD and configured in repository variables.

## Audit and campaign safety

- A roster snapshot is immutable and keyed by normalized unique US phone numbers.
- Events are append-only JSONL with a SHA-256 hash chain. The campaign prototype assumes a single writer; it is tamper-evident, not WORM storage.
- A lost acknowledgment or recovered in-flight dispatch becomes `NEEDS_REVIEW` and is never automatically retried.
- No real message can be sent by this package.

