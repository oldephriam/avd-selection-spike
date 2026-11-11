from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from collections.abc import Callable, Sequence
from typing import TextIO

from .roster import normalize_us_number
from .sandbox_adapter import (
    CONFIRMATION_PHRASE,
    DispatchBlocked,
    HmacReferenceMapper,
    InMemoryMockProvider,
    RateLimitPolicy,
    SandboxDispatchLoop,
)


_REGISTRY_VERSION = 1
_VOLUNTEER_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_CAMPAIGN_ID = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_FIXTURE_DISCLOSURE = "Synthetic-only CLI fixture consent; not human consent or legal evidence."

# Deliberately public, fixed test keys. This CLI is never configured for a real provider.
_CONSENT_FIXTURE_KEY = b"synthetic-cli-consent-key-not-for-prod-v1"
_SUPPRESSION_FIXTURE_KEY = b"synthetic-cli-suppression-key-not-prod-v1"


@dataclass(frozen=True, slots=True)
class SyntheticVolunteer:
    volunteer_id: str
    display_name: str
    phone: str


def _mapper() -> HmacReferenceMapper:
    return HmacReferenceMapper(
        consent_key_id="cli-consent-fixture-v1",
        consent_key=_CONSENT_FIXTURE_KEY,
        suppression_key_id="cli-suppression-fixture-v1",
        suppression_key=_SUPPRESSION_FIXTURE_KEY,
    )


def _validate_volunteer(volunteer_id: object, display_name: object, phone: object) -> SyntheticVolunteer:
    if not isinstance(volunteer_id, str) or not _VOLUNTEER_ID.fullmatch(volunteer_id):
        raise ValueError("volunteer ID must be 1-64 lowercase safe characters")
    if not isinstance(display_name, str) or not display_name.startswith("Synthetic "):
        raise ValueError("display name must be explicitly synthetic (start with 'Synthetic ')")
    if not display_name.strip() or any(
        unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} for char in display_name
    ):
        raise ValueError("display name is blank or contains unsafe formatting characters")
    if not isinstance(phone, str):
        raise ValueError("synthetic phone must be a string")
    canonical = normalize_us_number(phone)
    _mapper().for_test_recipient("synthetic-cli-validation", canonical)
    return SyntheticVolunteer(volunteer_id, display_name.strip(), canonical)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _ensure_local_registry_path(path: Path) -> None:
    if os.name != "nt":
        return
    absolute = os.path.abspath(os.fspath(path))
    drive, _ = os.path.splitdrive(absolute)
    if not drive or drive.startswith("\\\\"):
        raise ValueError("registry path must be on a local fixed drive, not a UNC share")
    try:
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(drive + os.sep)
    except (AttributeError, OSError) as exc:
        raise ValueError("cannot verify that the registry drive is local") from exc
    if drive_type != 3:  # DRIVE_FIXED; reject remote, removable, and unknown drives.
        raise ValueError("registry path must be on a local fixed drive")

    absolute_path = Path(absolute)
    current = Path(absolute_path.anchor)
    for component in absolute_path.parts[1:]:
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise ValueError("cannot verify that the registry path stays on local storage") from exc
        if getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError("registry path cannot traverse Windows reparse points")


def _load_registry(path: Path, *, missing_ok: bool = False) -> list[SyntheticVolunteer]:
    _ensure_local_registry_path(path)
    if not path.exists():
        if missing_ok:
            return []
        raise ValueError(f"synthetic volunteer registry does not exist: {path}")
    try:
        document = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("synthetic volunteer registry is not valid UTF-8 JSON") from exc
    if (
        type(document) is not dict
        or set(document) != {"version", "volunteers"}
        or type(document.get("version")) is not int
        or document["version"] != _REGISTRY_VERSION
        or type(document.get("volunteers")) is not list
    ):
        raise ValueError("synthetic volunteer registry has an unsupported schema/version")

    volunteers: list[SyntheticVolunteer] = []
    seen_ids: set[str] = set()
    seen_phones: set[str] = set()
    for record in document["volunteers"]:
        if type(record) is not dict or set(record) != {"id", "name", "phone"}:
            raise ValueError("synthetic volunteer record has unexpected fields")
        volunteer = _validate_volunteer(record["id"], record["name"], record["phone"])
        if volunteer.volunteer_id in seen_ids or volunteer.phone in seen_phones:
            raise ValueError("synthetic volunteer registry contains duplicate IDs or numbers")
        seen_ids.add(volunteer.volunteer_id)
        seen_phones.add(volunteer.phone)
        volunteers.append(volunteer)
    return sorted(volunteers, key=lambda item: item.volunteer_id)


def _save_registry(path: Path, volunteers: Sequence[SyntheticVolunteer]) -> None:
    _ensure_local_registry_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "version": _REGISTRY_VERSION,
        "volunteers": [
            {"id": item.volunteer_id, "name": item.display_name, "phone": item.phone}
            for item in sorted(volunteers, key=lambda item: item.volunteer_id)
        ],
    }
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(document, temporary, ensure_ascii=False, sort_keys=True, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name is not None and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sandbox-pilot-gate",
        description="Synthetic-only offline mock CLI. No live transport or real-pilot mode exists.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    volunteers = commands.add_parser("volunteers", help="manage a local synthetic-only roster")
    volunteer_commands = volunteers.add_subparsers(dest="volunteer_command", required=True)
    for name in ("list", "add", "remove"):
        subcommand = volunteer_commands.add_parser(name)
        subcommand.add_argument("--registry", required=True, type=Path)
        if name == "add":
            subcommand.add_argument("--id", required=True)
            subcommand.add_argument("--name", required=True)
            subcommand.add_argument("--phone", required=True)
        elif name == "remove":
            subcommand.add_argument("--id", required=True)

    dry_run = commands.add_parser(
        "dry-run", help="review and submit one synthetic message to the in-memory mock only"
    )
    dry_run.add_argument("--registry", required=True, type=Path)
    dry_run.add_argument("--volunteer-id", required=True)
    dry_run.add_argument("--campaign-id", required=True)
    dry_run.add_argument("--message", required=True)
    dry_run.add_argument("--approver-one", required=True)
    dry_run.add_argument("--approver-two", required=True)
    dry_run.add_argument("--reviewer", required=True)
    return parser


def _manage_volunteers(args: argparse.Namespace, stdout: TextIO) -> int:
    if args.volunteer_command == "list":
        volunteers = _load_registry(args.registry)
        if not volunteers:
            stdout.write("No synthetic volunteers in registry.\n")
        for item in volunteers:
            stdout.write(f"{item.volunteer_id}\t{item.display_name}\t{item.phone}\n")
        return 0

    if args.volunteer_command == "add":
        volunteer = _validate_volunteer(args.id, args.name, args.phone)
        volunteers = _load_registry(args.registry, missing_ok=True)
        if any(item.volunteer_id == volunteer.volunteer_id for item in volunteers):
            raise ValueError("volunteer ID already exists")
        if any(item.phone == volunteer.phone for item in volunteers):
            raise ValueError("normalized synthetic phone already exists")
        _save_registry(args.registry, [*volunteers, volunteer])
        stdout.write(f"Added synthetic volunteer {volunteer.volunteer_id}.\n")
        return 0

    volunteers = _load_registry(args.registry)
    remaining = [item for item in volunteers if item.volunteer_id != args.id]
    if len(remaining) == len(volunteers):
        raise ValueError("volunteer ID was not found")
    _save_registry(args.registry, remaining)
    stdout.write(f"Removed synthetic volunteer {args.id}.\n")
    return 0


def _dry_run(
    args: argparse.Namespace,
    input_fn: Callable[[str], str],
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if input_fn is input and not sys.stdin.isatty():
        raise ValueError("per-message confirmation requires an interactive terminal; piped input is refused")
    if not _CAMPAIGN_ID.fullmatch(args.campaign_id):
        raise ValueError("campaign ID must be 1-128 safe ASCII characters")
    if not isinstance(args.message, str) or not args.message.strip():
        raise ValueError("a complete non-empty message is required")
    volunteers = _load_registry(args.registry)
    selected = next((item for item in volunteers if item.volunteer_id == args.volunteer_id), None)
    if selected is None:
        raise ValueError("synthetic volunteer ID was not found")

    provider = InMemoryMockProvider()
    loop = SandboxDispatchLoop(
        _mapper(),
        RateLimitPolicy(
            max_campaign_attempts=1,
            campaign_window_seconds=3600,
            max_recipient_attempts=1,
            recipient_window_seconds=86400,
        ),
        provider=provider,
    )
    timestamp = datetime.now(timezone.utc)
    disclosure_hash = hashlib.sha256(_FIXTURE_DISCLOSURE.encode("utf-8")).hexdigest()
    loop.consent_ledger.record_grant(
        args.campaign_id,
        selected.phone,
        disclosure_hash,
        affirmative=True,
        captured_at=timestamp,
        source_ref="synthetic-cli-fixture",
    )
    try:
        loop.kill_switch.enable(actor=args.approver_one, second_approver=args.approver_two)
    except ValueError:
        stdout.write("mock submissions: 0\n")
        raise

    snapshot_source = json.dumps(
        [{"id": item.volunteer_id, "name": item.display_name, "phone": item.phone} for item in volunteers],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    snapshot_id = hashlib.sha256(snapshot_source).hexdigest()
    roster_index = next(i for i, item in enumerate(volunteers) if item.volunteer_id == selected.volunteer_id)
    preview = loop.create_preview(
        args.campaign_id,
        selected.phone,
        selected.display_name,
        args.message,
        snapshot_id=snapshot_id,
        roster_index=roster_index,
    )

    stdout.write("OFFLINE MOCK DRY RUN — no live transport exists in this CLI.\n")
    stdout.write("SYNTHETIC FIXTURE CONSENT ONLY — not human consent or legal evidence.\n")
    stdout.write(f"volunteer: {selected.display_name} ({selected.phone})\n")
    stdout.write(f"campaign: {args.campaign_id}\n")
    stdout.write(f"two-person kill switch: enabled by {args.approver_one} + {args.approver_two}\n")
    stdout.write("full rendered message:\n")
    stdout.write(f"{args.message}\n")
    stdout.write(f"Type exactly {CONFIRMATION_PHRASE!r} to submit this one preview to the in-memory mock: ")
    stdout.flush()

    try:
        answer = input_fn("")
    except (EOFError, OSError):
        stdout.write("\nmock submissions: 0\n")
        raise ValueError("explicit per-message confirmation was not provided")
    if answer != CONFIRMATION_PHRASE:
        stdout.write("\nmock submissions: 0\n")
        raise ValueError("exact per-message confirmation phrase was not provided")

    confirmation = loop.confirm(preview, answer, operator_id=args.reviewer)
    attempt = loop.dispatch(confirmation)
    try:
        stdout.write(f"attempt state: {attempt.state.value}\n")
        stdout.write(f"mock submissions: {len(provider.submissions)}\n")
        stdout.write(f"payload SHA-256: {attempt.payload_sha256}\n")
        stdout.flush()
    except OSError as exc:
        try:
            stderr.write(
                "error: mock submission already occurred; "
                f"outcome={attempt.state.value}; attempt_id={attempt.attempt_id}; "
                f"stdout reporting failed: {exc}\n"
            )
            stderr.flush()
        except OSError:
            pass
        return 3
    return 0 if attempt.state.value == "ACCEPTED_BY_PROVIDER" else 2


def main(
    argv: Sequence[str] | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the offline CLI; an injected input_fn is a trusted test-only hook."""
    output = stdout if stdout is not None else sys.stdout
    errors = stderr if stderr is not None else sys.stderr
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "volunteers":
            return _manage_volunteers(args, output)
        return _dry_run(args, input_fn, output, errors)
    except (DispatchBlocked, OSError, ValueError, json.JSONDecodeError) as exc:
        errors.write(f"error: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
