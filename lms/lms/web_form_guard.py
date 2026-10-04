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
Guest does not have (PermissionError). For Guest INSERTS the guard takes over attachment fields: the value must
be "<name>,data:image/(jpeg|png|webp);base64,<data>" whose bytes really are that image, at most 5 MB. It is saved
as a public File (storage hook -> S3/CDN) attached to the new record. Anything else (other types, SVG, HTML, a
path or URL to an existing file) is refused before anything is stored.
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


GUEST_IMAGE_MAX_BYTES = 5 * 1024 * 1024
_ATTACH_TYPES = ("Attach", "Attach Image")
_DATA_URL = re.compile(r"([^,]{0,255}),data:([a-zA-Z0-9.+/-]{1,64});base64,([A-Za-z0-9+/=\s]+)", re.S)
_IMAGE_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def _is_image(mime: str, content: bytes) -> bool:
	if mime == "image/jpeg":
		return content[:3] == b"\xff\xd8\xff"
	if mime == "image/png":
		return content[:8] == b"\x89PNG\r\n\x1a\n"
	if mime == "image/webp":
		return content[:4] == b"RIFF" and content[8:12] == b"WEBP"
	return False


def _take_guest_images(wf, payload) -> list:
	"""Guest inserts only: validate every attachment field and remove it from the payload, so core accept()
	never tries to save a File as Guest. Returns [(fieldname, file_name, bytes)] to save after the insert."""
	if frappe.session.user != "Guest" or payload.get("name"):
		return []
	meta = frappe.get_meta(wf.doc_type)
	images = []
	for field in wf.web_form_fields:
		df = meta.get_field(field.fieldname)
		if not df or df.fieldtype not in _ATTACH_TYPES:
			continue
		value = payload.get(field.fieldname)
		if not value:
			continue
		m = _DATA_URL.fullmatch(value) if isinstance(value, str) else None
		if not m:
			_invalid()  # a guest may upload a new image, never point the field at an existing or remote file
		mime = m.group(2).lower()
		if mime not in _IMAGE_EXT:
			_invalid()
		try:
			content = base64.b64decode(re.sub(r"\s+", "", m.group(3)), validate=True)
		except (binascii.Error, ValueError):
			_invalid()
		if not content or len(content) > GUEST_IMAGE_MAX_BYTES or not _is_image(mime, content):
			_invalid()
		stem = re.sub(r"\.[^.]*$", "", m.group(1).replace("\\", "/").split("/")[-1])
		stem = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:60] or "photo"
		images.append((field.fieldname, f"{stem}.{_IMAGE_EXT[mime]}", content))
		payload[field.fieldname] = ""
	return images


def _save_guest_images(doc, images) -> None:
	for fieldname, file_name, content in images:
		f = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": file_name,
				"attached_to_doctype": doc.doctype,
				"attached_to_name": doc.name,
				"attached_to_field": fieldname,
				"is_private": 0,
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
