// Copyright (c) 2026, kalvyai and contributors
// For license information, please see license.txt

frappe.listview_settings["KI AI Usage Log"] = {
	add_fields: ["status"],
	get_indicator(doc) {
		const colors = { Done: "green", "Temporary Failure": "orange", Error: "red" };
		return [__(doc.status), colors[doc.status], `status,=,${doc.status}`];
	},
};
