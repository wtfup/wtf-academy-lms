"""Event triggers: payment recorded -> enrolment confirmed, LMS Certificate -> certificate ready.

Pure unit tests (frappe mocked). See test_wtf_whatsapp_schedules for the scheduler jobs.
"""

import unittest
from unittest.mock import MagicMock, patch

import frappe

from lms import wtf_whatsapp as wa

CONF = {"wastudio_token": "WA-SECRET-TOKEN"}


class _SiteConf(unittest.TestCase):
	def setUp(self):
		self._own_conf = not getattr(frappe.local, "conf", None)
		if self._own_conf:
			frappe.local.conf = frappe._dict()

	def tearDown(self):
		if self._own_conf:
			del frappe.local.conf


def fake_db(values):
	"""frappe.db whose get_value answers from {(doctype, name, field): value}."""
	db = MagicMock()

	def get_value(doctype, name=None, fieldname=None, *a, **kw):
		if isinstance(fieldname, (list, tuple)):
			return values.get((doctype, name, "*"))
		return values.get((doctype, name, fieldname))

	db.get_value.side_effect = get_value
	return db


def payment(**overrides):
	row = {
		"name": "PAY-0001",
		"amount": 24999,
		"amount_with_gst": 0,
		"member": "riya@example.com",
		"address": None,
		"payment_for_document_type": "LMS Course",
		"payment_for_document": "sports-nutrition",
		"payment_received": 1,
		"payment_for_certificate": 0,
	}
	row.update(overrides)
	return frappe._dict(row)


class TestPaymentCallbackHook(unittest.TestCase):
	def _run(self, already_recorded):
		from lms.lms import utils

		data = frappe._dict(payment="PAY-0001", payment_gateway="Razorpay")
		with (
			patch.object(utils, "get_payment_callback_data", return_value=data),
			patch.object(utils, "serialize_callbacks_without_the_constraint"),
			patch.object(utils, "payment_already_recorded", return_value=already_recorded),
			patch.object(utils, "update_payment_details"),
			patch.object(utils, "complete_enrollment"),
			patch("lms.wtf_meta.queue_purchase"),
			patch("lms.wtf_whatsapp.queue_enrolment_confirmed") as queue,
		):
			utils.update_payment_record("LMS Course", "sports-nutrition")
		return queue

	def test_queued_once_when_payment_is_first_recorded(self):
		self._run(already_recorded=False).assert_called_once_with("PAY-0001")

	def test_not_queued_for_a_replayed_callback(self):
		self._run(already_recorded=True).assert_not_called()


class TestEnrolmentConfirmed(_SiteConf):
	def test_queue_enqueues_after_commit(self):
		with patch.dict(frappe.conf, CONF), patch("frappe.enqueue") as enqueue:
			wa.queue_enrolment_confirmed("PAY-0001")
		args, kwargs = enqueue.call_args
		self.assertEqual(args[0], "lms.wtf_whatsapp.send_enrolment_confirmed")
		self.assertEqual(kwargs["queue"], "short")
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertEqual(kwargs["payment_name"], "PAY-0001")

	def test_queue_is_noop_without_token_and_never_raises(self):
		with patch.dict(frappe.conf, {"wastudio_token": ""}), patch("frappe.enqueue") as enqueue:
			wa.queue_enrolment_confirmed("PAY-0001")
		enqueue.assert_not_called()
		with (
			patch.dict(frappe.conf, CONF),
			patch("frappe.enqueue", side_effect=Exception("redis")),
			patch("frappe.log_error"),
		):
			wa.queue_enrolment_confirmed("PAY-0001")

	def _send(self, row, address_phone=None):
		db = fake_db(
			{
				("LMS Payment", "PAY-0001", "*"): row,
				("User", "riya@example.com", "first_name"): "Riya Sharma",
				("LMS Course", "sports-nutrition", "title"): "Sports Nutrition",
				("Address", "ADDR-1", "phone"): address_phone,
			}
		)
		with patch.object(frappe, "db", db), patch.object(wa, "send_template") as send:
			wa.send_enrolment_confirmed("PAY-0001")
		return send

	def test_sends_name_amount_and_course_with_the_billing_phone(self):
		send = self._send(payment(address="ADDR-1", amount=20000, amount_with_gst=24999), address_phone="98765 43210")
		send.assert_called_once_with(
			"riya@example.com",
			wa.ENROLMENT_CONFIRMED,
			["Riya", "₹24,999", "Sports Nutrition"],
			"sports-nutrition",
			key="PAY-0001",
			phone="98765 43210",
		)

	def test_without_billing_phone_the_users_mobile_is_used(self):
		send = self._send(payment())
		self.assertIsNone(send.call_args.kwargs["phone"])

	def test_not_sent_for_unpaid_batch_certificate_or_missing_payments(self):
		for row in (
			payment(payment_received=0),
			payment(payment_for_document_type="LMS Batch"),
			payment(payment_for_certificate=1),
			None,
		):
			self._send(row).assert_not_called()


class TestCertificateReady(_SiteConf):
	def test_after_insert_enqueues_the_send(self):
		doc = frappe._dict(name="CERT-1", member="riya@example.com")
		with patch.dict(frappe.conf, CONF), patch("frappe.enqueue") as enqueue:
			wa.on_certificate_insert(doc, "after_insert")
		args, kwargs = enqueue.call_args
		self.assertEqual(args[0], "lms.wtf_whatsapp.send_certificate_ready")
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertEqual(kwargs["certificate"], "CERT-1")

	def test_after_insert_never_breaks_certificate_issue(self):
		doc = frappe._dict(name="CERT-1", member="riya@example.com")
		with (
			patch.dict(frappe.conf, CONF),
			patch("frappe.enqueue", side_effect=Exception("redis")),
			patch("frappe.log_error"),
		):
			wa.on_certificate_insert(doc, "after_insert")

	def _send(self, cert):
		db = fake_db(
			{
				("LMS Certificate", "CERT-1", "*"): cert,
				("User", "riya@example.com", "first_name"): "Riya",
				("User", "riya@example.com", "username"): "riya",
				("LMS Course", "sports-nutrition", "title"): "Sports Nutrition",
				("LMS Batch", "B-1", "title"): "June Batch",
			}
		)
		with patch.object(frappe, "db", db), patch.object(wa, "send_template") as send:
			wa.send_certificate_ready("CERT-1")
		return send

	def test_course_certificate_links_the_course_certification_page(self):
		send = self._send(frappe._dict(name="CERT-1", member="riya@example.com", course="sports-nutrition", batch_name=None))
		send.assert_called_once_with(
			"riya@example.com",
			wa.CERTIFICATE_READY,
			["Riya", "Sports Nutrition"],
			"courses/sports-nutrition/certification",
			key="CERT-1",
		)

	def test_batch_certificate_links_the_profile_certificates(self):
		send = self._send(frappe._dict(name="CERT-1", member="riya@example.com", course=None, batch_name="B-1"))
		args = send.call_args.args
		self.assertEqual(args[2], ["Riya", "June Batch"])
		self.assertEqual(args[3], "user/riya/certificates")

	def test_missing_certificate_sends_nothing(self):
		self._send(None).assert_not_called()


class TestHooks(unittest.TestCase):
	def test_certificate_after_insert_is_wired(self):
		from lms import hooks

		events = hooks.doc_events["LMS Certificate"]["after_insert"]
		events = events if isinstance(events, list) else [events]
		self.assertIn("lms.wtf_whatsapp.on_certificate_insert", events)


if __name__ == "__main__":
	unittest.main()
