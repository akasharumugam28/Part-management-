"""Data access functions. Every query is parameterized."""
from werkzeug.security import check_password_hash, generate_password_hash

from database import get_db, insert, like_operator, utcnow

PART_FIELDS = ("part_name", "part_number", "line_storage", "ols_storage")


# --------------------------------------------------------------------- admin

def get_admin(admin_id):
    return get_db().execute("SELECT * FROM admin WHERE id = ?", (admin_id,)).fetchone()


def get_admin_by_username(username):
    # Usernames are case-insensitive (lower() matches SQLite's NOCASE for ASCII
    # and the PostgreSQL unique index on lower(username)).
    return get_db().execute(
        "SELECT * FROM admin WHERE lower(username) = lower(?)", (username.strip(),)
    ).fetchone()


def admin_exists():
    return get_db().execute("SELECT 1 FROM admin LIMIT 1").fetchone() is not None


def create_admin(username, password):
    get_db().execute(
        "INSERT INTO admin (username, password_hash, must_change_password, created_at) "
        "VALUES (?, ?, 0, ?)",
        (username.strip(), generate_password_hash(password), utcnow()),
    )


def reset_admin_password(admin_id, password):
    get_db().execute(
        "UPDATE admin SET password_hash = ?, must_change_password = 0 WHERE id = ?",
        (generate_password_hash(password), admin_id),
    )


# Used so a login attempt for an unknown username costs the same as a real one.
_DUMMY_HASH = generate_password_hash("not-a-real-password")


def verify_admin(username, password):
    admin = get_admin_by_username(username) if username else None
    if admin is None:
        check_password_hash(_DUMMY_HASH, password or "")
        return None
    return admin if check_password_hash(admin["password_hash"], password or "") else None


# --------------------------------------------------------------------- parts

def _escape_like(term):
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_parts(search=None):
    """All parts; with ``search``, those matching it or having a matching sub-part."""
    db = get_db()
    sql = "SELECT * FROM parts"
    params = ()
    if search:
        like = f"%{_escape_like(search)}%"
        op = like_operator(db)  # case-insensitive on both backends
        sql += (
            f" WHERE part_name {op} ? ESCAPE '\\' OR part_number {op} ? ESCAPE '\\'"
            f" OR line_storage {op} ? ESCAPE '\\' OR ols_storage {op} ? ESCAPE '\\'"
            " OR EXISTS (SELECT 1 FROM sub_parts s WHERE s.part_id = parts.id"
            f" AND (s.part_name {op} ? ESCAPE '\\' OR s.part_number {op} ? ESCAPE '\\'))"
        )
        params = (like,) * 6
    sql += " ORDER BY id"
    return db.execute(sql, params).fetchall()


def count_parts():
    return get_db().execute("SELECT COUNT(*) FROM parts").fetchone()[0]


def get_part(part_id):
    return get_db().execute("SELECT * FROM parts WHERE id = ?", (part_id,)).fetchone()


def create_part(data):
    now = utcnow()
    return insert(
        get_db(),
        "INSERT INTO parts (part_name, part_number, line_storage, ols_storage, "
        "trolley_qty, image_filename, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (*(data[f] for f in PART_FIELDS), data.get("trolley_qty"), data.get("image_filename"), now, now),
    )


def update_part(part_id, data):
    get_db().execute(
        "UPDATE parts SET part_name = ?, part_number = ?, line_storage = ?, ols_storage = ?, "
        "trolley_qty = ?, image_filename = ?, updated_at = ? WHERE id = ?",
        (*(data[f] for f in PART_FIELDS), data.get("trolley_qty"), data.get("image_filename"),
         utcnow(), part_id),
    )


def delete_part(part_id):
    get_db().execute("DELETE FROM parts WHERE id = ?", (part_id,))


# ----------------------------------------------------------------- sub-parts

SUB_PART_FIELDS = ("part_name", "part_number")


def list_sub_parts(part_id):
    return get_db().execute(
        "SELECT * FROM sub_parts WHERE part_id = ? ORDER BY id", (part_id,)
    ).fetchall()


def sub_parts_by_part(part_ids=None):
    """Map part id -> list of its sub-parts (for all parts, or only ``part_ids``)."""
    rows = get_db().execute("SELECT * FROM sub_parts ORDER BY part_id, id").fetchall()
    grouped = {}
    for r in rows:
        if part_ids is None or r["part_id"] in part_ids:
            grouped.setdefault(r["part_id"], []).append(r)
    return grouped


def count_sub_parts():
    return get_db().execute("SELECT COUNT(*) FROM sub_parts").fetchone()[0]


def get_sub_part(part_id, sub_id):
    return get_db().execute(
        "SELECT * FROM sub_parts WHERE id = ? AND part_id = ?", (sub_id, part_id)
    ).fetchone()


def create_sub_part(part_id, data):
    now = utcnow()
    return insert(
        get_db(),
        "INSERT INTO sub_parts (part_id, part_name, part_number, trolley_qty, image_filename, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (part_id, *(data[f] for f in SUB_PART_FIELDS), data.get("trolley_qty"),
         data.get("image_filename"), now, now),
    )


def update_sub_part(sub_id, data):
    get_db().execute(
        "UPDATE sub_parts SET part_name = ?, part_number = ?, trolley_qty = ?, image_filename = ?, "
        "updated_at = ? WHERE id = ?",
        (*(data[f] for f in SUB_PART_FIELDS), data.get("trolley_qty"), data.get("image_filename"),
         utcnow(), sub_id),
    )


def delete_sub_part(sub_id):
    get_db().execute("DELETE FROM sub_parts WHERE id = ?", (sub_id,))
