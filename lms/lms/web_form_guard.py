"""WTF: safe web form submit.

The payments app overrides `frappe.website.doctype.web_form.web_form.accept` with
`payments.overrides.payment_webform.accept`, which inserts `data.doctype` (taken from the request)
with ignore_permissions and never checks the Web Form's own doc_type or published flag. Through any
public web form, a Guest could create records of any doctype.

LMS is installed after payments, so its `override_whitelisted_methods` entry is the last one and
wins (frappe.override_whitelisted_method returns overrides[-1]). Both the core cmd and the payments
cmd resolve here. This wrapper pins the doctype to the Web Form record, then delegates to Frappe
core's accept (which also pins the doctype, checks published/login_required/allow_edit and only
sets the form's own fields). Payment web forms are delegated to payments with the pinned doctype.

Guest photos: core accept() saves a web form attachment as a File with the Guest's own permissions, which
Guest does not have (PermissionError). For Guest INSERTS on the "Alumni Story" form only, the guard takes over
its Attach Image fields: the value must be "<name>,data:image/(jpeg|png|webp);base64,<data>" (<= 7 M base64
chars, <= 5 MB, <= 40 MP), at most 5 such submissions per IP per day. Pillow re-encodes it as JPEG (metadata and
trailing bytes dropped) and it is saved as a PRIVATE File attached to the new record (off the CDN until
lms.lms.alumni_story publishes it on approval). Anything else is refused before anything is stored. Every other
form, and every other field type, keeps core behaviour.
"""

import base64
import binascii
import inspect
import json
import re

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import sbool

CORE_CMD = "frappe.website.doctype.web_form.web_form.accept"
GUARD = "lms.lms.web_form_guard.accept"


def _invalid():
	frappe.throw(_("Invalid request"), frappe.ValidationError)


# Guest photos: only the share-your-story form ("Alumni Story"), only Attach Image fields.
GUEST_IMAGE_DOCTYPE = "Alumni Story"
GUEST_IMAGE_MAX_BYTES = 5 * 1024 * 1024
GUEST_IMAGE_MAX_B64 = 7_000_000  # characters of base64, checked before decoding (5 MB is ~6.99 M)
GUEST_IMAGE_MAX_PIXELS = 40_000_000  # checked from the header, before the pixels are decoded
GUEST_IMAGE_MAX_SIDE = 2400  # stored size: plenty for a portrait, bounded on disk
GUEST_PHOTOS_PER_IP_PER_DAY = 5
_DATA_URL = re.compile(r"([^,]{0,255}),data:([a-zA-Z0-9.+/-]{1,64});base64,([A-Za-z0-9+/=\s]+)", re.S)
_IMAGE_TYPES = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}


def photo_cap_key(ip) -> str:
	return f"wtf_story_photo_ip:{ip or '-'}"


def _count_photo_submission() -> None:
	"""Storage-fill protection: at most GUEST_PHOTOS_PER_IP_PER_DAY submissions with a photo per IP in 24 h.
	The per-minute rate limit on accept() still applies to every submission."""
	key = frappe.cache.make_key(photo_cap_key(getattr(frappe.local, "request_ip", None)))
	n = frappe.cache.incr(key)
	if n == 1:
		frappe.cache.expire(key, 86400)
	if n > GUEST_PHOTOS_PER_IP_PER_DAY:
		frappe.throw(
			_("Too many photos from this connection today. Please try again tomorrow, or send your story without a photo."),
			frappe.TooManyRequestsError,
		)


def _reencode(mime: str, content: bytes) -> bytes:
	"""Decode and re-encode as JPEG: caps pixels before decoding (decompression bombs), drops EXIF/GPS and
	every other metadata block, and drops anything appended after the image data."""
	import io
	import warnings

	from PIL import Image, ImageOps

	try:
		with Image.open(io.BytesIO(content)) as src:
			if src.format != _IMAGE_TYPES[mime]:
				_invalid()
			w, h = src.size
			if w < 1 or h < 1 or w * h > GUEST_IMAGE_MAX_PIXELS:
				_invalid()
			with warnings.catch_warnings():
				warnings.simplefilter("error", Image.DecompressionBombWarning)
				src.load()
			im = ImageOps.exif_transpose(src)
			if im.mode != "RGB":
				rgba = im.convert("RGBA")
				im = Image.new("RGB", rgba.size, (255, 255, 255))
				im.paste(rgba, mask=rgba.getchannel("A"))
			im.thumbnail((GUEST_IMAGE_MAX_SIDE, GUEST_IMAGE_MAX_SIDE))
			out = io.BytesIO()
			im.save(out, "JPEG", quality=86, optimize=True)  # no exif=/icc_profile=: metadata is not carried over
			return out.getvalue()
	except frappe.ValidationError:
		raise
	except Exception:
		_invalid()


def _take_guest_images(wf, payload) -> list:
	"""Guest inserts on the Alumni Story form only: validate its Attach Image fields and remove them from the
	payload, so core accept() never tries to save a File as Guest. Every other form and field keeps core
	behaviour. Returns [(fieldname, file_name, jpeg_bytes)] to save after the insert."""
	if frappe.session.user != "Guest" or payload.get("name") or wf.doc_type != GUEST_IMAGE_DOCTYPE:
		return []
	meta = frappe.get_meta(wf.doc_type)
	images, counted = [], False
	for field in wf.web_form_fields:
		df = meta.get_field(field.fieldname)
		if not df or df.fieldtype != "Attach Image":
			continue
		value = payload.get(field.fieldname)
		if not value:
			continue
		if not isinstance(value, str) or len(value) > GUEST_IMAGE_MAX_B64 + 400:
			_invalid()
		if not counted:
			_count_photo_submission()
			counted = True
		m = _DATA_URL.fullmatch(value)
		if not m:
			_invalid()  # a guest may upload a new image, never point the field at an existing or remote file
		mime = m.group(2).lower()
		if mime not in _IMAGE_TYPES or len(m.group(3)) > GUEST_IMAGE_MAX_B64:
			_invalid()
		try:
			content = base64.b64decode(re.sub(r"\s+", "", m.group(3)), validate=True)
		except (binascii.Error, ValueError):
			_invalid()
		if not content or len(content) > GUEST_IMAGE_MAX_BYTES:
			_invalid()
		images.append((field.fieldname, f"story-photo-{frappe.generate_hash(length=12)}.jpg", _reencode(mime, content)))
		payload[field.fieldname] = ""
	return images


def _save_guest_images(doc, images) -> None:
	"""PRIVATE Files: the storage hook leaves private files on disk (never the CDN). lms.lms.alumni_story makes
	the photo public only once the story is Approved, learner-approved and name/photo-consented."""
	for fieldname, file_name, content in images:
		f = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": file_name,
				"attached_to_doctype": doc.doctype,
				"attached_to_name": doc.name,
				"attached_to_field": fieldname,
				"is_private": 1,
				"content": content,
			}
		)
		f.save(ignore_permissions=True)
		doc.db_set(fieldname, f.file_url)


@frappe.whitelist(methods=["POST", "PUT"], allow_guest=True)
@rate_limit(key="web_form", limit=10, seconds=60)  # same bucket and limit as core's accept
def accept(
	web_form: str,
	data=None,
	web_form_request_key: str | None = None,
	docname: str | None = None,
	for_payment=False,
):
	# Imported here, not at module level: a module-level name bound to a whitelisted function
	# re-exposes it as a guest endpoint under this module's path (see test_guest_surface).
	from frappe.website.doctype.web_form.web_form import accept as core_accept

	# The delegates carry their own @rate_limit on the same cache key (cmd + ip + web_form), so
	# calling them through the decorator would count every submit twice. This function's own
	# decorator is the single limiter; call the undecorated bodies.
	core_accept = inspect.unwrap(core_accept)

	try:
		payload = json.loads(data) if isinstance(data, str | bytes) else data
	except ValueError:
		_invalid()
	if not isinstance(payload, dict):
		_invalid()
	payload = frappe._dict(payload)

	# JSON bodies send 1/0/null; sbool leaves non-strings alone
	if for_payment is None or for_payment == "" or for_payment in (0, 1):
		for_payment = bool(for_payment)
	for_payment = sbool(for_payment)
	if not isinstance(for_payment, bool):
		_invalid()

	wf = frappe.get_doc("Web Form", web_form)
	wf.raise_if_unpublished()

	if wf.login_required and frappe.session.user == "Guest":
		frappe.throw(_("You must login to use this form"), frappe.PermissionError)

	if payload.get("doctype") and payload.doctype != wf.doc_type:
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	payload.doctype = wf.doc_type
	if docname and not payload.get("name"):
		payload.name = docname

	if for_payment or wf.get("accept_payment"):
		if not wf.get("accept_payment"):
			frappe.throw(_("Not permitted"), frappe.PermissionError)
		# payments' accept ignores key_required / web_form_request_key, so a key-gated form
		# would accept keyless Guest submits and edits. Refuse rather than reimplement the key flow.
		if wf.get("key_required"):
			frappe.throw(_("Not permitted"), frappe.PermissionError)
		from payments.overrides.payment_webform import accept as payments_accept

		payments_accept = inspect.unwrap(payments_accept)
		return payments_accept(web_form=web_form, data=json.dumps(payload), for_payment=for_payment)

	images = _take_guest_images(wf, payload)
	doc = core_accept(web_form=web_form, data=json.dumps(payload), web_form_request_key=web_form_request_key)
	_save_guest_images(doc, images)
	return doc


def check_override_precedence() -> None:
	"""after_migrate: the guard only protects the site while it wins the override. App order decides
	that (frappe.override_whitelisted_method takes the last app), so log loudly if it ever stops.
	Never raises: a failed check must not fail the migrate."""
	try:
		resolved = frappe.override_whitelisted_method(CORE_CMD)
		if resolved == GUARD:
			return
		title = "WTF web form override is not the LMS guard"
		message = (
			f"{CORE_CMD} resolves to {resolved}, not {GUARD}. Guests may be able to create records "
			"of any doctype through a public Web Form. Check the installed app order "
			"(payments must come before lms) and clear the cache."
		)
	except Exception:
		title, message = "WTF web form override check failed", None
	try:
		frappe.log_error(title=title, message=message)
	except Exception:
		pass
