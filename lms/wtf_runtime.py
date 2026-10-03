"""WTF runtime shims for Frappe framework races (we fork LMS, not Frappe).

Installed once per worker from the `before_request` hook; each shim is idempotent."""

import frappe

_installed = False


def install_shims():
	global _installed
	if _installed:
		return
	_guard_published_web_forms()
	_installed = True


def _guard_published_web_forms():
	"""frappe.website.router.get_page_info_from_web_form iterates get_published_web_forms(),
	which is @redis_cache-wrapped. Right after a cache clear, concurrent requests can receive
	None while another request repopulates the key -> TypeError -> HTTP 500 on any website
	route (seen ~1/60 on /lms under 60-way concurrency). Treat None as "no web forms"."""
	from frappe.website.doctype.web_form import web_form

	original = web_form.get_published_web_forms
	if getattr(original, "_wtf_guarded", False):
		return

	def guarded():
		return original() or []

	guarded.clear_cache = original.clear_cache  # router.clear_cache() relies on this handle
	guarded._wtf_guarded = True
	web_form.get_published_web_forms = guarded
