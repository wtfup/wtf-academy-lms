"""Browser Meta pixel / GA4 on the LMS SPA page (lms/templates/wtf_tracking.html).

Rendered with a plain Jinja environment (same built-in `tojson` filter Frappe's env has), so
these run without a site as well as under `bench run-tests`.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

import frappe
from jinja2 import Environment, FileSystemLoader

import lms

APP_DIR = os.path.dirname(lms.__file__)
REPO_DIR = os.path.dirname(APP_DIR)

PURCHASE = {
	"event_id": "purchase:PAY-0001",
	"transaction_id": "PAY-0001",
	"value": 4999.0,
	"currency": "INR",
	"content_ids": ["sports-nutrition"],
	"content_type": "product",
	"content_name": "Sports </script><script>alert(1)</script> Nutrition",
}


def render(wtf_tracking):
	env = Environment(loader=FileSystemLoader(APP_DIR))
	return env.get_template("templates/wtf_tracking.html").render(wtf_tracking=wtf_tracking)


class TestTrackingTemplate(unittest.TestCase):
	def test_renders_nothing_when_not_configured(self):
		self.assertEqual(render({}).strip(), "")
		self.assertEqual(render(None).strip(), "")

	def test_pixel_base_inits_and_tracks_page_view(self):
		html = render({"pixel_id": "2804119553060376", "ga4_id": None, "purchase": None})
		self.assertIn("connect.facebook.net/en_US/fbevents.js", html)
		self.assertIn("fbq('init', \"2804119553060376\")", html)
		self.assertIn("fbq('track', 'PageView')", html)
		self.assertNotIn("Purchase", html)
		self.assertNotIn("googletagmanager", html)

	def test_purchase_fires_with_shared_event_id(self):
		html = render({"pixel_id": "2804119553060376", "ga4_id": None, "purchase": PURCHASE})
		self.assertIn("fbq('track', 'Purchase', {", html)
		self.assertIn('{eventID: "purchase:PAY-0001"}', html)
		self.assertIn('"value": 4999.0', html)
		self.assertIn('"currency": "INR"', html)
		self.assertIn('"content_ids": ["sports-nutrition"]', html)
		self.assertIn('"content_type": "product"', html)

	def test_values_cannot_break_out_of_the_script(self):
		html = render({"pixel_id": "1", "ga4_id": "G-1", "purchase": PURCHASE})
		self.assertNotIn("</script><script>alert(1)", html)

	def test_ga4_base_and_purchase(self):
		html = render({"pixel_id": None, "ga4_id": "G-ABC123", "purchase": PURCHASE})
		self.assertIn("googletagmanager.com/gtag/js?id=G-ABC123", html)
		self.assertIn("gtag('config', \"G-ABC123\")", html)
		self.assertIn("gtag('event', 'purchase', {", html)
		self.assertIn('"transaction_id": "PAY-0001"', html)
		self.assertIn('"item_id": "sports-nutrition"', html)
		self.assertNotIn("fbevents.js", html)


class TestSpaIncludesTracking(unittest.TestCase):
	def test_frontend_index_includes_the_fork_template_in_head(self):
		with open(os.path.join(REPO_DIR, "frontend", "index.html")) as f:
			html = f.read()
		head = html.split("</head>")[0]
		self.assertTrue(
			'{% include "templates/wtf_tracking.html" %}' in head,
			"frontend/index.html <head> must include templates/wtf_tracking.html",
		)

	def test_lms_page_context_carries_browser_tracking(self):
		from lms.www import _lms

		tracking = {"pixel_id": "1", "ga4_id": None, "purchase": None}
		with (
			patch.object(_lms, "get_boot", return_value={}),
			patch.object(_lms, "get_meta", return_value={}),
			patch.object(_lms, "capture"),
			patch.object(frappe, "db", MagicMock()),
			patch.object(frappe, "form_dict", frappe._dict(), create=True),
			patch("lms.wtf_meta.get_browser_tracking", return_value=tracking),
		):
			context = _lms.get_context()
		self.assertEqual(context.wtf_tracking, tracking)
