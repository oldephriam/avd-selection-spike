from __future__ import annotations

import csv
import hashlib
import io
import re
from pathlib import Path

from .models import Contact, RosterSnapshot


class RosterError(ValueError):
    pass


def normalize_us_number(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise RosterError("phone must be a complete 10-digit US number")
    if digits[0] in "01" or digits[3] in "01":
        raise RosterError("phone has an invalid US area/exchange code")
    return "+1" + digits


def load_roster(path: str | Path, *, campaign_id: str) -> RosterSnapshot:
    source = Path(path).read_bytes()
    source_hash = hashlib.sha256(source).hexdigest()
    try:
        text = source.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise RosterError("roster CSV has no header")
        fields = {field.strip().casefold(): field for field in reader.fieldnames if field}
        name_col = fields.get("name") or fields.get("full name")
        phone_col = fields.get("phone") or fields.get("number")
        if not name_col or not phone_col:
            raise RosterError("roster requires name and phone columns")
        contacts: list[Contact] = []
        seen: set[str] = set()
        for row_number, row in enumerate(reader):
            name = (row.get(name_col) or "").strip()
            raw_phone = (row.get(phone_col) or "").strip()
            if not name or not raw_phone:
                raise RosterError(f"row {row_number + 2} has a blank name or phone")
            recipient_key = normalize_us_number(raw_phone)
            if recipient_key in seen:
                raise RosterError(f"duplicate normalized phone at row {row_number + 2}")
            seen.add(recipient_key)
            contacts.append(Contact(len(contacts), name, recipient_key))
    except UnicodeDecodeError as exc:
        raise RosterError("roster must be UTF-8 CSV") from exc
    if not contacts:
        raise RosterError("roster contains no contacts")
    snapshot_id = hashlib.sha256(
        (campaign_id + ":" + source_hash).encode("utf-8")
    ).hexdigest()
    return RosterSnapshot(campaign_id, source_hash, snapshot_id, tuple(contacts))
