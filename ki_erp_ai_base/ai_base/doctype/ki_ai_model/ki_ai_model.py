# Copyright (c) 2026, kalvyai and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from ki_erp_ai_base.providers import PROVIDER_KINDS


class KIAIModel(Document):
	"""A model offered by one provider type, as listed by its models API. Fills the Model dropdown
	of KI AI Provider."""

	def validate(self):
		duplicate = frappe.db.exists(
			"KI AI Model",
			{"provider_type": self.provider_type, "model_name": self.model_name, "name": ["!=", self.name]},
		)
		if duplicate:
			frappe.throw(_("This model is already listed for this provider."), frappe.DuplicateEntryError)


def on_doctype_update():
	frappe.db.add_unique("KI AI Model", ["provider_type", "model_name"], constraint_name="unique_model")


def ensure_models(provider_type, names):
	"""Return the options for ``names`` under ``provider_type``, creating missing ones."""
	names = {name for name in names if name}
	existing = set(
		frappe.get_all(
			"KI AI Model",
			filters={"provider_type": provider_type, "model_name": ["in", list(names)]},
			pluck="model_name",
		)
		if names
		else []
	)
	for name in sorted(names - existing):
		frappe.get_doc({"doctype": "KI AI Model", "provider_type": provider_type, "model_name": name}).insert(
			ignore_permissions=True
		)
	return names


def seed_models():
	"""List each provider type's default model and every model in use."""
	for provider_type, spec in PROVIDER_KINDS.items():
		ensure_models(provider_type, [spec["model"]])
	for provider in frappe.get_all("KI AI Provider", fields=["provider_type", "model_name"]):
		ensure_models(provider.provider_type, [provider.model_name])
