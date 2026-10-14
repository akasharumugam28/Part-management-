import sqlite3

import pytest

from app import create_app
from conftest import create_part, db_rows, make_config


def qty(app, part_id=1):
    return db_rows(app, "SELECT trolley_qty FROM parts WHERE id = ?", (part_id,))[0][0]


@pytest.mark.parametrize("value,expected", [("20", 20), ("40", 40), (" 75 ", 75), ("", None)])
def test_create_saves_qty(app, auth_client, value, expected):
    assert create_part(auth_client, trolley_qty=value).status_code == 303
    assert qty(app) == expected


@pytest.mark.parametrize("value", ["0", "-5", "2.5", "abc", "10000"])
def test_invalid_qty_rejected(app, auth_client, value):
    r = create_part(auth_client, trolley_qty=value)
    assert r.status_code == 400
    assert b"Trolley Qty must be a whole number from 1 to 9999." in r.data
    assert db_rows(app, "SELECT COUNT(*) FROM parts")[0][0] == 0


def test_missing_fields_still_show_required_message(auth_client):
    r = create_part(auth_client, part_name="", trolley_qty="abc")
    assert b"Please fill in all required fields." in r.data


def test_edit_changes_and_clears_qty(app, auth_client):
    create_part(auth_client, trolley_qty="20")
    form = auth_client.get("/parts/1/edit").get_data(as_text=True)
    assert 'name="trolley_qty" value="20"' in form
    assert 'data-qty-preset="20"' in form and 'data-qty-preset="40"' in form
    data = {"part_name": "Bolt 1", "part_number": "PN-0001", "line_storage": "L-1", "ols_storage": "O-1"}
    auth_client.post("/parts/1/update", data={**data, "trolley_qty": "40"})
    assert qty(app) == 40
    auth_client.post("/parts/1/update", data={**data, "trolley_qty": ""})
    assert qty(app) is None


def test_qty_shown_next_to_image_and_on_details(auth_client):
    create_part(auth_client, trolley_qty="40")
    html = auth_client.get("/").get_data(as_text=True)
    row = html[html.index('class="cell-image"'):]
    assert row.index('class="qty-pill"') < row.index('class="cell-name"')
    assert '<span class="qty-pill" title="Trolley Qty">40</span>' in html
    assert "<dt>Trolley Qty</dt><dd>40</dd>" in auth_client.get("/parts/1").get_data(as_text=True)


def test_database_rejects_bad_qty(app):
    from database import connect

    conn = connect(app.config["DATABASE"])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO parts (part_name, part_number, line_storage, ols_storage, trolley_qty, "
            "created_at, updated_at) VALUES ('a', 'b', 'c', 'd', 0, 'x', 'x')"
        )
    conn.close()


def test_existing_database_gets_qty_column(tmp_path):
    cfg = make_config(tmp_path)
    conn = sqlite3.connect(cfg["DATABASE"])
    conn.execute(
        "CREATE TABLE parts (id INTEGER PRIMARY KEY AUTOINCREMENT, part_name TEXT NOT NULL, "
        "part_number TEXT NOT NULL, line_storage TEXT NOT NULL, ols_storage TEXT NOT NULL, "
        "image_filename TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO parts VALUES (1, 'Bolt', 'PN', 'L', 'O', NULL, 'c', 'u')")
    conn.commit()
    conn.close()

    app = create_app(cfg)
    rows = db_rows(app, "SELECT part_name, trolley_qty FROM parts")
    assert [tuple(r) for r in rows] == [("Bolt", None)]
    create_app(cfg)  # running the migration again is harmless
