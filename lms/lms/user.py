import time

import frappe
from frappe import _
from frappe.model.naming import append_number_if_name_exists
from frappe.utils import cint, escape_html, random_string
from frappe.website.utils import cleanup_page_name, is_signup_disabled

from frappe.rate_limiter import rate_limit

from lms.lms.utils import get_country_code, get_lms_route

# WTF: per-IP signup cap. Without it one script exhausts the site-wide hourly cap (real learners get
# 'Temporarily Disabled') and sprays verification mail. 20/hour leaves room for a gym on shared Wi-Fi.
SIGNUP_LIMIT_PER_IP_HOUR = 20


def validate_username_duplicates(doc, method):
	while not doc.username or doc.username_exists():
		doc.username = append_number_if_name_exists(
			doc.doctype, cleanup_page_name(doc.full_name), fieldname="username"
		)
	if " " in doc.username:
		doc.username = doc.username.replace(" ", "")

	if len(doc.username) < 4:
		doc.username = doc.email.replace("@", "").replace(".", "")


def add_lms_student_role(doc, method):
	doc.append_roles("LMS Student")


@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
@rate_limit(limit=SIGNUP_LIMIT_PER_IP_HOUR, seconds=60 * 60)
def sign_up(email: str, full_name: str, verify_terms: bool, user_category: str):
	if is_signup_disabled():
		frappe.throw(_("Sign Up is disabled"), _("Not Allowed"))

	user = frappe.db.get("User", {"email": email})
	if user:
		if user.enabled:
			return 0, _("Already Registered")
		else:
			return 0, _("Registered but disabled")
	else:
		max_signups_allowed_per_hour = cint(frappe.get_system_settings("max_signups_allowed_per_hour") or 300)
		users_created_past_hour = frappe.db.get_creation_count("User", 60)
		if users_created_past_hour >= max_signups_allowed_per_hour:
			frappe.respond_as_web_page(
				_("Temporarily Disabled"),
				_(
					"Too many users signed up recently, so the registration is disabled. Please try back in an hour"
				),
				http_status_code=429,
			)

	default_role = frappe.db.get_single_value("Portal Settings", "default_role")

	# Concurrent signups deadlock on User insert (Frappe doesn't retry 1213); retry, and append roles pre-insert to keep it to one transaction.
	for attempt in range(3):
		try:
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": escape_html(full_name),
					"verify_terms": verify_terms,
					"user_category": user_category,
					"country": "",
					"enabled": 1,
					"new_password": random_string(10),
					"user_type": "Website User",
				}
			)
			user.flags.ignore_permissions = True
			user.flags.ignore_password_policy = True
			if default_role:
				user.append_roles(default_role)
			user.insert()
			break
		except frappe.DuplicateEntryError:
			# A concurrent signup for the same email won the race; treat as already registered.
			frappe.db.rollback()
			return 0, _("Already Registered")
		except frappe.QueryDeadlockError:
			frappe.db.rollback()
			if attempt == 2:
				raise
			time.sleep(0.1 * (attempt + 1))

	set_country_from_ip(None, user.name)

	if user.flags.email_sent:
		return 1, _("Signup successful. Please check your email for verification.")
	else:
		return 2, _("Signup successful. Please ask your administrator to verify your sign-up.")


def local_redirect_path(redirect_to: str | None) -> str | None:
	"""A same-site redirect as path + query + fragment, or None.

	frappe's sanitize_redirect rebuilds even a bare path into an absolute URL from the request URL,
	which behind Traefik + nginx is http://, and it rewrites a foreign host to /desk instead of
	refusing it. Callers that hand this back to the site (update_password -> sanitizeRedirect) need
	a path, so: refuse other hosts, keep only the path part of the same-site URL.
	"""
	from urllib.parse import urlsplit, urlunsplit

	from frappe.www.login import sanitize_redirect

	if not redirect_to or not isinstance(redirect_to, str):
		return None
	request_host = urlsplit(frappe.local.request.url).hostname
	original = urlsplit(redirect_to)
	if original.netloc and (original.hostname or "").lower() != (request_host or "").lower():
		return None
	sanitized = sanitize_redirect(redirect_to)
	if not sanitized:
		return None
	parts = urlsplit(sanitized)
	if (parts.hostname or "").lower() != (request_host or "").lower():
		return None
	path = parts.path or "/"
	if not path.startswith("/") or path.startswith("//") or "\\" in path:
		return None
	return urlunsplit(("", "", path, parts.query, parts.fragment))


@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
@rate_limit(limit=SIGNUP_LIMIT_PER_IP_HOUR, seconds=60 * 60)
def web_sign_up(
	email: str,
	full_name: str,
	redirect_to: str | None = None,
	mobile_no: str | None = None,
	whatsapp_opt_in: str | int | bool | None = None,
) -> tuple[int, str]:
	"""Drop-in for frappe.core.doctype.user.user.sign_up (the /login#signup form).

	Core inserts the user with a random new_password and then calls add_roles(), which saves
	again while new_password is still set; that second save is treated as a password change and
	mails a brand-new learner "Security Alert: Your password has been changed". Same behaviour
	here, but the default role is appended before the single insert.

	WTF: optional mobile_no (Indian mobile, stored as 91XXXXXXXXXX; invalid or already used
	numbers are ignored) and whatsapp_opt_in (1/true/on = consent to WhatsApp updates). With a
	number and a course in redirect_to, academy_account_ready_v1 is queued (lms/wtf_whatsapp.py)."""
	from lms.wtf_whatsapp import queue_account_ready, signup_fields

	if is_signup_disabled():
		frappe.throw(_("Sign Up is disabled"), title=_("Not Allowed"))

	user = frappe.db.get("User", {"email": email})
	if user:
		return (0, _("Already Registered")) if user.enabled else (0, _("Registered but disabled"))

	max_signups_allowed_per_hour = cint(frappe.get_system_settings("max_signups_allowed_per_hour") or 300)
	if frappe.db.get_creation_count("User", 60) >= max_signups_allowed_per_hour:
		frappe.respond_as_web_page(
			_("Temporarily Disabled"),
			_("Too many users signed up recently, so the registration is disabled. Please try back in an hour"),
			http_status_code=429,
		)
		return 0, _("Temporarily Disabled")

	# stored as a path: the site's update-password page refuses absolute (and http://) URLs
	redirect_path = local_redirect_path(redirect_to)
	whatsapp = signup_fields(mobile_no, whatsapp_opt_in, redirect_path)

	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": escape_html(full_name),
			"enabled": 1,
			"new_password": random_string(10),
			"user_type": "Website User",
			**whatsapp,
		}
	)
	user.flags.ignore_permissions = True
	user.flags.ignore_password_policy = True
	default_role = frappe.db.get_single_value("Portal Settings", "default_role")
	if default_role:
		user.append_roles(default_role)
	user.insert()

	if redirect_path:
		frappe.cache.hset("redirect_after_login", user.name, redirect_path)
	queue_account_ready(user.name, full_name, whatsapp)

	if user.flags.email_sent:
		return 1, _("Please check your email for verification")
	return 2, _("Please ask your administrator to verify your sign-up")


def set_country_from_ip(login_manager: object = None, user: str = None):
	if not user and login_manager:
		user = login_manager.user
	user_country = frappe.db.get_value("User", user, "country")
	if user_country:
		return
	frappe.db.set_value("User", user, "country", get_country_code())
	return


def on_login(login_manager):
	default_app = frappe.db.get_single_value("System Settings", "default_app")
	if default_app == "lms":
		frappe.local.response["home_page"] = get_lms_route()
