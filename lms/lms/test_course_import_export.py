# Copyright (c) 2021, FOSS United and Contributors
# See license.txt

import io
import json
import unittest
import zipfile
from unittest.mock import MagicMock, patch

import frappe

from lms.lms.course_import_export import (
	get_asset_privacy,
	get_assessments_from_lesson,
	get_course_fields,
	read_asset_content,
	replace_assessment_names,
	write_assets,
)

RAW_URL = "https://www.youtube.com/watch?v=htpg8CuD1Ec"


class TestImportExportContentGuards(unittest.TestCase):
	"""Course export/import reads each lesson's EditorJS content. A lesson with
	non-JSON content (e.g. a raw URL pasted into the Desk form) used to 500 the
	whole export. These readers must fail soft. Fixture-free: the non-JSON paths
	never reach the DB.
	"""

	def test_export_carries_the_lesson_locking_setting(self):
		self.assertIn("enforce_lesson_completion", get_course_fields())

	def test_get_assessments_from_lesson_non_json(self):
		# Non-JSON content yields no assessments/questions/test_cases and never hits the DB.
		self.assertEqual(get_assessments_from_lesson(frappe._dict(content=RAW_URL)), ([], [], []))
		self.assertEqual(get_assessments_from_lesson(frappe._dict(content=None)), ([], [], []))

	def test_replace_assessment_names_passes_non_json_through(self):
		# Mutate-and-redump path: unparseable content is returned unchanged, not crashed.
		self.assertEqual(replace_assessment_names(None, RAW_URL), RAW_URL)

	def test_replace_assessment_names_handles_non_object_json(self):
		# Valid JSON that isn't an EditorJS envelope must not raise either.
		self.assertEqual(replace_assessment_names(None, "[1, 2]"), "[1, 2]")

	def test_replace_assessment_names_skips_malformed_blocks(self):
		# Valid EditorJS envelope but the blocks are shaped wrong: a non-dict block, and
		# blocks whose `data` is a truthy non-dict / null. This mutate-and-redump path
		# iterates raw blocks (not via get_editorjs_blocks), so it must skip them itself
		# rather than AttributeError on block.get("data", {}).get(...). No DB is reached
		# because no assessment name is extracted.
		content = frappe.as_json(
			{
				"blocks": [
					"a string",
					{"type": "quiz", "data": "x"},
					{"type": "quiz", "data": None},
					{"type": "paragraph", "data": {"text": "ok"}},
				]
			}
		)
		# Round-trips without raising; the well-formed paragraph is preserved.
		result = replace_assessment_names(None, content)
		self.assertIn("paragraph", result)


CDN = "https://cdn.example.com"
CDN_ASSET = f"{CDN}/lms/0123456789abcdef0123456789abcdef.png"


def _response(payload):
	resp = MagicMock()
	resp.read.return_value = payload
	resp.__enter__.return_value = resp
	return resp


class TestExportAssetsOnTheMediaCDN(unittest.TestCase):
	"""Public media now lives on the CDN (lms.wtf_storage), so a File's file_url can be
	an absolute https URL with nothing on disk. Export must fetch those bytes instead of
	calling get_full_path()/zip_file.write(), which raised FileNotFoundError. Fixture-free."""

	def _zip(self, assets):
		buf = io.BytesIO()
		with zipfile.ZipFile(buf, "w") as zf:
			write_assets(zf, assets)
		return zipfile.ZipFile(io.BytesIO(buf.getvalue()))

	def test_cdn_asset_is_downloaded_into_the_zip(self):
		with patch.dict(frappe.conf, {"wtf_media_cdn": CDN}), patch(
			"lms.lms.course_import_export.urlopen", return_value=_response(b"PNGBYTES")
		) as urlopen, patch("frappe.get_doc") as get_doc:
			zf = self._zip([CDN_ASSET])
		self.assertEqual(zf.read("assets/0123456789abcdef0123456789abcdef.png"), b"PNGBYTES")
		self.assertEqual(urlopen.call_args.kwargs.get("timeout"), 30)
		get_doc.assert_not_called()

	def test_cdn_download_failure_skips_the_asset_and_logs(self):
		with patch.dict(frappe.conf, {"wtf_media_cdn": CDN}), patch(
			"lms.lms.course_import_export.urlopen", side_effect=OSError("timed out")
		), patch("frappe.log_error") as log_error:
			zf = self._zip([CDN_ASSET])
		self.assertEqual(zf.namelist(), [])
		log_error.assert_called_once()

	def test_other_remote_urls_are_never_fetched(self):
		# lesson content is author-controlled: fetching any URL would be SSRF (e.g. instance metadata)
		with patch.dict(frappe.conf, {"wtf_media_cdn": CDN}), patch(
			"lms.lms.course_import_export.urlopen"
		) as urlopen, patch("frappe.log_error"):
			zf = self._zip(["http://169.254.169.254/latest/meta-data/x.png", "https://evil.example/a.png"])
		urlopen.assert_not_called()
		self.assertEqual(zf.namelist(), [])

	def test_empty_and_non_string_assets_are_ignored(self):
		with patch.dict(frappe.conf, {"wtf_media_cdn": CDN}), patch(
			"lms.lms.course_import_export.urlopen"
		) as urlopen:
			zf = self._zip([None, "", 42])
		urlopen.assert_not_called()
		self.assertEqual(zf.namelist(), [])

	def test_read_asset_content_fetches_cdn_urls(self):
		with patch.dict(frappe.conf, {"wtf_media_cdn": CDN}), patch(
			"lms.lms.course_import_export.urlopen", return_value=_response(b"PNGBYTES")
		), patch("frappe.get_doc") as get_doc:
			self.assertEqual(read_asset_content(CDN_ASSET), b"PNGBYTES")
		get_doc.assert_not_called()


class TestImportOfCdnExportedAssets(unittest.TestCase):
	"""A zip exported from a CDN-backed site cites https CDN URLs. Import keys privacy
	off the cited URL's base name; a CDN URL is public, so the asset is recreated public
	(and lms.wtf_storage moves it back to the CDN)."""

	def test_cdn_cited_asset_is_public(self):
		buf = io.BytesIO()
		with zipfile.ZipFile(buf, "w") as zf:
			zf.writestr("course.json", json.dumps({"image": CDN_ASSET}))
			zf.writestr("instructors.json", "[]")
			zf.writestr("assets/0123456789abcdef0123456789abcdef.png", b"PNGBYTES")
		with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
			privacy = get_asset_privacy(zf)
		self.assertEqual(privacy, {"0123456789abcdef0123456789abcdef.png": 0})
