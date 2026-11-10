import csv
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_TEST_ROOT = tempfile.mkdtemp()
os.environ["LOCALAPPDATA"] = _TEST_ROOT
os.environ["OPERATIONS_TOOLKIT_PREVIEW"] = "1"

import cnmaestro_speed_manager as toolkit
from association_service import UNASSIGNED_LABEL, build_association_report

GENERATED = datetime(2026, 11, 9, 14, 2, 31, tzinfo=timezone.utc)


def sample_report():
    return build_association_report(
        sm_rows=[
            {"mac": "11:22:33:44:55:01", "name": "10014 Johnson", "ap_mac": "AA:BB:CC:00:00:01", "status": "online", "status_time": 120, "network": "Rural", "distance": 3.42, "type": "pmp", "mode": "sm", "last_sync": "2026-11-09T14:01:00+00:00"},
            {"mac": "11:22:33:44:55:02", "name": "10015 Flores", "ap_mac": "AA:BB:CC:00:00:01", "status": "offline", "status_time": 90000, "network": "Rural", "distance": 7.1, "type": "pmp", "mode": "sm"},
            {"mac": "11:22:33:44:55:03", "name": "10016 Diaz", "ap_mac": "", "status": "online", "status_time": 60, "network": "Rural", "type": "pmp", "mode": "sm"},
        ],
        ap_rows=[
            {"mac": "AA:BB:CC:00:00:01", "name": "North Sector A", "network": "Rural", "tower": "North", "status": "online", "status_time": 50000, "connected_sms": 2, "type": "pmp", "mode": "ap"},
            {"mac": "AA:BB:CC:00:00:02", "name": "South Sector B", "network": "Rural", "tower": "South", "status": "offline", "status_time": 900000, "connected_sms": 0, "type": "pmp", "mode": "ap"},
        ],
        generated=GENERATED,
    )


class AssociationUiTests(unittest.TestCase):
    def setUp(self):
        self.app = toolkit.App()
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        self.app.destroy()

    def seed(self, report=None):
        self.app.association_report = report or sample_report()
        self.app.render_associations()
        self.app.update()

    def test_ap_associations_sits_under_cnmaestro_service_group(self):
        hierarchy = [
            (service["label"], [(tool["key"], tool["label"]) for tool in service["tools"]])
            for service in self.app.service_navigation
        ]
        self.assertEqual(hierarchy, [
            ("cnMaestro", [("speed_manager", "Speed Manager"), ("ap_associations", "AP Associations")]),
            ("Mosaic", [("mosaic_speed_test", "Speed Test")]),
        ])
        self.assertEqual(
            set(self.app.nav_items),
            {"overview", "speed_manager", "ap_associations", "mosaic_speed_test", "audit", "settings"},
        )
        self.assertIn("ap_associations", self.app.pages)

    def test_page_opens_in_app_with_the_report_columns(self):
        self.app.deiconify()
        self.app.show_page("ap_associations")
        self.app.update()
        self.assertEqual(self.app.active_page, "ap_associations")
        self.assertTrue(self.app.pages["ap_associations"].winfo_ismapped())
        self.assertEqual(tuple(self.app.association_tree["columns"]), (
            "name", "mac", "kind", "network", "tower", "sms", "reported", "state", "age", "distance",
        ))
        self.assertEqual([child for child in self.app.winfo_children() if child.winfo_class() == "Toplevel"], [])

    def test_report_renders_ap_groups_with_sm_children(self):
        self.seed()
        parents = self.app.association_tree.get_children()
        self.assertEqual(len(parents), 3)
        values = self.app.association_tree.item(parents[0], "values")
        self.assertEqual(values[:6], ("North Sector A", "AA:BB:CC:00:00:01", "AP", "Rural", "North", "2"))
        self.assertEqual(values[6], "2 ⚠")
        self.assertEqual([len(self.app.association_tree.get_children(parent)) for parent in parents], [2, 0, 1])
        child = self.app.association_tree.item(self.app.association_tree.get_children(parents[0])[0], "values")
        self.assertEqual(child[:3], ("10014 Johnson", "11:22:33:44:55:01", "SM"))
        self.assertEqual(child[7], "Online")
        self.assertEqual(child[9], "3.42 km")

    def test_unassigned_bucket_is_visible_and_last(self):
        self.seed()
        parents = self.app.association_tree.get_children()
        values = self.app.association_tree.item(parents[-1], "values")
        self.assertEqual(values[0], UNASSIGNED_LABEL)
        self.assertEqual(values[2], "-")
        self.assertEqual(values[1], "")

    def test_search_narrows_the_tree_without_losing_the_ap_row(self):
        self.seed()
        self.app.association_query.set("flores")
        self.app.update()
        parents = self.app.association_tree.get_children()
        self.assertEqual(len(parents), 1)
        children = self.app.association_tree.get_children(parents[0])
        self.assertEqual(len(children), 1)
        self.assertEqual(self.app.association_tree.item(children[0], "values")[0], "10015 Flores")
        self.app.association_query.set("")
        self.app.update()
        self.assertEqual(len(self.app.association_tree.get_children()), 3)

    def test_online_only_hides_offline_sms_but_keeps_every_ap(self):
        self.seed()
        self.app.association_online_only.set(True)
        self.app.render_associations()
        self.app.update()
        parents = self.app.association_tree.get_children()
        self.assertEqual(len(parents), 3)
        self.assertEqual([len(self.app.association_tree.get_children(parent)) for parent in parents], [1, 0, 1])

    def test_no_report_means_an_empty_table_and_hint_status(self):
        self.assertIsNone(self.app.association_report)
        self.assertEqual(self.app.association_tree.get_children(), ())
        self.assertIn("Not loaded", self.app.association_status.get())

    def test_finish_sets_summary_status_and_restores_the_refresh_button(self):
        self.app.finish_associations(sample_report(), None)
        self.app.update()
        self.assertIn("2 APs", self.app.association_status.get())
        self.assertIn("2 assigned", self.app.association_status.get())
        self.assertIn("2026-11-09 14:02:31 UTC", self.app.association_status.get())
        self.assertEqual(str(self.app.association_refresh.cget("state")), "normal")
        self.assertEqual(str(self.app.association_cancel_button.cget("state")), "disabled")
        self.assertEqual(len(self.app.association_tree.get_children()), 3)

    def test_cancellation_is_reported_as_status_not_an_error_dialog(self):
        with mock.patch.object(toolkit.messagebox, "showerror") as error:
            self.app.finish_associations(None, toolkit.AssociationReportCancelled("cancelled"))
        error.assert_not_called()
        self.assertEqual(self.app.association_status.get(), "Report cancelled.")
        self.assertEqual(str(self.app.association_refresh.cget("state")), "normal")

    def test_refresh_failure_is_surfaced(self):
        with mock.patch.object(toolkit.messagebox, "showerror") as error:
            self.app.finish_associations(None, RuntimeError("401 Unauthorized"))
        error.assert_called_once()
        self.assertIn("401 Unauthorized", self.app.association_status.get())

    def test_refresh_without_a_connection_explains_the_problem(self):
        self.app.api = None
        with mock.patch.object(toolkit.messagebox, "showerror") as error, \
                mock.patch.object(toolkit, "fetch_association_report") as fetch:
            self.app.load_associations()
        error.assert_called_once()
        self.assertIn("Connect", error.call_args.args[1])
        fetch.assert_not_called()

    def test_refresh_runs_off_the_ui_thread_and_renders_on_completion(self):
        async def fake_fetch(api, progress=None, cancel=None):
            if progress:
                progress("AP statistics", 1, 2)
                progress("SM statistics", 2, 3)
            return sample_report()

        self.app.api = mock.Mock()
        with mock.patch.object(toolkit, "fetch_association_report", fake_fetch):
            self.app.load_associations()
            self.assertTrue(self.app.association_running)
            deadline = time.time() + 5
            while self.app.association_running and time.time() < deadline:
                self.app.update()
                time.sleep(0.02)
        self.assertFalse(self.app.association_running)
        self.assertEqual(len(self.app.association_tree.get_children()), 3)
        self.assertIn("2 APs", self.app.association_status.get())

    def export(self, method_name):
        path = Path(_TEST_ROOT) / f"{method_name}.csv"
        with mock.patch.object(toolkit.filedialog, "asksaveasfilename", return_value=str(path)), \
                mock.patch.object(toolkit.messagebox, "showinfo") as info:
            getattr(self.app, method_name)()
        info.assert_called_once()
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_summary_export_has_one_row_per_ap_and_the_unassigned_bucket(self):
        self.seed()
        rows = self.export("export_association_summary")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["AP Name"], "North Sector A")
        self.assertEqual(rows[0]["SM MACs"], "11:22:33:44:55:01; 11:22:33:44:55:02")
        self.assertEqual(rows[0]["Connected SMs (AP reported)"], "2")
        self.assertEqual(rows[0]["Count Check"], "No")
        self.assertEqual(rows[-1]["AP Name"], UNASSIGNED_LABEL)
        self.assertEqual(rows[0]["Report Generated (UTC)"], "2026-11-09 14:02:31 UTC")

    def test_detail_export_has_one_row_per_sm_with_ap_context(self):
        self.seed()
        rows = self.export("export_association_detail")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["SM MAC"], "11:22:33:44:55:01")
        self.assertEqual(rows[0]["AP MAC"], "AA:BB:CC:00:00:01")
        self.assertEqual(rows[0]["AP Name"], "North Sector A")
        self.assertEqual(rows[0]["Tower"], "North")
        self.assertEqual(rows[0]["Distance (km)"], "3.42")
        self.assertEqual(rows[-1]["AP MAC"], "")
        self.assertEqual(rows[-1]["AP Name"], "")

    def test_exports_follow_the_current_filter(self):
        self.seed()
        self.app.association_query.set("diaz")
        self.app.update()
        self.assertEqual(len(self.export("export_association_detail")), 1)

    def test_export_is_formula_safe_and_uses_the_filtered_view(self):
        report = build_association_report(
            sm_rows=[{"mac": "11:22:33:44:55:01", "name": "=cmd|calc", "ap_mac": "AA:BB:CC:00:00:01", "status": "online", "status_time": 60, "network": "Rural", "type": "pmp", "mode": "sm"}],
            ap_rows=[{"mac": "AA:BB:CC:00:00:01", "name": "=danger", "network": "Rural", "tower": "North", "status": "online", "status_time": 50000, "connected_sms": 1, "type": "pmp", "mode": "ap"}],
            generated=GENERATED,
        )
        self.seed(report)
        summary = self.export("export_association_summary")
        detail = self.export("export_association_detail")
        self.assertEqual(summary[0]["AP Name"], "'=danger")
        self.assertEqual(detail[0]["SM Name"], "'=cmd|calc")

    def test_export_without_a_report_tells_the_operator_to_refresh(self):
        self.app.association_report = None
        with mock.patch.object(toolkit.messagebox, "showinfo") as info, \
                mock.patch.object(toolkit.filedialog, "asksaveasfilename") as dialog:
            self.app.export_association_summary()
        info.assert_called_once()
        self.assertIn("Refresh", info.call_args.args[1])
        dialog.assert_not_called()


if __name__ == "__main__":
    unittest.main()
