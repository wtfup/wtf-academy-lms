"""web_sign_up collects the WhatsApp number + consent and sends academy_account_ready_v1.

Pure unit tests (frappe mocked); the 3-argument call the live /login#signup form and the site
make today must keep working unchanged.
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from lms import wtf_whatsapp as wa
from lms.lms import user as user_module

PHONE = "919876543210"


class SignupHarness(unittest.TestCase):
	def setUp(self):
		self._request = getattr(frappe.local, "request", None)
		# no request: rate_limit passes straight through (local_redirect_path is mocked below)
		frappe.local.request = None

	def tearDown(self):
		frappe.local.request = self._request

	def run_signup(self, *args, phone_taken=False, courses=None, queue_error=None, **kwargs):
		courses = {"sports-nutrition": "Sports Nutrition"} if courses is None else courses
		db = MagicMock()
		db.get.return_value = None
		db.get_creation_count.return_value = 0
		db.get_single_value.return_value = None

		def exists(doctype, filters=None, *a, **kw):
			if doctype == "User":
				return "other@example.com" if phone_taken else None
			if doctype == "LMS Course":
				return filters if filters in courses else None
			return None

		def get_value(doctype, name=None, fieldname=None, *a, **kw):
			if doctype == "LMS Course" and fieldname == "title":
				return courses.get(name)
			return None

		db.exists.side_effect = exists
		db.get_value.side_effect = get_value
		created = {}

		def get_doc(values):
			created.update(values)
			doc = MagicMock()
			doc.name = values["email"]
			doc.flags = SimpleNamespace(email_sent=True, ignore_permissions=False, ignore_password_policy=False)
			return doc

		queue = MagicMock(side_effect=queue_error)
		with (
			patch.object(user_module, "is_signup_disabled", return_value=False),
			patch.object(user_module, "local_redirect_path", side_effect=lambda r: r if r and r.startswith("/") else None),
			patch.object(frappe, "db", db),
			patch.object(frappe, "get_doc", side_effect=get_doc),
			patch.object(frappe, "get_system_settings", return_value=300),
			patch.object(frappe, "cache", MagicMock()),
			patch.object(wa, "queue_template", queue),
			patch("frappe.log_error"),
		):
			result = user_module.web_sign_up(*args, **kwargs)
		return SimpleNamespace(result=result, created=created, queue=queue)


class TestBackwardCompatibleSignup(SignupHarness):
	def test_the_old_three_argument_call_still_works(self):
		r = self.run_signup("riya@example.com", "Riya Sharma", "/lms/courses/sports-nutrition")
		self.assertEqual(r.result[0], 1)
		self.assertEqual(r.created["email"], "riya@example.com")
		self.assertNotIn("mobile_no", r.created)
		self.assertEqual(r.created.get("wtf_whatsapp_opt_in"), 0)
		r.queue.assert_not_called()

	def test_the_old_two_argument_call_still_works(self):
		r = self.run_signup("riya@example.com", "Riya Sharma")
		self.assertEqual(r.result[0], 1)
		r.queue.assert_not_called()


class TestSignupWhatsApp(SignupHarness):
	def test_number_and_consent_are_stored_and_account_ready_is_queued(self):
		r = self.run_signup(
			"riya@example.com",
			"Riya Sharma",
			"/lms/courses/sports-nutrition",
			mobile_no="98765 43210",
			whatsapp_opt_in="1",
		)
		self.assertEqual(r.result[0], 1)
		self.assertEqual(r.created["mobile_no"], PHONE)
		self.assertEqual(r.created["wtf_whatsapp_opt_in"], 1)
		self.assertEqual(r.created["wtf_signup_course"], "sports-nutrition")
		r.queue.assert_called_once_with(
			"riya@example.com", wa.ACCOUNT_READY, ["Riya", "Sports Nutrition"], "sports-nutrition", "signup"
		)

	def test_account_ready_is_utility_so_it_goes_without_consent(self):
		r = self.run_signup(
			"riya@example.com", "Riya", "/lms/courses/sports-nutrition", mobile_no="9876543210", whatsapp_opt_in=None
		)
		self.assertEqual(r.created["wtf_whatsapp_opt_in"], 0)
		r.queue.assert_called_once()

	def test_opt_in_values(self):
		for value, expected in (
			("1", 1),
			(1, 1),
			(True, 1),
			("true", 1),
			("on", 1),
			("0", 0),
			("false", 0),
			(0, 0),
			(False, 0),
			("", 0),
			(None, 0),
		):
			self.assertEqual(wa.parse_opt_in(value), expected, value)

	def test_course_comes_from_the_redirect_path(self):
		with patch.object(frappe, "db", MagicMock(exists=MagicMock(side_effect=lambda dt, n: n == "cpt"))):
			self.assertEqual(wa.course_from_path("/lms/courses/cpt"), "cpt")
			self.assertEqual(wa.course_from_path("/lms/courses/cpt/learn/1-1?x=1"), "cpt")
			self.assertEqual(wa.course_from_path("/courses/cpt"), "cpt")
			self.assertIsNone(wa.course_from_path("/lms/courses/unknown"))
			self.assertIsNone(wa.course_from_path("/lms"))
			self.assertIsNone(wa.course_from_path(None))

	def test_no_course_in_the_redirect_means_no_account_ready(self):
		r = self.run_signup("riya@example.com", "Riya", "/lms", mobile_no="9876543210", whatsapp_opt_in="1")
		self.assertEqual(r.created["mobile_no"], PHONE)
		self.assertIsNone(r.created.get("wtf_signup_course"))
		r.queue.assert_not_called()

	def test_invalid_number_is_ignored_and_signup_succeeds(self):
		r = self.run_signup(
			"riya@example.com", "Riya", "/lms/courses/sports-nutrition", mobile_no="12345", whatsapp_opt_in="1"
		)
		self.assertEqual(r.result[0], 1)
		self.assertNotIn("mobile_no", r.created)
		self.assertEqual(r.created["wtf_whatsapp_opt_in"], 0)
		r.queue.assert_not_called()

	def test_a_number_already_on_another_user_is_not_stored(self):
		# User.mobile_no is unique: storing it would fail the whole signup.
		r = self.run_signup(
			"riya@example.com",
			"Riya",
			"/lms/courses/sports-nutrition",
			mobile_no="9876543210",
			whatsapp_opt_in="1",
			phone_taken=True,
		)
		self.assertEqual(r.result[0], 1)
		self.assertNotIn("mobile_no", r.created)
		self.assertEqual(r.created["wtf_whatsapp_opt_in"], 0)
		r.queue.assert_not_called()

	def test_whatsapp_failure_never_breaks_signup(self):
		r = self.run_signup(
			"riya@example.com",
			"Riya",
			"/lms/courses/sports-nutrition",
			mobile_no="9876543210",
			whatsapp_opt_in="1",
			queue_error=Exception("boom"),
		)
		self.assertEqual(r.result[0], 1)


if __name__ == "__main__":
	unittest.main()
