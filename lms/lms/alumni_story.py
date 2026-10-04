"""WTF: publication safety for the custom "Alumni Story" DocType (share-your-story).

The DocType is configuration (the project's infra/configure_testimonials.py); these doc_events guard it:

- validate: ticking learner_approved_final needs a date, a channel and the final text, and stores
  approved_text_hash = sha256(final_headline + "\\n" + final_story). If the final text changes after the learner
  approved it, the approval is cleared (the learner must approve the new text). The project's
  scripts/content/export_testimonials.py publishes only when the stored hash matches the current text.
- on_update: the guest's photo (saved PRIVATE by lms.lms.web_form_guard) becomes public, and goes to the CDN
  through the storage hook, only when the story is Approved, learner-approved and the learner allowed name
  and photo. A Rejected story deletes its photo. (Deleting the story deletes its attachments: Frappe core.)
"""

import hashlib

import frappe
from frappe import _

DOCTYPE = "Alumni Story"


def text_hash(headline, story) -> str:
	return hashlib.sha256(f"{(headline or '').strip()}\n{(story or '').strip()}".encode()).hexdigest()


def validate(doc, method=None):
	current = text_hash(doc.get("final_headline"), doc.get("final_story"))
	if not doc.get("learner_approved_final"):
		doc.approved_text_hash = ""
		return
	before = doc.get_doc_before_save()
	was_approved = bool(before and before.get("learner_approved_final"))
	if was_approved and text_hash(before.get("final_headline"), before.get("final_story")) != current:
		# The text the learner approved is not the text being saved: the approval is stale.
		doc.learner_approved_final = 0
		doc.learner_approved_on = None
		doc.approved_text_hash = ""
		frappe.msgprint(_("The final text changed after the learner approved it, so the approval was cleared. "
						  "Send the new text to the learner and tick the approval again once they confirm it."))
		return
	if not (doc.get("final_headline") or "").strip() or not (doc.get("final_story") or "").strip():
		frappe.throw(_("Write the final headline and story before ticking the learner's approval."), frappe.ValidationError)
	if not doc.get("learner_approved_on") or not doc.get("approval_channel"):
		frappe.throw(_("Fill in when and how the learner approved the final text."), frappe.ValidationError)
	if not was_approved or not doc.get("approved_text_hash"):
		doc.approved_text_hash = current


def _photo_file(doc):
	url = doc.get("photo")
	if not url:
		return None
	name = frappe.db.get_value("File", {"file_url": url, "attached_to_doctype": DOCTYPE, "attached_to_name": doc.name})
	return frappe.get_doc("File", name) if name else None


def on_update(doc, method=None):
	if doc.get("status") == "Rejected":
		for name in frappe.get_all("File", filters={"attached_to_doctype": DOCTYPE, "attached_to_name": doc.name}, pluck="name"):
			frappe.delete_doc("File", name, ignore_permissions=True, force=True)
		if doc.get("photo"):
			doc.db_set("photo", "", update_modified=False)
		return
	publishable = doc.get("status") == "Approved" and doc.get("learner_approved_final") and doc.get("publish_with_name_photo")
	if not publishable:
		return
	f = _photo_file(doc)
	if not f or not f.is_private:
		return
	f.is_private = 0
	f.save(ignore_permissions=True)  # Frappe moves it to public/files
	from lms.wtf_storage import upload_public_file

	upload_public_file(f)  # public media -> S3/CDN (no-op when the media CDN is not configured)
	doc.db_set("photo", f.file_url, update_modified=False)
