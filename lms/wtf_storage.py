"""WTF media storage: public File uploads -> private S3 bucket served via CloudFront CDN.

- Credentials come from the EC2 instance role (IMDSv2, hop limit 2 so containers reach it);
  no keys in site_config or the DB.
- Only PUBLIC files move (course images, lesson media, logos). Private files (payment proofs,
  certificates, assignments) stay on disk and are covered by the nightly S3 backup.
- Configure in site_config: "wtf_media_bucket", "wtf_media_cdn" (e.g. https://cdn.wtfgymsacademy.com),
  optional "wtf_media_region" (default ap-south-1). If not configured, the hook is a no-op.
"""

import mimetypes
import os

import frappe

CACHE_CONTROL = "public, max-age=31536000, immutable"


def _conf():
    bucket = frappe.conf.get("wtf_media_bucket")
    cdn = (frappe.conf.get("wtf_media_cdn") or "").rstrip("/")
    return (bucket, cdn, frappe.conf.get("wtf_media_region") or "ap-south-1") if bucket and cdn else None


def _client(region):
    import boto3

    return boto3.client("s3", region_name=region)


def _key(doc) -> str:
    # content-hash keyed -> immutable URLs, safe to cache forever
    ext = os.path.splitext(doc.file_name or "")[1].lower()[:10]
    return f"lms/{(doc.content_hash or frappe.generate_hash(length=32))[:32]}{ext}"


def upload_public_file(doc, method=None):
    conf = _conf()
    if not conf or doc.is_private or doc.is_folder or not doc.file_url or not doc.file_url.startswith("/files/"):
        return
    bucket, cdn, region = conf
    path = frappe.get_site_path("public", doc.file_url.lstrip("/"))
    if not os.path.isfile(path):
        return
    key = _key(doc)
    ctype = mimetypes.guess_type(doc.file_name or "")[0] or "application/octet-stream"
    try:
        _client(region).upload_file(path, bucket, key, ExtraArgs={"ContentType": ctype, "CacheControl": CACHE_CONTROL})
    except Exception:
        # fail open: the local file stays and keeps serving; the upload itself must not fail
        frappe.log_error(title="WTF media upload failed", message=frappe.get_traceback())
        return
    url = f"{cdn}/{key}"
    frappe.db.set_value("File", doc.name, "file_url", url, update_modified=False)
    doc.file_url = url
    if doc.attached_to_doctype and doc.attached_to_name and doc.attached_to_field:
        frappe.db.set_value(doc.attached_to_doctype, doc.attached_to_name, doc.attached_to_field, url, update_modified=False)
    # keep the local copy only if another File row still points at it (same content dedup)
    if not frappe.db.exists("File", {"file_url": f"/files/{os.path.basename(path)}", "name": ["!=", doc.name]}):
        os.remove(path)


def delete_public_file(doc, method=None):
    conf = _conf()
    if not conf or not doc.file_url or not doc.file_url.startswith(conf[1] + "/"):
        return
    if frappe.db.exists("File", {"file_url": doc.file_url, "name": ["!=", doc.name]}):
        return  # another record still uses this object
    bucket, cdn, region = conf
    try:
        _client(region).delete_object(Bucket=bucket, Key=doc.file_url[len(cdn) + 1:])
    except Exception:
        # an orphaned object is acceptable; an S3/IAM error must never block the File delete
        frappe.log_error(title="WTF media delete failed", message=frappe.get_traceback())
