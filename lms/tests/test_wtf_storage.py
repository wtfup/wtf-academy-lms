import os
from unittest.mock import MagicMock, patch

import botocore.config

import frappe
from frappe.tests import IntegrationTestCase

CONF = {
	"wtf_media_bucket": "b",
	"wtf_media_cdn": "https://cdn.example.com",
	"wtf_media_region": "ap-south-1",
}


class TestWtfStorage(IntegrationTestCase):
	def _file(self, content=b"hello-wtf", name="t.png", private=0, **extra):
		return frappe.get_doc(
			{"doctype": "File", "file_name": name, "content": content, "is_private": private, **extra}
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
			a = self._file(content=data, name="a.png")
			b = self._file(content=data, name="b.png")
			a.reload()
			b.reload()
			assert a.file_url == b.file_url
			a.delete()
			s3.delete_object.assert_not_called()
			b.delete()
			s3.delete_object.assert_called_once()

	def test_s3_delete_failure_does_not_block_file_delete_and_logs(self):
		s3 = MagicMock()
		s3.delete_object.side_effect = Exception("AccessDenied")
		with patch.dict(frappe.conf, CONF), patch("lms.wtf_storage._client", return_value=s3):
			f = self._file(content=os.urandom(32))
			f.reload()
			name = f.name
			before = frappe.db.count("Error Log")
			f.delete()
		s3.delete_object.assert_called_once()
		assert not frappe.db.exists("File", name)
		assert frappe.db.count("Error Log") == before + 1

	# A1: only media moves; the CDN sends X-Frame-Options/no CORS, so a PDF there is blank in lessons
	def test_pdf_stays_local_with_no_s3_call(self):
		s3 = MagicMock()
		with patch.dict(frappe.conf, CONF), patch("lms.wtf_storage._client", return_value=s3):
			f = self._file(content=os.urandom(32), name="handout.pdf")
		f.reload()
		assert f.file_url.startswith("/files/")
		assert os.path.exists(frappe.get_site_path("public", f.file_url.lstrip("/")))
		s3.upload_file.assert_not_called()

	def test_other_non_media_types_stay_local(self):
		for name in ("notes.txt", "sheet.csv", "data.json", "noext"):
			s3 = MagicMock()
			with patch.dict(frappe.conf, CONF), patch("lms.wtf_storage._client", return_value=s3):
				f = self._file(content=os.urandom(32), name=name)
			f.reload()
			assert f.file_url.startswith("/files/"), name
			s3.upload_file.assert_not_called()

	def test_image_video_audio_move_with_their_content_type(self):
		for name, ctype in (("pic.png", "image/png"), ("clip.mp4", "video/mp4"), ("voice.mp3", "audio/mpeg")):
			s3 = MagicMock()
			with patch.dict(frappe.conf, CONF), patch("lms.wtf_storage._client", return_value=s3):
				f = self._file(content=os.urandom(32), name=name)
			f.reload()
			assert f.file_url.startswith("https://cdn.example.com/lms/"), name
			assert s3.upload_file.call_args.kwargs["ExtraArgs"]["ContentType"] == ctype

	# A2: once S3 has the object, nothing after it may fail the upload
	def _log_count(self):
		return frappe.db.count("Error Log", {"method": "WTF media upload failed"})

	def test_unknown_attached_field_still_succeeds_and_logs(self):
		s3 = MagicMock()
		before = self._log_count()
		with patch.dict(frappe.conf, CONF), patch("lms.wtf_storage._client", return_value=s3):
			f = self._file(
				content=os.urandom(32),
				attached_to_doctype="User",
				attached_to_name="Administrator",
				attached_to_field="no_such_field_wtf",
			)
		f.reload()
		assert f.file_url.startswith("https://cdn.example.com/lms/")
		assert self._log_count() == before + 1

	def test_os_remove_failure_still_succeeds_and_logs(self):
		s3 = MagicMock()
		before = self._log_count()
		with patch.dict(frappe.conf, CONF), patch("lms.wtf_storage._client", return_value=s3), patch(
			"lms.wtf_storage.os.remove", side_effect=OSError("busy")
		):
			f = self._file(content=os.urandom(32))
		f.reload()
		assert f.file_url.startswith("https://cdn.example.com/lms/")
		assert self._log_count() == before + 1

	# A3: a slow S3 must give up before gunicorn does
	def test_client_has_explicit_timeouts(self):
		with patch("boto3.client") as client:
			from lms.wtf_storage import _client

			_client("ap-south-1")
		cfg = client.call_args.kwargs["config"]
		assert isinstance(cfg, botocore.config.Config)
		assert cfg.connect_timeout == 5
		assert cfg.read_timeout == 60
		assert cfg.retries == {"max_attempts": 2}

	# A4: a File made private must come back off the public CDN
	def _cdn_file(self, s3, data=None):
		with patch.dict(frappe.conf, CONF), patch("lms.wtf_storage._client", return_value=s3):
			f = self._file(content=data or os.urandom(32))
		f.reload()
		assert f.file_url.startswith("https://cdn.example.com/lms/")
		return f

	def _download_writes(self, payload):
		def download_file(bucket, key, path):
			with open(path, "wb") as fh:
				fh.write(payload)

		return download_file

	def test_making_cdn_file_private_pulls_it_back_and_deletes_object(self):
		s3 = MagicMock()
		f = self._cdn_file(s3)
		key = f.file_url[len("https://cdn.example.com/"):]
		s3.download_file.side_effect = self._download_writes(b"bytes-back")
		with patch.dict(frappe.conf, CONF), patch("lms.wtf_storage._client", return_value=s3):
			f.is_private = 1
			f.save(ignore_permissions=True)
		f.reload()
		assert f.file_url.startswith("/private/files/"), f.file_url
		path = frappe.get_site_path(f.file_url.lstrip("/"))
		with open(path, "rb") as fh:
			assert fh.read() == b"bytes-back"
		s3.download_file.assert_called_once()
		assert s3.download_file.call_args.args[:2] == ("b", key)
		s3.delete_object.assert_called_once_with(Bucket="b", Key=key)

	def test_making_shared_cdn_file_private_keeps_object_for_other_rows(self):
		s3 = MagicMock()
		data = os.urandom(32)
		a = self._cdn_file(s3, data)
		with patch.dict(frappe.conf, CONF), patch("lms.wtf_storage._client", return_value=s3):
			b = self._file(content=data, name="other.png")
		b.reload()
		assert a.file_url == b.file_url
		s3.download_file.side_effect = self._download_writes(data)
		with patch.dict(frappe.conf, CONF), patch("lms.wtf_storage._client", return_value=s3):
			a.is_private = 1
			a.save(ignore_permissions=True)
		a.reload()
		assert a.file_url.startswith("/private/files/")
		s3.delete_object.assert_not_called()

	def test_make_private_download_failure_fails_open_and_logs(self):
		s3 = MagicMock()
		f = self._cdn_file(s3)
		url = f.file_url
		s3.download_file.side_effect = Exception("AccessDenied")
		before = frappe.db.count("Error Log", {"method": "WTF media make-private failed"})
		with patch.dict(frappe.conf, CONF), patch("lms.wtf_storage._client", return_value=s3):
			f.is_private = 1
			f.save(ignore_permissions=True)
		f.reload()
		assert f.file_url == url
		s3.delete_object.assert_not_called()
		assert frappe.db.count("Error Log", {"method": "WTF media make-private failed"}) == before + 1

	def test_public_save_of_cdn_file_does_nothing(self):
		s3 = MagicMock()
		f = self._cdn_file(s3)
		with patch.dict(frappe.conf, CONF), patch("lms.wtf_storage._client", return_value=s3):
			f.save(ignore_permissions=True)
		s3.download_file.assert_not_called()
		s3.delete_object.assert_not_called()

	# QA cleanup (scripts/qa/cleanup_qa_users.py) finds test-made rows by the leading file name
	def test_error_log_names_the_file_first(self):
		s3 = MagicMock()
		s3.upload_file.side_effect = Exception("AccessDenied")
		with patch.dict(frappe.conf, CONF), patch("lms.wtf_storage._client", return_value=s3):
			f = self._file(content=os.urandom(32), name="qa-storage-failure.png")
		row = frappe.get_last_doc("Error Log", filters={"method": "WTF media upload failed"})
		assert row.error.startswith("qa-storage-failure.png\n")
		assert (row.reference_doctype, row.reference_name) == ("File", f.name)
