import json

import frappe
from frappe.tests import IntegrationTestCase

CORE_CMD = "frappe.website.doctype.web_form.web_form.accept"
PAYMENTS_CMD = "payments.overrides.payment_webform.accept"
GUARD = "lms.lms.web_form_guard.accept"


def _web_form(name, doc_type, fields, login_required=0, published=1):
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
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for wf in (cls.public, cls.private, cls.unpublished):
			frappe.delete_doc("Web Form", wf.name, force=True)
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
