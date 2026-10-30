"""Usage log rows that outlive the caller's transaction.

A call is billed by the provider whatever happens next, but when the request or job that made it
fails, Frappe rolls back everything it wrote, the usage row included, and the tokens would no
longer count against the monthly cap. The rows written in a transaction are kept in memory until it
ends: on commit they are forgotten, on rollback a background job writes them again.

Rows written inside a savepoint that is rolled back (``frappe.db.rollback(save_point=...)``) are
lost: Frappe runs no callback for it.
"""

import frappe

DOCTYPE = "KI AI Usage Log"


def record(values):
	"""Insert a usage row and keep it if the current transaction is rolled back."""
	log = frappe.get_doc({"doctype": DOCTYPE, **values}).insert(ignore_permissions=True)
	unsaved = getattr(frappe.local, "ki_ai_unsaved_logs", None)
	if unsaved is None:
		# First row of this transaction. Commit resets the rollback callbacks and rollback the
		# commit ones, so exactly one of the two runs.
		unsaved = frappe.local.ki_ai_unsaved_logs = []
		frappe.db.after_commit.add(_forget)
		frappe.db.after_rollback.add(_requeue)
	unsaved.append(log.as_dict(convert_dates_to_str=True))
	return log


def _forget():
	frappe.local.ki_ai_unsaved_logs = None


def _requeue():
	rows = getattr(frappe.local, "ki_ai_unsaved_logs", None)
	frappe.local.ki_ai_unsaved_logs = None
	# Committing here would drop the other rollback callbacks queued after this one, so the rows
	# are written by a job. Tests roll back on purpose: their rows must not reach the site.
	if rows and not frappe.flags.in_test:
		frappe.enqueue("ki_erp_ai_base.usage.restore", queue="short", rows=rows)


def restore(rows):
	"""Write again the usage rows of a rolled-back transaction, with their original name and time."""
	for row in rows:
		if not frappe.db.exists(DOCTYPE, row["name"]):
			frappe.get_doc(row).db_insert()
