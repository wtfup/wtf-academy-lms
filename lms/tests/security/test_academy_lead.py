"""lms.lms.academy_lead.create_lead: the only way the marketing site stores a Career Scorecard lead.

Guest-callable, but only with the shared secret (header X-Academy-Lead-Secret == site_config
academy_lead_secret). It re-validates every field, inserts ONLY an "Academy Lead" and is rate limited.
"""

import json
from types import SimpleNamespace

import frappe
from frappe.tests import IntegrationTestCase

METHOD = "lms.lms.academy_lead.create_lead"
DOCTYPE = "Academy Lead"
SECRET = "IsoTestSecret0123456789abcdefghijklmnopqrstuvwx"  # 48 chars, test-only
HEADER = "X-Academy-Lead-Secret"

# Test-only copy of the project's infra/configure_leads.py DocType (the throwaway site lacks it).
FIELDS = [
	{"fieldname": "full_name", "fieldtype": "Data", "label": "Full name", "reqd": 1},
	{"fieldname": "phone", "fieldtype": "Data", "label": "Phone", "reqd": 1},
	{"fieldname": "email", "fieldtype": "Data", "label": "Email", "options": "Email", "reqd": 1},
	{"fieldname": "city", "fieldtype": "Data", "label": "City"},
	{"fieldname": "source", "fieldtype": "Data", "label": "Source", "default": "scorecard"},
	{"fieldname": "recommended_course", "fieldtype": "Data", "label": "Recommended course"},
	{"fieldname": "answers_json", "fieldtype": "Long Text", "label": "Answers"},
	{"fieldname": "consent", "fieldtype": "Check", "label": "Consent", "reqd": 1},
	{"fieldname": "created_from_ip_hash", "fieldtype": "Data", "label": "IP hash", "read_only": 1},
]
RIGHTS = ("read", "write", "create", "delete", "report", "export", "share", "print", "email")


def _ensure_doctype():
	if frappe.db.exists("DocType", DOCTYPE):
		return False
	d = frappe.new_doc("DocType")
	d.update({"name": DOCTYPE, "module": "Website", "custom": 1, "autoname": "hash"})
	for f in FIELDS:
		d.append("fields", f)
	for role in ("System Manager", "Moderator"):
		if frappe.db.exists("Role", role):
			d.append("permissions", {"role": role, **{r: 1 for r in RIGHTS}})
	d.insert(ignore_permissions=True)
	frappe.db.commit()
	return True


def _valid(**kw):
	return {
		"full_name": "Iso Lead Test",
		"phone": "+91 98765-43210",
		"email": f"qa+lead-iso-{frappe.generate_hash(length=8)}@wtfgymsacademy.com",
		"city": "Pune",
		"recommended_course": "certified-nutritionist",
		"answers_json": json.dumps({"situation": "student", "city": "Pune"}),
		"consent": 1,
		"created_from_ip_hash": "a" * 64,
		**kw,
	}


class TestAcademyLead(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.created = _ensure_doctype()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		for n in frappe.get_all(DOCTYPE, filters={"email": ["like", "qa+lead-iso-%"]}, pluck="name"):
			frappe.delete_doc(DOCTYPE, n, force=True, ignore_permissions=True)
		if cls.created:
			frappe.delete_doc("DocType", DOCTYPE, force=True, ignore_permissions=True)
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		self._conf = frappe.local.conf.get("academy_lead_secret")
		frappe.local.conf.academy_lead_secret = SECRET
		frappe.local.request_ip = "203.0.113.9"
		for k in frappe.cache.get_keys("rl:"):
			frappe.cache.delete(k.decode() if isinstance(k, bytes) else k)
		self._reset_ceiling()

	def _ceiling_key(self):
		from lms.lms import academy_lead

		return frappe.cache.make_key(academy_lead.RATE_KEY)

	def _reset_ceiling(self):
		frappe.cache.delete(self._ceiling_key())

	def tearDown(self):
		frappe.local.conf.academy_lead_secret = self._conf
		frappe.local.request = None
		frappe.set_user("Administrator")

	def _call(self, data, secret=SECRET, header=True):
		"""What /api/method/<METHOD> does: Guest session, request headers, args filtered by frappe.call."""
		headers = {HEADER: secret} if header else {}
		frappe.local.request = SimpleNamespace(headers=headers, method="POST")
		frappe.local.form_dict = frappe._dict({**data, "cmd": METHOD})
		frappe.set_user("Guest")
		try:
			return frappe.call(frappe.get_attr(METHOD), **data)
		finally:
			frappe.set_user("Administrator")

	def _count(self, email):
		return frappe.db.count(DOCTYPE, {"email": email})

	# ---- whitelisting ----
	def test_is_guest_post_only(self):
		fn = frappe.get_attr(METHOD)
		self.assertIn(fn, frappe.guest_methods)
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[fn], ["POST"])

	def test_ceiling_is_a_global_key_of_120_per_minute(self):
		from lms.lms import academy_lead

		self.assertEqual(academy_lead.RATE_KEY, "academy_lead:global")
		self.assertEqual((academy_lead.RATE_LIMIT, academy_lead.RATE_SECONDS), (120, 60))

	def test_unauthenticated_calls_do_not_consume_the_allowance(self):
		from lms.lms import academy_lead

		d = _valid()
		for i in range(200):
			with self.assertRaises(frappe.PermissionError):
				if i % 2:
					self._call(d, header=False)
				else:
					self._call(d, secret="wrong")
		self.assertFalse(frappe.cache.get(self._ceiling_key()))
		# fill the authenticated allowance to one below the ceiling: the next good call still lands
		frappe.cache.setex(self._ceiling_key(), 60, academy_lead.RATE_LIMIT - 1)
		self.assertEqual(self._call(d), {"ok": True})
		self.assertEqual(self._count(d["email"]), 1)

	def test_authenticated_ceiling(self):
		from lms.lms import academy_lead

		frappe.cache.setex(self._ceiling_key(), 60, academy_lead.RATE_LIMIT)
		d = _valid()
		with self.assertRaises(frappe.RateLimitExceededError):
			self._call(d)
		self.assertEqual(self._count(d["email"]), 0)
		# the ceiling counter does not depend on the caller IP
		frappe.local.request_ip = "198.51.100.77"
		with self.assertRaises(frappe.RateLimitExceededError):
			self._call(_valid())

	def test_authenticated_calls_count_towards_the_ceiling_with_an_expiry(self):
		self._call(_valid())
		self._call(_valid())
		self.assertEqual(int(frappe.cache.get(self._ceiling_key())), 2)
		ttl = frappe.cache.ttl(self._ceiling_key())
		self.assertTrue(0 < ttl <= 60, ttl)

	def test_a_counter_without_expiry_is_repaired(self):
		frappe.cache.incrby(self._ceiling_key(), 5)  # no ttl
		self._call(_valid())
		self.assertTrue(0 < frappe.cache.ttl(self._ceiling_key()) <= 60)

	# ---- secret ----
	def test_missing_header_is_403(self):
		d = _valid()
		with self.assertRaises(frappe.PermissionError):
			self._call(d, header=False)
		self.assertEqual(self._count(d["email"]), 0)

	def test_wrong_secret_is_403(self):
		d = _valid()
		for bad in ("", "x", SECRET[:-1], SECRET + "x", SECRET.lower()):
			with self.assertRaises(frappe.PermissionError, msg=repr(bad)):
				self._call(d, secret=bad)
		self.assertEqual(self._count(d["email"]), 0)

	def test_unconfigured_site_refuses_even_an_empty_header(self):
		frappe.local.conf.academy_lead_secret = None
		d = _valid()
		for s in ("", "None"):
			with self.assertRaises(frappe.PermissionError):
				self._call(d, secret=s)
		self.assertEqual(self._count(d["email"]), 0)

	def _error(self, **kw):
		try:
			self._call(_valid(), **kw)
		except frappe.PermissionError as e:
			return (type(e), str(e), frappe.local.message_log[-1] if frappe.local.message_log else None)
		self.fail("expected PermissionError")

	def test_same_403_configured_or_not_and_compare_digest_always_runs(self):
		from unittest.mock import patch

		from lms.lms import academy_lead

		frappe.local.message_log = []
		configured = self._error(secret="wrong")
		frappe.local.message_log = []
		frappe.local.conf.academy_lead_secret = None
		with patch.object(academy_lead.hmac, "compare_digest", wraps=academy_lead.hmac.compare_digest) as cd:
			unconfigured = self._error(secret="wrong")
			missing = self._error(header=False)
		self.assertEqual(configured[:2], unconfigured[:2])
		self.assertEqual(unconfigured[:2], missing[:2])
		self.assertEqual(cd.call_count, 2, "compare_digest must run against a dummy when the secret is unset")

	# ---- golden path ----
	def test_valid_call_inserts_exactly_one_lead(self):
		d = _valid()
		out = self._call(d)
		self.assertEqual(out, {"ok": True})
		rows = frappe.get_all(DOCTYPE, filters={"email": d["email"]}, fields=["*"])
		self.assertEqual(len(rows), 1)
		r = rows[0]
		self.assertEqual(r.phone, "919876543210")
		self.assertEqual(r.source, "scorecard")
		self.assertEqual(r.full_name, "Iso Lead Test")
		self.assertEqual(r.recommended_course, "certified-nutritionist")
		self.assertEqual(r.consent, 1)
		self.assertEqual(json.loads(r.answers_json), {"situation": "student", "city": "Pune"})
		self.assertEqual(r.created_from_ip_hash, "a" * 64)

	def test_bidi_and_format_controls_are_stripped_from_name_and_city(self):
		marks = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f\u061c"
		d = _valid(full_name=f"Iso{marks} Lead\u202e Test", city=f"\u2067Pu{marks}ne")
		self._call(d)
		row = frappe.db.get_value(DOCTYPE, {"email": d["email"]}, ["full_name", "city"], as_dict=True)
		self.assertEqual((row.full_name, row.city), ("Iso Lead Test", "Pune"))

	def test_source_cannot_be_overridden(self):
		d = _valid(source="evil")
		self._call(d)
		self.assertEqual(frappe.db.get_value(DOCTYPE, {"email": d["email"]}, "source"), "scorecard")

	# ---- validation ----
	def test_invalid_fields_insert_nothing(self):
		cases = {
			"consent missing": {"consent": 0},
			"consent text": {"consent": "yes"},
			"short name": {"full_name": "a"},
			"long name": {"full_name": "a" * 141},
			"control char": {"full_name": "Iso\x00Lead"},
			"bad phone": {"phone": "12345"},
			"foreign phone": {"phone": "+1 415 555 0100"},
			"bad email": {"email": "x@"},
			"two emails comma": {"email": "qa+a@wtfgymsacademy.com,evil@example.com"},
			"two emails semicolon": {"email": "qa+a@wtfgymsacademy.com;evil@example.com"},
			"email with space": {"email": "qa+a@wtfgymsacademy.com evil@example.com"},
			"two at signs": {"email": "qa+a@evil@wtfgymsacademy.com"},
			"long city": {"city": "x" * 61},
			"bad course": {"recommended_course": "<script>"},
			"long course": {"recommended_course": "a" * 141},
			"answers too big": {"answers_json": json.dumps({"city": "x" * 4100})},
			"answers not json": {"answers_json": "{nope"},
			"answers not object": {"answers_json": "[1,2]"},
			"bad ip hash": {"created_from_ip_hash": "not-a-hash"},
		}
		for label, override in cases.items():
			d = _valid(**override)
			before = frappe.db.count(DOCTYPE)
			with self.assertRaises(frappe.ValidationError, msg=label):
				self._call(d)
			self.assertEqual(frappe.db.count(DOCTYPE), before, label)

	# ---- scope ----
	def test_cannot_insert_any_other_doctype(self):
		marker = f"qa-lead-iso-evil-{frappe.generate_hash(length=8)}"
		counts = {dt: frappe.db.count(dt) for dt in ("ToDo", "Note", "Contact", "File", "User")}
		for dt in counts:
			d = _valid(doctype=dt, description=marker, title=marker, first_name=marker)
			self._call(d)
		for dt, n in counts.items():
			self.assertEqual(frappe.db.count(dt), n, f"create_lead created a {dt}")

	def test_guest_cannot_read_leads(self):
		d = _valid()
		self._call(d)
		frappe.set_user("Guest")
		try:
			self.assertFalse(frappe.has_permission(DOCTYPE, "read"))
			with self.assertRaises(frappe.PermissionError):
				frappe.get_list(DOCTYPE, fields=["email"])
		finally:
			frappe.set_user("Administrator")

	def test_returns_nothing_about_the_row(self):
		out = self._call(_valid())
		self.assertEqual(set(out), {"ok"})
