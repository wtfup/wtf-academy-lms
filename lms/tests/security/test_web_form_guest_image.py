import base64
import io
import json
import struct
import zlib

import frappe
from frappe.tests import IntegrationTestCase
from PIL import Image

from lms.lms import web_form_guard as guard
from lms.tests.security.alumni_story_fixture import drop_alumni_story, ensure_alumni_story

CORE_CMD = "frappe.website.doctype.web_form.web_form.accept"
IP = "203.0.113.9"


def jpeg(size=(8, 8), color=(200, 30, 30), exif=None) -> bytes:
	b = io.BytesIO()
	im = Image.new("RGB", size, color)
	if exif is not None:
		im.save(b, "JPEG", exif=exif)
	else:
		im.save(b, "JPEG")
	return b.getvalue()


def png(size=(8, 8)) -> bytes:
	b = io.BytesIO()
	Image.new("RGBA", size, (0, 0, 255, 128)).save(b, "PNG")
	return b.getvalue()


def png_claiming(width, height) -> bytes:
	"""A tiny PNG whose header claims width x height (decompression-bomb shape)."""
	raw = png((1, 1))
	ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
	chunk = b"IHDR" + ihdr
	return raw[:8] + struct.pack(">I", 13) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF) + raw[33:]


def data_value(name, mime, content):
	return f"{name},data:{mime};base64,{base64.b64encode(content).decode()}"


def _web_form(name, doc_type, fields):
	return frappe.get_doc(
		{
			"doctype": "Web Form",
			"name": name,
			"title": name,
			"route": name,
			"doc_type": doc_type,
			"module": "Website",
			"is_standard": 0,
			"published": 1,
			"login_required": 0,
			"web_form_fields": [{"fieldname": f, "fieldtype": t, "label": f} for f, t in fields],
		}
	).insert(ignore_permissions=True)


class TestWebFormGuestImage(IntegrationTestCase):
	"""WTF: the share-your-story form (doctype "Alumni Story") may carry one optional photo. Guest has no
	File permission, so core accept() cannot save it; the guard does, ONLY for Alumni Story Attach Image
	fields: decoded and re-encoded with Pillow (pixel cap, metadata stripped, trailing bytes dropped),
	saved PRIVATE until a moderator approves the story, at most 5 photo submissions per IP per day."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.created = ensure_alumni_story()
		h = frappe.generate_hash(length=6)
		cls.wf = _web_form(f"wtf-img-{h}", "Alumni Story", [("full_name", "Data"), ("photo", "Attach Image"), ("doc_file", "Attach")])
		cls.other = _web_form(f"wtf-img-other-{h}", "Contact", [("first_name", "Data"), ("image", "Attach Image")])
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.delete_doc("Web Form", cls.wf.name, force=True)
		frappe.delete_doc("Web Form", cls.other.name, force=True)
		drop_alumni_story(cls.created)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		frappe.local.request_ip = IP
		frappe.cache.delete_value(guard.photo_cap_key(IP))

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.cache.delete_value(guard.photo_cap_key(IP))
		frappe.local.request_ip = None

	def _submit(self, data, wf=None):
		method = frappe.get_attr(frappe.override_whitelisted_method(CORE_CMD))
		frappe.set_user("Guest")
		try:
			return method(web_form=(wf or self.wf).name, data=json.dumps(data))
		finally:
			frappe.set_user("Administrator")

	def _marker(self):
		return f"wfimg{frappe.generate_hash(length=8)}"

	def _files_for(self, doctype, docname):
		return frappe.get_all(
			"File",
			filters={"attached_to_doctype": doctype, "attached_to_name": docname},
			fields=["name", "file_url", "file_name", "is_private", "attached_to_field"],
		)

	def _stored_bytes(self, file_name):
		with open(frappe.get_doc("File", file_name).get_full_path(), "rb") as fh:
			return fh.read()

	# ---- golden path ----
	def test_guest_photo_is_saved_private_reencoded_and_attached(self):
		doc = self._submit({"full_name": self._marker(), "photo": data_value("me.png", "image/png", png())})
		files = self._files_for("Alumni Story", doc.name)
		self.assertEqual(len(files), 1)
		f = files[0]
		self.assertEqual(f.is_private, 1)  # off the CDN until a moderator approves the story
		self.assertTrue(f.file_url.startswith("/private/files/"))
		self.assertEqual(f.attached_to_field, "photo")
		self.assertTrue(f.file_name.endswith(".jpg"))
		self.assertEqual(frappe.db.get_value("Alumni Story", doc.name, "photo"), f.file_url)
		out = Image.open(io.BytesIO(self._stored_bytes(f.name)))
		self.assertEqual(out.format, "JPEG")

	def test_no_photo_is_fine(self):
		doc = self._submit({"full_name": self._marker(), "photo": ""})
		self.assertEqual(self._files_for("Alumni Story", doc.name), [])

	# ---- re-encoding ----
	def test_bytes_appended_after_the_image_are_dropped(self):
		payload = jpeg() + b"<html><script>alert(1)</script></html>"
		doc = self._submit({"full_name": self._marker(), "photo": data_value("x.jpg", "image/jpeg", payload)})
		stored = self._stored_bytes(self._files_for("Alumni Story", doc.name)[0].name)
		self.assertNotIn(b"<script", stored)
		self.assertNotIn(b"<html", stored)
		self.assertTrue(stored.endswith(b"\xff\xd9"))

	def test_gps_and_other_exif_is_stripped(self):
		exif = Image.Exif()
		exif[0x010F] = "TestCam"  # Make
		gps = exif.get_ifd(0x8825)
		gps[1] = "N"
		gps[2] = (28.0, 35.0, 1.0)
		src = jpeg(exif=exif)
		self.assertIn(0x8825, Image.open(io.BytesIO(src)).getexif())  # the fixture really carries GPS
		doc = self._submit({"full_name": self._marker(), "photo": data_value("gps.jpg", "image/jpeg", src)})
		out = Image.open(io.BytesIO(self._stored_bytes(self._files_for("Alumni Story", doc.name)[0].name)))
		self.assertEqual(len(out.getexif()), 0)
		self.assertNotIn(b"TestCam", self._stored_bytes(self._files_for("Alumni Story", doc.name)[0].name))

	def test_large_photo_is_scaled_down(self):
		doc = self._submit({"full_name": self._marker(), "photo": data_value("big.jpg", "image/jpeg", jpeg((4000, 3000)))})
		out = Image.open(io.BytesIO(self._stored_bytes(self._files_for("Alumni Story", doc.name)[0].name)))
		self.assertLessEqual(max(out.size), guard.GUEST_IMAGE_MAX_SIDE)

	# ---- refusals ----
	def _refused(self, value, exc=frappe.ValidationError):
		m = self._marker()
		before = frappe.db.count("File")
		with self.assertRaises(exc):
			self._submit({"full_name": m, "photo": value})
		self.assertFalse(frappe.db.exists("Alumni Story", {"full_name": m}))
		self.assertEqual(frappe.db.count("File"), before)

	def test_non_image_type_is_refused(self):
		self._refused(data_value("x.html", "text/html", b"<script>alert(1)</script>"))
		self._refused(data_value("x.svg", "image/svg+xml", b"<svg onload=alert(1)></svg>"))
		self._refused(data_value("x.pdf", "application/pdf", b"%PDF-1.4"))

	def test_bytes_must_really_be_an_image(self):
		self._refused(data_value("x.png", "image/png", b"<html><script>alert(1)</script></html>"))

	def test_decompression_bomb_is_refused_before_decoding(self):
		self._refused(data_value("bomb.png", "image/png", png_claiming(8000, 6000)))  # 48 MP > 40 MP

	def test_oversize_payload_is_refused_before_decoding(self):
		big = "big.png,data:image/png;base64," + "A" * (guard.GUEST_IMAGE_MAX_B64 + 4)
		self._refused(big)

	def test_oversize_photo_is_refused(self):
		self._refused(data_value("big.png", "image/png", png() + b"\0" * (5 * 1024 * 1024)))

	def test_guest_cannot_point_the_field_at_an_existing_or_remote_file(self):
		self._refused("/files/someone-else.png")
		self._refused("https://evil.example/x.png")
		self._refused("/private/files/secret.png")

	def test_unsafe_file_name_is_replaced(self):
		doc = self._submit({"full_name": self._marker(), "photo": data_value("../../evil.html", "image/png", png())})
		f = self._files_for("Alumni Story", doc.name)[0]
		self.assertNotIn("/", f.file_name)
		self.assertNotIn("evil", f.file_name)

	# ---- scope: Alumni Story Attach Image only ----
	def test_other_doctypes_keep_core_behaviour(self):
		m = self._marker()
		before = frappe.db.count("File")
		with self.assertRaises(frappe.PermissionError):  # core accept() saves the File as Guest
			self._submit({"first_name": m, "image": data_value("me.png", "image/png", png())}, wf=self.other)
		self.assertEqual(frappe.db.count("File"), before)

	def test_plain_attach_fields_keep_core_behaviour(self):
		m = self._marker()
		before = frappe.db.count("File")
		with self.assertRaises(frappe.PermissionError):
			self._submit({"full_name": m, "doc_file": data_value("me.png", "image/png", png())})
		self.assertEqual(frappe.db.count("File"), before)

	# ---- storage-fill protection ----
	def test_photo_submissions_are_capped_per_ip_per_day(self):
		value = data_value("me.png", "image/png", png())
		for _ in range(guard.GUEST_PHOTOS_PER_IP_PER_DAY):
			self._submit({"full_name": self._marker(), "photo": value})
		self._refused(value, exc=frappe.TooManyRequestsError)
		# a story without a photo is still welcome
		self._submit({"full_name": self._marker(), "photo": ""})
		# the cap is per IP
		frappe.local.request_ip = "203.0.113.10"
		try:
			self._submit({"full_name": self._marker(), "photo": value})
		finally:
			frappe.cache.delete_value(guard.photo_cap_key("203.0.113.10"))
			frappe.local.request_ip = IP

	def test_photo_cap_expires_within_a_day(self):
		self._submit({"full_name": self._marker(), "photo": data_value("me.png", "image/png", png())})
		ttl = frappe.cache.ttl(frappe.cache.make_key(guard.photo_cap_key(IP)))
		self.assertTrue(0 < ttl <= 86400, ttl)
