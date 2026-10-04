"""WTF Academy WhatsApp lifecycle: approved WA Studio templates sent from the academy number.

Sender
- send_template(user_or_phone, template, params, button_param, key, phone=None) is the one
  place a message leaves. It runs in a worker (queue_template enqueues it on the short queue
  after commit) or directly inside a scheduler job.
- site_config: "wastudio_token" (secret, project-bound: no channel_number is sent; empty means
  no-op) and optional "wastudio_url" (default https://wastudio.wtflabs.ai).
- Utility templates (account, payment, enrolment, certificate) go to any valid number.
  Marketing templates (trial day 1/3/7, learning nudge) need User.wtf_whatsapp_opt_in = 1, so
  they are never sent to a bare phone.
- Templates that WA Studio does not list as APPROVED are skipped (list cached 10 minutes); the
  skip is recorded as "skipped_not_approved", the only status a later attempt may overwrite.
- Idempotent: the dedupe log "WTF WhatsApp Message" has a unique dedupe_key
  "<template>:<user or hashed phone>:<key>". The row is claimed (status "sending") and
  committed BEFORE the HTTP call, under a row lock, so a retried or duplicated job sends at most
  once. A failed or timed-out send stays "failed" (it may have reached the learner).
- Fail open: nothing here raises into signup, payment or certificate code. Logs carry a masked
  phone (91******3210) and an error type or HTTP status, never the token or the raw number.

Triggers: web_sign_up (account ready), update_payment_record (enrolment confirmed),
LMS Certificate after_insert (certificate ready), hourly payment pending, daily trial day
1/3/7 and the learning nudge (see the functions below and lms/hooks.py).
"""

import hashlib
import re

import frappe
import requests
from frappe.utils import cint, flt

DOCTYPE = "WTF WhatsApp Message"
DEFAULT_URL = "https://wastudio.wtflabs.ai"
TIMEOUT = 8
LANGUAGE = "en"
APPROVAL_CACHE_KEY = "wtf_whatsapp:template_statuses"
APPROVAL_TTL = 10 * 60
RETRYABLE = {"skipped_not_approved"}

ACCOUNT_READY = "academy_account_ready_v1"
TRIAL_DAY1 = "academy_trial_day1_v1"
TRIAL_DAY3 = "academy_trial_day3_v1"
TRIAL_DAY7 = "academy_trial_day7_v1"
PAYMENT_PENDING = "academy_payment_pending_v1"
ENROLMENT_CONFIRMED = "academy_enrolment_confirmed_v1"
LEARNING_NUDGE = "academy_learning_nudge_v1"
CERTIFICATE_READY = "academy_certificate_ready_v1"

UTILITY = {ACCOUNT_READY, PAYMENT_PENDING, ENROLMENT_CONFIRMED, CERTIFICATE_READY}
MARKETING = {TRIAL_DAY1, TRIAL_DAY3, TRIAL_DAY7, LEARNING_NUDGE}
TEMPLATES = UTILITY | MARKETING


# ---------------------------------------------------------------- formatting


def normalize_phone(phone):
	"""91 + 10-digit Indian mobile, digits only. Anything else is None."""
	digits = re.sub(r"\D", "", str(phone or ""))
	if len(digits) == 12 and digits.startswith("91"):
		digits = digits[2:]
	if len(digits) == 10 and digits[0] in "6789":
		return "91" + digits
	return None


def mask_phone(phone):
	digits = re.sub(r"\D", "", str(phone or ""))
	if not digits:
		return ""
	if len(digits) <= 6:
		return "*" * len(digits)
	return digits[:2] + "*" * (len(digits) - 6) + digits[-4:]


def _scrub(text):
	"""Masks any long digit run (a phone) and the token in free text from WA Studio."""
	text = str(text or "")
	token = _token()
	if token:
		text = text.replace(token, "***")
	return re.sub(r"\d{8,}", lambda m: mask_phone(m.group(0)), text)


def format_inr(amount):
	"""₹ with Indian digit grouping: ₹24,999, ₹1,24,999, ₹1,180.50."""
	amount = flt(amount)
	whole = int(abs(amount))
	paise = round((abs(amount) - whole) * 100)
	if paise == 100:
		whole, paise = whole + 1, 0
	digits = str(whole)
	if len(digits) > 3:
		head, tail = digits[:-3], digits[-3:]
		groups = []
		while len(head) > 2:
			groups.insert(0, head[-2:])
			head = head[:-2]
		if head:
			groups.insert(0, head)
		digits = ",".join(groups) + "," + tail
	text = f"₹{digits}" + (f".{paise:02d}" if paise else "")
	return f"-{text}" if amount < 0 else text


# ---------------------------------------------------------------- config / approval


def _token():
	return (frappe.conf.get("wastudio_token") or "").strip() or None


def _base_url():
	return ((frappe.conf.get("wastudio_url") or "").strip() or DEFAULT_URL).rstrip("/")


def _headers(token):
	return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _template_rows(data):
	if isinstance(data, list):
		return data
	if not isinstance(data, dict):
		return []
	for key in ("result", "messageTemplates", "templates", "data"):
		rows = data.get(key)
		if isinstance(rows, list):
			return rows
	return []


def template_statuses():
	"""{template name: status} from WA Studio, cached 10 minutes. None when the list is unknown."""
	cached = frappe.cache.get_value(APPROVAL_CACHE_KEY)
	if cached is not None:
		return cached
	token = _token()
	if not token:
		return None
	try:
		response = requests.get(
			f"{_base_url()}/api/v1/getMessageTemplates",
			params={"pageSize": 300},
			headers=_headers(token),
			timeout=TIMEOUT,
		)
		if response.status_code >= 400:
			return None
		statuses = {}
		for row in _template_rows(response.json()):
			if not isinstance(row, dict):
				continue
			name = row.get("name") or row.get("elementName")
			status = str(row.get("status") or "").upper()
			if name and statuses.get(name) != "APPROVED":
				statuses[name] = status
	except Exception:
		return None
	frappe.cache.set_value(APPROVAL_CACHE_KEY, statuses, expires_in_sec=APPROVAL_TTL)
	return statuses


def is_template_approved(template):
	statuses = template_statuses()
	return bool(statuses) and statuses.get(template) == "APPROVED"


# ---------------------------------------------------------------- sender


def _log(template, phone, reason):
	# Template, masked phone and a reason only: never the token or the raw number.
	frappe.log_error(
		title="WhatsApp template not sent",
		message=f"{template} to {mask_phone(phone)}: {reason}",
	)


def dedupe_key(template, user, phone, key):
	recipient = user or "ph-" + hashlib.sha256(phone.encode()).hexdigest()[:16]
	return f"{template}:{recipient}:{key}"


def _is_user(user_or_phone):
	value = str(user_or_phone or "")
	return "@" in value or not normalize_phone(value)


def _user_row(user):
	return frappe.db.get_value(
		"User", user, ["first_name", "mobile_no", "wtf_whatsapp_opt_in", "enabled"], as_dict=True
	)


def _write_row(existing, values):
	"""Claims (or re-claims a retryable) dedupe row and commits. Returns its name or None."""
	if existing:
		frappe.db.set_value(DOCTYPE, existing.name, values)
		frappe.db.commit()
		return existing.name
	doc = frappe.get_doc({"doctype": DOCTYPE, **values})
	try:
		doc.insert(ignore_permissions=True)
	except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
		# a concurrent job claimed the same key first
		frappe.db.rollback()
		return None
	frappe.db.commit()
	return doc.name


def _post(token, phone, template, params, button_param):
	body = {
		"template_name": template,
		"language": LANGUAGE,
		"parameters": [{"name": str(i), "value": str(value)} for i, value in enumerate(params or [], 1)],
	}
	if button_param:
		# Dynamic URL-button suffix: WA Studio's per-button value map, keyed by button index.
		body["button_params"] = {"0": str(button_param)}
	try:
		response = requests.post(
			f"{_base_url()}/api/v1/sendTemplateMessage",
			params={"whatsappNumber": phone},
			json=body,
			headers=_headers(token),
			timeout=TIMEOUT,
		)
	except Exception as e:
		return "failed", None, type(e).__name__
	try:
		data = response.json()
	except Exception:
		data = {}
	if not isinstance(data, dict):
		data = {}
	if response.status_code < 400 and (data.get("result") == "success" or data.get("ok") is True):
		return "sent", data.get("messageId") or data.get("message_id"), None
	detail = data.get("error") or data.get("info") or data.get("message") or ""
	error = f"HTTP {response.status_code}" + (f": {_scrub(detail)[:200]}" if detail else "")
	return "failed", None, error


def send_template(user_or_phone, template, params, button_param=None, key=None, phone=None):
	"""Sends one approved template at most once per (template, recipient, key). Never raises."""
	token = _token()
	if not token:
		return "disabled"
	if template not in TEMPLATES:
		return "unknown_template"
	target = None
	try:
		user = user_or_phone if _is_user(user_or_phone) else None
		info = (_user_row(user) or frappe._dict()) if user else frappe._dict()
		target = (
			normalize_phone(phone)
			or normalize_phone(info.get("mobile_no"))
			or (None if user else normalize_phone(user_or_phone))
		)
		if not target:
			return "skipped_no_phone"
		if template in MARKETING and not (user and cint(info.get("wtf_whatsapp_opt_in"))):
			return "skipped_no_opt_in"

		k = dedupe_key(template, user, target, key)
		existing = frappe.db.get_value(DOCTYPE, {"dedupe_key": k}, ["name", "status"], as_dict=True, for_update=True)
		if existing and existing.status not in RETRYABLE:
			return "duplicate"

		row = {"user": user, "phone_masked": mask_phone(target), "template": template, "dedupe_key": k, "error": None}
		if not is_template_approved(template):
			_write_row(existing, {**row, "status": "skipped_not_approved"})
			return "skipped_not_approved"

		name = _write_row(existing, {**row, "status": "sending"})
		if not name:
			return "duplicate"

		status, message_id, error = _post(token, target, template, params, button_param)
		frappe.db.set_value(DOCTYPE, name, {"status": status, "message_id": message_id, "error": error})
		frappe.db.commit()
		if status != "sent":
			_log(template, target, error)
		return status
	except Exception as e:
		# Exception text can carry the request (token) or the number: log its type only.
		_log(template, target, type(e).__name__)
		return "error"


def queue_template(user_or_phone, template, params, button_param=None, key=None, phone=None):
	"""Request-side entry point: send after the surrounding transaction commits. Never raises."""
	try:
		if not _token():
			return
		frappe.enqueue(
			"lms.wtf_whatsapp.send_template",
			queue="short",
			enqueue_after_commit=True,
			user_or_phone=user_or_phone,
			template=template,
			params=params,
			button_param=button_param,
			key=key,
			phone=phone,
		)
	except Exception as e:
		_log(template, phone, f"queue failed ({type(e).__name__})")
