import os
import sqlite3
import tempfile
import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont
import unittest
from unittest import mock

_TEST_DATA_ROOT = tempfile.mkdtemp()
os.environ["LOCALAPPDATA"] = _TEST_DATA_ROOT
os.environ["OPERATIONS_TOOLKIT_PREVIEW"] = "1"

import cnmaestro_speed_manager as toolkit


class InlineToolViewTests(unittest.TestCase):
    def setUp(self):
        self.app = toolkit.App()
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        self.app.destroy()

    def is_descendant(self, widget, ancestor):
        current = widget
        while current is not None:
            if current is ancestor:
                return True
            current = getattr(current, "master", None)
        return False

    def all_descendants(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self.all_descendants(child)

    def assert_speed_manager_context_is_active(self):
        self.assertEqual(self.app.active_page, "speed_manager")
        self.assertEqual(set(self.app.nav_items), {"overview", "speed_manager", "ap_associations", "mosaic_speed_test", "audit", "settings"})
        _, bar, _, _ = self.app.nav_items["speed_manager"]
        self.assertEqual(bar.cget("background"), self.app.colors["accent"])

    def test_nested_sidebar_hierarchy_and_audit_log_label(self):
        self.assertEqual(
            [(service["label"], [(tool["key"], tool["label"]) for tool in service["tools"]])
             for service in self.app.service_navigation],
            [("cnMaestro", [("speed_manager", "Speed Manager"), ("ap_associations", "AP Associations")]), ("Mosaic", [("mosaic_speed_test", "Speed Test")])],
        )
        _, _, _, overview = self.app.nav_items["overview"]
        _, _, _, audit = self.app.nav_items["audit"]
        _, _, _, settings = self.app.nav_items["settings"]
        self.assertEqual([overview.cget("text"), audit.cget("text"), settings.cget("text")],
                         ["Overview", "Audit Log", "Settings"])

    def test_navigation_labels_use_v120_text_size(self):
        labels = [self.app.nav_items[key][3] for key in ("overview", "speed_manager", "ap_associations", "mosaic_speed_test", "audit", "settings")]
        labels.append(self.app.service_navigation[0]["widgets"][2])
        sizes = [tkfont.Font(root=self.app, font=label.cget("font")).cget("size") for label in labels]
        self.assertEqual(sizes, [11, 11, 11, 11, 11, 11, 11])

    def test_nested_tool_label_fits_without_clipping(self):
        self.app.deiconify()
        self.app.geometry("1280x720")
        self.app.update()
        row, _, _, label = self.app.nav_items["speed_manager"]
        nested_x = label.winfo_rootx() - self.app.sidebar.winfo_rootx()
        self.assertEqual(self.app.sidebar.winfo_width(), 190)
        self.assertLessEqual(nested_x, 55)
        self.assertGreaterEqual(label.winfo_width(), label.winfo_reqwidth())
        self.assertGreater(nested_x, self.app.nav_items["overview"][3].winfo_rootx() - self.app.sidebar.winfo_rootx())

    def test_dense_v120_workspace_embeds_scan_filters_table_and_publish(self):
        self.app.deiconify()
        self.app.geometry("1280x720")
        self.app.update()

        self.assertEqual(self.app.colors["bg"], "#071625")
        self.assertEqual(self.app.colors["surface"], "#142333")
        self.assertEqual(self.app.colors["border"], "#34485a")
        self.assertEqual(self.app.colors["accent"], "#42d6ef")
        self.assertNotIn("scan_filters", self.app.pages)
        self.assertNotIn("preview_publish", self.app.pages)
        self.assertTrue(self.is_descendant(self.app.controls, self.app.pages["speed_manager"]))
        self.assertTrue(self.is_descendant(self.app.publish_panel, self.app.pages["speed_manager"]))
        self.assertTrue(self.app.controls.winfo_ismapped())
        self.assertTrue(self.app.publish_panel.winfo_ismapped())
        self.assertTrue(self.app.tree.winfo_ismapped())
        self.assertFalse(hasattr(self.app, "metric_vars"))
        publish_bottom = self.app.publish_panel.winfo_rooty() + self.app.publish_panel.winfo_height()
        content_bottom = self.app.content.winfo_rooty() + self.app.content.winfo_height()
        self.assertLessEqual(publish_bottom, content_bottom)
        self.assertGreater(self.app.tree.winfo_height(), 80)

    def test_startup_does_not_create_action_or_audit_toplevels(self):
        action_windows = [child for child in self.app.winfo_children() if isinstance(child, tk.Toplevel)]
        self.assertEqual(action_windows, [])
        self.assertNotIsInstance(self.app.controls, tk.Toplevel)
        self.assertFalse(getattr(self.app, "preview_window", None))

    def test_open_controls_keeps_integrated_speed_manager_workspace(self):
        self.app.deiconify()
        self.app.open_controls()
        self.app.update()

        self.assertTrue(self.app.pages["speed_manager"].winfo_ismapped())
        self.assertTrue(self.app.controls.winfo_ismapped())
        self.assert_speed_manager_context_is_active()

    def test_valid_preview_opens_structured_popout_window(self):
        self.app.rows = {
            "AA:BB:CC:DD:EE:FF": {
                "name": "Test SM", "mac": "AA:BB:CC:DD:EE:FF", "package": "25 Mbps",
                "network": "Test Network", "tower": "Test Tower", "ap_mac": "11:22:33:44:55:66",
                "online": True, "status_time": 120, "downlink": 26880, "uplink": 3225,
                "error": "", "cache_age_hours": 0, "stale": False,
            }
        }
        self.app.checked = {"AA:BB:CC:DD:EE:FF"}
        self.app.target.set("50 Mbps")
        self.app.api = object()
        self.app.api = object()
        self.app.deiconify()

        self.app.preview_changes()
        self.app.update()

        self.assertEqual(len(self.app.preview), 1)
        self.assertTrue(self.app.pages["speed_manager"].winfo_ismapped())
        self.assertIsInstance(self.app.preview_window, tk.Toplevel)
        self.assertTrue(self.app.preview_window.winfo_ismapped())
        self.assertEqual(len(self.app.preview_tree.get_children()), 1)
        values = self.app.preview_tree.item(self.app.preview_tree.get_children()[0], "values")
        self.assertEqual(values[0], "Test SM")
        self.assertEqual(values[3], "50 Mbps")
        self.assertFalse(self.app.out.winfo_ismapped())
        self.assert_speed_manager_context_is_active()


    def test_preview_without_cnmaestro_connection_notifies_user(self):
        self.app.api = None
        with mock.patch.object(toolkit.messagebox, "showerror") as error:
            self.app.preview_changes()
        error.assert_called_once()
        self.assertIn("Connect", error.call_args.args[0])
        self.assertFalse(getattr(self.app, "preview_window", None))


    def widget_texts(self, root):
        values = []
        for widget in root.winfo_children():
            if "text" in widget.keys(): values.append(str(widget.cget("text")))
            values.extend(self.widget_texts(widget))
        return values

    def test_speed_manager_removes_confirmation_and_update_controls(self):
        publish_text = self.widget_texts(self.app.publish_panel)
        settings_text = self.widget_texts(self.app.pages["settings"])
        self.assertNotIn("Confirmation", publish_text)
        self.assertNotIn("Check updates", publish_text)
        self.assertIn("Check for updates", settings_text)

    def test_publish_without_connection_notifies_and_does_not_execute(self):
        self.app.api = None
        with mock.patch.object(toolkit.messagebox, "showerror") as error, mock.patch.object(self.app, "execute") as execute:
            self.app.publish_from_ui()
        error.assert_called_once()
        self.assertIn("Connect", error.call_args.args[0])
        execute.assert_not_called()

    def test_publish_wrapper_satisfies_hidden_internal_confirmation(self):
        self.app.api = object()
        observed = []
        with mock.patch.object(self.app, "execute", side_effect=lambda: observed.append(self.app.confirm.get())):
            self.app.publish_from_ui()
        self.assertEqual(observed, [toolkit.PHRASE])
        self.assertEqual(self.app.confirm.get(), "")

    def test_sidebar_audit_log_opens_embedded_table_with_export_hook(self):
        with sqlite3.connect(toolkit.DB) as connection:
            connection.execute("DELETE FROM audit")
            connection.execute(
                "INSERT INTO audit(timestamp,mac,name,old_package,target_package,template,"
                "job_id,job_state,verified_package,success,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("2026-08-19T18:00:00+00:00", "AA:BB:CC:DD:EE:FF", "Audit Test SM",
                 "25 Mbps", "50 Mbps", "50mbps Package", "job-1", "completed",
                 "50 Mbps", 1, ""),
            )
        self.app.deiconify()
        self.app.show_page("audit")
        self.app.update()

        self.assertTrue(self.app.pages["audit"].winfo_ismapped())
        self.assertEqual(self.app.active_page, "audit")
        values = self.app.audit_tree.item(self.app.audit_tree.get_children()[0], "values")
        self.assertEqual(values[:2], ("2026-08-19T18:00:00+00:00", "Audit Test SM"))
        export_buttons = [widget for widget in self.all_descendants(self.app.pages["audit"])
                          if isinstance(widget, ttk.Button) and widget.cget("text") == "Export CSV"]
        self.assertEqual(len(export_buttons), 1)
        self.assertTrue(hasattr(self.app, "export_audit_csv"))

    def test_packaged_smoke_path_exercises_integrated_actions(self):
        self.app.deiconify()
        self.app.smoke_inline_views()
        self.app.update()

        self.assertEqual(self.app.title(), "Operations Toolkit - inline views smoke complete")
        self.assertTrue(self.app.pages["speed_manager"].winfo_ismapped())
        self.assert_speed_manager_context_is_active()


if __name__ == "__main__":
    unittest.main()
