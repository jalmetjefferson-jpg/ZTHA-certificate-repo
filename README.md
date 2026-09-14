# ZTHA Certificate Delivery System

A reusable certificate portal for ZTHA Academy batches.

## What it does

- Public student portal where graduates enter name + email to download their certificate.
- Public verification page for QR-code certificate validation.
- Admin dashboard for each batch.
- Upload CSV or paste a graduate list.
- Generate downloadable PNG certificates and print-quality copies.
- Download all certificates as a ZIP.
- Persistent certificate register for verification.
- Editable batch metadata, certificate ID pattern, template image, field positions, colors, and font sizing.
- Runs cleanly under a subpath such as `https://ztha.academy/certificate`.

## Admin URLs

When deployed under `ztha.academy/certificate`:

- Student portal: `https://ztha.academy/certificate/`
- Certificate verification: `https://ztha.academy/certificate/verify`
- Admin dashboard: `https://ztha.academy/certificate/admin`

## Required production environment variables

```bash
ADMIN_PASSWORD="set-a-strong-admin-password"
SECRET_KEY="set-a-long-random-flask-secret"
DATA_DIR="/data"
URL_PREFIX="/certificate"
```

`DATA_DIR` must be persistent. It stores:

- uploaded graduate CSV
- uploaded certificate template
- private certificate signing key
- generated certificate images
- certificate verification register
- admin password fallback if `ADMIN_PASSWORD` is not set

## Deployment shape for ztha.academy/certificate

This is a Flask app, so it cannot be pasted directly into a normal WordPress page as PHP/HTML. Use one of these safe deployment options:

1. Run the Flask app on a Python-capable host with persistent storage, then reverse-proxy `https://ztha.academy/certificate` to it.
2. Deploy to an app host that supports Python and persistent disk, then connect `certificate.ztha.academy` or proxy `/certificate` from the main domain.

For the exact requested URL, `ztha.academy/certificate`, the best production route is a reverse proxy from the Academy server/domain to this Flask app with `URL_PREFIX=/certificate`.

## Batch workflow

1. Open `/certificate/admin`.
2. Paste graduates as `Name, email` or upload a CSV.
3. Set batch, year, issue date, ID prefix, and starting number.
4. Upload a new certificate template if needed.
5. Use Advanced layout controls to move/edit dynamic fields.
6. Preview one certificate.
7. Generate all certificates.
8. Download the ZIP and/or share the public portal link with graduates.

## CSV columns accepted

Required:

- `full_name` or `name`, or `First name` + `Last name`
- `email`

Optional:

- `certificate_id`

If no certificate ID is supplied, the system generates IDs using:

`{id_prefix}-{batch}-{year}-{0001...}`

Example:

```csv
full_name,email,certificate_id
Ozioma Joseph,princessjok2018@gmail.com,ZTHA-B6-2026-0001
Emmanuel Eze,eeze14689@gmail.com,ZTHA-B6-2026-0002
```

## Security notes

- Keep `DATA_DIR/secret.key` private. It signs verification codes.
- Do not reset the certificate register after certificates have been issued unless you want old verification links to stop working.
- Set `ADMIN_PASSWORD` in production. Do not rely on the local fallback password.
