# AOSP API 35 launch-provenance verification

## Outcome

The hosted A/B run completed successfully for commit `209f7c39b021606a802f8592a0f897e837fc66e1` (workflow run `43063042033`). The test compared the instrumentation-launched baseline against launching the exported `ProbeLaunchActivity` wrapper from host ADB after the instrumentation lifecycle monitor reported readiness.

Both variants retained the two existing `startActivityForResult()` hops, the Contacts caller gate, and the Contacts `ListView.performItemClick()` selection path. No changes were made to or rebuilds performed of the pinned Contacts APK.

## Pinned environment

- System image: `system-images;android-35;default;x86_64`
- API / ABI: Android 35 / x86_64
- Build fingerprint: `Android/sdk_phone64_x86_64/emu64x:15/AE3A.240806.019/12368160:userdebug/test-keys`
- Picker: `com.android.contacts`, version `1.7.34`, code `10734`
- Picker signing certificate SHA-256: `28bbfe4a7b97e74681dc55c2fbb6ccb8d6c74963733f6af6ae74d8c3a6e879fd`
- Hosted evidence artifact: `aosp-api35-selection-avd-43063042033` (14-day retention)

## A/B observations

- **Instrumentation baseline:** Contacts reported no calling activity at the gate. The gate failed closed before selection. Probe received `RESULT_CANCELED`, with no result data or URI.
- **Host-ADB launch:** Contacts reported the expected Probe caller. The gate passed; the existing `performItemClick()` path selected the unique row matching the fixture display name and normalized phone. Probe received `RESULT_OK` with a readable Contacts content URI and a granted read permission.
- **Readback identity:** The exact fixture display name and normalized phone matched. The host provider Data-row ID comparison was false; do not describe this as an exact row-ID match. The discrepancy remains documented rather than waived as a verified row-ID link.
- **Framework trace:** The bounded trace saw the relevant app components but did not expose `resultTo`, request codes, or a recognized activity-manager launch/result event. The observed A/B difference supports launch provenance/timing as a factor in this harness; it does not prove the internal framework mechanism.

The run therefore verifies the requested caller/result behavior for host-ADB launch and preserves the baseline failure for comparison. It does not establish a row-ID correspondence or justify changing the pinned picker or framework.

## Guardrails retained

- Fail closed at the Contacts caller gate; retain both result-launch calls and the actual Contacts selection click path.
- Keep the pinned AOSP Contacts binary, version, and certificate unchanged.
- Keep the seeder limited to `WRITE_CONTACTS`; do not grant `READ_CONTACTS` to it.
- Keep ADB traces bounded and sanitized; do not log raw phone numbers or contact URIs.
- The Probe and harness remain selection/readback-only. No message composition, SMS/RCS transport, or dispatch is part of this verification.

## To repeat

Push the change to `main` and run the **AOSP Android 35 selection-only AVD** workflow in verify mode. Require KVM and the configured picker identity variables. Inspect the artifact for that exact commit and confirm the pin, caller-gate outcome, Probe result callback, readback labels, and any row-ID comparison before updating this record.

## Separate work required before any real messaging

This verification does not authorize or implement sending. Keep real messaging disabled while developing a separate reviewed flow. Before enabling a real transport, require a visible recipient and full-message preview, explicit per-message user confirmation, sandbox tests using synthetic contacts, duplicate/failure handling, and a review of permissions, consent, and applicable policy. Do not reuse the selection-only probe as a sender.
