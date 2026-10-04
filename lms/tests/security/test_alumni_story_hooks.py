import hashlib
import io

import frappe
from frappe.tests import IntegrationTestCase
from PIL import Image

from lms.lms import alumni_story
from lms.tests.security.alumni_story_fixture import drop_alumni_story, ensure_alumni_story


def jpeg() -> bytes:
	b = io.BytesIO()
	Image.new("RGB", (8, 8), (10, 120, 10)).save(b, "JPEG")
	return b.getvalue()


class TestAlumniStoryHooks(IntegrationTestCase):
	"""WTF: publication safety on the custom "Alumni Story" DocType.
	- Ticking learner_approved_final needs a date, a channel and final text, and stores approved_text_hash.
	- Changing final_headline / final_story after approval clears the approval (stale approval).
	- The guest photo stays private until the story is Approved, learner-approved and name/photo-consented;
	  a Rejected or deleted story deletes its photo."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.created = ensure_alumni_story()

	@classmethod
	def tearDownClass(cls):
		drop_alumni_story(cls.created)
		super().tearDownClass()

	def _story(self, **kw):
		d = frappe.get_doc({"doctype": "Alumni Story", "full_name": f"Test Learner {frappe.generate_hash(length=6)}", "consent": "Yes", **kw})
		d.insert(ignore_permissions=True)
		return d

	def _with_photo(self, **kw):
		d = self._story(**kw)
		f = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"story-{frappe.generate_hash(length=10)}.jpg",
				"attached_to_doctype": "Alumni Story",
				"attached_to_name": d.name,
				"attached_to_field": "photo",
				"is_private": 1,
				"content": jpeg(),
			}
		).insert(ignore_permissions=True)
		d.db_set("photo", f.file_url)
		d.reload()
		return d, f

	def _approve(self, d, **kw):
		d.update({"final_headline": "Test headline", "final_story": "Test final story.", "learner_approved_final": 1,
				  "learner_approved_on": "2026-10-04", "approval_channel": "WhatsApp", **kw})
		d.save(ignore_permissions=True)
		return d

	# ---- the approved-text hash ----
	def test_text_hash_is_sha256_of_headline_newline_story(self):
		# Same vector as the project's tests/test_testimonials_offline.py (export_testimonials.text_hash).
		self.assertEqual(alumni_story.text_hash("H", "S"), "0a5adc8fb63b9705c72ca29cff8820550c7e40f1429fbba61ffdabce7f66ace6")
		self.assertEqual(alumni_story.text_hash("H", "S"), hashlib.sha256(b"H\nS").hexdigest())
		self.assertEqual(alumni_story.text_hash(" H ", "S\n"), alumni_story.text_hash("H", "S"))

	def test_ticking_approval_stores_the_hash_of_the_approved_text(self):
		d = self._approve(self._story())
		self.assertEqual(d.approved_text_hash, alumni_story.text_hash("Test headline", "Test final story."))

	def test_approval_needs_date_channel_and_final_text(self):
		for missing in ({"learner_approved_on": None}, {"approval_channel": ""}, {"final_story": ""}, {"final_headline": " "}):
			d = self._story()
			with self.assertRaises(frappe.ValidationError, msg=str(missing)):
				self._approve(d, **missing)

	def test_changing_the_final_text_after_approval_clears_the_approval(self):
		for field in ("final_story", "final_headline"):
			d = self._approve(self._story())
			d.set(field, "Edited after the learner approved.")
			d.save(ignore_permissions=True)
			d.reload()
			self.assertEqual(d.learner_approved_final, 0, field)
			self.assertFalse(d.learner_approved_on, field)
			self.assertFalse(d.approved_text_hash, field)

	def test_other_edits_keep_the_approval(self):
		d = self._approve(self._story())
		d.status = "Approved"
		d.save(ignore_permissions=True)
		d.reload()
		self.assertEqual(d.learner_approved_final, 1)
		self.assertEqual(d.approved_text_hash, alumni_story.text_hash("Test headline", "Test final story."))

	def test_unticking_clears_the_hash(self):
		d = self._approve(self._story())
		d.learner_approved_final = 0
		d.save(ignore_permissions=True)
		self.assertFalse(d.approved_text_hash)

	# ---- the photo ----
	def test_photo_stays_private_while_pending_or_without_consent(self):
		d, f = self._with_photo(publish_with_name_photo=1)
		self._approve(d)  # learner-approved but status still Pending
		self.assertEqual(frappe.db.get_value("File", f.name, "is_private"), 1)
		d2, f2 = self._with_photo(publish_with_name_photo=0)
		self._approve(d2, status="Approved")
		self.assertEqual(frappe.db.get_value("File", f2.name, "is_private"), 1)

	def test_photo_goes_public_when_approved_learner_approved_and_consented(self):
		d, f = self._with_photo(publish_with_name_photo=1)
		self._approve(d, status="Approved")
		row = frappe.db.get_value("File", f.name, ["is_private", "file_url"], as_dict=True)
		self.assertEqual(row.is_private, 0)
		self.assertFalse(row.file_url.startswith("/private/"))
		self.assertEqual(frappe.db.get_value("Alumni Story", d.name, "photo"), row.file_url)

	def test_rejected_story_deletes_its_photo(self):
		d, f = self._with_photo()
		d.status = "Rejected"
		d.save(ignore_permissions=True)
		self.assertFalse(frappe.db.exists("File", f.name))
		self.assertFalse(frappe.db.get_value("Alumni Story", d.name, "photo"))

	def test_deleted_story_deletes_its_photo(self):
		d, f = self._with_photo()
		frappe.delete_doc("Alumni Story", d.name, force=True, ignore_permissions=True)
		self.assertFalse(frappe.db.exists("File", f.name))
