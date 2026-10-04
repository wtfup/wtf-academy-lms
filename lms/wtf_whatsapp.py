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
			"error": None,
		}
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
	"""academy_account_ready_v1 (utility) right after signup, when a number and course are known."""
	try:
		course = fields.get("wtf_signup_course")
		if not fields.get("mobile_no") or not course:
			return
		queue_template(user, ACCOUNT_READY, [first_name(full_name), course_title(course)], course, "signup")
	except Exception as e:
		_log(ACCOUNT_READY, None, f"signup queue ({type(e).__name__})")


# ---------------------------------------------------------------- payment recorded


def _enqueue(method, **kwargs):
	"""Request-side: run a send job after commit on the short queue. Never raises."""
	try:
		if not _token():
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
		title = frappe.db.get_value("LMS Batch", cert.batch_name, "title") or cert.batch_name
		button = f"user/{frappe.db.get_value('User', cert.member, 'username')}/certificates"
	else:
		return
	send_template(
		cert.member, CERTIFICATE_READY, [user_first_name(cert.member), title], button, key=cert.name
	)


# ---------------------------------------------------------------- scheduler jobs
#
# Each job may run late, twice or not at all on a given tick; the dedupe log is what makes a
# message go out once. Windows are chosen so a late run still finds its rows:
# - payment pending (hourly): any unpaid course checkout created 1 to 24 hours ago, key = the
#   payment, so the first run after the 1-hour mark sends and later runs dedupe.
# - trial day N (daily): users whose signup falls on the calendar day today - N, key "trial"
#   (the template differs per day). A run later the same day selects the same users and
#   dedupes; a day with no run is not backfilled (no stale "yesterday" message on day 9).
# - learning nudge (daily): key "<course>:<today>", plus no nudge for that course in the last
#   7 days, so a learner idle for weeks hears from us at most once a week.


def _each(rows, fn, label):
	for row in rows:
		try:
			fn(row)
		except Exception as e:
			_log(label, None, f"{row.get('name')}: {type(e).__name__}")


def send_payment_pending(now=None):
	"""Hourly: academy_payment_pending_v1 once per unpaid course checkout (latest per learner+course)."""
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
		if frappe.db.exists(
			"LMS Payment",
			{
				"member": row.member,
				"payment_received": 1,
				"payment_for_document_type": "LMS Course",
				"payment_for_document": row.payment_for_document,
			},
		):
			return
		course = row.payment_for_document
		send_template(
			row.member,
			PAYMENT_PENDING,
			[user_first_name(row.member), course_title(course), format_inr(payment_total(row))],
			course,
			key=row.name,
			phone=billing_phone(row),
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
	"""Daily: trial day 1/3/7 to opted-in learners with no paid enrolment (marketing)."""
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
			send_template(user.name, template, params, course, key="trial")

		_each(users, send, template)


NUDGE_IDLE_DAYS = 5
NUDGE_EVERY_DAYS = 7


def send_learning_nudges(now=None):
	"""Daily: paid, opted-in learner with no lesson progress for 5 days (at most once per 7 days)."""
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
		if frappe.db.exists(
			DOCTYPE,
			{
				"template": LEARNING_NUDGE,
				"user": row.member,
				"dedupe_key": ["like", f"{LEARNING_NUDGE}:{row.member}:{row.course}:%"],
				"status": ["in", ["sending", "sent", "failed"]],
				"creation": [">", now - timedelta(days=NUDGE_EVERY_DAYS)],
			},
		):
			return
		send_template(
			row.member,
			LEARNING_NUDGE,
			[user_first_name(row.member), course_title(row.course), f"{round(flt(row.progress))}%"],
			row.course,
			key=f"{row.course}:{today}",
		)

	_each(enrollments, send, LEARNING_NUDGE)
