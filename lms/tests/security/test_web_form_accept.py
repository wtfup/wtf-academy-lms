import json
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

CORE_CMD = "frappe.website.doctype.web_form.web_form.accept"
PAYMENTS_CMD = "payments.overrides.payment_webform.accept"
GUARD = "lms.lms.web_form_guard.accept"


def _web_form(name, doc_type, fields, login_required=0, published=1, **extra):
	if frappe.db.exists("Web Form", name):
		frappe.delete_doc("Web Form", name, force=True)
	wf = frappe.get_doc(
		{
			"doctype": "Web Form",
			"name": name,
			"title": name,
			"route": name,
			"doc_type": doc_type,
			"module": "Website",
			"is_standard": 0,
			"published": published,
			"login_required": login_required,
			**extra,
			"web_form_fields": [
				{"fieldname": f, "fieldtype": "Data", "label": f.title()} for f in fields
			],
		}
	)
	wf.insert(ignore_permissions=True)
	return wf


class TestWebFormAccept(IntegrationTestCase):
	"""The payments app overrides web_form.accept with a version that takes the target doctype
	from the request. LMS must win the override and pin the doctype to the Web Form record."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		hash = frappe.generate_hash(length=6)
		cls.public = _web_form(f"wtf-public-{hash}", "ToDo", ["description"])
		cls.private = _web_form(f"wtf-private-{hash}", "ToDo", ["description"], login_required=1)
		cls.unpublished = _web_form(f"wtf-off-{hash}", "ToDo", ["description"], published=0)
		cls.editable = _web_form(
			f"wtf-edit-{hash}", "ToDo", ["description"], login_required=1, allow_edit=1
		)
		cls.pay = _web_form(f"wtf-pay-{hash}", "ToDo", ["description"])
		cls.keyed_pay = _web_form(f"wtf-keypay-{hash}", "ToDo", ["description"])
		# set after insert: the Web Form validator wants a gateway for accept_payment forms
		frappe.db.set_value("Web Form", cls.pay.name, "accept_payment", 1)
		frappe.db.set_value("Web Form", cls.keyed_pay.name, {"accept_payment": 1, "key_required": 1})
		cls.users = []
		for tag in ("a", "b"):
			email = f"wf-iso-{tag}-{hash}@example.com"
			frappe.get_doc(
				{"doctype": "User", "email": email, "first_name": f"Wf{tag}", "send_welcome_email": 0}
			).insert(ignore_permissions=True)
			cls.users.append(email)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for wf in (cls.public, cls.private, cls.unpublished, cls.editable, cls.pay, cls.keyed_pay):
			frappe.delete_doc("Web Form", wf.name, force=True)
		for email in cls.users:
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.request = None

	def _submit(self, cmd, web_form, data, user="Guest", **kw):
		"""Call the method the request would actually reach (after override resolution)."""
		method = frappe.get_attr(frappe.override_whitelisted_method(cmd))
		frappe.set_user(user)
		try:
			return method(web_form=web_form, data=json.dumps(data), **kw)
		finally:
			frappe.set_user("Administrator")

	def _count(self, doctype, **filters):
		return frappe.db.count(doctype, filters)

	# ---- precedence ----
	def test_lms_override_wins_for_core_and_payments_cmds(self):
		self.assertEqual(frappe.override_whitelisted_method(CORE_CMD), GUARD)
		self.assertEqual(frappe.override_whitelisted_method(PAYMENTS_CMD), GUARD)

	# ---- golden path ----
	def test_guest_can_submit_public_form_for_its_own_doctype(self):
		marker = f"wf-ok-{frappe.generate_hash(length=8)}"
		for cmd in (CORE_CMD, PAYMENTS_CMD):
			doc = self._submit(cmd, self.public.name, {"doctype": "ToDo", "description": f"{marker}-{cmd}"})
			self.assertEqual(doc.doctype, "ToDo")
			self.assertTrue(frappe.db.exists("ToDo", {"description": f"{marker}-{cmd}"}))

	def test_guest_can_submit_without_doctype_in_payload(self):
		marker = f"wf-nodt-{frappe.generate_hash(length=8)}"
		doc = self._submit(CORE_CMD, self.public.name, {"description": marker})
		self.assertEqual(doc.doctype, "ToDo")

	# ---- doctype tampering ----
	def test_guest_cannot_create_other_doctype(self):
		marker = f"wf-evil-{frappe.generate_hash(length=8)}"
		for cmd in (CORE_CMD, PAYMENTS_CMD):
			for target in ("Contact", "Note", "User"):
				before = frappe.db.count(target)
				with self.assertRaises(frappe.PermissionError, msg=f"{cmd} -> {target}"):
					self._submit(cmd, self.public.name, {"doctype": target, "description": marker})
				self.assertEqual(frappe.db.count(target), before, f"{cmd} created a {target}")
		self.assertFalse(frappe.db.exists("ToDo", {"description": marker}))

	# ---- login required / unpublished ----
	def test_login_required_form_rejects_guest(self):
		marker = f"wf-priv-{frappe.generate_hash(length=8)}"
		for cmd in (CORE_CMD, PAYMENTS_CMD):
			with self.assertRaises(frappe.PermissionError):
				self._submit(cmd, self.private.name, {"doctype": "ToDo", "description": marker})
		self.assertFalse(frappe.db.exists("ToDo", {"description": marker}))

	def test_login_required_form_accepts_logged_in_user(self):
		marker = f"wf-priv-ok-{frappe.generate_hash(length=8)}"
		doc = self._submit(CORE_CMD, self.private.name, {"description": marker}, user="Administrator")
		self.assertEqual(doc.doctype, "ToDo")

	def test_unpublished_form_rejects_guest(self):
		marker = f"wf-off-{frappe.generate_hash(length=8)}"
		with self.assertRaises(frappe.PermissionError):
			self._submit(PAYMENTS_CMD, self.unpublished.name, {"doctype": "ToDo", "description": marker})
		self.assertFalse(frappe.db.exists("ToDo", {"description": marker}))

	# ---- field tampering ----
	def test_restricted_fields_are_ignored(self):
		marker = f"wf-fields-{frappe.generate_hash(length=8)}"
		doc = self._submit(
			CORE_CMD,
			self.public.name,
			{
				"doctype": "ToDo",
				"description": marker,
				"owner": "Administrator",
				"modified_by": "Administrator",
				"allocated_to": "Administrator",
				"status": "Closed",
				"docstatus": 1,
			},
		)
		row = frappe.db.get_value(
			"ToDo", doc.name, ["owner", "allocated_to", "status", "docstatus"], as_dict=True
		)
		self.assertEqual(row.owner, "Guest")
		self.assertFalse(row.allocated_to)
		self.assertEqual(row.status, "Open")
		self.assertEqual(row.docstatus, 0)

	def test_guest_cannot_update_existing_record_via_name(self):
		victim = frappe.get_doc({"doctype": "ToDo", "description": "victim"}).insert()
		with self.assertRaises(frappe.ValidationError):
			self._submit(CORE_CMD, self.public.name, {"name": victim.name, "description": "pwned"})
		self.assertEqual(frappe.db.get_value("ToDo", victim.name, "description"), "victim")

	def test_payment_flag_is_rejected_on_non_payment_form(self):
		with self.assertRaises(frappe.PermissionError):
			self._submit(
				PAYMENTS_CMD, self.public.name, {"description": "pay"}, for_payment=True
			)

	# ---- F2: payment path honours key_required ----
	def test_payment_form_inserts_for_its_own_doctype(self):
		marker = f"wf-pay-{frappe.generate_hash(length=8)}"
		doc = self._submit(CORE_CMD, self.pay.name, {"doctype": "ToDo", "description": marker})
		self.assertEqual(doc.doctype, "ToDo")
		self.assertTrue(frappe.db.exists("ToDo", {"description": marker}))

	def test_payment_form_rejects_foreign_doctype(self):
		before = frappe.db.count("Contact")
		with self.assertRaises(frappe.PermissionError):
			self._submit(CORE_CMD, self.pay.name, {"doctype": "Contact", "description": "x"})
		self.assertEqual(frappe.db.count("Contact"), before)

	def test_key_required_payment_form_rejects_keyless_guest(self):
		marker = f"wf-keypay-{frappe.generate_hash(length=8)}"
		for cmd in (CORE_CMD, PAYMENTS_CMD):
			with self.assertRaises(frappe.PermissionError):
				self._submit(cmd, self.keyed_pay.name, {"description": marker})
			with self.assertRaises(frappe.PermissionError):
				self._submit(cmd, self.keyed_pay.name, {"description": marker}, web_form_request_key="bogus")
		self.assertFalse(frappe.db.exists("ToDo", {"description": marker}))

	def test_docname_on_payments_cmd_cannot_edit_without_allow_edit(self):
		victim = frappe.get_doc({"doctype": "ToDo", "description": "victim-docname"}).insert()
		with self.assertRaises(frappe.ValidationError):
			self._submit(PAYMENTS_CMD, self.public.name, {"description": "pwned"}, docname=victim.name)
		self.assertEqual(frappe.db.get_value("ToDo", victim.name, "description"), "victim-docname")

	# ---- F4: only `accept` is exposed from the guard module ----
	def test_guard_module_exposes_no_alias_of_core_accept(self):
		import lms.lms.web_form_guard as guard

		self.assertFalse(hasattr(guard, "core_accept"))
		exposed = [n for n, v in vars(guard).items() if callable(v) and v in frappe.whitelisted]
		self.assertEqual(exposed, ["accept"])

	# ---- F6: malformed input is a clean 4xx, never a 500 ----
	def test_malformed_data_is_validation_error(self):
		method = frappe.get_attr(GUARD)
		frappe.set_user("Guest")
		try:
			for bad in ("not json", "null", "5", "[1, 2]", '"str"', 7, None, ["a"]):
				with self.assertRaises(frappe.ValidationError, msg=repr(bad)):
					method(web_form=self.public.name, data=bad)
		finally:
			frappe.set_user("Administrator")

	def test_junk_for_payment_is_validation_error(self):
		for junk in ("yes", "maybe", "{}"):
			with self.assertRaises((frappe.ValidationError, frappe.PermissionError), msg=junk):
				self._submit(CORE_CMD, self.public.name, {"description": "x"}, for_payment=junk)

	# ---- F7: rate limit ----
	def test_guard_is_rate_limited_like_core(self):
		marker = f"wf-rl-{frappe.generate_hash(length=8)}"
		frappe.local.request = SimpleNamespace(method="POST")
		frappe.local.request_ip = f"10.9.{frappe.generate_hash(length=2)}"
		frappe.local.form_dict = frappe._dict(cmd=CORE_CMD, web_form=self.public.name)
		method = frappe.get_attr(GUARD)
		frappe.set_user("Guest")
		try:
			# core allows 10 per 60s; the guard must not double count against it
			for i in range(10):
				method(web_form=self.public.name, data=json.dumps({"description": f"{marker}-{i}"}))
			with self.assertRaises(frappe.RateLimitExceededError):
				method(web_form=self.public.name, data=json.dumps({"description": f"{marker}-x"}))
		finally:
			frappe.set_user("Administrator")
			frappe.local.form_dict = frappe._dict()

	# ---- F8: allow_edit ----
	def test_allow_edit_owner_can_update_own_doc_and_other_user_cannot(self):
		owner, other = self.users
		doc = self._submit(CORE_CMD, self.editable.name, {"description": "mine"}, user=owner)
		self.assertEqual(frappe.db.get_value("ToDo", doc.name, "owner"), owner)

		self._submit(CORE_CMD, self.editable.name, {"description": "mine v2"}, user=owner, docname=doc.name)
		self.assertEqual(frappe.db.get_value("ToDo", doc.name, "description"), "mine v2")

		with self.assertRaises(frappe.PermissionError):
			self._submit(CORE_CMD, self.editable.name, {"description": "stolen"}, user=other, docname=doc.name)
		with self.assertRaises(frappe.PermissionError):
			self._submit(PAYMENTS_CMD, self.editable.name, {"name": doc.name, "description": "stolen"}, user=other)
		self.assertEqual(frappe.db.get_value("ToDo", doc.name, "description"), "mine v2")

	# ---- F8: doctype case variants ----
	def test_case_variant_doctype_never_inserts_a_todo(self):
		marker = f"wf-case-{frappe.generate_hash(length=8)}"
		for variant in ("todo", " ToDo ", "TODO", "ToDo\n"):
			for cmd in (CORE_CMD, PAYMENTS_CMD):
				with self.assertRaises(frappe.PermissionError, msg=f"{cmd} {variant!r}"):
					self._submit(cmd, self.public.name, {"doctype": variant, "description": marker})
		# a variant is a different string than doc_type, so it is a 403: nothing is inserted at all
		self.assertEqual(frappe.db.count("ToDo", {"description": marker}), 0)

	# ---- F8: full dispatch through frappe.handler ----
	def test_handler_dispatch_resolves_to_guard_and_rejects_foreign_doctype(self):
		frappe.set_user("Guest")
		try:
			for cmd in (CORE_CMD, PAYMENTS_CMD):
				frappe.local.form_dict = frappe._dict(
					cmd=cmd,
					web_form=self.public.name,
					data=json.dumps({"doctype": "Contact", "description": "x"}),
				)
				frappe.local.request = SimpleNamespace(method="POST")
				resolved = frappe.override_whitelisted_method(frappe.local.form_dict.cmd)
				self.assertEqual(resolved, GUARD)
				method = frappe.get_attr(resolved)
				self.assertIn(method, frappe.whitelisted)
				self.assertIn(method, frappe.guest_methods)
				self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[method], ["POST", "PUT"])
				with self.assertRaises(frappe.PermissionError):
					frappe.call(method, **{k: v for k, v in frappe.local.form_dict.items() if k != "cmd"})
		finally:
			frappe.set_user("Administrator")
			frappe.local.form_dict = frappe._dict()

	# ---- F5: after_migrate precedence check ----
	def test_after_migrate_logs_when_override_is_not_the_guard(self):
		from lms.lms.web_form_guard import check_override_precedence

		self.assertIn("lms.lms.web_form_guard.check_override_precedence", frappe.get_hooks("after_migrate", app_name="lms"))
		before = frappe.db.count("Error Log", {"method": ["like", "%web form override%"]})
		check_override_precedence()  # healthy: no log
		self.assertEqual(frappe.db.count("Error Log", {"method": ["like", "%web form override%"]}), before)
		with patch("frappe.override_whitelisted_method", return_value=PAYMENTS_CMD):
			check_override_precedence()  # must log, not raise
		self.assertEqual(frappe.db.count("Error Log", {"method": ["like", "%web form override%"]}), before + 1)
