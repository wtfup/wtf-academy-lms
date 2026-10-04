"""Academy WhatsApp lifecycle (WA Studio templates) for the LMS.

Pure unit tests in the test_wtf_meta style: frappe.db, frappe.get_all, frappe.enqueue,
frappe.cache and requests are mocked, so they run without a site
(`python -m unittest lms.tests.test_wtf_whatsapp` inside the app image) as well as under
`bench run-tests`.
"""

import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import frappe

from lms import wtf_whatsapp as wa

TOKEN = "WA-SECRET-TOKEN"
CONF = {"wastudio_token": TOKEN, "wastudio_url": "https://wa.example", "wtf_whatsapp_enabled": 1}
DEFAULT = object()
FIXED_NOW = datetime(2026, 10, 10, 9, 30)
PHONE = "919876543210"
ALL_APPROVED = {name: "APPROVED" for name in wa.TEMPLATES}


def _live_row(name, category):
	return {
		"name": name,
		"status": "APPROVED",
		"category": category,
		"waba_id": "1613713363552381",
		"phone_number_id": "100000000000001",
		"language": "en",
		"buttons": [
			{"type": "URL", "text": "Open", "url": "https://online.wtfgymsacademy.com/lms/courses/{{1}}"}
		],
	}


# Shape verified live 2026-10-04 (GET /api/v1/getMessageTemplates?pageSize=300, project token):
# {"ok": true, "result": [...10 rows...]}, all 8 academy_* + 2 older templates APPROVED.
LIVE_TEMPLATE_ROWS = [
	_live_row(name, "MARKETING" if name in wa.MARKETING else "UTILITY") for name in sorted(wa.TEMPLATES)
] + [_live_row("academy_welcome_legacy", "UTILITY"), _live_row("academy_course_info", "MARKETING")]


class _SiteConf(unittest.TestCase):
	"""Gives frappe.conf a backing dict when no site is initialised (standalone runs)."""

	def setUp(self):
		self._own_conf = not getattr(frappe.local, "conf", None)
		if self._own_conf:
			frappe.local.conf = frappe._dict()

	def tearDown(self):
		if self._own_conf:
			del frappe.local.conf


def ok_response(message_id="wamid.ABC"):
	response = MagicMock(status_code=200)
	response.json.return_value = {"result": "success", "messageId": message_id}
	return response


class TestPhone(unittest.TestCase):
	def test_ten_digit_indian_mobile_gets_91(self):
		self.assertEqual(wa.normalize_phone("98765 43210"), PHONE)
		self.assertEqual(wa.normalize_phone("9876543210"), PHONE)

	def test_already_prefixed_numbers_are_kept(self):
		self.assertEqual(wa.normalize_phone("+91 98765-43210"), PHONE)
		self.assertEqual(wa.normalize_phone("919876543210"), PHONE)

	def test_anything_else_is_rejected(self):
		for value in (
			"",
			None,
			"12345",
			"098765432100",
			"5876543210",  # Indian mobiles start 6-9
			"447946095812",
			"9198765432101",
			"abc",
		):
			self.assertIsNone(wa.normalize_phone(value), value)

	def test_mask_keeps_country_code_and_last_four(self):
		self.assertEqual(wa.mask_phone(PHONE), "91******3210")
		self.assertEqual(wa.mask_phone(None), "")


class TestMoney(unittest.TestCase):
	def test_rupees_with_indian_grouping(self):
		self.assertEqual(wa.format_inr(24999), "₹24,999")
		self.assertEqual(wa.format_inr(9999.0), "₹9,999")
		self.assertEqual(wa.format_inr(124999), "₹1,24,999")
		self.assertEqual(wa.format_inr(12345678), "₹1,23,45,678")
		self.assertEqual(wa.format_inr(999), "₹999")
		self.assertEqual(wa.format_inr(1180.5), "₹1,180.50")


class TestTemplateCatalog(unittest.TestCase):
	def test_categories_match_the_submitted_templates(self):
		self.assertEqual(
			wa.MARKETING,
			{
				"academy_trial_day1_v1",
				"academy_trial_day3_v1",
				"academy_trial_day7_v1",
				"academy_learning_nudge_v1",
			},
		)
		self.assertEqual(
			wa.UTILITY,
			{
				"academy_account_ready_v1",
				"academy_payment_pending_v1",
				"academy_enrolment_confirmed_v1",
				"academy_certificate_ready_v1",
			},
		)


class TestApprovalCheck(_SiteConf):
	def _statuses(self, rows, cached=None, status_code=200):
		cache = MagicMock()
		cache.get_value.return_value = cached
		response = MagicMock(status_code=status_code)
		response.json.return_value = {"ok": True, "result": rows}
		get = MagicMock(return_value=response)
		with (
			patch.dict(frappe.conf, CONF),
			patch.object(frappe, "cache", cache),
			patch.object(wa.requests, "get", get),
		):
			result = wa.template_statuses()
		return result, cache, get

	def test_fetches_the_template_list_and_caches_it_for_ten_minutes(self):
		rows = [
			{"name": "academy_account_ready_v1", "status": "APPROVED", "language": "en"},
			{"name": "academy_trial_day1_v1", "status": "PENDING", "language": "en"},
		]
		result, cache, get = self._statuses(rows)
		self.assertEqual(result["academy_account_ready_v1"], "APPROVED")
		self.assertEqual(result["academy_trial_day1_v1"], "PENDING")
		url = get.call_args.args[0]
		self.assertEqual(url, "https://wa.example/api/v1/getMessageTemplates")
		self.assertEqual(get.call_args.kwargs["params"], {"pageSize": 300})
		self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], f"Bearer {TOKEN}")
		self.assertTrue(get.call_args.kwargs["timeout"])
		self.assertEqual(cache.set_value.call_args.kwargs["expires_in_sec"], 600)

	def test_cached_list_is_not_refetched(self):
		result, _, get = self._statuses([], cached={"academy_account_ready_v1": "APPROVED"})
		get.assert_not_called()
		self.assertEqual(result, {"academy_account_ready_v1": "APPROVED"})

	def test_approved_variant_wins_over_another_language(self):
		rows = [
			{"name": "academy_account_ready_v1", "status": "REJECTED", "language": "hi"},
			{"name": "academy_account_ready_v1", "status": "APPROVED", "language": "en"},
		]
		result, _, _ = self._statuses(rows)
		self.assertEqual(result["academy_account_ready_v1"], "APPROVED")

	def test_list_failure_is_unknown_and_not_cached(self):
		with patch("frappe.log_error"):
			result, cache, _ = self._statuses([], status_code=500)
		self.assertIsNone(result)
		cached = [c for c in cache.set_value.call_args_list if c.args[0] == wa.APPROVAL_CACHE_KEY]
		self.assertEqual(cached, [])

	def _failing_fetch(self, get, markers=()):
		cache = MagicMock()
		cache.get_value.side_effect = lambda key: 1 if key in markers else None
		with (
			patch.dict(frappe.conf, CONF),
			patch.object(frappe, "cache", cache),
			patch.object(wa.requests, "get", get),
			patch("frappe.log_error") as log_error,
		):
			result = wa.template_statuses()
		return result, cache, log_error

	def test_fetch_failure_is_negative_cached_for_a_minute_and_logged_masked(self):
		for get in (
			MagicMock(return_value=MagicMock(status_code=503)),
			MagicMock(side_effect=wa.requests.exceptions.Timeout(f"timeout {TOKEN}")),
		):
			result, cache, log_error = self._failing_fetch(get)
			self.assertIsNone(result)
			get.assert_called_once()
			sets = {c.args[0]: c.kwargs.get("expires_in_sec") for c in cache.set_value.call_args_list}
			self.assertEqual(sets.get(wa.FETCH_FAILED_KEY), 60)
			self.assertEqual(sets.get(wa.FETCH_FAILED_LOGGED_KEY), 600)
			self.assertNotIn(wa.APPROVAL_CACHE_KEY, sets)
			log_error.assert_called_once()
			self.assertNotIn(TOKEN, repr(log_error.call_args))

	def test_during_the_negative_cache_there_is_no_refetch(self):
		get = MagicMock()
		result, _, log_error = self._failing_fetch(get, markers=(wa.FETCH_FAILED_KEY,))
		self.assertIsNone(result)
		get.assert_not_called()
		log_error.assert_not_called()

	def test_failure_log_is_once_per_ten_minutes(self):
		get = MagicMock(return_value=MagicMock(status_code=500))
		_, _, log_error = self._failing_fetch(get, markers=(wa.FETCH_FAILED_LOGGED_KEY,))
		get.assert_called_once()
		log_error.assert_not_called()

	def test_a_failed_list_still_attempts_the_send(self):
		self.assertFalse(wa.template_blocked("academy_account_ready_v1", None))

	def test_parses_the_live_project_token_response(self):
		result, cache, _ = self._statuses(LIVE_TEMPLATE_ROWS)
		self.assertEqual(len(result), 10)
		for name in wa.TEMPLATES:
			self.assertEqual(result[name], "APPROVED", name)
		self.assertEqual(cache.set_value.call_args.args[1], result)

	def test_empty_list_is_unknown_not_cached_and_logged_once_masked(self):
		cache = MagicMock()
		cache.get_value.return_value = None
		response = MagicMock(status_code=200)
		response.json.return_value = {"ok": True, "result": []}
		with (
			patch.dict(frappe.conf, CONF),
			patch.object(frappe, "cache", cache),
			patch.object(wa.requests, "get", MagicMock(return_value=response)),
			patch("frappe.log_error") as log_error,
		):
			self.assertIsNone(wa.template_statuses())
			# the "already logged" marker is now set: a second empty fetch does not log again
			cache.get_value.side_effect = lambda key: 1 if key == wa.EMPTY_LIST_LOGGED_KEY else None
			self.assertIsNone(wa.template_statuses())
		self.assertEqual(log_error.call_count, 1)
		self.assertNotIn(TOKEN, repr(log_error.call_args))
		cached = [c for c in cache.set_value.call_args_list if c.args[0] == wa.APPROVAL_CACHE_KEY]
		self.assertEqual(cached, [])


class SendHarness(_SiteConf):
	"""Runs send_template with a mocked frappe.db / requests and records what happened."""

	user_row = None
	existing = None
	statuses = ALL_APPROVED

	def setUp(self):
		super().setUp()
		self.user_row = frappe._dict(
			first_name="Riya Sharma", mobile_no=PHONE, wtf_whatsapp_opt_in=1, enabled=1
		)

	def send(
		self,
		*args,
		post=None,
		existing=None,
		statuses=DEFAULT,
		insert_error=None,
		conf=None,
		history=(),
		recent=None,
		enrolled=None,
		paid_since=None,
		payment_row=None,
		**kwargs,
	):
		db = MagicMock()
		answers = {wa.DOCTYPE: recent, "LMS Enrollment": enrolled, "LMS Payment": paid_since}
		db.exists.side_effect = lambda doctype, filters=None, *a, **kw: answers.get(doctype)
		get_all = MagicMock(return_value=[frappe._dict(status=s) for s in history])

		def get_value(doctype, filters=None, fieldname=None, *a, **kw):
			if doctype == "User":
				return self.user_row
			if doctype == wa.DOCTYPE:
				return existing
			if doctype == "LMS Payment":
				return payment_row
			return None

		db.get_value.side_effect = get_value
		doc = MagicMock()
		doc.name = "WAMSG-1"
		if insert_error:
			doc.insert.side_effect = insert_error
		get_doc = MagicMock(return_value=doc)
		post = post or MagicMock(return_value=ok_response())
		with (
			patch.dict(frappe.conf, conf if conf is not None else CONF),
			patch.object(frappe, "db", db),
			patch.object(frappe, "get_doc", get_doc),
			patch.object(frappe, "get_all", get_all),
			patch.object(
				wa, "template_statuses", return_value=self.statuses if statuses is DEFAULT else statuses
			),
			patch.object(wa.requests, "post", post),
			patch("frappe.log_error") as log_error,
		):
			result = wa.send_template(*args, **kwargs)
		return frappe._dict(
			result=result, db=db, get_doc=get_doc, doc=doc, post=post, log_error=log_error, get_all=get_all
		)


class TestSendTemplate(SendHarness):
	def test_sends_the_wa_studio_payload_once_and_records_it(self):
		r = self.send(
			"riya@example.com",
			"academy_account_ready_v1",
			["Riya", "Sports Nutrition"],
			button_param="sports-nutrition",
			key="signup",
		)
		self.assertEqual(r.result, "sent")
		r.post.assert_called_once()
		self.assertEqual(r.post.call_args.args[0], "https://wa.example/api/v1/sendTemplateMessage")
		self.assertEqual(r.post.call_args.kwargs["params"], {"whatsappNumber": PHONE})
		self.assertEqual(r.post.call_args.kwargs["headers"]["Authorization"], f"Bearer {TOKEN}")
		self.assertTrue(r.post.call_args.kwargs["timeout"] <= 10)
		body = r.post.call_args.kwargs["json"]
		self.assertEqual(
			body,
			{
				"template_name": "academy_account_ready_v1",
				"language": "en",
				"parameters": [{"name": "1", "value": "Riya"}, {"name": "2", "value": "Sports Nutrition"}],
				"button_params": {"0": "sports-nutrition"},
			},
		)
		self.assertNotIn("channel_number", body)
		# the claim row is inserted (status sending) and committed BEFORE the HTTP call
		row = r.get_doc.call_args.args[0]
		self.assertEqual(row["doctype"], wa.DOCTYPE)
		self.assertEqual(row["status"], "sending")
		self.assertEqual(row["dedupe_key"], "academy_account_ready_v1:riya@example.com:signup")
		self.assertEqual(row["user"], "riya@example.com")
		self.assertEqual(row["phone_masked"], "91******3210")
		self.assertNotIn(PHONE, json.dumps(row))
		r.doc.insert.assert_called_once()
		r.db.commit.assert_called()
		r.db.set_value.assert_called_with(
			wa.DOCTYPE, "WAMSG-1", {"status": "sent", "message_id": "wamid.ABC", "error": None}
		)

	def test_no_button_param_means_no_button_field(self):
		r = self.send("riya@example.com", "academy_account_ready_v1", ["Riya", "X"], key="k")
		self.assertNotIn("button_params", r.post.call_args.kwargs["json"])

	def test_empty_token_is_a_noop(self):
		r = self.send(
			"riya@example.com",
			"academy_account_ready_v1",
			["Riya", "X"],
			key="k",
			conf={"wastudio_token": ""},
		)
		self.assertEqual(r.result, "disabled")
		r.post.assert_not_called()
		r.get_doc.assert_not_called()

	def test_default_base_url(self):
		r = self.send(
			"riya@example.com",
			"academy_account_ready_v1",
			["Riya", "X"],
			key="k",
			conf={"wastudio_token": TOKEN, "wtf_whatsapp_enabled": "1"},
		)
		self.assertEqual(r.post.call_args.args[0], "https://wastudio.wtflabs.ai/api/v1/sendTemplateMessage")

	def test_unknown_template_is_refused(self):
		r = self.send("riya@example.com", "something_else", ["Riya"], key="k")
		self.assertEqual(r.result, "unknown_template")
		r.post.assert_not_called()

	def test_explicit_phone_wins_over_the_users_mobile(self):
		r = self.send(
			"riya@example.com",
			"academy_enrolment_confirmed_v1",
			["Riya", "₹1", "X"],
			key="p",
			phone="9123456789",
		)
		self.assertEqual(r.post.call_args.kwargs["params"], {"whatsappNumber": "919123456789"})

	def test_a_bare_phone_can_be_the_recipient_for_utility(self):
		r = self.send("9876543210", "academy_payment_pending_v1", ["Riya", "X", "₹1"], key="PAY-1")
		self.assertEqual(r.result, "sent")
		row = r.get_doc.call_args.args[0]
		self.assertIsNone(row["user"])
		self.assertNotIn(PHONE, row["dedupe_key"])

	def test_no_valid_phone_is_skipped_without_a_row(self):
		self.user_row.mobile_no = "12345"
		r = self.send("riya@example.com", "academy_account_ready_v1", ["Riya", "X"], key="k")
		self.assertEqual(r.result, "skipped_no_phone")
		r.post.assert_not_called()
		r.get_doc.assert_not_called()


class TestIdempotency(SendHarness):
	def test_an_existing_sent_row_is_never_sent_again(self):
		for status in ("sent", "sending", "failed"):
			r = self.send(
				"riya@example.com",
				"academy_account_ready_v1",
				["Riya", "X"],
				key="signup",
				existing=frappe._dict(name="WAMSG-0", status=status),
			)
			self.assertEqual(r.result, "duplicate", status)
			r.post.assert_not_called()
			r.get_doc.assert_not_called()

	def test_the_existing_row_is_read_under_a_lock(self):
		r = self.send("riya@example.com", "academy_account_ready_v1", ["Riya", "X"], key="signup")
		calls = [c for c in r.db.get_value.call_args_list if c.args[0] == wa.DOCTYPE]
		self.assertTrue(calls)
		self.assertEqual(calls[0].args[1], {"dedupe_key": "academy_account_ready_v1:riya@example.com:signup"})
		self.assertTrue(calls[0].kwargs.get("for_update"))

	def test_losing_the_insert_race_does_not_send(self):
		r = self.send(
			"riya@example.com",
			"academy_account_ready_v1",
			["Riya", "X"],
			key="signup",
			insert_error=frappe.DuplicateEntryError("dup"),
		)
		self.assertEqual(r.result, "duplicate")
		r.post.assert_not_called()
		r.db.rollback.assert_called()

	def test_a_skipped_not_approved_row_is_retried_once_approved(self):
		r = self.send(
			"riya@example.com",
			"academy_payment_pending_v1",
			["Riya", "X", "₹1"],
			key="PAY-1",
			existing=frappe._dict(name="WAMSG-0", status="skipped_not_approved"),
		)
		self.assertEqual(r.result, "sent")
		r.post.assert_called_once()
		r.get_doc.assert_not_called()
		first = r.db.set_value.call_args_list[0]
		self.assertEqual(first.args[:2], (wa.DOCTYPE, "WAMSG-0"))
		self.assertEqual(first.args[2]["status"], "sending")


class TestOptInGating(SendHarness):
	def test_marketing_needs_the_opt_in(self):
		self.user_row.wtf_whatsapp_opt_in = 0
		r = self.send(
			"riya@example.com", "academy_trial_day1_v1", ["Riya", "X"], button_param="x", key="trial"
		)
		self.assertEqual(r.result, "skipped_no_opt_in")
		r.post.assert_not_called()
		r.get_doc.assert_not_called()

	def test_marketing_to_an_opted_in_user_is_sent(self):
		r = self.send(
			"riya@example.com", "academy_trial_day1_v1", ["Riya", "X"], button_param="x", key="trial"
		)
		self.assertEqual(r.result, "sent")

	def test_marketing_to_a_bare_phone_is_never_sent(self):
		r = self.send(PHONE, "academy_learning_nudge_v1", ["Riya", "X", "10%"], key="n")
		self.assertEqual(r.result, "skipped_no_opt_in")
		r.post.assert_not_called()

	def test_utility_does_not_need_the_opt_in(self):
		self.user_row.wtf_whatsapp_opt_in = 0
		for template in wa.UTILITY:
			r = self.send("riya@example.com", template, ["Riya", "X", "Y"], key="k")
			self.assertEqual(r.result, "sent", template)


class TestNotApproved(SendHarness):
	def test_pending_template_is_skipped_and_logged_as_retryable(self):
		r = self.send(
			"riya@example.com",
			"academy_account_ready_v1",
			["Riya", "X"],
			key="signup",
			statuses={"academy_account_ready_v1": "PENDING"},
		)
		self.assertEqual(r.result, "skipped_not_approved")
		r.post.assert_not_called()
		row = r.get_doc.call_args.args[0]
		self.assertEqual(row["status"], "skipped_not_approved")

	def test_unknown_or_empty_list_never_drops_the_send(self):
		# WA Studio rejects an unapproved template itself; that failure is recorded.
		for statuses in (None, {}, {"some_other_template": "APPROVED"}):
			r = self.send(
				"riya@example.com", "academy_account_ready_v1", ["Riya", "X"], key="s", statuses=statuses
			)
			self.assertEqual(r.result, "sent", statuses)
			r.post.assert_called_once()

	def test_only_an_explicit_non_approved_status_blocks(self):
		self.assertTrue(wa.template_blocked("t", {"t": "PENDING"}))
		self.assertTrue(wa.template_blocked("t", {"t": "REJECTED"}))
		self.assertFalse(wa.template_blocked("t", {"t": "APPROVED"}))
		self.assertFalse(wa.template_blocked("t", {}))
		self.assertFalse(wa.template_blocked("t", None))
		self.assertFalse(wa.template_blocked("t", {"other": "PENDING"}))


class TestFailOpen(SendHarness):
	def test_http_error_is_recorded_failed_and_logged_masked(self):
		response = MagicMock(status_code=400)
		response.json.return_value = {"error": f"bad number {PHONE} token {TOKEN}"}
		r = self.send(
			"riya@example.com",
			"academy_account_ready_v1",
			["Riya", "X"],
			key="s",
			post=MagicMock(return_value=response),
		)
		self.assertEqual(r.result, "failed")
		final = r.db.set_value.call_args.args[2]
		self.assertEqual(final["status"], "failed")
		self.assertIn("HTTP 400", final["error"])
		self.assertNotIn(PHONE, final["error"])
		self.assertNotIn(TOKEN, final["error"])
		r.log_error.assert_called_once()
		logged = repr(r.log_error.call_args)
		self.assertNotIn(PHONE, logged)
		self.assertNotIn(TOKEN, logged)
		self.assertIn("91******3210", logged)

	def test_unsuccessful_result_body_is_a_failure(self):
		response = MagicMock(status_code=200)
		response.json.return_value = {"result": "error", "info": "template paused"}
		r = self.send(
			"riya@example.com",
			"academy_account_ready_v1",
			["Riya", "X"],
			key="s",
			post=MagicMock(return_value=response),
		)
		self.assertEqual(r.result, "failed")

	def test_timeout_never_raises_and_never_logs_secrets(self):
		post = MagicMock(side_effect=wa.requests.exceptions.Timeout(f"timeout {TOKEN} {PHONE}"))
		r = self.send("riya@example.com", "academy_account_ready_v1", ["Riya", "X"], key="s", post=post)
		self.assertEqual(r.result, "failed")
		self.assertEqual(r.db.set_value.call_args.args[2]["error"], "Timeout")
		logged = repr(r.log_error.call_args)
		self.assertNotIn(TOKEN, logged)
		self.assertNotIn(PHONE, logged)

	def test_unexpected_error_is_swallowed(self):
		db = MagicMock()
		db.get_value.side_effect = Exception(f"db down {PHONE}")
		with (
			patch.dict(frappe.conf, CONF),
			patch.object(frappe, "db", db),
			patch("frappe.log_error") as log_error,
		):
			result = wa.send_template("riya@example.com", "academy_account_ready_v1", ["Riya", "X"], key="s")
		self.assertEqual(result, "error")
		self.assertNotIn(PHONE, repr(log_error.call_args))


class TestKillSwitch(SendHarness):
	def test_off_by_default_does_nothing_and_writes_no_rows(self):
		for conf in (
			{"wastudio_token": TOKEN},
			{"wastudio_token": TOKEN, "wtf_whatsapp_enabled": 0},
			{"wastudio_token": TOKEN, "wtf_whatsapp_enabled": "0"},
			{"wastudio_token": TOKEN, "wtf_whatsapp_enabled": "false"},
			{"wastudio_token": TOKEN, "wtf_whatsapp_enabled": ""},
		):
			r = self.send("riya@example.com", "academy_account_ready_v1", ["Riya", "X"], key="s", conf=conf)
			self.assertEqual(r.result, "disabled", conf)
			r.post.assert_not_called()
			r.get_doc.assert_not_called()
			r.db.set_value.assert_not_called()
			r.db.get_value.assert_not_called()

	def test_truthy_values_turn_it_on(self):
		for value in (1, "1", "true", "True", True):
			with patch.dict(frappe.conf, {"wtf_whatsapp_enabled": value}):
				self.assertTrue(wa.whatsapp_enabled(), value)

	def test_queue_is_a_noop_when_off(self):
		with patch.dict(frappe.conf, {"wastudio_token": TOKEN}), patch("frappe.enqueue") as enqueue:
			wa.queue_template("riya@example.com", "academy_account_ready_v1", ["Riya", "X"], "x", "signup")
			wa.enqueue_send("riya@example.com", "academy_trial_day1_v1", ["Riya", "X"], "x", "trial")
		enqueue.assert_not_called()


class TestRecipientRules(SendHarness):
	def test_disabled_users_get_nothing(self):
		self.user_row.enabled = 0
		for template in sorted(wa.TEMPLATES):
			r = self.send("riya@example.com", template, ["Riya", "X", "Y"], key="k")
			self.assertEqual(r.result, "skipped_disabled", template)
			r.post.assert_not_called()
			r.get_doc.assert_not_called()

	def test_marketing_stops_after_three_consecutive_failures(self):
		r = self.send(
			"riya@example.com", "academy_trial_day1_v1", ["Riya", "X"], key="trial", history=("failed",) * 3
		)
		self.assertEqual(r.result, "skipped_failing")
		r.post.assert_not_called()
		filters = r.get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["user"], "riya@example.com")
		self.assertEqual(filters["status"], ["in", ["sent", "failed"]])
		self.assertEqual(r.get_all.call_args.kwargs["order_by"], "creation desc")
		self.assertEqual(r.get_all.call_args.kwargs["limit"], 3)

	def test_a_success_in_between_resets_the_failure_count(self):
		r = self.send(
			"riya@example.com",
			"academy_trial_day1_v1",
			["Riya", "X"],
			key="trial",
			history=("failed", "sent", "failed"),
		)
		self.assertEqual(r.result, "sent")
		r = self.send(
			"riya@example.com", "academy_trial_day1_v1", ["Riya", "X"], key="trial", history=("failed",) * 2
		)
		self.assertEqual(r.result, "sent")

	def test_utility_is_not_stopped_by_failures(self):
		r = self.send(
			"riya@example.com",
			"academy_enrolment_confirmed_v1",
			["Riya", "₹1", "X"],
			key="p",
			history=("failed",) * 3,
		)
		self.assertEqual(r.result, "sent")

	def test_course_is_recorded_on_the_row(self):
		r = self.send("riya@example.com", "academy_account_ready_v1", ["Riya", "X"], key="s", course="cpt")
		self.assertEqual(r.get_doc.call_args.args[0]["course"], "cpt")

	def test_window_skips_a_recent_send_for_the_same_course(self):
		with patch.object(wa, "now_datetime", return_value=FIXED_NOW):
			r = self.send(
				"riya@example.com",
				"academy_payment_pending_v1",
				["Riya", "X", "₹1"],
				key="cpt:PAY-2",
				course="cpt",
				window_hours=24,
				recent="WAMSG-0",
			)
		self.assertEqual(r.result, "skipped_recent")
		r.post.assert_not_called()
		r.get_doc.assert_not_called()
		filters = r.db.exists.call_args.args[1]
		self.assertEqual(filters["template"], "academy_payment_pending_v1")
		self.assertEqual(filters["user"], "riya@example.com")
		self.assertEqual(filters["course"], "cpt")
		self.assertEqual(filters["status"], ["in", ["sending", "sent", "failed"]])
		self.assertEqual(filters["creation"], [">", FIXED_NOW - timedelta(hours=24)])

	def test_window_with_nothing_recent_sends(self):
		with patch.object(wa, "now_datetime", return_value=FIXED_NOW):
			r = self.send(
				"riya@example.com",
				"academy_payment_pending_v1",
				["Riya", "X", "₹1"],
				key="cpt:PAY-2",
				course="cpt",
				window_hours=24,
			)
		self.assertEqual(r.result, "sent")


class TestPendingPaymentRecheck(SendHarness):
	"""The long-queue worker re-checks a payment-pending send: the learner may pay meanwhile."""

	def pending(self, **kwargs):
		return self.send(
			"riya@example.com",
			"academy_payment_pending_v1",
			["Riya", "CPT", "₹1"],
			"cpt",
			key="cpt:PAY-2",
			course="cpt",
			window_hours=24,
			pending_payment="PAY-2",
			**kwargs,
		)

	def unpaid(self, **overrides):
		row = {"payment_received": 0, "member": "riya@example.com", "payment_for_document": "cpt"}
		row.update(overrides)
		return frappe._dict(row)

	def test_still_unpaid_and_not_enrolled_is_sent(self):
		with patch.object(wa, "now_datetime", return_value=FIXED_NOW):
			r = self.pending(payment_row=self.unpaid())
		self.assertEqual(r.result, "sent")

	def test_paid_while_queued_is_not_sent(self):
		with patch.object(wa, "now_datetime", return_value=FIXED_NOW):
			for kwargs in (
				{"payment_row": self.unpaid(payment_received=1)},
				{"payment_row": self.unpaid(), "paid_since": "PAY-3"},
				{"payment_row": self.unpaid(), "enrolled": "ENR-1"},
				{"payment_row": None},
			):
				r = self.pending(**kwargs)
				self.assertEqual(r.result, "skipped_resolved", kwargs)
				r.post.assert_not_called()
				r.get_doc.assert_not_called()


class TestDedupeKey(unittest.TestCase):
	def test_short_keys_are_kept_readable(self):
		self.assertEqual(wa.dedupe_key("t", "a@b.co", None, "k"), "t:a@b.co:k")

	def test_long_keys_are_hashed_below_the_column_limit(self):
		user = "x" * 200 + "@example.com"
		key = wa.dedupe_key("academy_learning_nudge_v1", user, None, "c" * 140 + ":2026-10-10")
		self.assertLessEqual(len(key), 140)
		self.assertTrue(key.startswith("academy_learning_nudge_v1:"))
		self.assertEqual(
			key, wa.dedupe_key("academy_learning_nudge_v1", user, None, "c" * 140 + ":2026-10-10")
		)
		self.assertNotEqual(key, wa.dedupe_key("academy_learning_nudge_v1", user, None, "d" * 140))


class TestQueueTemplate(_SiteConf):
	def test_enqueues_on_short_after_commit(self):
		with patch.dict(frappe.conf, CONF), patch("frappe.enqueue") as enqueue:
			wa.queue_template("riya@example.com", "academy_account_ready_v1", ["Riya", "X"], "x", "signup")
		args, kwargs = enqueue.call_args
		self.assertEqual(args[0], "lms.wtf_whatsapp.send_template")
		self.assertEqual(kwargs["queue"], "short")
		self.assertTrue(kwargs["enqueue_after_commit"])
		self.assertEqual(kwargs["user_or_phone"], "riya@example.com")
		self.assertEqual(kwargs["template"], "academy_account_ready_v1")
		self.assertEqual(kwargs["params"], ["Riya", "X"])
		self.assertEqual(kwargs["button_param"], "x")
		self.assertEqual(kwargs["key"], "signup")

	def test_no_token_no_job(self):
		with patch.dict(frappe.conf, {"wastudio_token": None}), patch("frappe.enqueue") as enqueue:
			wa.queue_template("riya@example.com", "academy_account_ready_v1", ["Riya", "X"], "x", "signup")
		enqueue.assert_not_called()

	def test_enqueue_failure_is_swallowed(self):
		with (
			patch.dict(frappe.conf, CONF),
			patch("frappe.enqueue", side_effect=Exception("redis down")),
			patch("frappe.log_error") as log_error,
		):
			wa.queue_template("riya@example.com", "academy_account_ready_v1", ["Riya", "X"], "x", "signup")
		log_error.assert_called_once()


class TestEnqueueSend(_SiteConf):
	def test_scheduled_sends_go_one_job_per_recipient_on_the_long_queue(self):
		with patch.dict(frappe.conf, CONF), patch("frappe.enqueue") as enqueue:
			wa.enqueue_send(
				"riya@example.com",
				"academy_trial_day1_v1",
				["Riya", "X"],
				"x",
				"trial",
				course="x",
				window_hours=None,
			)
		args, kwargs = enqueue.call_args
		self.assertEqual(args[0], "lms.wtf_whatsapp.send_template")
		self.assertEqual(kwargs["queue"], "long")
		self.assertEqual(kwargs["template"], "academy_trial_day1_v1")
		self.assertEqual(kwargs["key"], "trial")
		self.assertEqual(kwargs["course"], "x")

	def test_enqueue_failure_is_swallowed(self):
		with (
			patch.dict(frappe.conf, CONF),
			patch("frappe.enqueue", side_effect=Exception("redis down")),
			patch("frappe.log_error"),
		):
			wa.enqueue_send("riya@example.com", "academy_trial_day1_v1", ["Riya", "X"], "x", "trial")


class TestShippedSchema(unittest.TestCase):
	def _app(self):
		import lms

		return os.path.dirname(lms.__file__)

	def test_dedupe_log_doctype(self):
		path = os.path.join(
			self._app(), "lms", "doctype", "wtf_whatsapp_message", "wtf_whatsapp_message.json"
		)
		with open(path) as f:
			meta = json.load(f)
		self.assertEqual(meta["name"], wa.DOCTYPE)
		fields = {f["fieldname"]: f for f in meta["fields"]}
		for name in (
			"user",
			"phone_masked",
			"template",
			"dedupe_key",
			"status",
			"message_id",
			"error",
			"course",
		):
			self.assertIn(name, fields)
		self.assertEqual(fields["dedupe_key"].get("unique"), 1)
		self.assertEqual(
			set(fields["status"]["options"].split("\n")),
			{"sending", "sent", "failed", "skipped_not_approved"},
		)
		roles = {p["role"] for p in meta["permissions"]}
		self.assertEqual(roles, {"System Manager"})

	def test_user_custom_fields(self):
		with open(os.path.join(self._app(), "fixtures", "custom_field.json")) as f:
			fields = {row["name"]: row for row in json.load(f)}
		opt_in = fields.get("User-wtf_whatsapp_opt_in")
		self.assertIsNotNone(opt_in)
		self.assertEqual((opt_in["dt"], opt_in["fieldtype"], opt_in["default"]), ("User", "Check", "0"))
		course = fields.get("User-wtf_signup_course")
		self.assertIsNotNone(course)
		self.assertEqual((course["dt"], course["fieldtype"]), ("User", "Data"))


if __name__ == "__main__":
	unittest.main()
