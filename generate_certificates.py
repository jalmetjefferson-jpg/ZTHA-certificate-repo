"""
ZTHA Academy — Certificate rendering engine + CLI
==================================================
Web UI:  python app.py            (recommended — opens at http://localhost:5000)
CLI:     python generate_certificates.py --make-blank | --test | (no flag = all)

Requires: pillow, qrcode, pandas, flask   (pip install -r requirements.txt)
"""

import argparse
import glob
import hashlib
import hmac
import os
import re
import secrets
import sys
from datetime import datetime

import pandas as pd
import qrcode
from PIL import Image, ImageDraw, ImageFont

# Everything the app writes to disk lives under DATA_DIR. Keeping it all in one
# place means a single persistent volume/disk (mounted at this path in
# production) covers the register, generated certificates, and secrets —
# nothing gets silently lost on a redeploy or restart.
DATA_DIR = os.environ.get("DATA_DIR", "data")

# ============================================================================
# CONFIG — every coordinate, size and color lives here. Template is 1536x1024.
# Values under "certificate data" are just defaults; the web UI can override them.
# ============================================================================
CONFIG = {
    # ---- files -------------------------------------------------------------
    "template":       "assets/template.png",        # original with {{...}} placeholders — committed to git
    "template_blank": "assets/template_blank.png",  # cleaned version, auto-regenerated if missing
    "students_csv":   "students.csv",                # CLI-only convenience path
    "out_dir":        os.path.join(DATA_DIR, "output", "certificates"),
    "log_csv":        os.path.join(DATA_DIR, "output", "certificate_log.csv"),  # persistent register
    "secret_file":    os.path.join(DATA_DIR, "secret.key"),  # signing key — never share or commit this
    "font_serif":     "assets/fonts/Cinzel-Variable.ttf",   # student name + ribbon
    "font_sans":      "assets/fonts/Poppins-SemiBold.ttf",  # date / batch / cert id

    # ---- certificate data (defaults — editable in the web UI) ---------------
    "batch":       "B5",
    "year":        "2026",
    "issue_date":  "July 3, 2026",
    "id_prefix":   "ZTHA",                       # -> ZTHA-B5-2026-0001
    "id_start":    1,
    # base URL the QR code + printed verification code point to. Point this at
    # wherever the app is reachable (localhost for private use, an ngrok/public
    # URL when sharing, or your own domain once deployed).
    "verify_base_url": "http://localhost:5000",

    # a second, much higher-resolution copy of every certificate is rendered
    # for printing (e.g. scale 3 turns 1536x1024 into 4608x3072, ~300+ DPI at
    # a large frame size). Set to 1 to skip generating a print version.
    "print_scale": 3,

    # ---- student name (center) ----------------------------------------------
    "name_center_x":   768,     # horizontal center of the page
    "name_center_y":   449,     # vertical center of the name line
    "name_max_width":  880,     # auto-shrink until the name fits this width
    "name_font_size":  62,      # starting size
    "name_min_size":   26,      # never shrink below this
    "name_weight":     600,     # Cinzel variable-font weight (400-900)
    "name_tracking":   0.10,    # letter-spacing as a fraction of font size
    "name_color":      (21, 59, 46),     # deep green #153B2E

    # ---- top-left ribbon (band interior x 121-273, centered on x=197) --------
    # The year ("CLASS OF 2026") is baked into the template, so only the batch
    # value is drawn, in the space between the BATCH label and the divider.
    "ribbon_center_x":    197,
    "ribbon_batch_y":     130,   # vertical center of the batch line
    "ribbon_year_y":      None,  # None = year is part of the template art
    "ribbon_font_size":   36,
    "ribbon_weight":      700,
    "ribbon_color":       (242, 175, 25),   # gold #F2AF19

    # ---- bottom info row (values centered on the dashed lines at y≈720) ------
    "info_font_size":  22,
    "info_color":      (45, 45, 45),
    "issue_date_pos":  (556, 714),   # (center x, baseline y)
    "batch_pos":       (812, 714),
    "cert_id_pos":     (1112, 714),
    "info_max_width": {              # value auto-shrinks to fit its dashed line
        "issue_date_pos": 200,       # dash 461-652
        "batch_pos":      105,       # dash 765-860
        "cert_id_pos":    185,       # dash 1023-1201
    },

    # ---- QR code (gold frame (971,773)-(1087,899)) ----------------------------
    "qr_box_center":  (1029, 836),   # center of the frame interior
    "qr_size":        96,            # final pasted size in px (snapped to module grid)
    "qr_dark":        (17, 17, 17),  # module color
    "qr_light":       (255, 255, 255),

    # ---- verification code caption (clean gap between QR box and footer) -----
    "verify_code_pos":   (1029, 924),   # (center x, baseline y)
    "verify_code_font_size": 15,
    "verify_code_color": (110, 110, 110),

    # ---- erase boxes used by --make-blank (left, top, right, bottom) ---------
    "erase_cream": [                       # flat-filled with sampled cream
        (350, 406, 1192, 494),             # {{STUDENT_NAME}}
        (448, 694, 605, 726),              # {{ISSUE_DATE}}
        (754, 694, 862, 726),              # {{BATCH}} bottom row
        (1011, 694, 1206, 726),            # {{CERTIFICATE_ID}}
    ],
    "cream_sample": (350, 470),            # where to sample the cream color
    "erase_ribbon": [                      # per-row green fill (band has a gradient)
        (140, 112, 270, 155),              # ribbon {{BATCH}}
        (140, 225, 270, 262),              # ribbon {{YEAR}}
    ],
    "ribbon_sample_x": (134, 142),         # clean green strip inside the band, per row
    "erase_qr": (972, 772, 1075, 881),     # dummy QR inside the gold frame
    "qr_bg_sample": (990, 774),            # white inside the frame
}

# ============================================================================
# Engine
# ============================================================================


def get_secret(cfg=CONFIG):
    """Load the signing key, generating it on first run. Keep this file private —
    anyone who has it can forge valid verification codes."""
    path = cfg["secret_file"]
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read().strip()
    key = secrets.token_hex(32).encode()
    with open(path, "wb") as f:
        f.write(key)
    return key


def verification_code(cert_id, name, secret):
    """Short tamper-evident code — cannot be produced without the secret key,
    so a forged certificate ID alone won't pass verification."""
    msg = f"{cert_id.strip().upper()}|{name.strip().upper()}".encode()
    digest = hmac.new(secret, msg, hashlib.sha256).hexdigest().upper()
    return f"{digest[:4]}-{digest[4:8]}"


def verify_url(cert_id, code, cfg):
    base = cfg["verify_base_url"].rstrip("/")
    return f"{base}/verify?id={cert_id}&code={code}"


REGISTER_COLUMNS = ["full_name", "email", "certificate_id", "verification_code",
                   "filename", "print_filename", "issue_date", "batch", "year"]


def load_register(cfg=CONFIG):
    """The persistent, append-only record of every certificate ever issued."""
    if os.path.exists(cfg["log_csv"]):
        df = pd.read_csv(cfg["log_csv"], dtype=str).fillna("")
        return df.reindex(columns=REGISTER_COLUMNS, fill_value="")  # tolerate older registers
    return pd.DataFrame(columns=REGISTER_COLUMNS)


def reset_register(cfg=CONFIG, delete_files=True):
    """Wipe the issued-certificate history so ID/email checks start clean.
    The old register is archived (never silently discarded) before removal.
    Returns a dict describing what was archived/removed."""
    result = {"archived_to": None, "records_cleared": 0, "files_removed": 0}
    if os.path.exists(cfg["log_csv"]):
        reg = load_register(cfg)
        result["records_cleared"] = len(reg)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_dir = os.path.dirname(cfg["log_csv"]) or "."
        archive_path = os.path.join(archive_dir, f"certificate_log_backup_{stamp}.csv")
        os.replace(cfg["log_csv"], archive_path)
        result["archived_to"] = archive_path
    if delete_files and os.path.isdir(cfg["out_dir"]):
        pngs = glob.glob(os.path.join(cfg["out_dir"], "*.png"))
        for p in pngs:
            os.remove(p)
        result["files_removed"] = len(pngs)
    return result


def verify(cert_id, code, cfg=CONFIG):
    """Check an ID + code against the register. Returns a dict describing the result."""
    reg = load_register(cfg)
    cert_id = (cert_id or "").strip()
    code = (code or "").strip().upper()
    match = reg[reg["certificate_id"].str.upper() == cert_id.upper()]
    if match.empty:
        return {"valid": False, "reason": "No certificate with that ID has been issued."}
    row = match.iloc[0]
    if row["verification_code"].upper() != code:
        return {"valid": False, "reason": "Certificate ID found, but the verification code doesn't match."}
    return {"valid": True, "full_name": row["full_name"], "certificate_id": row["certificate_id"],
           "batch": row.get("batch", ""), "year": row.get("year", ""),
           "issue_date": row["issue_date"]}


def make_blank(cfg=CONFIG):
    """Erase all baked-in placeholders from the template."""
    im = Image.open(cfg["template"]).convert("RGB")
    draw = ImageDraw.Draw(im)

    cream = im.getpixel(cfg["cream_sample"])
    for box in cfg["erase_cream"]:
        draw.rectangle(box, fill=cream)

    sx0, sx1 = cfg["ribbon_sample_x"]
    for (l, t, r, b) in cfg["erase_ribbon"]:
        for y in range(t, b + 1):
            row = [im.getpixel((x, y)) for x in range(sx0, sx1)]
            avg = tuple(sum(c[i] for c in row) // len(row) for i in range(3))
            draw.line([(l, y), (r, y)], fill=avg)

    qr_bg = im.getpixel(cfg["qr_bg_sample"])
    draw.rectangle(cfg["erase_qr"], fill=qr_bg)

    im.save(cfg["template_blank"])
    return cfg["template_blank"]


def load_font(path, size, weight=None):
    font = ImageFont.truetype(path, size)
    if weight is not None:
        try:
            font.set_variation_by_axes([weight])
        except OSError:
            pass  # not a variable font
    return font


def tracked_width(draw, text, font, tracking_px):
    widths = [draw.textlength(ch, font=font) for ch in text]
    return sum(widths) + tracking_px * (len(text) - 1), widths


def draw_tracked_text(draw, text, font, center_x, center_y, tracking_px, color):
    """Draw text with letter-spacing, centered at (center_x, center_y)."""
    total, widths = tracked_width(draw, text, font, tracking_px)
    cap_box = font.getbbox("H")            # center on the cap height, not the em box
    cap_mid = (cap_box[1] + cap_box[3]) / 2
    x = center_x - total / 2
    y = center_y - cap_mid
    for ch, w in zip(text, widths):
        draw.text((x, y), ch, font=font, fill=color)
        x += w + tracking_px


def fit_name_font(draw, name, cfg):
    """Shrink the font until the tracked name fits name_max_width."""
    size = cfg["name_font_size"]
    while size > cfg["name_min_size"]:
        font = load_font(cfg["font_serif"], size, cfg["name_weight"])
        tracking = cfg["name_tracking"] * size
        total, _ = tracked_width(draw, name, font, tracking)
        if total <= cfg["name_max_width"]:
            return font, tracking
        size -= 2
    font = load_font(cfg["font_serif"], size, cfg["name_weight"])
    return font, cfg["name_tracking"] * size


def make_qr(url, cfg):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=10, border=1)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color=cfg["qr_dark"], back_color=cfg["qr_light"]).convert("RGB")
    modules = img.width // 10                      # box_size=10 -> exact module count
    side = max(modules, (cfg["qr_size"] // modules) * modules)  # exact multiple = crisp modules
    return img.resize((side, side), Image.NEAREST)


_SCALED_SCALAR_KEYS = ["name_center_x", "name_center_y", "name_max_width", "name_font_size",
                      "name_min_size", "ribbon_center_x", "ribbon_batch_y", "ribbon_font_size",
                      "info_font_size", "qr_size", "verify_code_font_size"]
_SCALED_TUPLE_KEYS = ["qr_box_center", "verify_code_pos", "issue_date_pos", "batch_pos", "cert_id_pos"]


def scaled_cfg(cfg, scale):
    """A copy of cfg with every pixel-based coordinate/size multiplied by scale,
    so the same layout can be rendered at a higher resolution for printing."""
    if scale == 1:
        return cfg
    c = dict(cfg)
    for k in _SCALED_SCALAR_KEYS:
        c[k] = cfg[k] * scale
    for k in _SCALED_TUPLE_KEYS:
        x, y = cfg[k]
        c[k] = (x * scale, y * scale)
    if cfg.get("ribbon_year_y") is not None:
        c["ribbon_year_y"] = cfg["ribbon_year_y"] * scale
    c["info_max_width"] = {k: v * scale for k, v in cfg["info_max_width"].items()}
    return c


def build_assets(cfg, scale=1):
    """Load everything shared across renders once (except the QR, which is per-certificate).
    scale > 1 upscales the base template and every font size for a print-quality render."""
    base = Image.open(cfg["template_blank"]).convert("RGB")
    if scale != 1:
        base = base.resize((base.width * scale, base.height * scale), Image.LANCZOS)
    return {
        "base": base,
        "secret": get_secret(cfg),
        "info_font": load_font(cfg["font_sans"], cfg["info_font_size"]),
        "verify_font": load_font(cfg["font_sans"], cfg["verify_code_font_size"]),
        "ribbon_font": load_font(cfg["font_serif"], cfg["ribbon_font_size"], cfg["ribbon_weight"]),
    }


def render_one(name, cert_id, cfg, assets):
    """Render a single certificate and return (image, verification_code)."""
    im = assets["base"].copy()
    draw = ImageDraw.Draw(im)
    code = verification_code(cert_id, name, assets["secret"])

    display_name = name.upper()
    font, tracking = fit_name_font(draw, display_name, cfg)
    draw_tracked_text(draw, display_name, font,
                      cfg["name_center_x"], cfg["name_center_y"],
                      tracking, cfg["name_color"])

    draw_tracked_text(draw, cfg["batch"], assets["ribbon_font"],
                      cfg["ribbon_center_x"], cfg["ribbon_batch_y"], 1, cfg["ribbon_color"])
    if cfg["ribbon_year_y"] is not None:
        draw_tracked_text(draw, cfg["year"], assets["ribbon_font"],
                          cfg["ribbon_center_x"], cfg["ribbon_year_y"], 1, cfg["ribbon_color"])

    for key, text in [("issue_date_pos", cfg["issue_date"]),
                      ("batch_pos", cfg["batch"]),
                      ("cert_id_pos", cert_id)]:
        x, baseline = cfg[key]
        font = assets["info_font"]
        max_w = cfg["info_max_width"].get(key)
        size = cfg["info_font_size"]
        while max_w and draw.textlength(text, font=font) > max_w and size > 10:
            size -= 1
            font = load_font(cfg["font_sans"], size)
        draw.text((x, baseline), text, font=font,
                  fill=cfg["info_color"], anchor="ms")

    qr_img = make_qr(verify_url(cert_id, code, cfg), cfg)
    cx, cy = cfg["qr_box_center"]
    im.paste(qr_img, (cx - qr_img.width // 2, cy - qr_img.height // 2))

    vx, vy = cfg["verify_code_pos"]
    draw.text((vx, vy), f"Verify: {code}", font=assets["verify_font"],
              fill=cfg["verify_code_color"], anchor="ms")
    return im, code


def cert_id_for(index, cfg):
    return f"{cfg['id_prefix']}-{cfg['batch']}-{cfg['year']}-{cfg['id_start'] + index:04d}"


def safe_filename(name, cert_id, suffix=""):
    """Includes the certificate ID so two different people who happen to share a
    name (common across batches/years) never overwrite each other's file."""
    clean_name = re.sub(r"[^\w\-]", "", name.replace(" ", "_"))
    clean_id = re.sub(r"[^\w\-]", "", cert_id)
    return f"{clean_name}_{clean_id}_ZTHA_Certificate{suffix}.png"


def find_columns(df):
    """Locate the name/email/certificate-id columns, tolerating header variations.
    Returns (name_col, email_col, id_col, first_col, last_col) — any may be None
    except when noted."""
    def norm(c):
        return re.sub(r"[^a-z]", "", str(c).lower())
    cols = {norm(c): c for c in df.columns}
    name_col = next((cols[k] for k in ("fullname", "name", "studentname", "student")
                     if k in cols), None)
    email_col = next((cols[k] for k in ("email", "emailaddress", "studentemail")
                      if k in cols), None)
    id_col = next((cols[k] for k in ("certificateid", "certid", "certificateno",
                                     "certno", "certificatenumber", "id")
                   if k in cols), None)
    first = cols.get("firstname")
    last = cols.get("lastname")
    return name_col, email_col, id_col, first, last


def clean_rows(df, cfg=CONFIG):
    """Extract (name, email, certificate_id) rows. Title-case names, lowercase
    emails, skip blanks, duplicate emails/IDs within the file, and IDs already
    issued in a past run. Returns (rows, skipped) — rows is a list of
    {'name':..., 'email':..., 'cert_id':...}; cert_id is None when the CSV has
    no certificate-id column."""
    name_col, email_col, id_col, first, last = find_columns(df)
    if name_col is None and first and last:
        df = df.copy()
        df["__name"] = df[first].fillna("").astype(str) + " " + df[last].fillna("").astype(str)
        name_col = "__name"
    if name_col is None:
        return None, ["CSV needs a name column ('full_name' or 'First name'/'Last name')."]
    if email_col is None:
        return None, ["CSV needs an 'email' column — students look up their certificate by name + email."]

    issued_ids = set(load_register(cfg)["certificate_id"].str.upper())

    rows, seen_emails, seen_ids, skipped = [], set(), set(), []
    for i, (_, rec) in enumerate(df.iterrows(), start=2):  # 2 = first data row in the CSV
        raw = rec[name_col]
        name = re.sub(r"\s+", " ", str(raw)).strip() if pd.notna(raw) else ""
        if not name or name.lower() == "nan":
            skipped.append(f"row {i}: blank name")
            continue
        name = name.title()

        raw_email = rec[email_col]
        email = str(raw_email).strip().lower() if pd.notna(raw_email) else ""
        if not email or email.lower() == "nan":
            skipped.append(f"row {i}: '{name}' has no email address")
            continue
        if email in seen_emails:
            skipped.append(f"row {i}: duplicate email '{email}' in this file")
            continue

        cert_id = None
        if id_col is not None:
            raw_id = rec[id_col]
            cert_id = str(raw_id).strip() if pd.notna(raw_id) else ""
            if not cert_id or cert_id.lower() == "nan":
                skipped.append(f"row {i}: '{name}' has no certificate ID")
                continue
            if cert_id.upper() in seen_ids:
                skipped.append(f"row {i}: duplicate certificate ID '{cert_id}' in this file")
                continue
            if cert_id.upper() in issued_ids:
                skipped.append(f"row {i}: certificate ID '{cert_id}' was already issued in a previous batch")
                continue
            seen_ids.add(cert_id.upper())

        seen_emails.add(email)
        rows.append({"name": name, "email": email, "cert_id": cert_id})
    return rows, skipped


def lookup(name, email, cfg=CONFIG):
    """Find an issued certificate by name + email, as entered by a student on
    the public portal. Returns a dict of the register row, or None."""
    name = re.sub(r"\s+", " ", (name or "")).strip().lower()
    email = (email or "").strip().lower()
    if not name or not email:
        return None
    reg = load_register(cfg)
    match = reg[(reg["email"].str.lower() == email) & (reg["full_name"].str.lower() == name)]
    if match.empty:
        return None
    return match.iloc[0].to_dict()


def render(rows, cfg, progress=None):
    """Render all certificates to disk (standard + a high-res print copy),
    append to the persistent register, return the new log rows.
    rows: list of {'name':..., 'email':..., 'cert_id':...}; cert_id None -> sequential fallback."""
    assets = build_assets(cfg)
    scale = cfg.get("print_scale", 1)
    print_cfg = scaled_cfg(cfg, scale) if scale != 1 else None
    print_assets = build_assets(print_cfg, scale) if print_cfg else None

    os.makedirs(cfg["out_dir"], exist_ok=True)
    log = []
    for i, row in enumerate(rows):
        name = row["name"]
        cert_id = row["cert_id"] or cert_id_for(i, cfg)

        im, code = render_one(name, cert_id, cfg, assets)
        fname = safe_filename(name, cert_id)
        im.save(os.path.join(cfg["out_dir"], fname))

        print_fname = ""
        if print_assets:
            print_im, _ = render_one(name, cert_id, print_cfg, print_assets)
            print_fname = safe_filename(name, cert_id, "_Print")
            print_im.save(os.path.join(cfg["out_dir"], print_fname))

        log.append({"full_name": name, "email": row.get("email", ""),
                    "certificate_id": cert_id, "verification_code": code,
                    "filename": fname, "print_filename": print_fname,
                    "issue_date": cfg["issue_date"],
                    "batch": cfg["batch"], "year": cfg["year"]})
        if progress:
            progress(i + 1, len(rows), name, cert_id)
    os.makedirs(os.path.dirname(cfg["log_csv"]) or ".", exist_ok=True)
    combined = pd.concat([load_register(cfg), pd.DataFrame(log)], ignore_index=True)
    combined.to_csv(cfg["log_csv"], index=False)
    return log


# ============================================================================
# CLI
# ============================================================================


def main():
    ap = argparse.ArgumentParser(description="ZTHA bulk certificate generator")
    ap.add_argument("--make-blank", action="store_true",
                    help="erase placeholders from template.png and exit")
    ap.add_argument("--test", action="store_true", help="render only the first 3 students")
    args = ap.parse_args()

    if args.make_blank:
        print("Saved", make_blank(CONFIG))
        return

    if not os.path.exists(CONFIG["template_blank"]):
        sys.exit("template_blank.png not found — run with --make-blank first.")

    df = pd.read_csv(CONFIG["students_csv"])
    rows, skipped = clean_rows(df)
    if rows is None:
        sys.exit(skipped[0])
    if skipped:
        print(f"Skipped {len(skipped)} row(s):")
        print("\n".join("  " + s for s in skipped))
    if args.test:
        rows = rows[:3]
        print(f"--test: rendering first {len(rows)} students")

    log = render(rows, CONFIG, progress=lambda i, n, name, cid: print(f"  {cid}  {name}"))
    print(f"\n{len(log)} certificate(s) -> {CONFIG['out_dir']}")
    print(f"Register -> {CONFIG['log_csv']}")


if __name__ == "__main__":
    main()
