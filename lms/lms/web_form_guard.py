"""WTF: safe web form submit.

The payments app overrides `frappe.website.doctype.web_form.web_form.accept` with
`payments.overrides.payment_webform.accept`, which inserts `data.doctype` (taken from the request)
with ignore_permissions and never checks the Web Form's own doc_type or published flag. Through any
public web form, a Guest could create records of any doctype.

LMS is installed after payments, so its `override_whitelisted_methods` entry is the last one and
wins (frappe.override_whitelisted_method returns overrides[-1]). Both the core cmd and the payments
cmd resolve here. This wrapper pins the doctype to the Web Form record, then delegates to Frappe
core's accept (which also pins the doctype, checks published/login_required/allow_edit and only
sets the form's own fields). Payment web forms are delegated to payments with the pinned doctype.
"""

import json

import frappe
from frappe import _
from frappe.website.doctype.web_form.web_form import accept as core_accept


@frappe.whitelist(methods=["POST", "PUT"], allow_guest=True)
def accept(
	web_form: str,
	data: str | dict,
	web_form_request_key: str | None = None,
	docname: str | None = None,
	for_payment: bool | str = False,
):
	wf = frappe.get_doc("Web Form", web_form)
	wf.raise_if_unpublished()

	if wf.login_required and frappe.session.user == "Guest":
		frappe.throw(_("You must login to use this form"), frappe.PermissionError)

	payload = frappe._dict(json.loads(data) if isinstance(data, str) else data)
	if payload.get("doctype") and payload.doctype != wf.doc_type:
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	payload.doctype = wf.doc_type
	if docname and not payload.get("name"):
		payload.name = docname

	if frappe.parse_json(for_payment) or wf.get("accept_payment"):
		if not wf.get("accept_payment"):
			frappe.throw(_("Not permitted"), frappe.PermissionError)
		from payments.overrides.payment_webform import accept as payments_accept

		return payments_accept(web_form=web_form, data=json.dumps(payload), for_payment=for_payment)

	return core_accept(web_form=web_form, data=json.dumps(payload), web_form_request_key=web_form_request_key)
