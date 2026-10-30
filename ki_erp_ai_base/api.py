"""What other apps import from AI Base.

from ki_erp_ai_base.api import AIError, generate

result, usage = generate("You classify support tickets.", text=ticket.description,
                         json_schema=SCHEMA, feature="ticket_triage", reference=ticket)
"""

import frappe
from frappe import _

from ki_erp_ai_base.providers import (
	PROVIDER_KINDS,
	AIError,
	BudgetExceeded,
	FileNotSupported,
	ProviderBusy,
	prepare_file,
)

__all__ = [
	"PROVIDER_KINDS",
	"AIError",
	"BudgetExceeded",
	"FileNotSupported",
	"ProviderBusy",
	"generate",
	"get_providers",
	"prepare_file",
]


def get_providers(company=None):
	"""Enabled providers in priority order: those of ``company`` and those shared by every company.

	Permissions are not checked: the calling app decides who may use AI.
	"""
	or_filters = None
	if company and frappe.get_meta("KI AI Provider").has_field("company"):
		or_filters = [["company", "=", company], ["company", "is", "not set"]]
	names = frappe.get_all(
		"KI AI Provider",
		filters={"enabled": 1},
		or_filters=or_filters,
		pluck="name",
		order_by="priority asc, creation asc",
	)
	return [frappe.get_doc("KI AI Provider", name) for name in names]


def generate(instructions, company=None, providers=None, **kwargs):
	"""Call the first provider that answers and return ``(result, usage)``.

	Providers are tried in priority order (or the order of ``providers``). One that is busy, has
	spent its monthly cap or cannot read the files is skipped for the next. Other arguments are
	those of ``KIAIProvider.generate``; ``usage`` also gives the ``provider`` name.

	:raise AIError: no provider configured, a permanent failure, or the last provider's error when
	        none could answer.
	"""
	if providers is None:
		providers = get_providers(company)
	if not providers:
		raise AIError(_("No AI provider is configured. Add one in KI AI Provider."))
	for provider in providers:
		try:
			result, usage = provider.generate(instructions, **kwargs)
		except (ProviderBusy, BudgetExceeded, FileNotSupported) as error:
			skipped = error
			continue
		return result, dict(usage, provider=provider.name)
	raise skipped
