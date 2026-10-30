import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from ki_erp_ai_base.ai_base.doctype.ki_ai_model.ki_ai_model import seed_models

ROLES = ("AI Manager", "AI User")

# Company scoping, added only when ERPNext (which defines Company) is installed: the base app needs
# nothing but Frappe. User Permissions on Company then restrict providers and usage like any
# ERPNext document.
COMPANY_FIELDS = {
	"KI AI Provider": [
		{
			"fieldname": "company",
			"fieldtype": "Link",
			"label": "Company",
			"options": "Company",
			"insert_after": "last_health_check",
			"in_standard_filter": 1,
			"description": "Leave empty to share the provider with every company.",
		}
	],
	"KI AI Usage Log": [
		{
			"fieldname": "company",
			"fieldtype": "Link",
			"label": "Company",
			"options": "Company",
			"insert_after": "reference_name",
			"in_standard_filter": 1,
			"read_only": 1,
		}
	],
}


def before_install():
	make_roles()


def after_install():
	after_migrate()


def after_migrate():
	make_roles()
	seed_models()
	if "erpnext" in frappe.get_installed_apps():
		create_custom_fields(COMPANY_FIELDS, ignore_validate=True)


def after_app_install(app_name):
	if app_name == "erpnext":
		create_custom_fields(COMPANY_FIELDS, ignore_validate=True)


def make_roles():
	for role in ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
				ignore_permissions=True
			)
