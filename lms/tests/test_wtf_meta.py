"""Meta Purchase (Conversions API + browser pixel) for paid LMS checkouts.

Pure unit tests: frappe.db, frappe.enqueue, frappe.cache and requests are mocked, so they run
without a site (plain `python -m unittest lms.tests.test_wtf_meta` inside the app image) as
well as under `bench run-tests`.
"""

import hashlib
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from lms import wtf_meta

CONF = {"meta_pixel_id": "2804119553060376", "meta_capi_token": "SECRET-TOKEN"}


def sha(value):
	return hashlib.sha256(value.encode()).hexdigest()


def payment_row(**overrides):
	row = {
		"name": "PAY-0001",
		"amount": 4999,
		"amount_with_gst": 0,
		"currency": "INR",
		"member": "Learner@Example.com ",
		"payment_for_document_type": "LMS Course",
		"payment_for_document": "sports-nutrition",
		"payment_received": 1,
		"wtf_meta_purchase_sent": 0,
	}
	row.update(overrides)
	return frappe._dict(row)


REQUEST_CTX = {
	"client_ip_address": "203.0.113.7",
	"client_user_agent": "Mozilla/5.0 Test",
	"fbp": "fb.1.1700000000000.123",
	"fbc": "fb.1.1700000000000.AbCd",
	"event_time": 1700000000,
}


class _SiteConf(unittest.TestCase):
	"""Gives frappe.conf a backing dict when no site is initialised (standalone runs)."""

	def setUp(self):
		self._own_conf = not getattr(frappe.local, "conf", None)
		if self._own_conf:
			frappe.local.conf = frappe._dict()

	def tearDown(self):
		if self._own_conf:
			del frappe.local.conf


class TestHashing(unittest.TestCase):
	def test_email_is_trimmed_lowercased_then_sha256(self):
		self.assertEqual(wtf_meta.hash_value("  Learner@Example.COM "), sha("learner@example.com"))

	def test_empty_values_are_not_hashed(self):
		self.assertIsNone(wtf_meta.hash_value(""))
		self.assertIsNone(wtf_meta.hash_value(None))
		self.assertIsNone(wtf_meta.hash_value("   "))

	def test_indian_mobile_gets_country_code_and_no_plus(self):
		self.assertEqual(wtf_meta.normalize_phone("98765 43210"), "919876543210")
		self.assertEqual(wtf_meta.normalize_phone("+91-98765-43210"), "919876543210")
		self.assertEqual(wtf_meta.normalize_phone("098765 43210"), "919876543210")
		self.assertEqual(wtf_meta.normalize_phone("0044 20 7946 0958"), "442079460958")
		self.assertIsNone(wtf_meta.normalize_phone(""))
		self.assertIsNone(wtf_meta.normalize_phone(None))


class TestPurchasePayload(unittest.TestCase):
	def test_event_contract(self):
		payload = wtf_meta.build_purchase_event(
			payment_row(),
			title="Sports Nutrition",
			phone="98765 43210",
			request_ctx=REQUEST_CTX,
			event_source_url="https://online.wtfgymsacademy.com/lms/courses/sports-nutrition",
		)
		self.assertEqual(payload["event_name"], "Purchase")
		self.assertEqual(payload["event_id"], "purchase:PAY-0001")
		self.assertEqual(payload["action_source"], "website")
		self.assertEqual(payload["event_time"], 1700000000)
		self.assertEqual(
			payload["event_source_url"], "https://online.wtfgymsacademy.com/lms/courses/sports-nutrition"
		)
		self.assertEqual(
			payload["custom_data"],
			{
				"value": 4999.0,
				"currency": "INR",
				"content_ids": ["sports-nutrition"],
				"content_type": "product",
				"content_name": "Sports Nutrition",
			},
		)
		user = payload["user_data"]
		self.assertEqual(user["em"], [sha("learner@example.com")])
		self.assertEqual(user["ph"], [sha("919876543210")])
		self.assertEqual(user["external_id"], [sha("learner@example.com")])
		self.assertEqual(user["fbp"], "fb.1.1700000000000.123")
		self.assertEqual(user["fbc"], "fb.1.1700000000000.AbCd")
		self.assertEqual(user["client_ip_address"], "203.0.113.7")
		self.assertEqual(user["client_user_agent"], "Mozilla/5.0 Test")

	def test_value_is_the_total_charged_including_gst(self):
		payload = wtf_meta.build_purchase_event(
			payment_row(amount=1000, amount_with_gst=1180), title="T", phone=None, request_ctx={}
		)
		self.assertEqual(payload["custom_data"]["value"], 1180.0)

	def test_missing_identifiers_are_omitted_not_sent_empty(self):
		payload = wtf_meta.build_purchase_event(payment_row(), title="T", phone=None, request_ctx={})
		for key in ("ph", "fbp", "fbc", "client_ip_address", "client_user_agent"):
			self.assertNotIn(key, payload["user_data"])
		self.assertIn("event_time", payload)


class TestRequestContext(unittest.TestCase):
	def test_reads_first_forwarded_hop_user_agent_and_fb_cookies(self):
		request = SimpleNamespace(
			headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.2", "User-Agent": "UA/1"},
			remote_addr="10.0.0.2",
			cookies={"_fbp": "fb.1.1.2", "_fbc": "fb.1.1.click"},
		)
		with patch.object(frappe.local, "request", request, create=True):
			ctx = wtf_meta.capture_request_context()
		self.assertEqual(ctx["client_ip_address"], "203.0.113.7")
		self.assertEqual(ctx["client_user_agent"], "UA/1")
		self.assertEqual(ctx["fbp"], "fb.1.1.2")
		self.assertEqual(ctx["fbc"], "fb.1.1.click")
		self.assertIsInstance(ctx["event_time"], int)

	def test_falls_back_to_remote_addr_and_tolerates_no_request(self):
		request = SimpleNamespace(headers={}, remote_addr="198.51.100.1", cookies={})
		with patch.object(frappe.local, "request", request, create=True):
			self.assertEqual(wtf_meta.capture_request_context()["client_ip_address"], "198.51.100.1")
		with patch.object(frappe.local, "request", None, create=True):
			ctx = wtf_meta.capture_request_context()
		self.assertNotIn("client_ip_address", ctx)
		self.assertIsInstance(ctx["event_time"], int)


class TestQueuePurchase(_SiteConf):
	def test_enqueues_on_short_queue_after_commit_with_request_context(self):
		with patch.dict(frappe.conf, CONF), patch("frappe.enqueue") as enqueue, patch.object(
			wtf_meta, "capture_request_context", return_value=REQUEST_CTX
		), patch.object(wtf_meta, "remember_browser_purchase"):
			wtf_meta.queue_purchase("PAY-0001")
		enqueue.assert_called_once()
		args, kwargs = enqueue.call_args
		self.assertEqual(args[0], "lms.wtf_meta.send_purchase")
		self.assertEqual(kwargs["queue"], "short")
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertEqual(kwargs["payment_name"], "PAY-0001")
		self.assertEqual(kwargs["request_ctx"], REQUEST_CTX)

	def test_not_configured_is_noop(self):
		for conf in ({"meta_pixel_id": None, "meta_capi_token": "x"}, {"meta_pixel_id": "1", "meta_capi_token": None}):
			with patch.dict(frappe.conf, conf), patch("frappe.enqueue") as enqueue, patch.object(
				wtf_meta, "remember_browser_purchase"
			) as remember:
				wtf_meta.queue_purchase("PAY-0001")
			enqueue.assert_not_called()
			# The browser pixel only needs the (public) pixel id, not the CAPI token.
			self.assertEqual(remember.called, bool(conf["meta_pixel_id"]))

	def test_remembers_the_purchase_for_the_learners_next_lms_page(self):
		cache = MagicMock()
		with patch.object(frappe, "cache", cache), patch.object(
			frappe, "session", frappe._dict(user="learner@example.com")
		):
			wtf_meta.remember_browser_purchase("PAY-0001")
		args, kwargs = cache.set_value.call_args
		self.assertEqual(args[:2], ("wtf_meta:browser_purchase:learner@example.com", "PAY-0001"))
		self.assertTrue(kwargs["expires_in_sec"])

	def test_never_breaks_the_payment_callback(self):
		with patch.dict(frappe.conf, CONF), patch("frappe.enqueue", side_effect=Exception("redis down")), patch(
			"frappe.log_error"
		) as log_error:
			wtf_meta.queue_purchase("PAY-0001")
		log_error.assert_called_once()


class TestSendPurchase(_SiteConf):
	def _send(self, row, post=None):
		db = MagicMock()
		db.get_value.return_value = row
		post = post or MagicMock(return_value=MagicMock(status_code=200))
		with patch.dict(frappe.conf, CONF), patch.object(frappe, "db", db), patch.object(
			wtf_meta, "_details", return_value=("Sports Nutrition", "9876543210", "https://x/lms/courses/c")
		), patch.object(wtf_meta.requests, "post", post), patch("frappe.log_error") as log_error:
			wtf_meta.send_purchase("PAY-0001", REQUEST_CTX)
		return db, post, log_error

	def test_sends_once_and_persists_the_marker_first(self):
		db, post, _ = self._send(payment_row())
		post.assert_called_once()
		self.assertEqual(db.get_value.call_args.kwargs.get("for_update"), True)
		db.set_value.assert_called_once_with(
			"LMS Payment", "PAY-0001", "wtf_meta_purchase_sent", 1, update_modified=False
		)
		url = post.call_args.args[0]
		body = post.call_args.kwargs["json"]
		self.assertEqual(url, "https://graph.facebook.com/v21.0/2804119553060376/events")
		self.assertNotIn("SECRET-TOKEN", url)
		self.assertEqual(body["access_token"], "SECRET-TOKEN")
		self.assertEqual(body["data"][0]["event_id"], "purchase:PAY-0001")
		self.assertNotIn("test_event_code", body)
		self.assertTrue(post.call_args.kwargs["timeout"])

	def test_not_sent_again_once_marked(self):
		db, post, _ = self._send(payment_row(wtf_meta_purchase_sent=1))
		post.assert_not_called()
		db.set_value.assert_not_called()

	def test_not_sent_for_unpaid_or_missing_payment(self):
		_, post, _ = self._send(payment_row(payment_received=0))
		post.assert_not_called()
		_, post, _ = self._send(None)
		post.assert_not_called()

	def test_not_sent_for_zero_amount_payment(self):
		db, post, _ = self._send(payment_row(amount=0, amount_with_gst=0))
		post.assert_not_called()
		db.set_value.assert_not_called()

	def test_test_event_code_is_passed_when_configured(self):
		with patch.dict(frappe.conf, {"meta_test_event_code": "TEST123"}):
			_, post, _ = self._send(payment_row())
		self.assertEqual(post.call_args.kwargs["json"]["test_event_code"], "TEST123")

	def test_fails_open_and_logs_without_token_or_pii(self):
		post = MagicMock(side_effect=Exception("boom SECRET-TOKEN learner@example.com"))
		_, _, log_error = self._send(payment_row(), post=post)
		log_error.assert_called_once()
		logged = repr(log_error.call_args)
		self.assertNotIn("SECRET-TOKEN", logged)
		self.assertNotIn("learner@example.com", logged.lower())
		self.assertIn("PAY-0001", logged)

	def test_http_error_response_fails_open(self):
		post = MagicMock(return_value=MagicMock(status_code=400, ok=False))
		_, _, log_error = self._send(payment_row(), post=post)
		log_error.assert_called_once()
		self.assertIn("400", repr(log_error.call_args))


class TestPaymentCallbackHook(unittest.TestCase):
	"""update_payment_record queues the Purchase only on the payment_received 0 -> 1 transition."""

	def _run(self, already_recorded):
		from lms.lms import utils

		data = frappe._dict(payment="PAY-0001", payment_gateway="Razorpay")
		with patch.object(utils, "get_payment_callback_data", return_value=data), patch.object(
			utils, "serialize_callbacks_without_the_constraint"
		), patch.object(utils, "payment_already_recorded", return_value=already_recorded), patch.object(
			utils, "update_payment_details"
		), patch.object(utils, "complete_enrollment"), patch("lms.wtf_meta.queue_purchase") as queue:
			utils.update_payment_record("LMS Course", "sports-nutrition")
		return queue

	def test_queued_once_when_payment_is_first_recorded(self):
		self._run(already_recorded=False).assert_called_once_with("PAY-0001")

	def test_not_queued_for_a_replayed_callback(self):
		self._run(already_recorded=True).assert_not_called()


class TestBrowserTracking(_SiteConf):
	def test_empty_without_pixel_or_ga4(self):
		with patch.dict(frappe.conf, {"meta_pixel_id": None, "ga4_measurement_id": None}):
			self.assertEqual(wtf_meta.get_browser_tracking(), {})

	def test_pixel_and_ga4_ids_without_pending_purchase(self):
		with patch.dict(frappe.conf, {**CONF, "ga4_measurement_id": "G-ABC123"}), patch.object(
			wtf_meta, "pop_browser_purchase", return_value=None
		):
			tracking = wtf_meta.get_browser_tracking()
		self.assertEqual(tracking, {"pixel_id": "2804119553060376", "ga4_id": "G-ABC123", "purchase": None})
		self.assertNotIn("SECRET-TOKEN", repr(tracking))

	def test_pending_purchase_for_this_learner_is_exposed_once(self):
		cache = MagicMock()
		cache.get_value.return_value = "PAY-0001"
		db = MagicMock()
		db.get_value.return_value = payment_row(member="learner@example.com")
		with patch.dict(frappe.conf, CONF), patch.object(frappe, "cache", cache), patch.object(
			frappe, "db", db
		), patch.object(frappe, "session", frappe._dict(user="learner@example.com")), patch.object(
			wtf_meta, "_title", return_value="Sports Nutrition"
		):
			purchase = wtf_meta.pop_browser_purchase()
		cache.delete_value.assert_called_once()
		self.assertEqual(
			purchase,
			{
				"event_id": "purchase:PAY-0001",
				"transaction_id": "PAY-0001",
				"value": 4999.0,
				"currency": "INR",
				"content_ids": ["sports-nutrition"],
				"content_type": "product",
				"content_name": "Sports Nutrition",
			},
		)

	def test_pending_purchase_of_another_user_or_unpaid_is_ignored(self):
		for row in (payment_row(member="someone@else.com"), payment_row(member="learner@example.com", payment_received=0)):
			cache = MagicMock()
			cache.get_value.return_value = "PAY-0001"
			db = MagicMock()
			db.get_value.return_value = row
			with patch.object(frappe, "cache", cache), patch.object(frappe, "db", db), patch.object(
				frappe, "session", frappe._dict(user="learner@example.com")
			):
				self.assertIsNone(wtf_meta.pop_browser_purchase())

	def test_guest_has_no_pending_purchase(self):
		cache = MagicMock()
		with patch.object(frappe, "cache", cache), patch.object(frappe, "session", frappe._dict(user="Guest")):
			self.assertIsNone(wtf_meta.pop_browser_purchase())
		cache.get_value.assert_not_called()


class TestSentMarkerField(unittest.TestCase):
	"""The idempotency marker ships as a Custom Field fixture (no core DocType JSON edit)."""

	def test_fixture_defines_hidden_read_only_check_on_lms_payment(self):
		import json
		import os

		import lms

		path = os.path.join(os.path.dirname(lms.__file__), "fixtures", "custom_field.json")
		with open(path) as f:
			fields = {row["name"]: row for row in json.load(f)}
		field = fields.get("LMS Payment-wtf_meta_purchase_sent")
		self.assertIsNotNone(field)
		self.assertEqual(field["dt"], "LMS Payment")
		self.assertEqual(field["fieldname"], wtf_meta.SENT_FIELD)
		self.assertEqual(field["fieldtype"], "Check")
		self.assertEqual((field["hidden"], field["read_only"], field["no_copy"]), (1, 1, 1))
