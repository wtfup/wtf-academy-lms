"""WTF runtime shims for Frappe framework races (we fork LMS, not Frappe).

Installed once per worker from the `before_request` hook; idempotent.

Root cause (frappe/utils/caching.py `redis_cache`): the wrapper does
    val = cache.get_value(key)      # miss -> None
    if cache.exists(key): return None   # ...but another request just filled the key
i.e. a check-then-act race. During a cache refill (every deploy / cache clear) a concurrent
caller can be handed None for a function that never returns None, and the website router then
crashes iterating it -> HTTP 500 (~1 in 60 page loads under 60-way concurrency).

For cached functions whose real result is never None, recompute directly from the database
(via the undecorated function) when the race yields None.
"""

import importlib

_installed = False

# (module, function) pairs on the website request path whose real return value is never None.
# Deliberately excludes document_page._find_matching_document_webview (None is a legit result).
NEVER_NONE_CACHED = (
	("frappe.website.doctype.web_form.web_form", "get_published_web_forms"),
	("frappe.website.doctype.web_page.web_page", "get_dynamic_web_pages"),
	("frappe.www.sitemap", "get_public_pages_from_doctypes"),
	("frappe.website.doctype.web_page_view.web_page_view", "get_page_view_count"),
)


def install_shims():
	global _installed
	if _installed:
		return
	for module_name, func_name in NEVER_NONE_CACHED:
		guard_never_none(module_name, func_name)
	_installed = True


def guard_never_none(module_name: str, func_name: str) -> bool:
	"""Replace module.func with a wrapper that never returns None. Returns True if installed."""
	try:
		module = importlib.import_module(module_name)
	except ImportError:
		return False
	cached = getattr(module, func_name, None)
	if cached is None or getattr(cached, "_wtf_guarded", False):
		return False
	uncached = getattr(cached, "__wrapped__", None)  # functools.wraps in redis_cache

	def guarded(*args, **kwargs):
		val = cached(*args, **kwargs)
		if val is None and uncached is not None:
			val = uncached(*args, **kwargs)
		return val

	for attr in ("clear_cache", "ttl", "__wrapped__", "__doc__", "__name__", "__qualname__"):
		if hasattr(cached, attr):
			setattr(guarded, attr, getattr(cached, attr))
	guarded._wtf_guarded = True
	setattr(module, func_name, guarded)
	return True
