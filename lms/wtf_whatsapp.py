"""WTF Academy WhatsApp lifecycle: approved WA Studio templates sent from the academy number.

Sender
- send_template(user_or_phone, template, params, button_param, key, phone=None) is the one
  place a message leaves. It always runs in a worker: request-side triggers use queue_template
  (short queue, after commit), scheduler jobs use enqueue_send (one long-queue job per recipient).
- site_config: "wtf_whatsapp_enabled" (kill switch, 1/true/"1"; default OFF: nothing is sent,
  enqueued or written to the dedupe log), "wastudio_token" (secret, project-bound: no
  channel_number is sent; empty means no-op) and optional "wastudio_url"
  (default https://wastudio.wtflabs.ai).
- Disabled users get nothing. Utility templates (payment pending, enrolment, certificate) go
  to any valid number. Marketing templates (account ready, which Meta recategorised, trial day
  1/3/7, learning nudge) need User.wtf_whatsapp_opt_in = 1 (never a bare phone) and stop after
  3 consecutive failed sends. A live WA Studio category of Marketing also forces the opt-in
  gate on a template the static map calls utility; without the live list the map decides.
- Only a template WA Studio explicitly lists with a non-APPROVED status is skipped (list cached
  10 minutes) and recorded "skipped_not_approved", the only status a later attempt may
  overwrite. An unknown or empty list is never cached and never blocks: the send is attempted
  and WA Studio's own rejection is recorded.
- Idempotent: the dedupe log "WTF WhatsApp Message" has a unique dedupe_key
  "<template>:<user or hashed phone>:<key>". The row is claimed (status "sending") and
  committed BEFORE the HTTP call, under a row lock, so a retried or duplicated job sends at most
  once. A failed or timed-out send stays "failed" (it may have reached the learner).
- Fail open: nothing here raises into signup, payment or certificate code. Logs carry a masked
  phone (91******3210) and an error type or HTTP status, never the token or the raw number.

Triggers: web_sign_up (account ready), update_payment_record (enrolment confirmed),
LMS Certificate after_insert (certificate ready), hourly payment pending, and at 10:30 site
time (cron) trial day 1/3/7 and the learning nudge (see the functions below and lms/hooks.py).
"""

import hashlib
import re
from datetime import timedelta

import frappe
import requests
from frappe.utils import add_days, cint, flt, getdate, now_datetime, nowdate

DOCTYPE = "WTF WhatsApp Message"
DEFAULT_URL = "https://wastudio.wtflabs.ai"
TIMEOUT = 8
LANGUAGE = "en"
APPROVAL_CACHE_KEY = "wtf_whatsapp:template_statuses"
APPROVAL_TTL = 10 * 60
EMPTY_LIST_LOGGED_KEY = "wtf_whatsapp:empty_template_list_logged"
CATEGORY_CACHE_KEY = "wtf_whatsapp:template_categories"
FETCH_FAILED_KEY = "wtf_whatsapp:template_list_failed"
FETCH_FAILED_TTL = 60
FETCH_FAILED_LOGGED_KEY = "wtf_whatsapp:template_list_failed_logged"
RETRYABLE = {"skipped_not_approved"}
MAX_KEY = 140
FAILING_STREAK = 3
WINDOW_STATUSES = ["sending", "sent", "failed"]

ACCOUNT_READY = "academy_account_ready_v1"
TRIAL_DAY1 = "academy_trial_day1_v1"
TRIAL_DAY3 = "academy_trial_day3_v1"
TRIAL_DAY7 = "academy_trial_day7_v1"
PAYMENT_PENDING = "academy_payment_pending_v1"
ENROLMENT_CONFIRMED = "academy_enrolment_confirmed_v1"
LEARNING_NUDGE = "academy_learning_nudge_v1"
CERTIFICATE_READY = "academy_certificate_ready_v1"

UTILITY = {PAYMENT_PENDING, ENROLMENT_CONFIRMED, CERTIFICATE_READY}
# academy_account_ready_v1 was submitted as UTILITY; Meta recategorised it to MARKETING
# (WA Studio sync 2026-10-04: priced as marketing, hit the 131049 per-user marketing cap).
MARKETING = {ACCOUNT_READY, TRIAL_DAY1, TRIAL_DAY3, TRIAL_DAY7, LEARNING_NUDGE}
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


def whatsapp_enabled():
	"""Kill switch, site_config "wtf_whatsapp_enabled": 1 / true / "1". Default OFF."""
	return str(frappe.conf.get("wtf_whatsapp_enabled") or "").strip().lower() in ("1", "true")


def _active_token():
	"""The token when sending is switched on and configured, else None."""
	return _token() if whatsapp_enabled() else None


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
	if frappe.cache.get_value(FETCH_FAILED_KEY):
		# WA Studio failed under a minute ago: don't make every queued send wait on another GET
		return None
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
			return _list_fetch_failed(f"HTTP {response.status_code}")
		statuses, categories = {}, {}
		for row in _template_rows(response.json()):
			if not isinstance(row, dict):
				continue
			name = row.get("name") or row.get("elementName")
			if not name:
				continue
			status = str(row.get("status") or "").upper()
			if statuses.get(name) != "APPROVED":
				statuses[name] = status
			category = str(row.get("category") or "").upper()
			# any language variant categorised Marketing makes the template marketing
			if category and categories.get(name) != "MARKETING":
				categories[name] = category
	except Exception as e:
		# exception text can carry the request: type only
		return _list_fetch_failed(type(e).__name__)
	if not statuses:
		# Unknown list: never cached, sends go ahead (WA Studio rejects unapproved templates
		# itself and that failure is recorded). Logged at most once per cache window.
		if not frappe.cache.get_value(EMPTY_LIST_LOGGED_KEY):
			frappe.cache.set_value(EMPTY_LIST_LOGGED_KEY, 1, expires_in_sec=APPROVAL_TTL)
			frappe.log_error(
				title="WhatsApp template list empty",
				message="WA Studio getMessageTemplates parsed to no templates; sending without the approval check.",
			)
		return None
	frappe.cache.set_value(CATEGORY_CACHE_KEY, categories, expires_in_sec=APPROVAL_TTL)
	frappe.cache.set_value(APPROVAL_CACHE_KEY, statuses, expires_in_sec=APPROVAL_TTL)
	return statuses


def template_categories():
	"""{template name: "MARKETING" | "UTILITY" | "AUTHENTICATION"} from the same WA Studio list
	(cached 10 minutes alongside the statuses). None when the list is unavailable."""
	cached = frappe.cache.get_value(CATEGORY_CACHE_KEY)
	if cached is not None:
		return cached
	if template_statuses() is None:
		return None
	return frappe.cache.get_value(CATEGORY_CACHE_KEY)


def is_marketing(template):
	"""Static map, overridden upwards by the live category: Meta can recategorise an approved
	template to Marketing, and then it must only go to opted-in users. A live "Utility" never
	downgrades a template the code treats as marketing."""
	if template in MARKETING:
		return True
	return (template_categories() or {}).get(template) == "MARKETING"


def _list_fetch_failed(reason):
	"""Negative-cache a failed template list for 60 s and log it at most once per 10 minutes.
	Returns None: an unknown list never blocks a send."""
	frappe.cache.set_value(FETCH_FAILED_KEY, 1, expires_in_sec=FETCH_FAILED_TTL)
	if not frappe.cache.get_value(FETCH_FAILED_LOGGED_KEY):
		frappe.cache.set_value(FETCH_FAILED_LOGGED_KEY, 1, expires_in_sec=APPROVAL_TTL)
		frappe.log_error(
			title="WhatsApp template list unavailable",
			message=f"WA Studio getMessageTemplates failed ({reason}); sending without the approval check.",
		)
	return None


def template_blocked(template, statuses):
	"""Only an explicit non-APPROVED status for this template blocks a send."""
	if not statuses or template not in statuses:
		return False
	return statuses[template] != "APPROVED"


# ---------------------------------------------------------------- sender


def _log(template, phone, reason):
	# Template, masked phone and a reason only: never the token or the raw number.
	frappe.log_error(
		title="WhatsApp template not sent",
		message=f"{template} to {mask_phone(phone)}: {reason}",
	)


def dedupe_key(template, user, phone, key):
	recipient = user or "ph-" + hashlib.sha256(phone.encode()).hexdigest()[:16]
	value = f"{template}:{recipient}:{key}"
	if len(value) > MAX_KEY:
		value = f"{template}:h:{hashlib.sha256(value.encode()).hexdigest()}"
	return value


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


def _failing(user):
	"""True after FAILING_STREAK consecutive failed sends to this user (opted out / unreachable)."""
	last = frappe.get_all(
		DOCTYPE,
		filters={"user": user, "status": ["in", ["sent", "failed"]]},
		fields=["status"],
		order_by="creation desc",
		limit=FAILING_STREAK,
	)
	return len(last) == FAILING_STREAK and all(row.status == "failed" for row in last)


def _recently_sent(template, user, course, hours):
	return frappe.db.exists(
		DOCTYPE,
		{
			"template": template,
			"user": user,
			"course": course,
			"status": ["in", WINDOW_STATUSES],
			"creation": [">", now_datetime() - timedelta(hours=hours)],
		},
	)


def _still_pending(payment_name):
	row = frappe.db.get_value(
		"LMS Payment", payment_name, ["payment_received", "member", "payment_for_document"], as_dict=True
	)
	if not row or cint(row.payment_received):
		return False
	if frappe.db.exists("LMS Enrollment", {"member": row.member, "course": row.payment_for_document}):
		return False
	return not frappe.db.exists(
		"LMS Payment",
		{
			"member": row.member,
			"payment_received": 1,
			"payment_for_document_type": "LMS Course",
			"payment_for_document": row.payment_for_document,
		},
	)


def send_template(
	user_or_phone,
	template,
	params,
	button_param=None,
	key=None,
	phone=None,
	course=None,
	window_hours=None,
	pending_payment=None,
):
	"""Sends one template at most once per (template, recipient, key). Never raises.

	course is recorded on the log row; with window_hours, nothing is sent if the same template
	went to this user for this course within that many hours (checkout retries, weekly nudge).
	pending_payment (payment pending only) is re-checked here, in the worker: if it was paid, or
	the learner is enrolled or paid for the course another way meanwhile, nothing is sent."""
	token = _active_token()
	if not token:
		return "disabled"
	if template not in TEMPLATES:
		return "unknown_template"
	target = None
	try:
		user = user_or_phone if _is_user(user_or_phone) else None
		info = (_user_row(user) or frappe._dict()) if user else frappe._dict()
		if user and not cint(info.get("enabled")):
			return "skipped_disabled"
		target = (
			normalize_phone(phone)
			or normalize_phone(info.get("mobile_no"))
			or (None if user else normalize_phone(user_or_phone))
		)
		if not target:
			return "skipped_no_phone"
		marketing = is_marketing(template)
		if marketing and not (user and cint(info.get("wtf_whatsapp_opt_in"))):
			return "skipped_no_opt_in"
		if marketing and _failing(user):
			return "skipped_failing"
		if window_hours and user and _recently_sent(template, user, course, window_hours):
			return "skipped_recent"
		if pending_payment and not _still_pending(pending_payment):
			return "skipped_resolved"

		k = dedupe_key(template, user, target, key)
		# may call WA Studio (cache miss): done before the row lock below is taken
		blocked = template_blocked(template, template_statuses())
		existing = frappe.db.get_value(
			DOCTYPE, {"dedupe_key": k}, ["name", "status"], as_dict=True, for_update=True
		)
		if existing and existing.status not in RETRYABLE:
			return "duplicate"

		row = {
			"user": user,
			"phone_masked": mask_phone(target),
			"template": template,
			"dedupe_key": k,
			"course": course,
			"error": None,
		}
		if blocked:
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
		if not _active_token():
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


def enqueue_send(
	user_or_phone,
	template,
	params,
	button_param=None,
	key=None,
	phone=None,
	course=None,
	window_hours=None,
	pending_payment=None,
):
	"""Scheduler-side: one long-queue job per recipient, so a slow WA Studio cannot time out the
	selecting job. Never raises."""
	try:
		if not _active_token():
			return
		frappe.enqueue(
			"lms.wtf_whatsapp.send_template",
			queue="long",
			user_or_phone=user_or_phone,
			template=template,
			params=params,
			button_param=button_param,
			key=key,
			phone=phone,
			course=course,
			window_hours=window_hours,
			pending_payment=pending_payment,
		)
	except Exception as e:
		_log(template, phone, f"enqueue failed ({type(e).__name__})")


# ---------------------------------------------------------------- helpers shared by triggers


def first_name(full_name):
	parts = str(full_name or "").strip().split()
	return parts[0] if parts else "there"


def user_first_name(user):
	return first_name(frappe.db.get_value("User", user, "first_name"))


def course_title(course):
	return frappe.db.get_value("LMS Course", course, "title") or course


COURSE_PATH = re.compile(r"^/(?:lms/)?courses/([^/?#]+)")


def course_from_path(path):
	"""The LMS Course slug in a /lms/courses/<slug>... (or /courses/<slug>) path, if it exists."""
	match = COURSE_PATH.match(str(path or ""))
	if not match:
		return None
	slug = match.group(1)
	return slug if frappe.db.exists("LMS Course", slug) else None


def parse_opt_in(value):
	if isinstance(value, str):
		return 1 if value.strip().lower() in ("1", "true", "yes", "on") else 0
	return 1 if value else 0


# ---------------------------------------------------------------- signup


def signup_fields(mobile_no=None, whatsapp_opt_in=None, redirect_path=None):
	"""User fields for a new signup: the number (only if valid and not on another user, since
	User.mobile_no is unique), the WhatsApp consent (only with a stored number) and the course
	slug from the redirect. Never raises."""
	fields = {"wtf_whatsapp_opt_in": 0}
	try:
		phone = normalize_phone(mobile_no)
		if phone and not frappe.db.exists("User", {"mobile_no": phone}):
			fields["mobile_no"] = phone
			fields["wtf_whatsapp_opt_in"] = parse_opt_in(whatsapp_opt_in)
		course = course_from_path(redirect_path)
		if course:
			fields["wtf_signup_course"] = course
	except Exception as e:
		_log(ACCOUNT_READY, None, f"signup fields ({type(e).__name__})")
	return fields


def queue_account_ready(user, full_name, fields):
	"""academy_account_ready_v1 right after signup, when a number, consent and course are known.
	It is a MARKETING template (Meta recategorised it), so no consent means no message."""
	try:
		course = fields.get("wtf_signup_course")
		if not fields.get("mobile_no") or not course or not fields.get("wtf_whatsapp_opt_in"):
			return
		queue_template(user, ACCOUNT_READY, [first_name(full_name), course_title(course)], course, "signup")
	except Exception as e:
		_log(ACCOUNT_READY, None, f"signup queue ({type(e).__name__})")


# ---------------------------------------------------------------- payment recorded


def _enqueue(method, **kwargs):
	"""Request-side: run a send job after commit on the short queue. Never raises."""
	try:
		if not _active_token():
			return
		frappe.enqueue(f"lms.wtf_whatsapp.{method}", queue="short", enqueue_after_commit=True, **kwargs)
	except Exception as e:
		_log(method, None, f"queue failed ({type(e).__name__})")


def queue_enrolment_confirmed(payment_name):
	"""Called by update_payment_record right after payment_received flips 0 -> 1 (next to
	lms.wtf_meta.queue_purchase); a replayed callback never reaches it."""
	_enqueue("send_enrolment_confirmed", payment_name=payment_name)


PAYMENT_FIELDS = [
	"name",
	"amount",
	"amount_with_gst",
	"member",
	"address",
	"payment_for_document_type",
	"payment_for_document",
	"payment_received",
	"payment_for_certificate",
]


def payment_total(row):
	return flt(row.get("amount_with_gst")) or flt(row.get("amount"))


def billing_phone(row):
	"""The billing address phone of a payment, if any (send_template falls back to User.mobile_no)."""
	if row.get("address"):
		return frappe.db.get_value("Address", row.address, "phone") or None
	return None


def _is_course_purchase(row):
	return bool(
		row
		and row.get("payment_for_document_type") == "LMS Course"
		and row.get("payment_for_document")
		and not cint(row.get("payment_for_certificate"))
	)


def send_enrolment_confirmed(payment_name):
	row = frappe.db.get_value("LMS Payment", payment_name, PAYMENT_FIELDS, as_dict=True)
	if not _is_course_purchase(row) or not cint(row.payment_received):
		return
	course = row.payment_for_document
	send_template(
		row.member,
		ENROLMENT_CONFIRMED,
		[user_first_name(row.member), format_inr(payment_total(row)), course_title(course)],
		course,
		key=row.name,
		phone=billing_phone(row),
		course=course,
	)


# ---------------------------------------------------------------- certificate issued


def on_certificate_insert(doc, method=None):
	"""LMS Certificate after_insert (doc_events)."""
	_enqueue("send_certificate_ready", certificate=doc.name)


def send_certificate_ready(certificate):
	cert = frappe.db.get_value(
		"LMS Certificate", certificate, ["name", "member", "course", "batch_name"], as_dict=True
	)
	if not cert or not cert.member:
		return
	if cert.course:
		title = course_title(cert.course)
		# /lms/courses/<course>/certification shows the learner their certificate card
		button = f"courses/{cert.course}/certification"
	elif cert.batch_name:
		username = frappe.db.get_value("User", cert.member, "username")
		if not username:
			return
		title = frappe.db.get_value("LMS Batch", cert.batch_name, "title") or cert.batch_name
		button = f"user/{username}/certificates"
	else:
		return
	send_template(
		cert.member,
		CERTIFICATE_READY,
		[user_first_name(cert.member), title],
		button,
		key=cert.name,
		course=cert.course or None,
	)


# ---------------------------------------------------------------- scheduler jobs
#
# Every job is a no-op while the kill switch is off. Jobs only SELECT recipients and enqueue one
# long-queue send per recipient (enqueue_send), so a slow WA Studio cannot time out the loop.
# Each job may run late, twice or not at all on a given tick; the dedupe log is what makes a
# message go out once:
# - payment pending (hourly): the latest unpaid course checkout created 1 to 24 hours ago per
#   (learner, course), skipped once any LMS Enrollment exists. At send time a 24 h
#   (learner, course) window stops a checkout retry from repeating the message.
# - trial day N (cron 10:30 site time): users whose signup falls on the calendar day today - N,
#   key "trial" (the template differs per day). A rerun the same day selects the same users and
#   dedupes; a day with no run is not backfilled (no stale message on day 9).
# - learning nudge (cron 10:30): key "<course>:<today>" plus a 7-day (learner, course) window
#   at send time, so a learner idle for weeks hears from us at most once a week.


def _each(rows, fn, label):
	for row in rows:
		try:
			fn(row)
		except Exception as e:
			_log(label, None, f"{row.get('name')}: {type(e).__name__}")


PAYMENT_PENDING_WINDOW_HOURS = 24


def send_payment_pending(now=None):
	"""Hourly: academy_payment_pending_v1 for the latest open course checkout per learner+course."""
	if not whatsapp_enabled():
		return
	now = now or now_datetime()
	rows = frappe.get_all(
		"LMS Payment",
		filters=[
			["payment_received", "=", 0],
			["payment_for_document_type", "=", "LMS Course"],
			["payment_for_certificate", "=", 0],
			["creation", ">=", now - timedelta(hours=24)],
			["creation", "<=", now - timedelta(hours=1)],
		],
		fields=PAYMENT_FIELDS,
		order_by="creation desc",
	)
	latest, seen = [], set()
	for row in rows:
		pair = (row.member, row.payment_for_document)
		if row.member and pair not in seen:
			seen.add(pair)
			latest.append(row)

	def send(row):
		course = row.payment_for_document
		# enrolled already: paid (this or another checkout), free, coupon or admin
		if frappe.db.exists("LMS Enrollment", {"member": row.member, "course": course}):
			return
		enqueue_send(
			row.member,
			PAYMENT_PENDING,
			[user_first_name(row.member), course_title(course), format_inr(payment_total(row))],
			course,
			key=f"{course}:{row.name}",
			phone=billing_phone(row),
			course=course,
			window_hours=PAYMENT_PENDING_WINDOW_HOURS,
			# re-checked by the worker: the learner may pay while this job waits in the queue
			pending_payment=row.name,
		)

	_each(latest, send, PAYMENT_PENDING)


TRIAL_DAYS = ((1, TRIAL_DAY1), (3, TRIAL_DAY3), (7, TRIAL_DAY7))


def trial_course(user):
	"""The course a trial learner enrolled in (first, free), else the course they signed up from."""
	enrolled = frappe.get_all(
		"LMS Enrollment", filters={"member": user.name}, fields=["course"], order_by="creation asc", limit=1
	)
	if enrolled and enrolled[0].course:
		return enrolled[0].course
	course = user.get("wtf_signup_course")
	return course if course and frappe.db.exists("LMS Course", course) else None


def course_price(course):
	row = frappe.db.get_value("LMS Course", course, ["paid_course", "course_price", "currency"], as_dict=True)
	if not row or not cint(row.paid_course) or flt(row.course_price) <= 0:
		return None
	if row.currency and row.currency != "INR":
		return f"{row.currency} {flt(row.course_price):,.0f}"
	return format_inr(row.course_price)


def send_trial_reminders(today=None):
	"""Cron 10:30: trial day 1/3/7 to opted-in learners with no paid enrolment (marketing)."""
	if not whatsapp_enabled():
		return
	today = getdate(today or nowdate())
	for days, template in TRIAL_DAYS:
		start = add_days(today, -days)
		users = frappe.get_all(
			"User",
			filters=[
				["enabled", "=", 1],
				["wtf_whatsapp_opt_in", "=", 1],
				["mobile_no", "is", "set"],
				["creation", ">=", str(start)],
				["creation", "<", str(add_days(start, 1))],
			],
			fields=["name", "first_name", "wtf_signup_course"],
		)

		def send(user, template=template):
			if frappe.db.exists("LMS Payment", {"member": user.name, "payment_received": 1}):
				return
			course = trial_course(user)
			if not course:
				return
			params = [first_name(user.first_name), course_title(course)]
			if template == TRIAL_DAY7:
				price = course_price(course)
				if not price:
					return
				params.append(price)
			enqueue_send(user.name, template, params, course, key="trial", course=course)

		_each(users, send, template)


NUDGE_IDLE_DAYS = 5
NUDGE_EVERY_DAYS = 7


def send_learning_nudges(now=None):
	"""Cron 10:30: paid, opted-in learner with no lesson progress for 5 days (at most weekly)."""
	if not whatsapp_enabled():
		return
	now = now or now_datetime()
	today = getdate(now)
	idle_since = now - timedelta(days=NUDGE_IDLE_DAYS)
	opted = frappe.get_all(
		"User",
		filters={"enabled": 1, "wtf_whatsapp_opt_in": 1, "mobile_no": ["is", "set"]},
		pluck="name",
	)
	if not opted:
		return
	enrollments = frappe.get_all(
		"LMS Enrollment",
		filters=[["member", "in", list(opted)], ["progress", "<", 100]],
		fields=["name", "member", "course", "progress", "payment", "creation"],
	)

	def send(row):
		if row.creation and row.creation > idle_since:
			return
		if not row.payment and not frappe.db.exists(
			"LMS Payment",
			{
				"member": row.member,
				"payment_received": 1,
				"payment_for_document_type": "LMS Course",
				"payment_for_document": row.course,
			},
		):
			return
		last = frappe.db.get_value(
			"LMS Course Progress",
			{"member": row.member, "course": row.course},
			"modified",
			order_by="modified desc",
		)
		if last and last > idle_since:
			return
		enqueue_send(
			row.member,
			LEARNING_NUDGE,
			[user_first_name(row.member), course_title(row.course), f"{round(flt(row.progress))}%"],
			row.course,
			key=f"{row.course}:{today}",
			course=row.course,
			window_hours=NUDGE_EVERY_DAYS * 24,
		)

	_each(enrollments, send, LEARNING_NUDGE)
