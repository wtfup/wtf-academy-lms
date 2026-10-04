"""Test-only: the custom "Alumni Story" DocType (configured by the project's infra/configure_testimonials.py)
recreated with the fields the fork's guard and hooks rely on, so the fork tests run on a throwaway site."""

import frappe

DOCTYPE = "Alumni Story"
FIELDS = [
	{"fieldname": "full_name", "label": "Full name", "fieldtype": "Data"},
	{"fieldname": "photo", "label": "Photo", "fieldtype": "Attach Image"},
	{"fieldname": "doc_file", "label": "Test-only attach", "fieldtype": "Attach"},
	{"fieldname": "consent", "label": "Consent", "fieldtype": "Select", "options": "\nYes"},
	{"fieldname": "publish_with_name_photo", "label": "Name and photo", "fieldtype": "Check"},
	{"fieldname": "status", "label": "Status", "fieldtype": "Select", "options": "Pending\nApproved\nRejected", "default": "Pending"},
	{"fieldname": "final_headline", "label": "Final headline", "fieldtype": "Data"},
	{"fieldname": "final_story", "label": "Final story", "fieldtype": "Long Text"},
	{"fieldname": "learner_approved_final", "label": "Approved final", "fieldtype": "Check"},
	{"fieldname": "learner_approved_on", "label": "Approved on", "fieldtype": "Date"},
	{"fieldname": "approval_channel", "label": "Approved via", "fieldtype": "Select", "options": "\nWhatsApp\nEmail"},
	{"fieldname": "approved_text_hash", "label": "Approved text hash", "fieldtype": "Data", "read_only": 1},
]


def ensure_alumni_story() -> bool:
	"""Create the DocType if this site lacks it. Returns True when created (caller deletes it)."""
	if frappe.db.exists("DocType", DOCTYPE):
		return False
	d = frappe.new_doc("DocType")
	d.update({"name": DOCTYPE, "module": "Website", "custom": 1, "autoname": "hash"})
	for f in FIELDS:
		d.append("fields", f)
	d.append("permissions", {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1})
	d.insert(ignore_permissions=True)
	frappe.db.commit()
	return True


def drop_alumni_story(created: bool) -> None:
	if created:
		for n in frappe.get_all(DOCTYPE, pluck="name"):
			frappe.delete_doc(DOCTYPE, n, force=True, ignore_permissions=True)
		frappe.delete_doc("DocType", DOCTYPE, force=True, ignore_permissions=True)
		frappe.db.commit()
