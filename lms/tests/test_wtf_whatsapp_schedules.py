"""Scheduler jobs: hourly payment pending, daily trial day 1/3/7, daily learning nudge.

Pure unit tests: frappe.get_all / frappe.db are fakes that record the filters each job asks
for, and send_template is mocked (its own gating/dedupe is covered in test_wtf_whatsapp).
"""

import unittest
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import frappe

from lms import wtf_whatsapp as wa

NOW = datetime(2026, 10, 10, 9, 30)


class FakeSite:
	"""Minimal frappe.get_all / frappe.db answering from in-memory tables."""

	def __init__(self, tables=None, exists=None, values=None):
		self.tables = tables or {}
		self.exists_answers = exists or (lambda doctype, filters: None)
		self.values = values or {}
		self.get_all_calls = []
		self.db = MagicMock()
		self.db.exists.side_effect = lambda doctype, filters=None, *a, **kw: self.exists_answers(
			doctype, filters
		)
		self.db.get_value.side_effect = self._get_value

	def _get_value(self, doctype, name=None, fieldname=None, *a, **kw):
		key = (
			doctype,
			name if not isinstance(name, dict) else tuple(sorted(name.items())),
			fieldname if not isinstance(fieldname, list) else "*",
		)
		value = self.values.get(key)
		return value(name) if callable(value) else value

	def get_all(self, doctype, filters=None, fields=None, *a, **kw):
		self.get_all_calls.append(frappe._dict(doctype=doctype, filters=filters, fields=fields, kwargs=kw))
		rows = self.tables.get(doctype, [])
		return rows(filters) if callable(rows) else list(rows)

	def calls(self, doctype):
		return [c for c in self.get_all_calls if c.doctype == doctype]

	def patch(self):
		return _Patches(self)


class _Patches:
	def __init__(self, site):
		self.site = site
		self.stack = []

	def __enter__(self):
		self.send = MagicMock(return_value="sent")
		for p in (
			patch.object(frappe, "db", self.site.db),
			patch.object(frappe, "get_all", self.site.get_all),
			patch.object(wa, "send_template", self.send),
			patch("frappe.log_error"),
		):
			p.start()
			self.stack.append(p)
		return self.send

	def __exit__(self, *exc):
		for p in reversed(self.stack):
			p.stop()


def filter_value(filters, field, op):
	for f in filters:
		if f[0] == field and f[1] == op:
			return f[2]
	raise AssertionError(f"no {field} {op} filter in {filters}")


TITLES = {
	("LMS Course", "sports-nutrition", "title"): "Sports Nutrition",
	("LMS Course", "cpt", "title"): "CPT",
}


class TestPaymentPending(unittest.TestCase):
	def payments(self, *rows):
		base = {
			"amount": 9999,
			"amount_with_gst": 0,
			"address": None,
			"payment_for_document_type": "LMS Course",
			"payment_received": 0,
			"payment_for_certificate": 0,
		}
		return [frappe._dict({**base, **row}) for row in rows]

	def run_job(self, rows, paid=()):
		def exists(doctype, filters):
			if doctype == "LMS Payment" and filters.get("payment_received") == 1:
				return (filters["member"], filters["payment_for_document"]) in paid
			return None

		values = dict(TITLES)
		values[("User", "riya@example.com", "first_name")] = "Riya S"
		values[("User", "aman@example.com", "first_name")] = "Aman"
		site = FakeSite({"LMS Payment": rows}, exists=exists, values=values)
		with site.patch() as send:
			wa.send_payment_pending(now=NOW)
		return site, send

	def test_window_is_unpaid_course_payments_created_one_to_twentyfour_hours_ago(self):
		site, _ = self.run_job([])
		filters = site.calls("LMS Payment")[0].filters
		self.assertEqual(filter_value(filters, "payment_received", "="), 0)
		self.assertEqual(filter_value(filters, "payment_for_document_type", "="), "LMS Course")
		self.assertEqual(filter_value(filters, "payment_for_certificate", "="), 0)
		self.assertEqual(filter_value(filters, "creation", ">="), NOW - timedelta(hours=24))
		self.assertEqual(filter_value(filters, "creation", "<="), NOW - timedelta(hours=1))
		self.assertEqual(site.calls("LMS Payment")[0].kwargs.get("order_by"), "creation desc")

	def test_sends_once_per_payment_with_amount_and_course(self):
		rows = self.payments(
			{
				"name": "PAY-2",
				"member": "riya@example.com",
				"payment_for_document": "sports-nutrition",
				"amount": 9999,
			}
		)
		_, send = self.run_job(rows)
		send.assert_called_once_with(
			"riya@example.com",
			wa.PAYMENT_PENDING,
			["Riya", "Sports Nutrition", "₹9,999"],
			"sports-nutrition",
			key="PAY-2",
			phone=None,
		)

	def test_only_the_latest_open_checkout_per_learner_and_course(self):
		rows = self.payments(
			{"name": "PAY-3", "member": "riya@example.com", "payment_for_document": "sports-nutrition"},
			{"name": "PAY-2", "member": "riya@example.com", "payment_for_document": "sports-nutrition"},
			{"name": "PAY-1", "member": "riya@example.com", "payment_for_document": "cpt"},
		)
		_, send = self.run_job(rows)
		self.assertEqual([c.kwargs["key"] for c in send.call_args_list], ["PAY-3", "PAY-1"])

	def test_skips_learners_who_paid_for_the_course_since(self):
		rows = self.payments(
			{"name": "PAY-2", "member": "riya@example.com", "payment_for_document": "sports-nutrition"},
			{"name": "PAY-4", "member": "aman@example.com", "payment_for_document": "sports-nutrition"},
		)
		_, send = self.run_job(rows, paid={("riya@example.com", "sports-nutrition")})
		self.assertEqual([c.args[0] for c in send.call_args_list], ["aman@example.com"])

	def test_one_bad_row_does_not_stop_the_job(self):
		rows = self.payments(
			{"name": "PAY-2", "member": "riya@example.com", "payment_for_document": "sports-nutrition"},
			{"name": "PAY-4", "member": "aman@example.com", "payment_for_document": "cpt"},
		)
		site = FakeSite({"LMS Payment": rows}, values=dict(TITLES))
		with site.patch() as send:
			send.side_effect = [Exception("boom"), "sent"]
			wa.send_payment_pending(now=NOW)
		self.assertEqual(send.call_count, 2)


class TestTrialReminders(unittest.TestCase):
	def run_job(self, users_by_day, paid=(), enrollments=None, courses=None):
		enrollments = enrollments or {}
		courses = courses or {}

		def users(filters):
			start = filter_value(filters, "creation", ">=")
			return [frappe._dict(u) for u in users_by_day.get(start, [])]

		def enrollment_rows(filters):
			member = filters["member"]
			return [frappe._dict(course=c) for c in enrollments.get(member, [])]

		def exists(doctype, filters):
			if doctype == "LMS Payment":
				return filters["member"] in paid
			if doctype == "LMS Course":
				return filters if filters in ("sports-nutrition", "cpt") else None
			return None

		values = dict(TITLES)
		for course, row in courses.items():
			values[("LMS Course", course, "*")] = frappe._dict(row)
		site = FakeSite({"User": users, "LMS Enrollment": enrollment_rows}, exists=exists, values=values)
		with site.patch() as send:
			wa.send_trial_reminders(today=date(2026, 10, 10))
		return site, send

	def test_each_day_n_selects_opted_in_users_who_signed_up_on_that_calendar_day(self):
		site, _ = self.run_job({})
		calls = site.calls("User")
		self.assertEqual(len(calls), 3)
		windows = [
			(filter_value(c.filters, "creation", ">="), filter_value(c.filters, "creation", "<"))
			for c in calls
		]
		self.assertEqual(
			windows,
			[("2026-10-09", "2026-10-10"), ("2026-10-07", "2026-10-08"), ("2026-10-03", "2026-10-04")],
		)
		for c in calls:
			self.assertEqual(filter_value(c.filters, "wtf_whatsapp_opt_in", "="), 1)
			self.assertEqual(filter_value(c.filters, "enabled", "="), 1)
			self.assertEqual(filter_value(c.filters, "mobile_no", "is"), "set")

	def test_day1_and_day3_use_the_free_enrolment_course(self):
		users = {
			"2026-10-09": [{"name": "riya@example.com", "first_name": "Riya S", "wtf_signup_course": "cpt"}],
			"2026-10-07": [{"name": "aman@example.com", "first_name": "Aman", "wtf_signup_course": None}],
		}
		_, send = self.run_job(
			users, enrollments={"riya@example.com": ["sports-nutrition"], "aman@example.com": ["cpt"]}
		)
		self.assertEqual(
			[c.args for c in send.call_args_list],
			[
				("riya@example.com", wa.TRIAL_DAY1, ["Riya", "Sports Nutrition"], "sports-nutrition"),
				("aman@example.com", wa.TRIAL_DAY3, ["Aman", "CPT"], "cpt"),
			],
		)
		self.assertEqual({c.kwargs["key"] for c in send.call_args_list}, {"trial"})

	def test_falls_back_to_the_signup_course(self):
		users = {
			"2026-10-09": [{"name": "riya@example.com", "first_name": "Riya", "wtf_signup_course": "cpt"}]
		}
		_, send = self.run_job(users)
		self.assertEqual(send.call_args.args[3], "cpt")

	def test_no_course_no_message(self):
		users = {
			"2026-10-09": [{"name": "riya@example.com", "first_name": "Riya", "wtf_signup_course": None}]
		}
		_, send = self.run_job(users)
		send.assert_not_called()

	def test_paid_learners_are_skipped(self):
		users = {
			"2026-10-09": [{"name": "riya@example.com", "first_name": "Riya", "wtf_signup_course": "cpt"}]
		}
		_, send = self.run_job(users, paid={"riya@example.com"})
		send.assert_not_called()

	def test_day7_carries_the_course_price(self):
		users = {
			"2026-10-03": [{"name": "riya@example.com", "first_name": "Riya", "wtf_signup_course": "cpt"}]
		}
		_, send = self.run_job(
			users, courses={"cpt": {"paid_course": 1, "course_price": 24999, "currency": "INR"}}
		)
		self.assertEqual(
			send.call_args.args, ("riya@example.com", wa.TRIAL_DAY7, ["Riya", "CPT", "₹24,999"], "cpt")
		)

	def test_day7_is_skipped_for_a_free_course(self):
		users = {
			"2026-10-03": [{"name": "riya@example.com", "first_name": "Riya", "wtf_signup_course": "cpt"}]
		}
		_, send = self.run_job(
			users, courses={"cpt": {"paid_course": 0, "course_price": 0, "currency": "INR"}}
		)
		send.assert_not_called()


class TestLearningNudges(unittest.TestCase):
	def run_job(
		self, enrollments, last_progress=None, paid=(), recently_nudged=(), opted=("riya@example.com",)
	):
		last_progress = last_progress or {}

		def users(filters):
			return list(opted)

		def exists(doctype, filters):
			if doctype == "LMS Payment":
				return (filters["member"], filters["payment_for_document"]) in paid
			if doctype == wa.DOCTYPE:
				return filters["user"] in recently_nudged
			return None

		def progress(name):
			return last_progress.get((name["member"], name["course"]))

		values = dict(TITLES)
		values[("User", "riya@example.com", "first_name")] = "Riya"
		site = FakeSite(
			{"User": users, "LMS Enrollment": [frappe._dict(e) for e in enrollments]},
			exists=exists,
			values=values,
		)
		site.db.get_value.side_effect = lambda doctype, name=None, fieldname=None, *a, **kw: (
			progress(name) if doctype == "LMS Course Progress" else site._get_value(doctype, name, fieldname)
		)
		with site.patch() as send:
			wa.send_learning_nudges(now=NOW)
		return site, send

	def enrollment(self, **overrides):
		row = {
			"name": "ENR-1",
			"member": "riya@example.com",
			"course": "sports-nutrition",
			"progress": 40.4,
			"payment": "PAY-1",
			"creation": NOW - timedelta(days=30),
		}
		row.update(overrides)
		return row

	def test_selects_unfinished_enrolments_of_opted_in_learners(self):
		site, _ = self.run_job([])
		user_filters = site.calls("User")[0].filters
		self.assertEqual(user_filters["wtf_whatsapp_opt_in"], 1)
		enr = site.calls("LMS Enrollment")[0].filters
		self.assertEqual(filter_value(enr, "member", "in"), ["riya@example.com"])
		self.assertEqual(filter_value(enr, "progress", "<"), 100)

	def test_no_opted_in_learners_no_query(self):
		site, send = self.run_job([], opted=())
		self.assertEqual(site.calls("LMS Enrollment"), [])
		send.assert_not_called()

	def test_paid_learner_idle_five_days_gets_the_nudge_with_progress(self):
		_, send = self.run_job(
			[self.enrollment()],
			last_progress={("riya@example.com", "sports-nutrition"): NOW - timedelta(days=6)},
		)
		send.assert_called_once_with(
			"riya@example.com",
			wa.LEARNING_NUDGE,
			["Riya", "Sports Nutrition", "40%"],
			"sports-nutrition",
			key="sports-nutrition:2026-10-10",
		)

	def test_recent_progress_means_no_nudge(self):
		_, send = self.run_job(
			[self.enrollment()],
			last_progress={("riya@example.com", "sports-nutrition"): NOW - timedelta(days=4)},
		)
		send.assert_not_called()

	def test_no_progress_at_all_counts_from_enrolment(self):
		_, send = self.run_job([self.enrollment(creation=NOW - timedelta(days=6))])
		send.assert_called_once()
		_, send = self.run_job([self.enrollment(creation=NOW - timedelta(days=2))])
		send.assert_not_called()

	def test_free_enrolment_is_not_a_paid_learner(self):
		_, send = self.run_job([self.enrollment(payment=None)])
		send.assert_not_called()
		_, send = self.run_job(
			[self.enrollment(payment=None)], paid={("riya@example.com", "sports-nutrition")}
		)
		send.assert_called_once()

	def test_at_most_once_per_seven_days(self):
		_, send = self.run_job([self.enrollment()], recently_nudged={"riya@example.com"})
		send.assert_not_called()

	def test_recent_nudge_lookup_is_scoped_to_course_and_window(self):
		site, _ = self.run_job([self.enrollment()])
		calls = [c for c in site.db.exists.call_args_list if c.args[0] == wa.DOCTYPE]
		filters = calls[0].args[1]
		self.assertEqual(filters["template"], wa.LEARNING_NUDGE)
		self.assertEqual(
			filters["dedupe_key"], ["like", f"{wa.LEARNING_NUDGE}:riya@example.com:sports-nutrition:%"]
		)
		self.assertEqual(filters["creation"], [">", NOW - timedelta(days=7)])
		self.assertEqual(filters["status"], ["in", ["sending", "sent", "failed"]])


class TestSchedulerHooks(unittest.TestCase):
	def test_jobs_are_wired(self):
		from lms import hooks

		self.assertIn("lms.wtf_whatsapp.send_payment_pending", hooks.scheduler_events["hourly"])
		self.assertIn("lms.wtf_whatsapp.send_trial_reminders", hooks.scheduler_events["daily"])
		self.assertIn("lms.wtf_whatsapp.send_learning_nudges", hooks.scheduler_events["daily"])


if __name__ == "__main__":
	unittest.main()
