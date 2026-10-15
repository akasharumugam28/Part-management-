"""Server-side field validation for parts."""

REQUIRED_MESSAGE = "Please fill in all required fields."

PART_LIMITS = {
    "part_name": ("Part Name", 120),
    "part_number": ("Part Number", 80),
    "line_storage": ("Line Storage", 120),
    "ols_storage": ("Current Storage", 120),
}


def clean_fields(source, limits):
    """Return (data, errors) where errors maps field -> message."""
    data, errors = {}, {}
    for field, (label, max_len) in limits.items():
        raw = source.get(field, "")
        value = " ".join(str(raw).split()) if raw is not None else ""
        data[field] = value
        if not value:
            errors[field] = f"{label} is required."
        elif len(value) > max_len:
            errors[field] = f"{label} must be {max_len} characters or fewer."
    return data, errors


QTY_PRESETS = (20, 40)
QTY_MAX = 9999
QTY_MESSAGE = f"Trolley Qty must be a whole number from 1 to {QTY_MAX}."


def clean_qty(raw):
    """Optional whole number 1..QTY_MAX. Returns (value or None, error or None)."""
    raw = str(raw or "").strip()
    if not raw:
        return None, None
    if not raw.isdigit() or not 1 <= int(raw) <= QTY_MAX:
        return None, QTY_MESSAGE
    return int(raw), None


SUB_PART_LIMITS = {
    "part_name": ("Sub-part Name", 120),
    "part_number": ("Sub-part Number", 80),
}


def _clean_with_qty(source, limits):
    data, errors = clean_fields(source, limits)
    data["trolley_qty"], qty_error = clean_qty(source.get("trolley_qty"))
    if qty_error:
        errors["trolley_qty"] = qty_error
    return data, errors


TROLLEY_COUNT_LABELS = {"total_trolleys": "Total Trolley", "empty_trolleys": "Empty Trolley"}
EMPTY_OVER_TOTAL = "Empty Trolley cannot be more than Total Trolley."


def clean_count(raw, label):
    """Optional whole number 0..QTY_MAX. Returns (value or None, error or None)."""
    raw = str(raw or "").strip()
    if not raw:
        return None, None
    if not raw.isdigit() or int(raw) > QTY_MAX:
        return None, f"{label} must be a whole number from 0 to {QTY_MAX}."
    return int(raw), None


def clean_part(source):
    data, errors = _clean_with_qty(source, PART_LIMITS)
    for field, label in TROLLEY_COUNT_LABELS.items():
        data[field], error = clean_count(source.get(field), label)
        if error:
            errors[field] = error
    total, empty = data["total_trolleys"], data["empty_trolleys"]
    if total is not None and empty is not None and empty > total:
        errors["empty_trolleys"] = EMPTY_OVER_TOTAL
    return data, errors


def clean_sub_part(source):
    return _clean_with_qty(source, SUB_PART_LIMITS)


def error_message(errors):
    """The toast shown for a rejected form."""
    number_fields = ("trolley_qty", *TROLLEY_COUNT_LABELS)
    if all(f in number_fields for f in errors):
        return next(iter(errors.values()))
    return REQUIRED_MESSAGE
