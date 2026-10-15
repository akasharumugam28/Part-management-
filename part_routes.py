"""Dashboard and routes for creating, viewing, editing and deleting parts."""
import logging
import io

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for

import database
import exports
import images
import models
from auth import login_required
from validation import QTY_MAX, QTY_PRESETS, clean_part, clean_sub_part, error_message

bp = Blueprint("parts", __name__)
log = logging.getLogger(__name__)

GENERIC_ERROR = "Something went wrong. Please try again."


def render_form(template="part_form.html", **context):
    return render_template(template, qty_presets=QTY_PRESETS, qty_max=QTY_MAX, **context)


def image_error_status(exc):
    # A storage outage is not the user's fault; a bad file is.
    return 503 if isinstance(exc, images.StorageError) else 400


def get_part_or_404(part_id):
    part = models.get_part(part_id)
    if part is None:
        abort(404)
    return part


@bp.route("/")
@login_required
def dashboard():
    q = request.args.get("q", "").strip()[:100]
    parts = models.list_parts(q or None)
    images.prefetch_urls(p["image_filename"] for p in parts)
    total_trolleys, empty_trolleys = models.trolley_totals()
    return render_template(
        "dashboard.html",
        parts=parts,
        subs=models.sub_parts_by_part(),
        total=models.count_parts(),
        total_subs=models.count_sub_parts(),
        total_trolleys=total_trolleys,
        empty_trolleys=empty_trolleys,
        q=q,
    )


def send_export(parts, subs, what):
    fmt = request.args.get("format", "")
    if fmt not in exports.FORMATS:
        abort(400)
    name = exports.clean_filename(request.args.get("name"))
    data = exports.build(fmt, parts, subs, name)
    log.info("Exported %s as %s.%s", what, name, fmt)
    return send_file(
        io.BytesIO(data), mimetype=exports.FORMATS[fmt],
        as_attachment=True, download_name=f"{name}.{fmt}", max_age=0,
    )


@bp.route("/parts/export")
@login_required
def export_all():
    return send_export(models.list_parts(), models.sub_parts_by_part(), "all parts")


@bp.route("/parts/<int:part_id>/export")
@login_required
def export_part(part_id):
    part = get_part_or_404(part_id)
    return send_export([part], {part_id: models.list_sub_parts(part_id)}, f"part id={part_id}")


@bp.route("/parts/new")
@login_required
def new_part():
    return render_form(form={}, errors={})


@bp.route("/parts/create", methods=["POST"])
@login_required
def create_part():
    data, errors = clean_part(request.form)

    def fail(message, status):
        flash(message, "error")
        return render_form(form=request.form, errors=errors), status

    if errors:
        return fail(error_message(errors), 400)

    storage = request.files.get("image")
    new_file = None
    if images.has_file(storage):
        try:
            new_file = images.validate_and_save(storage)
        except images.ImageError as exc:
            errors["image"] = str(exc)
            return fail(str(exc), image_error_status(exc))
    data["image_filename"] = new_file

    try:
        part_id = models.create_part(data)
    except database.Error:
        log.exception("Database error while creating part")
        images.delete_files([new_file])
        return fail(GENERIC_ERROR, 500)

    log.info("Created part id=%s", part_id)
    flash("Part created successfully.", "success")
    if new_file:
        flash("Image uploaded successfully.", "success")
    if request.form.get("add_another"):
        return redirect(url_for("parts.new_part"), code=303)
    return redirect(url_for("parts.dashboard"), code=303)


@bp.route("/parts/<int:part_id>")
@login_required
def part_details(part_id):
    part = get_part_or_404(part_id)
    subs = models.list_sub_parts(part_id)
    images.prefetch_urls([part["image_filename"], *(s["image_filename"] for s in subs)])
    return render_template("part_details.html", part=part, subs=subs)


@bp.route("/parts/<int:part_id>/edit")
@login_required
def edit_part(part_id):
    part = get_part_or_404(part_id)
    images.prefetch_urls([part["image_filename"]])
    return render_form(part=part, form=dict(part), errors={})


@bp.route("/parts/<int:part_id>/update", methods=["POST"])
@login_required
def update_part(part_id):
    part = get_part_or_404(part_id)
    data, errors = clean_part(request.form)

    def fail(message, status):
        flash(message, "error")
        return render_form(part=part, form=request.form, errors=errors), status

    if errors:
        return fail(error_message(errors), 400)

    try:
        data["image_filename"], new_file, obsolete = images.resolve_replacement(
            part["image_filename"], request.files.get("image"), request.form.get("remove_image")
        )
    except images.ImageError as exc:
        errors["image"] = str(exc)
        return fail(str(exc), image_error_status(exc))

    try:
        models.update_part(part_id, data)
    except database.Error:
        log.exception("Database error while updating part id=%s", part_id)
        images.delete_files([new_file])
        return fail(GENERIC_ERROR, 500)

    images.delete_files([obsolete])
    flash("Part updated successfully.", "success")
    if new_file:
        flash("Image uploaded successfully.", "success")
    return redirect(url_for("parts.dashboard"), code=303)


@bp.route("/parts/<int:part_id>/delete", methods=["POST"])
@login_required
def delete_part(part_id):
    part = get_part_or_404(part_id)
    sub_images = [s["image_filename"] for s in models.list_sub_parts(part_id)]
    try:
        models.delete_part(part_id)  # sub-parts go with it (ON DELETE CASCADE)
    except database.Error:
        log.exception("Database error while deleting part id=%s", part_id)
        flash(GENERIC_ERROR, "error")
        return redirect(url_for("parts.dashboard"), code=303)
    images.delete_files([part["image_filename"], *sub_images])
    log.info("Deleted part id=%s", part_id)
    flash("Part deleted successfully.", "success")
    return redirect(url_for("parts.dashboard"), code=303)


# ----------------------------------------------------------------- sub-parts

def get_sub_part_or_404(part_id, sub_id):
    sub = models.get_sub_part(part_id, sub_id)
    if sub is None:
        abort(404)
    return sub


@bp.route("/parts/<int:part_id>/sub-parts/new")
@login_required
def new_sub_part(part_id):
    part = get_part_or_404(part_id)
    return render_form("sub_part_form.html", part=part, form={}, errors={})


@bp.route("/parts/<int:part_id>/sub-parts/create", methods=["POST"])
@login_required
def create_sub_part(part_id):
    part = get_part_or_404(part_id)
    data, errors = clean_sub_part(request.form)

    def fail(message, status):
        flash(message, "error")
        return render_form("sub_part_form.html", part=part, form=request.form, errors=errors), status

    if errors:
        return fail(error_message(errors), 400)

    storage = request.files.get("image")
    new_file = None
    if images.has_file(storage):
        try:
            new_file = images.validate_and_save(storage)
        except images.ImageError as exc:
            errors["image"] = str(exc)
            return fail(str(exc), image_error_status(exc))
    data["image_filename"] = new_file

    try:
        sub_id = models.create_sub_part(part_id, data)
    except database.Error:
        log.exception("Database error while creating sub-part of part id=%s", part_id)
        images.delete_files([new_file])
        return fail(GENERIC_ERROR, 500)

    log.info("Created sub-part id=%s in part id=%s", sub_id, part_id)
    flash("Sub-part added successfully.", "success")
    if request.form.get("add_another"):
        return redirect(url_for("parts.new_sub_part", part_id=part_id), code=303)
    return redirect(url_for("parts.part_details", part_id=part_id), code=303)


@bp.route("/parts/<int:part_id>/sub-parts/<int:sub_id>/edit")
@login_required
def edit_sub_part(part_id, sub_id):
    part = get_part_or_404(part_id)
    sub = get_sub_part_or_404(part_id, sub_id)
    images.prefetch_urls([sub["image_filename"]])
    return render_form("sub_part_form.html", part=part, sub=sub, form=dict(sub), errors={})


@bp.route("/parts/<int:part_id>/sub-parts/<int:sub_id>/update", methods=["POST"])
@login_required
def update_sub_part(part_id, sub_id):
    part = get_part_or_404(part_id)
    sub = get_sub_part_or_404(part_id, sub_id)
    data, errors = clean_sub_part(request.form)

    def fail(message, status):
        flash(message, "error")
        return render_form("sub_part_form.html", part=part, sub=sub, form=request.form, errors=errors), status

    if errors:
        return fail(error_message(errors), 400)

    try:
        data["image_filename"], new_file, obsolete = images.resolve_replacement(
            sub["image_filename"], request.files.get("image"), request.form.get("remove_image")
        )
    except images.ImageError as exc:
        errors["image"] = str(exc)
        return fail(str(exc), image_error_status(exc))

    try:
        models.update_sub_part(sub_id, data)
    except database.Error:
        log.exception("Database error while updating sub-part id=%s", sub_id)
        images.delete_files([new_file])
        return fail(GENERIC_ERROR, 500)

    images.delete_files([obsolete])
    flash("Sub-part updated successfully.", "success")
    return redirect(url_for("parts.part_details", part_id=part_id), code=303)


@bp.route("/parts/<int:part_id>/sub-parts/<int:sub_id>/delete", methods=["POST"])
@login_required
def delete_sub_part(part_id, sub_id):
    sub = get_sub_part_or_404(part_id, sub_id)
    try:
        models.delete_sub_part(sub_id)
    except database.Error:
        log.exception("Database error while deleting sub-part id=%s", sub_id)
        flash(GENERIC_ERROR, "error")
        return redirect(url_for("parts.part_details", part_id=part_id), code=303)
    images.delete_files([sub["image_filename"]])
    log.info("Deleted sub-part id=%s from part id=%s", sub_id, part_id)
    flash("Sub-part deleted successfully.", "success")
    return redirect(url_for("parts.part_details", part_id=part_id), code=303)
