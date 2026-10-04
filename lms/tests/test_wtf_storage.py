import os
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

CONF = {
	"wtf_media_bucket": "b",
	"wtf_media_cdn": "https://cdn.example.com",
	"wtf_media_region": "ap-south-1",
}


class TestWtfStorage(IntegrationTestCase):
	def _file(self, content=b"hello-wtf", name="t.txt", private=0):
		return frappe.get_doc(
			{"doctype": "File", "file_name": name, "content": content, "is_private": private}
		).insert(ignore_permissions=True)

	def test_public_upload_moves_to_cdn_and_removes_local(self):
		s3 = MagicMock()
		with patch.dict(frappe.conf, CONF), patch("lms.wtf_storage._client", return_value=s3):
			f = self._file(content=os.urandom(32))
		f.reload()
		assert f.file_url.startswith("https://cdn.example.com/lms/")
		s3.upload_file.assert_called_once()
		assert s3.upload_file.call_args.kwargs["ExtraArgs"]["CacheControl"].endswith("immutable")

	def test_private_file_untouched(self):
		s3 = MagicMock()
		with patch.dict(frappe.conf, CONF), patch("lms.wtf_storage._client", return_value=s3):
			f = self._file(private=1, content=os.urandom(32))
		assert f.file_url.startswith("/private/files/")
		s3.upload_file.assert_not_called()

	def test_not_configured_is_noop(self):
		s3 = MagicMock()
		with patch.dict(frappe.conf, {"wtf_media_bucket": None}), patch(
			"lms.wtf_storage._client", return_value=s3
		):
			f = self._file(content=os.urandom(32))
		assert f.file_url.startswith("/files/")

	def test_s3_failure_keeps_local_file_and_logs(self):
		s3 = MagicMock()
		s3.upload_file.side_effect = Exception("AccessDenied")
		before = frappe.db.count("Error Log")
		with patch.dict(frappe.conf, CONF), patch("lms.wtf_storage._client", return_value=s3):
			f = self._file(content=os.urandom(32))
		f.reload()
		assert f.file_url.startswith("/files/")
		assert os.path.exists(frappe.get_site_path("public", f.file_url.lstrip("/")))
		assert frappe.db.count("Error Log") == before + 1

	def test_shared_object_not_deleted_while_referenced(self):
		s3 = MagicMock()
		data = os.urandom(32)
		with patch.dict(frappe.conf, CONF), patch("lms.wtf_storage._client", return_value=s3):
			a = self._file(content=data, name="a.txt")
			b = self._file(content=data, name="b.txt")
			a.reload()
			b.reload()
			assert a.file_url == b.file_url
			a.delete()
			s3.delete_object.assert_not_called()
			b.delete()
			s3.delete_object.assert_called_once()
