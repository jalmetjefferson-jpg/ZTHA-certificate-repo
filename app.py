"""
ZTHA Certificate Generator — public student portal + admin dashboard
Run:  python app.py   then open http://localhost:5000
      Admin dashboard: http://localhost:5000/admin

Production under a subpath:
      URL_PREFIX=/certificate gunicorn app:app --bind 0.0.0.0:$PORT

Admin password: set the ADMIN_PASSWORD environment variable in production.
Falls back to a random password saved in a local file for local/dev use.
"""

import csv
import io
import json
import os
import re
import secrets
import zipfile
from functools import wraps
import pandas as pd
from flask import (Flask, jsonify, redirect, render_template, request,
                   send_file, send_from_directory, session, url_for)
from werkzeug.utils import secure_filename

import generate_certificates as engine

app = Flask(__name__)

DATA_DIR = engine.DATA_DIR
UPLOAD_CSV = os.path.join(DATA_DIR, "uploads", "students.csv")
SETTINGS_FILE = os.path.join(DATA_DIR, "uploads", "settings.json")
ADMIN_PASSWORD_FILE = os.path.join(DATA_DIR, "admin_password.txt")
FLASK_SECRET_FILE = os.path.join(DATA_DIR, "flask_secret.key")
CUSTOM_TEMPLATE_FILE = os.path.join(DATA_DIR, "uploads", "certificate_template.png")
os.makedirs(os.path.join(DATA_DIR, "uploads"), exist_ok=True)

URL_PREFIX = os.environ.get("URL_PREFIX", "").strip().rstrip("/")
if URL_PREFIX and not URL_PREFIX.startswith("/"):
    URL_PREFIX = "/" + URL_PREFIX


class PrefixMiddleware:
    """Let the same Flask app run cleanly under /certificate behind a proxy.

    If the proxy strips /certificate before forwarding, Flask still works.
    If the proxy forwards /certificate/... directly, this strips it and sets
    SCRIPT_NAME so url_for() generates /certificate-prefixed links.
    """

    def __init__(self, app, prefix):
        self.app = app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path == self.prefix:
            environ["PATH_INFO"] = "/"
            environ["SCRIPT_NAME"] = self.prefix
        elif path.startswith(self.prefix + "/"):
            environ["PATH_INFO"] = path[len(self.prefix):]
            environ["SCRIPT_NAME"] = self.prefix
        return self.app(environ, start_response)


if URL_PREFIX:
    app.wsgi_app = PrefixMiddleware(app.wsgi_app, URL_PREFIX)


def get_or_create(path, factory):
    if os.path.exists(path):
        with open(path, "r") as f:
            return f.read().strip()
    value = factory()
    with open(path, "w") as f:
        f.write(value)
    return value


app.secret_key = os.environ.get("SECRET_KEY") or get_or_create(FLASK_SECRET_FILE, lambda: secrets.token_hex(32))

if os.environ.get("ADMIN_PASSWORD"):
    ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
    print("\n  Admin password: set via ADMIN_PASSWORD environment variable\n")
else:
    ADMIN_PASSWORD = get_or_create(ADMIN_PASSWORD_FILE, lambda: secrets.token_urlsafe(9))
    print(f"\n  Admin password (also saved in {ADMIN_PASSWORD_FILE}): {ADMIN_PASSWORD}\n")

# Settings the UI is allowed to override (certificate IDs come from the CSV when supplied).
# Layout defaults are calibrated to the committed template, but admins can override them
# from the Advanced editor so the system is reusable across future certificate designs.
UI_FIELDS = {
    "batch": str,
    "year": str,
    "issue_date": str,
    "id_prefix": str,
    "id_start": int,
    "print_scale": int,
    "name_center_x": int,
    "name_center_y": int,
    "name_max_width": int,
    "name_font_size": int,
    "name_min_size": int,
    "ribbon_center_x": int,
    "ribbon_batch_y": int,
    "ribbon_font_size": int,
    "info_font_size": int,
    "verify_code_font_size": int,
    "qr_size": int,
}

TUPLE_FIELDS = {
    "issue_date_pos": 2,
    "batch_pos": 2,
    "cert_id_pos": 2,
    "qr_box_center": 2,
    "verify_code_pos": 2,
}

COLOR_FIELDS = {
    "name_color",
    "ribbon_color",
    "info_color",
    "verify_code_color",
}


def prefixed_url(endpoint, **values):
    return url_for(endpoint, **values)


@app.context_processor
def inject_helpers():
    return {"url_for": url_for}


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def parse_tuple(value, expected_len=2):
    if isinstance(value, (list, tuple)) and len(value) == expected_len:
        return tuple(int(v) for v in value)
    parts = re.split(r"[,\s]+", str(value).strip())
    parts = [p for p in parts if p != ""]
    if len(parts) != expected_len:
        raise ValueError("expected two numbers")
    return tuple(int(float(p)) for p in parts)


def parse_color(value):
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return tuple(int(v) for v in value)
    value = str(value).strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
        return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
    return parse_tuple(value, 3)


def color_to_hex(value):
    try:
        r, g, b = value
        return f"#{int(r):02X}{int(g):02X}{int(b):02X}"
    except Exception:
        return "#000000"


def current_cfg(overrides=None, host_url=None):
    """Return the effective certificate config.

    host_url is the base URL this request came in on. QR and verification links
    always use it, so certificates generated on ztha.academy/certificate point
    back to ztha.academy/certificate/verify.
    """
    cfg = dict(engine.CONFIG)
    saved = {}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                saved = json.load(f)
        except (json.JSONDecodeError, OSError):
            saved = {}
    cfg.update({k: v for k, v in saved.items() if k in UI_FIELDS or k in TUPLE_FIELDS or k in COLOR_FIELDS})
    if os.path.exists(CUSTOM_TEMPLATE_FILE):
        cfg["template_blank"] = CUSTOM_TEMPLATE_FILE
    if overrides:
        for k, cast in UI_FIELDS.items():
            if k in overrides and str(overrides[k]).strip() != "":
                cfg[k] = cast(overrides[k])
        for k, n in TUPLE_FIELDS.items():
            if k in overrides and str(overrides[k]).strip() != "":
                cfg[k] = parse_tuple(overrides[k], n)
        for k in COLOR_FIELDS:
            if k in overrides and str(overrides[k]).strip() != "":
                cfg[k] = parse_color(overrides[k])
    if host_url:
        cfg["verify_base_url"] = host_url.rstrip("/")
    return cfg


def serializable_settings(cfg):
    settings = {k: cfg[k] for k in UI_FIELDS if k in cfg}
    for k in TUPLE_FIELDS:
        if k in cfg:
            settings[k] = ",".join(str(int(v)) for v in cfg[k])
    for k in COLOR_FIELDS:
        if k in cfg:
            settings[k] = color_to_hex(cfg[k])
    settings["custom_template"] = os.path.exists(CUSTOM_TEMPLATE_FILE)
    return settings


def save_settings(overrides):
    keep = {}
    for k, cast in UI_FIELDS.items():
        if k in overrides and str(overrides[k]).strip() != "":
            keep[k] = cast(overrides[k])
    for k, n in TUPLE_FIELDS.items():
        if k in overrides and str(overrides[k]).strip() != "":
            keep[k] = list(parse_tuple(overrides[k], n))
    for k in COLOR_FIELDS:
        if k in overrides and str(overrides[k]).strip() != "":
            keep[k] = list(parse_color(overrides[k]))
    with open(SETTINGS_FILE, "w") as f:
        json.dump(keep, f, indent=2)


def load_students(cfg):
    """Returns (rows, skipped). rows None = bad CSV, [] = nothing uploaded."""
    if not os.path.exists(UPLOAD_CSV):
        return [], []
    try:
        df = pd.read_csv(UPLOAD_CSV)
    except Exception as e:
        return None, [f"Could not read CSV: {e}"]
    return engine.clean_rows(df, cfg)


def rows_to_csv(rows):
    has_ids = any(str(row.get("certificate_id", "")).strip() for row in rows)
    fieldnames = ["full_name", "email"] + (["certificate_id"] if has_ids else [])
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        item = {"full_name": row.get("full_name", ""), "email": row.get("email", "")}
        if has_ids:
            item["certificate_id"] = row.get("certificate_id", "")
        writer.writerow(item)
    return buf.getvalue()


def parse_graduate_text(text, cfg):
    """Parse pasted graduates from CSV-ish lines.

    Accepts:
    - Name, email
    - Name <email>
    - Name | email | certificate_id
    - CSV with full_name,email,certificate_id headers
    """
    text = (text or "").strip()
    if not text:
        return [], ["Paste at least one graduate name and email."]
    rows, skipped = [], []
    lower_first = text.splitlines()[0].lower()
    if "email" in lower_first and ("name" in lower_first or "full_name" in lower_first):
        try:
            df = pd.read_csv(io.StringIO(text))
            parsed, errors = engine.clean_rows(df, cfg)
            if parsed is None:
                return [], errors
            for row in parsed:
                rows.append({"full_name": row["name"], "email": row["email"], "certificate_id": row.get("cert_id") or ""})
            return rows, errors
        except Exception as e:
            return [], [f"Could not read pasted CSV: {e}"]
    for i, line in enumerate(text.splitlines(), start=1):
        line = line.strip().strip("-•")
        if not line:
            continue
        email_match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", line, re.I)
        if not email_match:
            skipped.append(f"line {i}: no email found")
            continue
        email = email_match.group(0).lower()
        cert_id = ""
        parts = [p.strip() for p in re.split(r"[|,\t]+", line) if p.strip()]
        if len(parts) >= 3:
            cert_id = parts[2]
        name = line[:email_match.start()] + line[email_match.end():]
        name = re.sub(r"[<>()|,\t]+", " ", name)
        name = re.sub(r"\s+", " ", name).strip()
        if not name:
            skipped.append(f"line {i}: no name found")
            continue
        rows.append({"full_name": name.title(), "email": email, "certificate_id": cert_id})
    return rows, skipped


# ============================================================================
# Public: student portal + verification
# ============================================================================


@app.route("/")
def portal():
    return render_template("portal.html")


@app.route("/healthz")
def healthz():
    return jsonify(ok=True, app="ztha-certificate-generator")


@app.route("/api/lookup")
def api_lookup():
    name = request.args.get("name", "")
    email = request.args.get("email", "")
    if not name.strip() or not email.strip():
        return jsonify(found=False, reason="Enter both your full name and your email address.")
    record = engine.lookup(name, email, current_cfg(host_url=request.host_url))
    if not record:
        return jsonify(found=False,
                       reason="No certificate matches that name and email. Double-check both, "
                              "or contact ZTHA Academy if you believe this is an error.")
    return jsonify(found=True, full_name=record["full_name"], certificate_id=record["certificate_id"],
                  verification_code=record["verification_code"], batch=record["batch"],
                  year=record["year"], issue_date=record["issue_date"], filename=record["filename"],
                  print_filename=record.get("print_filename", ""))


@app.route("/verify")
def verify_page():
    return render_template("verify.html")


@app.route("/api/verify")
def api_verify():
    cert_id = request.args.get("id", "")
    code = request.args.get("code", "")
    if not cert_id or not code:
        return jsonify(valid=False, reason="Enter both a certificate ID and a verification code.")
    return jsonify(engine.verify(cert_id, code, current_cfg(host_url=request.host_url)))


@app.route("/certificates/<path:fname>")
def certificate(fname):
    return send_from_directory(current_cfg()["out_dir"], fname)


# ============================================================================
# Admin: login
# ============================================================================


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if request.form.get("password", "") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(request.args.get("next") or url_for("admin_dashboard"))
        error = "Incorrect password."
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin_login"))


# ============================================================================
# Admin: dashboard (CSV upload, paste import, batch settings, generation)
# ============================================================================


@app.route("/admin")
@require_admin
def admin_dashboard():
    return render_template("admin.html")


@app.route("/admin/state")
@require_admin
def admin_state():
    cfg = current_cfg(host_url=request.host_url)
    rows, skipped = load_students(cfg)
    return jsonify(
        settings=serializable_settings(cfg),
        students=rows or [],
        skipped=skipped if rows is not None else [],
        csv_error=skipped[0] if rows is None else None,
        template_ready=os.path.exists(cfg["template_blank"]),
    )


@app.route("/admin/upload", methods=["POST"])
@require_admin
def admin_upload():
    f = request.files.get("csv")
    if not f or not f.filename:
        return jsonify(error="No file received."), 400
    f.save(UPLOAD_CSV)
    cfg = current_cfg(host_url=request.host_url)
    rows, skipped = load_students(cfg)
    if rows is None:
        os.remove(UPLOAD_CSV)
        return jsonify(error=skipped[0]), 400
    return jsonify(students=rows, skipped=skipped)


@app.route("/admin/paste-graduates", methods=["POST"])
@require_admin
def admin_paste_graduates():
    cfg = current_cfg(host_url=request.host_url)
    payload = request.get_json(force=True)
    rows, skipped = parse_graduate_text(payload.get("graduates", ""), cfg)
    if not rows:
        return jsonify(error="No valid graduate rows found.", skipped=skipped), 400
    with open(UPLOAD_CSV, "w", newline="") as f:
        f.write(rows_to_csv(rows))
    parsed, csv_skipped = load_students(cfg)
    return jsonify(students=parsed or [], skipped=skipped + (csv_skipped or []))


@app.route("/admin/upload-template", methods=["POST"])
@require_admin
def admin_upload_template():
    f = request.files.get("template")
    if not f or not f.filename:
        return jsonify(error="No template image received."), 400
    if not f.filename.lower().endswith((".png", ".jpg", ".jpeg")):
        return jsonify(error="Upload a PNG or JPG certificate template."), 400
    # PIL validation happens inside the engine. Store as PNG path for consistency.
    tmp = os.path.join(DATA_DIR, "uploads", secure_filename(f.filename))
    f.save(tmp)
    from PIL import Image
    try:
        Image.open(tmp).convert("RGB").save(CUSTOM_TEMPLATE_FILE)
    except Exception as e:
        os.remove(tmp)
        return jsonify(error=f"Could not read template image: {e}"), 400
    if tmp != CUSTOM_TEMPLATE_FILE and os.path.exists(tmp):
        os.remove(tmp)
    return jsonify(ok=True, custom_template=True)


@app.route("/admin/preview")
@require_admin
def admin_preview():
    cfg = current_cfg(request.args, host_url=request.host_url)
    if not os.path.exists(cfg["template_blank"]):
        engine.make_blank(cfg)
    name = request.args.get("preview_name") or "Adaeze Chukwuemeka Okonkwo"
    cert_id = request.args.get("preview_cert_id") or engine.cert_id_for(0, cfg)
    assets = engine.build_assets(cfg)
    im, _code = engine.render_one(name.title(), cert_id, cfg, assets)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/admin/generate", methods=["POST"])
@require_admin
def admin_generate():
    data = request.get_json(force=True)
    cfg = current_cfg(data, host_url=request.host_url)
    save_settings(data)
    rows, skipped = load_students(cfg)
    if rows is None:
        return jsonify(error=skipped[0]), 400
    if not rows:
        return jsonify(error="No students loaded — upload a CSV or paste graduates first."), 400
    if not os.path.exists(cfg["template_blank"]):
        engine.make_blank(cfg)
    log = engine.render(rows, cfg)
    return jsonify(count=len(log), skipped=skipped, log=log)


@app.route("/admin/download-zip")
@require_admin
def admin_download_zip():
    cfg = current_cfg()
    out_dir = cfg["out_dir"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for fn in sorted(os.listdir(out_dir)):
            if fn.endswith(".png"):
                z.write(os.path.join(out_dir, fn), fn)
        if os.path.exists(cfg["log_csv"]):
            z.write(cfg["log_csv"], "certificate_log.csv")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"ZTHA_Certificates_{cfg['batch']}_{cfg['year']}.zip")


@app.route("/admin/log.csv")
@require_admin
def admin_log_csv():
    return send_file(os.path.abspath(current_cfg()["log_csv"]), as_attachment=True,
                     download_name="certificate_log.csv")


@app.route("/admin/reset", methods=["POST"])
@require_admin
def admin_reset():
    """Wipe the issued-certificate register + generated files so a fresh CSV
    can reuse any ID/email without 'already issued' collisions. The old
    register is archived to a timestamped backup file, never just discarded."""
    result = engine.reset_register(current_cfg(), delete_files=True)
    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("  ZTHA Certificate Generator")
    print(f"  Student portal:  http://localhost:{port}{URL_PREFIX or ''}")
    print(f"  Admin dashboard: http://localhost:{port}{URL_PREFIX or ''}/admin\n")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
