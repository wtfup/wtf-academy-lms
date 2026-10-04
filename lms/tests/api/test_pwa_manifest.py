import json
import os

import frappe
from PIL import Image

from lms.lms.api import get_pwa_manifest
from lms.lms.test_helpers import BaseTestUtils


def _manifest():
	response = get_pwa_manifest()
	return json.loads(response.get_data(as_text=True))


class TestPWAManifest(BaseTestUtils):
	def test_served_as_a_manifest(self):
		response = get_pwa_manifest()

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.headers["Content-Type"], "application/manifest+json")

	def test_declares_standalone_display(self):
		# Without this the installed app opens inside full browser chrome.
		self.assertEqual(_manifest()["display"], "standalone")

	def test_scope_and_id_track_the_lms_route(self):
		manifest = _manifest()
		self.assertEqual(manifest["scope"], manifest["start_url"])
		self.assertEqual(manifest["id"], manifest["start_url"])

	def test_declares_colours_for_the_os_chrome(self):
		manifest = _manifest()
		self.assertEqual(manifest["theme_color"], "#0D0D0D")
		self.assertEqual(manifest["background_color"], "#FFFFFF")

	def test_ships_both_icon_sizes(self):
		sizes = {icon["sizes"] for icon in _manifest()["icons"]}
		self.assertEqual(sizes, {"192x192", "512x512"})

	def test_declared_sizes_match_the_real_square_pngs(self):
		for icon in _manifest()["icons"]:
			declared = tuple(int(n) for n in icon["sizes"].split("x"))
			self.assertEqual(declared[0], declared[1], "declared size must be square")
			filename = icon["src"].rsplit("/", 1)[-1]
			path = os.path.join(
				frappe.get_app_path("lms"), "..", "frontend", "public", "manifest", filename
			)
			with Image.open(path) as img:
				self.assertEqual(img.size, declared, filename)

	def test_icons_are_the_wtf_icons_not_website_settings_images(self):
		for icon in _manifest()["icons"]:
			self.assertRegex(icon["src"], r"^/assets/lms/frontend/manifest/wtf-icon-(192|512)\.png$")
			self.assertEqual(icon["purpose"], "any")

	def test_banner_image_is_not_used_as_an_icon(self):
		"""It is a wide banner, and was being declared as a 192x192 square."""
		# Restore through set_single_value, not a bare write: it clears the document
		# cache on the way out, which a savepoint rollback does not do.
		original = frappe.db.get_single_value("Website Settings", "banner_image")
		self.addCleanup(frappe.db.set_single_value, "Website Settings", "banner_image", original)
		frappe.db.set_single_value("Website Settings", "banner_image", "/files/wide-banner.png")

		sources = [icon["src"] for icon in _manifest()["icons"]]

		self.assertNotIn("/files/wide-banner.png", sources)

	def test_description_is_the_academy_one_not_upstream(self):
		description = _manifest()["description"]

		self.assertEqual(description, "WTF Academy Online: fitness education courses by WTF Gyms.")
		self.assertNotIn("open source", description)

	def test_name_follows_website_settings(self):
		original = frappe.db.get_single_value("Website Settings", "app_name")
		self.addCleanup(frappe.db.set_single_value, "Website Settings", "app_name", original)
		frappe.db.set_single_value("Website Settings", "app_name", "Acme Academy")

		manifest = _manifest()

		self.assertEqual(manifest["name"], "Acme Academy")
		self.assertEqual(manifest["short_name"], "Acme Academy")
