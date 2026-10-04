"""web_sign_up stores the post-signup redirect as a same-site PATH.

frappe.www.login.sanitize_redirect rebuilds a path into an absolute URL from the request URL, and
behind Traefik + nginx that request is http://. The site's update-password page (sanitizeRedirect)
refuses absolute URLs, so the learner landed on /lms instead of the course they signed up for.
"""

from types import SimpleNamespace

import frappe
from frappe.tests import IntegrationTestCase

from lms.lms.user import local_redirect_path, web_sign_up

HOST = "wtf-test.localhost"


class TestSignupRedirect(IntegrationTestCase):
	def setUp(self):
		self._request = getattr(frappe.local, "request", None)
		# GET: rate_limit(methods=["POST"]) passes straight through; sanitize_redirect reads .url
		frappe.local.request = SimpleNamespace(url=f"http://{HOST}/api/method/frappe.core.doctype.user.user.sign_up", method="GET")

	def tearDown(self):
		frappe.local.request = self._request

	def test_path_is_kept_as_a_path(self):
		self.assertEqual(local_redirect_path("/lms/courses/cpt?x=1#top"), "/lms/courses/cpt?x=1#top")

	def test_same_host_absolute_url_is_reduced_to_its_path(self):
		for url in (f"http://{HOST}/lms/courses/cpt", f"https://{HOST}/lms/courses/cpt"):
			self.assertEqual(local_redirect_path(url), "/lms/courses/cpt", url)

	def test_other_hosts_and_junk_are_not_stored(self):
		for url in (
			"https://evil.example/lms",
			f"https://{HOST}.evil.example/lms",
			f"https://{HOST}@evil.example/lms",
			"//evil.example/lms",
			"",
			None,
		):
			self.assertIsNone(local_redirect_path(url), url)

	def test_web_sign_up_caches_a_scheme_less_path(self):
		email = f"qa+redirect-{frappe.generate_hash(length=8)}@example.com"
		try:
			web_sign_up(email, "QA Redirect", "/lms/courses/certified-personal-trainer")
			stored = frappe.cache.hget("redirect_after_login", email)
			self.assertEqual(stored, "/lms/courses/certified-personal-trainer")
			self.assertTrue(stored.startswith("/"))
			self.assertNotIn("://", stored)
		finally:
			frappe.cache.hdel("redirect_after_login", email)

	def test_web_sign_up_stores_nothing_for_a_foreign_redirect(self):
		email = f"qa+redirect-{frappe.generate_hash(length=8)}@example.com"
		try:
			web_sign_up(email, "QA Redirect", "https://evil.example/lms")
			self.assertIsNone(frappe.cache.hget("redirect_after_login", email))
		finally:
			frappe.cache.hdel("redirect_after_login", email)
