import base64
import json

import frappe
from frappe.tests import IntegrationTestCase

CORE_CMD = "frappe.website.doctype.web_form.web_form.accept"

# 1x1 PNG
PNG = base64.b64decode(
	"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def data_value(name, mime, content):
	return f"{name},data:{mime};base64,{base64.b64encode(content).decode()}"


class TestWebFormGuestImage(IntegrationTestCase):
	"""WTF: a guest web form may carry an optional photo (Attach Image). Guest has no File permission,
	so core accept() cannot save it; the guard saves it, only as a real JPG/PNG/WebP up to 5 MB, as a
	public File attached to the new record. Anything else is refused and nothing is stored."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		name = f"wtf-img-{frappe.generate_hash(length=6)}"
		cls.wf = frappe.get_doc(
			{
				"doctype": "Web Form",
				"name": name,
				"title": name,
				"route": name,
				"doc_type": "Contact",
				"module": "Website",
				"is_standard": 0,
				"published": 1,
				"login_required": 0,
				"web_form_fields": [
					{"fieldname": "first_name", "fieldtype": "Data", "label": "First name"},
					{"fieldname": "image", "fieldtype": "Attach Image", "label": "Image"},
				],
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.delete_doc("Web Form", cls.wf.name, force=True)
		frappe.db.commit()
		super().tearDownClass()

	def tearDown(self):
		frappe.set_user("Administrator")

	def _submit(self, data):
		method = frappe.get_attr(frappe.override_whitelisted_method(CORE_CMD))
		frappe.set_user("Guest")
		try:
			return method(web_form=self.wf.name, data=json.dumps(data))
		finally:
			frappe.set_user("Administrator")

	def _marker(self):
		return f"wfimg{frappe.generate_hash(length=8)}"

	def _files_for(self, docname):
		return frappe.get_all(
			"File",
			filters={"attached_to_doctype": "Contact", "attached_to_name": docname},
			fields=["name", "file_url", "file_name", "is_private", "attached_to_field"],
		)

	def test_guest_photo_is_saved_as_public_file_attached_to_the_new_record(self):
		m = self._marker()
		doc = self._submit({"first_name": m, "image": data_value("me.png", "image/png", PNG)})
		files = self._files_for(doc.name)
		self.assertEqual(len(files), 1)
		f = files[0]
		self.assertEqual(f.is_private, 0)
		self.assertEqual(f.attached_to_field, "image")
		self.assertTrue(f.file_name.endswith(".png"))
		self.assertEqual(frappe.db.get_value("Contact", doc.name, "image"), f.file_url)
		with open(frappe.get_doc("File", f.name).get_full_path(), "rb") as fh:
			self.assertEqual(fh.read(), PNG)

	def test_no_photo_is_fine(self):
		m = self._marker()
		doc = self._submit({"first_name": m, "image": ""})
		self.assertEqual(self._files_for(doc.name), [])
		self.assertFalse(frappe.db.get_value("Contact", doc.name, "image"))

	def _refused(self, value):
		m = self._marker()
		before = frappe.db.count("File")
		with self.assertRaises(frappe.ValidationError):
			self._submit({"first_name": m, "image": value})
		self.assertFalse(frappe.db.exists("Contact", {"first_name": m}))
		self.assertEqual(frappe.db.count("File"), before)

	def test_non_image_type_is_refused(self):
		self._refused(data_value("x.html", "text/html", b"<script>alert(1)</script>"))
		self._refused(data_value("x.svg", "image/svg+xml", b"<svg onload=alert(1)></svg>"))
		self._refused(data_value("x.pdf", "application/pdf", b"%PDF-1.4"))

	def test_bytes_must_really_be_an_image(self):
		self._refused(data_value("x.png", "image/png", b"<html><script>alert(1)</script></html>"))

	def test_oversize_photo_is_refused(self):
		self._refused(data_value("big.png", "image/png", PNG + b"\0" * (5 * 1024 * 1024)))

	def test_guest_cannot_point_the_field_at_an_existing_or_remote_file(self):
		self._refused("/files/someone-else.png")
		self._refused("https://evil.example/x.png")
		self._refused("/private/files/secret.png")

	def test_unsafe_file_name_is_replaced(self):
		m = self._marker()
		doc = self._submit({"first_name": m, "image": data_value("../../evil.html", "image/png", PNG)})
		f = self._files_for(doc.name)[0]
		self.assertNotIn("/", f.file_name)
		self.assertNotIn("..", f.file_name)
		self.assertTrue(f.file_name.endswith(".png"))
