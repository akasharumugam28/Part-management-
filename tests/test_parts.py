import io

from conftest import create_part, db_rows, part_dict, upload, upload_files


def part_count(app):
    return db_rows(app, "SELECT COUNT(*) FROM parts")[0][0]


def test_create_part(app, auth_client):
    r = create_part(auth_client, 1, {"image": upload("p.png")})
    assert r.status_code == 303 and r.location == "/"
    page = auth_client.get(r.location).get_data(as_text=True)
    assert "Part created successfully." in page and "Image uploaded successfully." in page
    assert "Bolt 1" in page and 'id="total-parts">1<' in page
    row = db_rows(app, "SELECT * FROM parts")[0]
    assert (row["part_name"], row["part_number"], row["line_storage"], row["ols_storage"]) == (
        "Bolt 1", "PN-0001", "L-1", "O-1")
    assert row["image_filename"] and upload_files(app) == [row["image_filename"]]


def test_create_part_without_image(app, auth_client):
    assert create_part(auth_client, 2).status_code == 303
    assert db_rows(app, "SELECT image_filename FROM parts")[0][0] is None
    assert upload_files(app) == []


def test_save_and_add_another(auth_client):
    r = create_part(auth_client, 1, {"add_another": "1"})
    assert r.status_code == 303 and r.location == "/parts/new"
    page = auth_client.get(r.location).get_data(as_text=True)
    assert "Part created successfully." in page and "Create Part" in page


def test_create_form_page(auth_client):
    page = auth_client.get("/parts/new").get_data(as_text=True)
    for name in ("part_name", "part_number", "line_storage", "ols_storage", "image"):
        assert f'name="{name}"' in page


def test_create_part_validation(app, auth_client):
    r = create_part(auth_client, 1, part_number="")
    assert r.status_code == 400
    assert b"Please fill in all required fields." in r.data
    assert b'value="Bolt 1"' in r.data  # entered data preserved
    r = create_part(auth_client, 1, {"image": (io.BytesIO(b"<svg></svg>"), "x.svg")})
    assert r.status_code == 400 and b"Invalid image format." in r.data
    assert part_count(app) == 0
    assert upload_files(app) == []


def test_many_parts(app, auth_client):
    for i in range(60):
        assert create_part(auth_client, i).status_code == 303
    assert part_count(app) == 60
    page = auth_client.get("/").get_data(as_text=True)
    assert 'id="total-parts">60<' in page and "PN-0059" in page


def test_part_details_view(auth_client):
    create_part(auth_client, 7)
    page = auth_client.get("/parts/1").get_data(as_text=True)
    for text in ("Bolt 7", "PN-0007", "L-7", "O-7", "Edit Part", "Delete Part"):
        assert text in page


def test_edit_part(app, auth_client):
    create_part(auth_client, 1)
    create_part(auth_client, 2)
    page = auth_client.get("/parts/1/edit").get_data(as_text=True)
    assert 'value="Bolt 1"' in page and "Save Changes" in page
    r = auth_client.post("/parts/1/update", data=part_dict(1, part_name="Nut", line_storage="LX"))
    assert r.status_code == 303 and r.location == "/"
    assert "Part updated successfully." in auth_client.get(r.location).get_data(as_text=True)
    rows = db_rows(app, "SELECT * FROM parts ORDER BY id")
    assert (rows[0]["id"], rows[0]["part_name"], rows[0]["line_storage"]) == (1, "Nut", "LX")
    assert rows[1]["part_name"] == "Bolt 2"  # other part untouched

    r = auth_client.post("/parts/1/update", data=part_dict(1, part_name=""))
    assert r.status_code == 400 and b"Please fill in all required fields." in r.data
    assert db_rows(app, "SELECT part_name FROM parts WHERE id = 1")[0][0] == "Nut"


def test_edit_part_image_replace_and_remove(app, auth_client):
    create_part(auth_client, 1, {"image": upload("a.png")})
    first = db_rows(app, "SELECT image_filename FROM parts")[0][0]
    auth_client.post("/parts/1/update", data={**part_dict(1), "image": upload("b.webp", "WEBP")},
                     content_type="multipart/form-data")
    second = db_rows(app, "SELECT image_filename FROM parts")[0][0]
    assert second.endswith(".webp") and upload_files(app) == [second]  # old file removed
    assert first != second
    auth_client.post("/parts/1/update", data={**part_dict(1), "remove_image": "1"})
    assert db_rows(app, "SELECT image_filename FROM parts")[0][0] is None
    assert upload_files(app) == []


def test_delete_part(app, auth_client):
    create_part(auth_client, 1, {"image": upload()})
    create_part(auth_client, 2)
    r = auth_client.post("/parts/1/delete")
    assert r.status_code == 303 and r.location == "/"
    page = auth_client.get(r.location).get_data(as_text=True)
    assert "Part deleted successfully." in page and 'id="total-parts">1<' in page
    assert [r["id"] for r in db_rows(app, "SELECT id FROM parts")] == [2]
    assert upload_files(app) == []  # image removed
    assert auth_client.get("/parts/1/delete").status_code == 405  # POST only


def test_edit_delete_unknown_part_404(auth_client):
    assert auth_client.get("/parts/999/edit").status_code == 404
    assert auth_client.post("/parts/999/update", data=part_dict(1)).status_code == 404
    assert auth_client.post("/parts/999/delete").status_code == 404


def test_trolley_routes_are_gone(auth_client):
    for path in ("/trolleys/new", "/trolleys/1", "/trolleys/1/edit"):
        assert auth_client.get(path).status_code == 404
    for path in ("/trolleys/create", "/trolleys/1/update", "/trolleys/1/delete", "/trolleys/1/parts/add"):
        assert auth_client.post(path, data=part_dict(1)).status_code == 404
    page = auth_client.get("/").get_data(as_text=True)
    assert "Trolley" not in page and "trolley" not in page


def test_dashboard_parts_table(auth_client):
    create_part(auth_client, 1)
    page = auth_client.get("/").get_data(as_text=True)
    for col in ("S.No", "Image", "Part Name", "Part Number", "Line Storage", "OLS Storage", "Actions"):
        assert f">{col}<" in page
    for label in ("View part PN-0001", "Edit part PN-0001", "Delete part PN-0001"):
        assert label in page


def test_dashboard_search(auth_client):
    create_part(auth_client, 1, part_name="Hex Bolt")
    create_part(auth_client, 2, part_name="Washer", ols_storage="Rack 9")
    page = auth_client.get("/?q=washer").get_data(as_text=True)
    assert "Washer" in page and "Hex Bolt" not in page
    page = auth_client.get("/?q=rack 9").get_data(as_text=True)
    assert "Washer" in page
    page = auth_client.get("/?q=100%25").get_data(as_text=True)  # LIKE wildcards are literal
    assert "No parts match" in page


def test_invalid_part_id_returns_404(auth_client):
    assert auth_client.get("/parts/999").status_code == 404
    assert auth_client.get("/parts/abc").status_code == 404


def test_oversized_image_rejected(tmp_path):
    from app import create_app
    from conftest import login, make_config
    app = create_app(make_config(tmp_path, MAX_IMAGE_BYTES=2000))
    app.test_cli_runner().invoke(args=["create-admin"])
    c = app.test_client()
    login(c)
    data = upload("big.png", size=(400, 400))[0].getvalue() + b"\0" * 5000
    r = create_part(c, 1, {"image": (io.BytesIO(data), "big.png")})
    assert r.status_code == 400
    assert b"too large" in r.data
