"""WTF media storage: public File uploads -> private S3 bucket served via CloudFront CDN.

- Credentials come from the EC2 instance role (IMDSv2, hop limit 2 so containers reach it);
  no keys in site_config or the DB.
- Only PUBLIC MEDIA moves (image/*, video/*, audio/*: course images, lesson media, logos).
  Everything else stays on disk: the CDN sends X-Frame-Options: SAMEORIGIN and no CORS, so a
  PDF there renders blank in a lesson's iframe / pdf.js viewer. Private files (payment proofs,
  certificates, assignments) stay on disk and are covered by the nightly S3 backup.
- A CDN file switched to private is pulled back to private/files and its object removed.
- Configure in site_config: "wtf_media_bucket", "wtf_media_cdn" (e.g. https://cdn.wtfgymsacademy.com),
  optional "wtf_media_region" (default ap-south-1). If not configured, the hook is a no-op.
"""

import mimetypes
import os

import frappe

CACHE_CONTROL = "public, max-age=31536000, immutable"
MEDIA_PREFIXES = ("image/", "video/", "audio/")


def _conf():
    bucket = frappe.conf.get("wtf_media_bucket")
    cdn = (frappe.conf.get("wtf_media_cdn") or "").rstrip("/")
    return (bucket, cdn, frappe.conf.get("wtf_media_region") or "ap-south-1") if bucket and cdn else None


def _client(region):
    import boto3
    from botocore.config import Config

    # bounded well under the gunicorn worker timeout, so a slow S3 fails open instead of 502ing
    config = Config(connect_timeout=5, read_timeout=60, retries={"max_attempts": 2})
    return boto3.client("s3", region_name=region, config=config)


def _log(title, doc):
    # Leads with the file name: QA cleanup removes rows whose message starts with "qa-".
    frappe.log_error(
        title=title,
        message=f"{doc.file_name}\n\n{frappe.get_traceback()}",
        reference_doctype="File",
        reference_name=doc.name,
    )


def _content_type(doc):
    ctype = mimetypes.guess_type(doc.file_name or "")[0] or mimetypes.guess_type(doc.file_url or "")[0]
    if not ctype and doc.get("file_type"):
        ctype = mimetypes.guess_type(f"x.{doc.file_type.lower()}")[0]
    return ctype


def _is_cdn_url(url, cdn):
    return bool(url) and url.startswith(cdn + "/")


def _key(doc) -> str:
    # content-hash keyed -> immutable URLs, safe to cache forever
    ext = os.path.splitext(doc.file_name or "")[1].lower()[:10]
    return f"lms/{(doc.content_hash or frappe.generate_hash(length=32))[:32]}{ext}"


def upload_public_file(doc, method=None):
    conf = _conf()
    if not conf or doc.is_private or doc.is_folder or not doc.file_url or not doc.file_url.startswith("/files/"):
        return
    ctype = _content_type(doc)
    if not ctype or not ctype.startswith(MEDIA_PREFIXES):
        return  # documents (PDF etc.) must stay same-origin to render in lessons
    bucket, cdn, region = conf
    path = frappe.get_site_path("public", doc.file_url.lstrip("/"))
    if not os.path.isfile(path):
        return
    key = _key(doc)
    try:
        _client(region).upload_file(path, bucket, key, ExtraArgs={"ContentType": ctype, "CacheControl": CACHE_CONTROL})
    except Exception:
        # fail open: the local file stays and keeps serving; the upload itself must not fail
        _log("WTF media upload failed", doc)
        return
    url = f"{cdn}/{key}"
    frappe.db.set_value("File", doc.name, "file_url", url, update_modified=False)
    doc.file_url = url
    # From here S3 has the object and the File points at it: nothing below may fail the upload.
    dt, dn, field = doc.attached_to_doctype, doc.attached_to_name, doc.attached_to_field
    if dt and dn and field:
        try:
            if not frappe.get_meta(dt).has_field(field):
                raise ValueError(f"{dt} has no field {field!r}")
            frappe.db.set_value(dt, dn, field, url, update_modified=False)
        except Exception:
            _log("WTF media upload failed", doc)
    # keep the local copy only if another File row still points at it (same content dedup)
    if not frappe.db.exists("File", {"file_url": f"/files/{os.path.basename(path)}", "name": ["!=", doc.name]}):
        try:
            os.remove(path)
        except Exception:
            _log("WTF media upload failed", doc)


def delete_public_file(doc, method=None):
    conf = _conf()
    if not conf or not _is_cdn_url(doc.file_url, conf[1]):
        return
    if frappe.db.exists("File", {"file_url": doc.file_url, "name": ["!=", doc.name]}):
        return  # another record still uses this object
    bucket, cdn, region = conf
    try:
        _client(region).delete_object(Bucket=bucket, Key=doc.file_url[len(cdn) + 1:])
    except Exception:
        # an orphaned object is acceptable; an S3/IAM error must never block the File delete
        _log("WTF media delete failed", doc)


def _private_target(doc, key):
    name = os.path.basename(doc.file_name or "") or os.path.basename(key)
    target = frappe.get_site_path("private", "files", name)
    if os.path.exists(target):
        stem, ext = os.path.splitext(name)
        name = f"{stem}{frappe.generate_hash(length=8)}{ext}"
        target = frappe.get_site_path("private", "files", name)
    return name, target


def make_cdn_file_private(doc, method=None):
    """File on_update: a CDN-served File switched to private must not stay public on the CDN."""
    conf = _conf()
    if not conf or not doc.is_private or doc.is_folder or not _is_cdn_url(doc.file_url, conf[1]):
        return
    bucket, cdn, region = conf
    cdn_url = doc.file_url
    key = cdn_url[len(cdn) + 1:]
    target = None
    try:
        client = _client(region)
        name, target = _private_target(doc, key)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        client.download_file(bucket, key, target)
        url = f"/private/files/{name}"
        frappe.db.set_value("File", doc.name, "file_url", url, update_modified=False)
        doc.file_url = url
    except Exception:
        # fail open: the file keeps serving from the CDN; an admin sees the Error Log
        if target and os.path.exists(target):
            try:
                os.remove(target)
            except OSError:
                pass
        _log("WTF media make-private failed", doc)
        return
    dt, dn, field = doc.attached_to_doctype, doc.attached_to_name, doc.attached_to_field
    if dt and dn and field:
        try:
            if frappe.get_meta(dt).has_field(field) and frappe.db.get_value(dt, dn, field) == cdn_url:
                frappe.db.set_value(dt, dn, field, url, update_modified=False)
        except Exception:
            _log("WTF media make-private failed", doc)
    if frappe.db.exists("File", {"file_url": cdn_url, "name": ["!=", doc.name]}):
        return  # another record still uses this object (same rule as delete_public_file)
    try:
        client.delete_object(Bucket=bucket, Key=key)
    except Exception:
        _log("WTF media make-private failed", doc)
