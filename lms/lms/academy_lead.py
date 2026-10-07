# Copyright (c) 2026, WTF Gyms
"""Career Scorecard leads from the marketing site (site/src/lib/lead-api.ts, POST /api/lead).

The site server calls create_lead over the compose network with the shared secret in the
X-Academy-Lead-Secret header (site_config academy_lead_secret, from SSM via the deploy .env). There is no
API user: a Frappe user with a create-only role still passes create checks on many core doctypes, so a
leaked key could not be kept to one doctype. This method can only ever insert an "Academy Lead"
(DocType from the project's infra/configure_leads.py; only System Manager and Moderator can read it).

Every field is validated again here; the site's checks are a convenience, not the boundary.
Nothing sensitive is logged, and the reply carries nothing about the row. No message is sent.
"""

import hmac
import json
import re

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import validate_email_address

DOCTYPE = "Academy Lead"
HEADER = "X-Academy-Lead-Secret"
SOURCE = "scorecard"

NAME_MAX = 140
EMAIL_MAX = 140
CITY_MAX = 60
COURSE_MAX = 140
ANSWERS_MAX_BYTES = 4096

_PHONE = re.compile(r"^(?:\+?91)?([6-9]\d{9})$")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_IP_HASH = re.compile(r"^[0-9a-f]{64}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# Every call comes from the site container (one IP), so this is a site-wide ceiling on top of the
# site's own 5 per minute per visitor.
RATE_LIMIT = 60
RATE_SECONDS = 60


def _secret_ok() -> bool:
	expected = frappe.conf.get("academy_lead_secret")
	if not expected or not isinstance(expected, str):
		return False
	given = frappe.get_request_header(HEADER) or ""
	return hmac.compare_digest(given.encode(), expected.encode())


def _invalid(field: str):
	# Field name only: never echo the submitted value.
	frappe.throw(_("Invalid lead field: {0}").format(field), frappe.ValidationError)


def _text(value, field: str, *, max_len: int, min_len: int = 0, required: bool = False) -> str:
	if value is None or value == "":
		if required:
			_invalid(field)
		return ""
	if not isinstance(value, str):
		_invalid(field)
	v = re.sub(r"[ \t]+", " ", value.strip())
	if len(v) < min_len or len(v) > max_len or _CONTROL.search(v):
		_invalid(field)
	return v


def normalize_phone(raw) -> str | None:
	"""Indian mobile -> 91XXXXXXXXXX, or None (same rule as the site's normalizeIndianMobile)."""
	if not isinstance(raw, str):
		return None
	m = _PHONE.match(re.sub(r"[\s-]+", "", raw.strip()))
	return f"91{m.group(1)}" if m else None


def _consent(value) -> bool:
	return value is True or value == 1 or value in ("1", "true")


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=RATE_LIMIT, seconds=RATE_SECONDS)
def create_lead(
	full_name=None,
	phone=None,
	email=None,
	consent=None,
	city=None,
	recommended_course=None,
	answers_json=None,
	created_from_ip_hash=None,
):
	if not _secret_ok():
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	name = _text(full_name, "full_name", max_len=NAME_MAX, min_len=2, required=True)

	mobile = normalize_phone(phone)
	if not mobile:
		_invalid("phone")

	mail = _text(email, "email", max_len=EMAIL_MAX, required=True).lower()
	if validate_email_address(mail, throw=False) != mail:
		_invalid("email")

	if not _consent(consent):
		_invalid("consent")

	town = _text(city, "city", max_len=CITY_MAX)

	course = _text(recommended_course, "recommended_course", max_len=COURSE_MAX)
	if course and not _SLUG.match(course):
		_invalid("recommended_course")

	answers = "{}"
	if answers_json not in (None, ""):
		if not isinstance(answers_json, str) or len(answers_json.encode()) > ANSWERS_MAX_BYTES:
			_invalid("answers_json")
		try:
			parsed = json.loads(answers_json)
		except ValueError:
			_invalid("answers_json")
		if not isinstance(parsed, dict):
			_invalid("answers_json")
		answers = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))

	ip_hash = created_from_ip_hash or ""
	if ip_hash and (not isinstance(ip_hash, str) or not _IP_HASH.match(ip_hash)):
		_invalid("created_from_ip_hash")

	frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"full_name": name,
			"phone": mobile,
			"email": mail,
			"city": town,
			"source": SOURCE,
			"recommended_course": course,
			"answers_json": answers,
			"consent": 1,
			"created_from_ip_hash": ip_hash,
		}
	).insert(ignore_permissions=True)
	return {"ok": True}
