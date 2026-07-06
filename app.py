"""
ZTHA Certificate Generator — public student portal + admin dashboard
Run:  python app.py   then open http://localhost:5000
      Admin dashboard: http://localhost:5000/admin

Admin password: set the ADMIN_PASSWORD environment variable in production.
Falls back to a random password saved in a local file for local/dev use.
"""

import io
import json
import os
import secrets
import zipfile
from functools import wraps

import pandas as pd
from flask import (Flask, jsonify, redirect, render_template, request,
                   send_file, send_from_directory, session, url_for)

import generate_certificates as engine

app = Flask(__name__)

DATA_DIR = engine.DATA_DIR
UPLOAD_CSV = os.path.join(DATA_DIR, "uploads", "students.csv")
SETTINGS_FILE = os.path.join(DATA_DIR, "uploads", "settings.json")
ADMIN_PASSWORD_FILE = os.path.join(DATA_DIR, "admin_password.txt")
FLASK_SECRET_FILE = os.path.join(DATA_DIR, "flask_secret.key")
os.makedirs(os.path.join(DATA_DIR, "uploads"), exist_ok=True)


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

# Settings the UI is allowed to override (certificate IDs come from the CSV).
# Deliberately ONLY batch metadata — layout coordinates live in the code's
# CONFIG (calibrated to the template), and the QR/verify domain is always
# derived from the request host. Both used to be overridable here, and stale
# saved values broke name placement and pointed QR codes at the wrong site.
UI_FIELDS = {
    "batch": str, "year": str, "issue_date": str,
}


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def current_cfg(overrides=None, host_url=None):
    """host_url: the base URL this request actually came in on (request.host_url).
    The QR / verify links always use it, so certificates generated on
    certificate.ztha.academy point there, and local runs point at localhost."""
    cfg = dict(engine.CONFIG)
    saved = {}
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                saved = json.load(f)
        except (json.JSONDecodeError, OSError):
            saved = {}
    cfg.update({k: v for k, v in saved.items() if k in UI_FIELDS})
    if overrides:
        for k, cast in UI_FIELDS.items():
            if k in overrides and str(overrides[k]).strip() != "":
                try:
                    cfg[k] = cast(overrides[k])
                except (TypeError, ValueError):
                    pass
    if host_url:
        cfg["verify_base_url"] = host_url.rstrip("/")
    return cfg


def save_settings(overrides):
    keep = {k: v for k, v in overrides.items() if k in UI_FIELDS and str(v).strip() != ""}
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


# ============================================================================
# Public: student portal + verification
# ============================================================================


@app.route("/")
def portal():
    return render_template("portal.html")


@app.route("/api/lookup")
def api_lookup():
    name = request.args.get("name", "")
    email = request.args.get("email", "")
    if not name.strip() or not email.strip():
        return jsonify(found=False, reason="Enter both your full name and your email address.")
    record = engine.lookup(name, email, engine.CONFIG)
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
    return jsonify(engine.verify(cert_id, code, engine.CONFIG))


@app.route("/certificates/<path:fname>")
def certificate(fname):
    return send_from_directory(engine.CONFIG["out_dir"], fname)


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
# Admin: dashboard (CSV upload, batch settings, generation)
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
        settings={k: cfg[k] for k in UI_FIELDS},
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
    save_settings({k: data.get(k, "") for k in UI_FIELDS})
    rows, skipped = load_students(cfg)
    if rows is None:
        return jsonify(error=skipped[0]), 400
    if not rows:
        return jsonify(error="No students loaded — upload a CSV first."), 400
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
    return send_file(os.path.abspath(engine.CONFIG["log_csv"]), as_attachment=True,
                     download_name="certificate_log.csv")


@app.route("/admin/reset", methods=["POST"])
@require_admin
def admin_reset():
    """Wipe the issued-certificate register + generated files so a fresh CSV
    can reuse any ID/email without 'already issued' collisions. The old
    register is archived to a timestamped backup file, never just discarded."""
    result = engine.reset_register(engine.CONFIG, delete_files=True)
    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("  ZTHA Certificate Generator")
    print(f"  Student portal:  http://localhost:{port}")
    print(f"  Admin dashboard: http://localhost:{port}/admin\n")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
