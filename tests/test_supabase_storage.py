"""Images in Supabase Storage (against an in-memory fake of its REST API)."""
import io
import logging
import sqlite3
import zipfile
from pathlib import Path

import pytest
from openpyxl import load_workbook

import images
import models
from app import create_app
from conftest import create_part, db_rows, image_bytes, login, make_config, part_dict, upload
from fake_supabase import BUCKET, FAKE_KEY, FAKE_URL, FakeSupabase


@pytest.fixture
def fake():
    return FakeSupabase()


@pytest.fixture
def sb_app(tmp_path, fake):
    app = create_app(make_config(tmp_path, SUPABASE_URL=FAKE_URL, SUPABASE_SERVICE_ROLE_KEY=FAKE_KEY))
    with app.app_context():
        images.storage().session = fake  # the real client, talking to the fake
    assert app.test_cli_runner().invoke(args=["create-admin"]).exit_code == 0
    return app


@pytest.fixture
def sb(sb_app):
    client = sb_app.test_client()
    assert login(client).status_code == 302
    return client


def image_name(app, table="parts", row_id=1):
    return db_rows(app, f"SELECT image_filename FROM {table} WHERE id = ?", (row_id,))[0][0]


def update_part(client, part_id=1, **fields):
    data = part_dict(part_id)
    data.update(fields)
    return client.post(f"/parts/{part_id}/update", data=data, content_type="multipart/form-data")


def test_upload_goes_to_private_bucket_not_disk(sb_app, sb, fake):
    r = create_part(sb, 1, {"image": upload("photo.png")})
    assert r.status_code == 303
    name = image_name(sb_app)
    assert images.is_safe_name(name) and name.endswith(".png")
    data, content_type = fake.objects()[name]
    assert content_type == "image/png" and data.startswith(b"\x89PNG")
    assert not Path(sb_app.config["UPLOAD_FOLDER"]).exists()


def test_pages_use_batch_signed_urls(sb_app, sb, fake):
    for i in (1, 2, 3):
        create_part(sb, i, {"image": upload()})
    names = [image_name(sb_app, row_id=i) for i in (1, 2, 3)]
    fake.calls.clear()
    r = sb.get("/")
    html = r.get_data(as_text=True)
    for name in names:
        assert f"{FAKE_URL}/storage/v1/object/sign/{BUCKET}/{name}?token=tok-{name}" in html
    assert fake.calls == ["sign_many"]  # one request for the whole page
    assert f"img-src 'self' {FAKE_URL}" in r.headers["Content-Security-Policy"]
    assert FAKE_KEY not in html
    for path in ("/parts/1", "/parts/1/edit"):
        assert f"token=tok-{names[0]}" in sb.get(path).get_data(as_text=True)


def test_uploads_route_redirects_to_signed_url(sb_app, sb, fake):
    create_part(sb, 1, {"image": upload()})
    name = image_name(sb_app)
    r = sb.get(f"/uploads/{name}")
    assert r.status_code == 302
    assert r.location == f"{FAKE_URL}/storage/v1/object/sign/{BUCKET}/{name}?token=tok-{name}"
    assert r.headers["Cache-Control"] == "private, max-age=300"
    assert sb.get("/uploads/0123456789abcdef0123456789abcdef.png").status_code == 404


@pytest.mark.parametrize("path", ["../secret.png", "a/b.png", ".hidden.png", "x%2F..%2Fy.png"])
def test_uploads_route_rejects_unsafe_names_without_calling_storage(sb, fake, path):
    fake.calls.clear()
    assert sb.get(f"/uploads/{path}").status_code == 404
    assert fake.calls == []


def test_uploads_route_requires_login(sb_app):
    r = sb_app.test_client().get("/uploads/whatever.png")
    assert r.status_code == 302 and "/login" in r.location


def test_replace_and_remove_image(sb_app, sb, fake):
    create_part(sb, 1, {"image": upload()})
    first = image_name(sb_app)
    assert update_part(sb, image=upload("new.webp", "WEBP")).status_code == 303
    second = image_name(sb_app)
    assert second.endswith(".webp") and list(fake.objects()) == [second]  # old one deleted
    assert update_part(sb, remove_image="1").status_code == 303
    assert image_name(sb_app) is None and fake.objects() == {}
    assert first != second


def test_delete_part_removes_its_and_sub_part_images(sb_app, sb, fake):
    create_part(sb, 1, {"image": upload()})
    sb.post("/parts/1/sub-parts/create", content_type="multipart/form-data",
            data={"part_name": "Washer", "part_number": "SP-1", "image": upload()})
    assert len(fake.objects()) == 2
    assert sb.post("/parts/1/delete").status_code == 303
    assert fake.objects() == {}


@pytest.mark.parametrize("failure", [500, 403, "network"])
def test_upload_failure_returns_503_and_saves_nothing(sb_app, sb, fake, failure, caplog):
    fake.fail["upload"] = failure
    with caplog.at_level(logging.WARNING):
        r = create_part(sb, 1, {"image": upload()})
    assert r.status_code == 503
    assert b"The image could not be saved. Please try again." in r.data
    assert db_rows(sb_app, "SELECT COUNT(*) FROM parts")[0][0] == 0
    assert FAKE_KEY not in caplog.text  # the key never reaches the logs


def test_invalid_file_is_rejected_before_storage(sb, fake):
    fake.calls.clear()
    r = create_part(sb, 1, {"image": (io.BytesIO(b"not an image"), "x.png")})
    assert r.status_code == 400 and b"Invalid image format." in r.data
    assert fake.calls == []


def test_database_failure_after_upload_removes_object(sb_app, sb, fake, monkeypatch):
    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(models, "create_part", boom)
    assert create_part(sb, 1, {"image": upload()}).status_code == 500
    assert fake.objects() == {}


def test_signing_outage_falls_back_to_redirect_route(sb_app, sb, fake):
    create_part(sb, 1, {"image": upload()})
    name = image_name(sb_app)
    fake.fail["sign_many"] = "network"
    html = sb.get("/").get_data(as_text=True)
    assert f"/uploads/{name}" in html  # page still renders, image via the redirect route


def test_export_reads_images_from_bucket(sb_app, sb, fake):
    create_part(sb, 1, {"image": upload()})
    create_part(sb, 2, {"image": upload()})
    r = sb.get("/parts/export?format=xlsx&name=x")
    assert r.status_code == 200
    assert len(load_workbook(io.BytesIO(r.data)).active._images) == 2
    fake.objects().clear()  # images gone from the bucket: export still works, without them
    r = sb.get("/parts/export?format=pdf&name=x")
    assert r.status_code == 200 and r.data.startswith(b"%PDF")


def test_cleanup_uploads_removes_bucket_orphans(sb_app, sb, fake):
    create_part(sb, 1, {"image": upload()})
    keep = image_name(sb_app)
    fake.objects()["orphan.png"] = (b"x", "image/png")
    result = sb_app.test_cli_runner().invoke(args=["cleanup-uploads"])
    assert result.exit_code == 0, result.output
    assert "Removed orphan.png" in result.output
    assert list(fake.objects()) == [keep]


def test_backup_with_uploads_downloads_bucket(sb_app, sb, fake):
    create_part(sb, 1, {"image": upload()})
    result = sb_app.test_cli_runner().invoke(args=["backup-db", "--with-uploads"])
    assert result.exit_code == 0, result.output
    archive = next(Path(sb_app.config["BACKUP_FOLDER"]).glob("uploads-*.zip"))
    with zipfile.ZipFile(archive) as zf:
        assert zf.namelist() == [image_name(sb_app)]


def test_init_storage_creates_private_bucket(tmp_path):
    fake = FakeSupabase(bucket=None)
    app = create_app(make_config(tmp_path, SUPABASE_URL=FAKE_URL, SUPABASE_SERVICE_ROLE_KEY=FAKE_KEY))
    with app.app_context():
        images.storage().session = fake
    result = app.test_cli_runner().invoke(args=["init-storage"])
    assert result.exit_code == 0, result.output
    assert "created (private)" in result.output
    cfg = fake.buckets[BUCKET]["config"]
    assert cfg["public"] is False
    assert set(cfg["allowed_mime_types"]) == {"image/jpeg", "image/png", "image/webp"}
    again = app.test_cli_runner().invoke(args=["init-storage"])
    assert "already exists" in again.output


def test_public_bucket_is_refused():
    store = images.SupabaseStorage(FAKE_URL, FAKE_KEY, BUCKET, session=FakeSupabase(public=True))
    with pytest.raises(images.StorageError, match="PUBLIC"):
        store.ensure_bucket()


def test_storage_repr_hides_key():
    assert FAKE_KEY not in repr(images.SupabaseStorage(FAKE_URL, FAKE_KEY, BUCKET))


# ------------------------------------------------- storage migration utility

def test_migrate_uploads_script(tmp_path, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent.parent / "scripts"))
    import migrate_uploads_to_supabase as mig

    folder = tmp_path / "uploads"
    folder.mkdir()
    good = "0123456789abcdef0123456789abcdef.png"
    (folder / good).write_bytes(image_bytes().getvalue())
    (folder / "fake.png").write_bytes(b"not an image")
    db_path = tmp_path / "src.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE parts (image_filename TEXT); CREATE TABLE sub_parts (image_filename TEXT);"
        f"INSERT INTO parts VALUES ('{good}'), ('fake.png'), ('gone.png'), ('../etc.png'), (NULL);"
    )
    conn.close()
    fake = FakeSupabase(bucket=None)
    for name, value in {"SUPABASE_URL": FAKE_URL, "SUPABASE_SERVICE_ROLE_KEY": FAKE_KEY,
                        "SUPABASE_BUCKET": BUCKET}.items():
        monkeypatch.setenv(name, value)
    argv = ["--upload-folder", str(folder), "--sqlite", str(db_path), "--create-bucket"]
    before = {f.name: f.read_bytes() for f in folder.iterdir()}

    assert mig.main([*argv, "--dry-run"], session=fake) == 0
    assert "would upload" in capsys.readouterr().out and fake.buckets == {}

    assert mig.main(argv, session=fake) == 0
    out = capsys.readouterr().out
    assert fake.buckets[BUCKET]["public"] is False
    assert list(fake.objects()) == [good]
    for expected in ("uploaded           " + good, "invalid image      fake.png",
                     "missing locally    gone.png", "unsafe name        ../etc.png"):
        assert expected in out

    assert mig.main(argv, session=fake) == 0  # rerun: nothing duplicated
    assert "already in bucket  " + good in capsys.readouterr().out
    assert list(fake.objects()) == [good]

    fake.objects()[good] = (b"different", "image/png")  # never overwritten
    assert mig.main(argv, session=fake) == 1
    assert "conflict           " + good in capsys.readouterr().out
    assert fake.objects()[good][0] == b"different"
    assert {f.name: f.read_bytes() for f in folder.iterdir()} == before  # local files untouched
