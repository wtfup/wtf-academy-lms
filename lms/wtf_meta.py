"""WTF Meta Purchase: paid LMS checkouts -> Meta Conversions API (server) + Meta pixel (browser).

Both sides share one dedup key, event_id = "purchase:<LMS Payment name>", so Meta counts the
browser `fbq('track', 'Purchase', ..., {eventID})` and the server event once.

Server (CAPI):
- lms.lms.utils.update_payment_record calls queue_purchase() right after it flips
  payment_received 0 -> 1. That is the only place the flip happens for a gateway payment, under
  a row lock (payment_already_recorded, for_update), so a replayed callback never reaches it.
  A doc_events on_update hook would never fire: the flip is a frappe.db.set_value.
- queue_purchase captures what only the learner's request knows (IP, user agent, _fbp/_fbc
  cookies, event time) and enqueues send_purchase on the short queue after commit, so a
  rolled-back callback sends nothing and the gateway callback never waits on Meta.
- send_purchase is idempotent: it locks the payment row, skips unless payment_received and not
  wtf_meta_purchase_sent, and commits the marker (custom field, lms/fixtures/custom_field.json)
  BEFORE the HTTP call. A retried or duplicated job therefore sends at most once.
- Fail open: any error is logged by payment name only (no token, no email/phone, no response
  body) and never raised.

Browser: queue_purchase also remembers the payment for the learner (cache, 1h). The payment
success page redirects to /lms/courses/<course>; lms/www/_lms.py pops it via
get_browser_tracking() and lms/templates/wtf_tracking.html fires the pixel / GA4 purchase once.

site_config: "meta_pixel_id" (public), "meta_capi_token" (secret, server only),
optional "meta_test_event_code", optional "ga4_measurement_id". Missing pixel/token -> no-op.
"""

import hashlib
import re
import time

import frappe
import requests
from frappe.utils import flt

GRAPH_URL = "https://graph.facebook.com/v21.0/{pixel_id}/events"
TIMEOUT = 10
DEFAULT_COUNTRY_CODE = "91"
SENT_FIELD = "wtf_meta_purchase_sent"
BROWSER_KEY = "wtf_meta:browser_purchase:{user}"
BROWSER_TTL = 60 * 60


def _pixel_id():
	return (frappe.conf.get("meta_pixel_id") or "").strip() or None


def _token():
	return (frappe.conf.get("meta_capi_token") or "").strip() or None


def hash_value(value):
	value = (value or "").strip().lower()
	return hashlib.sha256(value.encode()).hexdigest() if value else None


def normalize_phone(phone, default_country_code=DEFAULT_COUNTRY_CODE):
	"""E.164 digits without the plus. Bare 10-digit (or 0-prefixed) numbers are taken as Indian."""
	digits = re.sub(r"\D", "", phone or "")
	if digits.startswith("00"):
		digits = digits[2:]
	elif len(digits) == 11 and digits.startswith("0"):
		digits = default_country_code + digits[1:]
	elif len(digits) == 10:
		digits = default_country_code + digits
	return digits or None


def capture_request_context():
	ctx = {"event_time": int(time.time())}
	request = getattr(frappe.local, "request", None)
	if not request:
		return ctx
	headers = request.headers or {}
	cookies = request.cookies or {}
	forwarded = (headers.get("X-Forwarded-For") or "").split(",")[0].strip()
	values = {
		"client_ip_address": forwarded or getattr(request, "remote_addr", None),
		"client_user_agent": headers.get("User-Agent"),
		"fbp": cookies.get("_fbp"),
		"fbc": cookies.get("_fbc"),
	}
	ctx.update({key: value for key, value in values.items() if value})
	return ctx


def payment_total(payment):
	return flt(payment.get("amount_with_gst")) or flt(payment.get("amount"))


def build_purchase_event(payment, title, phone, request_ctx, event_source_url=None):
	request_ctx = request_ctx or {}
	user_data = {
		"em": hash_value(payment.member),
		"ph": hash_value(normalize_phone(phone)),
		"external_id": hash_value(payment.member),
	}
	user_data = {key: [value] for key, value in user_data.items() if value}
	for key in ("fbp", "fbc", "client_ip_address", "client_user_agent"):
		if request_ctx.get(key):
			user_data[key] = request_ctx[key]

	event = {
		"event_name": "Purchase",
		"event_time": int(request_ctx.get("event_time") or time.time()),
		"event_id": f"purchase:{payment.name}",
		"action_source": "website",
		"user_data": user_data,
		"custom_data": {
			"value": payment_total(payment),
			"currency": payment.currency,
			"content_ids": [payment.payment_for_document],
			"content_type": "product",
			"content_name": title,
		},
	}
	if event_source_url:
		event["event_source_url"] = event_source_url
	return event


def remember_browser_purchase(payment_name):
	user = frappe.session.user
	if user and user != "Guest":
		frappe.cache.set_value(BROWSER_KEY.format(user=user), payment_name, expires_in_sec=BROWSER_TTL)


def queue_purchase(payment_name):
	"""Called inside the payment callback, right after payment_received flips 0 -> 1."""
	try:
		if not _pixel_id():
			return
		remember_browser_purchase(payment_name)
		if not _token():
			return
		frappe.enqueue(
			"lms.wtf_meta.send_purchase",
			queue="short",
			enqueue_after_commit=True,
			payment_name=payment_name,
			request_ctx=capture_request_context(),
		)
	except Exception:
		# fail open: tracking must never fail a payment the learner has already made
		_log(payment_name, "queue failed")


def _log(payment_name, reason):
	# Payment name and a reason only: never the token, the learner's email/phone or Meta's body.
	frappe.log_error(
		title="Meta CAPI Purchase not sent",
		message=f"LMS Payment {payment_name}: {reason}",
		reference_doctype="LMS Payment",
		reference_name=payment_name,
	)


def _title(doctype, docname):
	return frappe.db.get_value(doctype, docname, "title") or docname


def _details(payment):
	from lms.lms.utils import get_lms_route

	phone = frappe.db.get_value("User", payment.member, "mobile_no") or frappe.db.get_value(
		"User", payment.member, "phone"
	)
	if not phone and payment.get("address"):
		phone = frappe.db.get_value("Address", payment.address, "phone")
	section = "courses" if payment.payment_for_document_type == "LMS Course" else "batches"
	url = frappe.utils.get_url(get_lms_route(f"{section}/{payment.payment_for_document}"))
	return _title(payment.payment_for_document_type, payment.payment_for_document), phone, url


PAYMENT_FIELDS = [
	"name",
	"amount",
	"amount_with_gst",
	"currency",
	"member",
	"address",
	"payment_for_document_type",
	"payment_for_document",
	"payment_received",
	SENT_FIELD,
]


def _claim(payment_name):
	"""Lock the payment, mark it sent and commit. Returns the row, or None if it must not send."""
	payment = frappe.db.get_value("LMS Payment", payment_name, PAYMENT_FIELDS, as_dict=True, for_update=True)
	if not payment or not payment.payment_received or payment.get(SENT_FIELD) or payment_total(payment) <= 0:
		return None
	frappe.db.set_value("LMS Payment", payment_name, SENT_FIELD, 1, update_modified=False)
	frappe.db.commit()
	return payment


def send_purchase(payment_name, request_ctx=None):
	pixel_id, token = _pixel_id(), _token()
	if not pixel_id or not token:
		return
	try:
		payment = _claim(payment_name)
		if not payment:
			return
		title, phone, url = _details(payment)
		body = {
			"data": [build_purchase_event(payment, title, phone, request_ctx, url)],
			"access_token": token,
		}
		test_code = frappe.conf.get("meta_test_event_code")
		if test_code:
			body["test_event_code"] = test_code
		response = requests.post(GRAPH_URL.format(pixel_id=pixel_id), json=body, timeout=TIMEOUT)
		if response.status_code >= 400:
			_log(payment_name, f"Meta responded HTTP {response.status_code}")
	except Exception as e:
		# Exception text can carry the request (token) or learner data: log its type only.
		_log(payment_name, f"{type(e).__name__}")


def pop_browser_purchase():
	"""The purchase this learner just paid for, once, for the pixel/GA4 on their next LMS page."""
	user = frappe.session.user
	if not user or user == "Guest":
		return None
	key = BROWSER_KEY.format(user=user)
	payment_name = frappe.cache.get_value(key)
	if not payment_name:
		return None
	frappe.cache.delete_value(key)
	payment = frappe.db.get_value("LMS Payment", payment_name, PAYMENT_FIELDS, as_dict=True)
	if not payment or not payment.payment_received or payment.member != user or payment_total(payment) <= 0:
		return None
	return {
		"event_id": f"purchase:{payment.name}",
		"transaction_id": payment.name,
		"value": payment_total(payment),
		"currency": payment.currency,
		"content_ids": [payment.payment_for_document],
		"content_type": "product",
		"content_name": _title(payment.payment_for_document_type, payment.payment_for_document),
	}


def get_browser_tracking():
	"""Public ids (never the CAPI token) and a pending purchase for the LMS SPA template."""
	pixel_id = _pixel_id()
	ga4_id = (frappe.conf.get("ga4_measurement_id") or "").strip() or None
	if not pixel_id and not ga4_id:
		return {}
	try:
		purchase = pop_browser_purchase()
	except Exception:
		purchase = None
	return {"pixel_id": pixel_id, "ga4_id": ga4_id, "purchase": purchase}
