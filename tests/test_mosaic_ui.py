import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

_TEST_ROOT = tempfile.mkdtemp()
os.environ["LOCALAPPDATA"] = _TEST_ROOT
os.environ["OPERATIONS_TOOLKIT_PREVIEW"] = "1"

import cnmaestro_speed_manager as toolkit


class MosaicUiTests(unittest.TestCase):
    def setUp(self):
        self.app = toolkit.App()
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        self.app.destroy()

    def test_nested_mosaic_speed_test_page_uses_in_app_navigation(self):
        hierarchy = [(service["label"], [(tool["key"], tool["label"]) for tool in service["tools"]]) for service in self.app.service_navigation]
        self.assertEqual(hierarchy, [
            ("cnMaestro", [("speed_manager", "Speed Manager"), ("ap_associations", "AP Associations")]),
            ("Mosaic", [("mosaic_speed_test", "Speed Test")]),
        ])
        self.assertIn("mosaic_speed_test", self.app.pages)
        self.app.deiconify()
        self.app.show_page("mosaic_speed_test")
        self.app.update()
        self.assertEqual(self.app.active_page, "mosaic_speed_test")
        self.assertTrue(self.app.pages["mosaic_speed_test"].winfo_ismapped())
        self.assertEqual([child for child in self.app.winfo_children() if child.winfo_class() == "Toplevel"], [])

    def test_mosaic_page_has_masked_credentials_candidate_table_and_actions(self):
        self.assertEqual(self.app.mosaic_password_entry.cget("show"), "*")
        self.assertEqual(tuple(self.app.mosaic_tree["columns"]), (
            "select", "customer", "code", "subscriber", "device", "model", "match", "eligibility", "state", "test_time", "download", "upload", "latency", "jitter"
        ))
        labels = {widget.cget("text") for widget in self.app.pages["mosaic_speed_test"].winfo_children() if hasattr(widget, "cget") and "text" in widget.keys()}
        self.assertIn("Mosaic Speed Test", labels)
        self.assertTrue(hasattr(self.app, "mosaic_find_button"))
        self.assertTrue(hasattr(self.app, "mosaic_run_button"))
        self.assertTrue(hasattr(self.app, "mosaic_reconcile_button"))

    def test_batch_completion_handoff_contains_only_successes_and_does_not_auto_run(self):
        rows = [
            {"name": "10014 Johnson Harriet", "mac": "AA", "success": True, "target_package": "50 Mbps"},
            {"name": "10029 Flores Lorenta", "mac": "BB", "success": False, "target_package": "50 Mbps"},
        ]
        self.app.deiconify()
        self.app.show_page("speed_manager")
        self.app.update()
        self.app.out.delete("1.0", "end")
        self.app.out.insert("1.0", json.dumps(rows))
        with mock.patch.object(self.app, "run_selected_mosaic_tests") as run:
            self.app.progtext.set("Completed | Success 1 | Failed 1")
            self.app.update()
        self.assertEqual([row["name"] for row in self.app.pending_speedtest_customers], ["10014 Johnson Harriet"])
        self.assertTrue(self.app.mosaic_handoff_outer.winfo_ismapped())
        run.assert_not_called()

    def test_handoff_review_opens_mosaic_page_without_starting_network_actions(self):
        self.app.pending_speedtest_customers = [{"name": "10014 Johnson Harriet", "mac": "AA", "success": True}]
        with mock.patch.object(self.app, "find_mosaic_matches") as find, mock.patch.object(self.app, "run_selected_mosaic_tests") as run:
            self.app.review_speedtests()
            self.app.update()
        self.assertEqual(self.app.active_page, "mosaic_speed_test")
        find.assert_not_called()
        run.assert_not_called()





    def test_clear_selected_and_clear_all_only_change_visible_candidates(self):
        self.app.pending_speedtest_customers = [{"name": "10014 Test"}]
        self.app.mosaic_candidates = [
            {"key": "2", "eligible": True, "customer": "10014 A"},
            {"key": "3", "eligible": True, "customer": "10015 B"},
        ]
        self.app.mosaic_checked = {"2"}
        self.app.clear_selected_mosaic_candidates()
        self.assertEqual([row["key"] for row in self.app.mosaic_candidates], ["3"])
        self.assertEqual(self.app.mosaic_checked, set())
        self.app.clear_all_mosaic_candidates()
        self.assertEqual(self.app.mosaic_candidates, [])
        self.assertEqual(self.app.pending_speedtest_customers, [])

    def test_csv_export_is_excel_compatible_and_formula_safe(self):
        import csv
        from pathlib import Path
        output = Path(_TEST_ROOT) / "speed-tests.csv"
        self.app.mosaic_candidates = [{
            "customer": "=danger", "subscriber_code": "10014", "subscriber_id": "1", "device_id": "2",
            "model": "ADTRAN", "match": "code_and_name", "eligibility": "Ready", "state": "verified",
            "test_time_utc": "2026-08-21 01:02:03 UTC", "download_mbps": 50.0, "upload_mbps": 10.0,
            "latency_ms": 12.0, "jitter_ms": 3.0,
        }]
        with mock.patch.object(toolkit.filedialog, "asksaveasfilename", return_value=str(output)), mock.patch.object(toolkit.messagebox, "showinfo"):
            self.app.export_mosaic_csv()
        with output.open(encoding="utf-8-sig", newline="") as handle: rows=list(csv.DictReader(handle))
        self.assertEqual(rows[0]["Customer"], "'=danger")
        self.assertEqual(rows[0]["Test time (UTC)"], "2026-08-21 01:02:03 UTC")
        self.assertEqual(rows[0]["Download Mbps"], "50.0")

    def test_result_management_controls_and_test_time_column_exist(self):
        self.assertEqual(self.app.mosaic_tree.heading("test_time")["text"], "Test time (UTC)")
        self.assertEqual(self.app.mosaic_remove_button.cget("text"), "Remove selected")
        self.assertEqual(self.app.mosaic_clear_button.cget("text"), "Clear all")
        self.assertEqual(self.app.mosaic_export_button.cget("text"), "Export CSV")



    def test_terminal_stale_candidate_is_labeled_for_explicit_cleanup(self):
        class FakeApi:
            async def read_device(self, device_id):
                return {
                    "support": {"applications": {"OoklaSpeedTest": {"supported": True, "driver": {"ref": "driver"}}}},
                    "application_status": {"applications": {"OoklaSpeedTest": {"state": "OK"}}},
                    "actions": {"applications": {"OoklaSpeedTest": {"pendingSync": True}}},
                    "data": {"applications": {"OoklaSpeedTest": {"dto": {"Settings": {"OoklaSpeedTest": {"State": "Complete", "ExpectingResults": "false", "Results": {"1": {"Status": "Complete", "StartTimeStamp": "200"}}}}}}}},
                }
        self.app.mosaic_api = FakeApi()
        record = {"fields": {"subscriberCode": "10014", "subscriberId": "1", "deviceId": "2", "model": "ADTRAN", "fullName": "Test", "disposition": "MANAGED_DEVICE", "lastInform": "2099-01-01T00:00:00+00:00"}}
        rows = asyncio.run(self.app.mosaic_candidates_for_records({"name": "10014 Test"}, 0, [record]))
        self.assertTrue(rows[0]["stale_pending"])
        self.assertFalse(rows[0]["eligible"])
        self.assertIn("Stale Ookla request", rows[0]["eligibility"])

    def test_guarded_clear_stale_request_refreshes_candidate_to_ready(self):
        class FakeApi:
            def __init__(self): self.clears = 0
            async def clear_terminal_ookla_pending(self, device_id, *, required=False): self.clears += 1; return {"cleared": True}
            async def read_device(self, device_id):
                return {"support": {"applications": {"OoklaSpeedTest": {"supported": True, "driver": {"ref": "driver"}}}}, "application_status": {"applications": {"OoklaSpeedTest": {"state": "OK"}}}, "actions": {"applications": {"OoklaSpeedTest": {"pendingSync": False}}}, "data": {}}
        api = FakeApi();self.app.mosaic_api = api
        record = {"fields": {"subscriberCode": "10014", "subscriberId": "1", "deviceId": "2", "model": "ADTRAN", "fullName": "Test", "disposition": "MANAGED_DEVICE", "lastInform": "2099-01-01T00:00:00+00:00"}}
        self.app.mosaic_candidates = [{"key": "2", "device_id": "2", "record": record, "stale_pending": True, "eligible": False, "eligibility": "Stale Ookla request", "state": "review"}]
        self.app.mosaic_checked = {"2"}
        def immediate(coroutine, callback):
            try: callback(asyncio.run(coroutine), None)
            except Exception as exc: callback(None, exc)
        with mock.patch.object(toolkit.messagebox, "askyesno", return_value=True), mock.patch.object(self.app, "bg", side_effect=immediate):
            self.app.clear_selected_stale_request()
        self.assertEqual(api.clears, 1)
        self.assertFalse(self.app.mosaic_candidates[0]["stale_pending"])
        self.assertTrue(self.app.mosaic_candidates[0]["eligible"])
        self.assertEqual(self.app.mosaic_candidates[0]["state"], "ready")
        self.assertEqual(self.app.mosaic_candidates[0]["eligibility"], "Ready")

    def test_stale_clear_control_exists(self):
        self.assertEqual(self.app.mosaic_clear_stale_button.cget("text"), "Clear stale request")


    def test_check_uncertain_offers_confirmed_local_retry_release(self):
        class FakeJournal:
            def __init__(self): self.released=[]
            def unresolved(self): return [{"id": 7, "device_id": "2", "state": "unknown"}]
            def release_retry_lock(self, entry_id): self.released.append(entry_id)
        journal=FakeJournal();self.app.mosaic_journal=journal;self.app.mosaic_api=object();self.app.mosaic_candidates=[{"key":"2","customer":"0000 Sacred Wind Testing","device_id":"2","eligible":True,"state":"submitting"}]
        async def release_candidate(client, journal, entry): return {"entry_id":7,"device_id":"2","state":"release_candidate","detail":"no remote evidence"}
        def immediate(coroutine, callback):
            try:callback(asyncio.run(coroutine),None)
            except Exception as exc:callback(None,exc)
        with mock.patch.object(toolkit,"reconcile_journal_entry",new=release_candidate),mock.patch.object(toolkit.messagebox,"askyesno",return_value=True),mock.patch.object(self.app,"bg",side_effect=immediate):self.app.reconcile_mosaic_unknown()
        self.assertEqual(journal.released,[7])
        self.assertEqual(self.app.mosaic_candidates[0]["state"],"ready")
        self.assertIn("released",self.app.mosaic_action_status.get())

    def test_declining_local_retry_release_preserves_lock(self):
        class FakeJournal:
            def __init__(self): self.released=[]
            def unresolved(self): return [{"id":7,"device_id":"2","state":"unknown"}]
            def release_retry_lock(self,entry_id):self.released.append(entry_id)
        journal=FakeJournal();self.app.mosaic_journal=journal;self.app.mosaic_api=object()
        async def release_candidate(client,journal,entry):return {"entry_id":7,"device_id":"2","state":"release_candidate","detail":"no remote evidence"}
        def immediate(coroutine,callback):callback(asyncio.run(coroutine),None)
        with mock.patch.object(toolkit,"reconcile_journal_entry",new=release_candidate),mock.patch.object(toolkit.messagebox,"askyesno",return_value=False),mock.patch.object(self.app,"bg",side_effect=immediate):self.app.reconcile_mosaic_unknown()
        self.assertEqual(journal.released,[])

    def test_mosaic_actions_without_connection_notify_user(self):
        self.app.mosaic_api = None
        self.app.mosaic_search_code.set("10014")
        with mock.patch.object(toolkit.messagebox, "showerror") as error:
            self.app.search_mosaic_subscriber()
            self.app.run_selected_mosaic_tests()
            self.app.reconcile_mosaic_unknown()
        self.assertEqual(error.call_count, 3)
        self.assertTrue(all("Mosaic" in call.args[0] for call in error.call_args_list))


    def test_search_add_appends_customer_and_preserves_existing_selection(self):
        existing={"key":"2","customer":"10014 Customer A","device_id":"2","eligible":True,"state":"ready","download_mbps":None}
        added={"key":"3","customer":"10015 Customer B","device_id":"3","eligible":True,"state":"ready","download_mbps":None}
        self.app.mosaic_candidates=[existing];self.app.mosaic_checked={"2"}
        self.app.finish_mosaic_search_add([added],None)
        self.assertEqual([row["key"] for row in self.app.mosaic_candidates],["2","3"])
        self.assertEqual(self.app.mosaic_checked,{"2"})
        self.assertIn("Added 1",self.app.mosaic_action_status.get())

    def test_readding_existing_device_refreshes_preflight_without_erasing_result(self):
        existing={"key":"2","customer":"10014 Customer A","device_id":"2","eligible":True,"eligibility":"Ready","state":"verified","download_mbps":50.0,"upload_mbps":10.0,"test_time_utc":"2026-08-21 01:00:00 UTC","record":{"fields":{"old":True}}}
        refreshed={"key":"2","customer":"10014 Customer A Updated","device_id":"2","eligible":False,"eligibility":"Another Mosaic action is pending","state":"review","download_mbps":None,"upload_mbps":None,"test_time_utc":None,"record":{"fields":{"new":True}}}
        self.app.mosaic_candidates=[existing];self.app.mosaic_checked={"2"}
        self.app.finish_mosaic_search_add([refreshed],None)
        row=self.app.mosaic_candidates[0]
        self.assertEqual(len(self.app.mosaic_candidates),1)
        self.assertEqual(row["customer"],"10014 Customer A Updated")
        self.assertFalse(row["eligible"])
        self.assertEqual(row["record"],{"fields":{"new":True}})
        self.assertEqual(row["download_mbps"],50.0)
        self.assertEqual(row["state"],"verified")
        self.assertEqual(self.app.mosaic_checked,{"2"})

    def test_standalone_search_is_labeled_search_and_add(self):
        self.assertEqual(self.app.mosaic_search_button.cget("text"),"Search & add")

    def test_independent_subscriber_code_search_builds_candidate(self):
        class FakeApi:
            async def search_subscriber(self, code):
                return [{"fields": {"subscriberCode": code, "subscriberId": "1", "deviceId": "2", "model": "ADTRAN", "fullName": "Test Customer", "disposition": "MANAGED_DEVICE", "lastInform": "2099-01-01T00:00:00+00:00"}}]
            async def read_device(self, device_id):
                return {"support": {"applications": {"OoklaSpeedTest": {"supported": True, "driver": {"ref": "driver"}}}}, "application_status": {"applications": {"OoklaSpeedTest": {"state": "OK"}}}, "actions": {"applications": {"OoklaSpeedTest": {"pendingSync": False}}}, "data": {}}
        self.app.mosaic_api = FakeApi()
        self.app.mosaic_search_code.set("10014")
        def immediate(coroutine, callback):
            try: callback(asyncio.run(coroutine), None)
            except Exception as exc: callback(None, exc)
        with mock.patch.object(self.app, "bg", side_effect=immediate):
            self.app.search_mosaic_subscriber()
        self.assertEqual(len(self.app.mosaic_candidates), 1)
        self.assertEqual(self.app.mosaic_candidates[0]["subscriber_code"], "10014")
        self.assertEqual(self.app.mosaic_candidates[0]["device_id"], "2")

    def test_multiple_devices_expand_into_reviewable_device_rows(self):
        class FakeApi:
            async def read_device(self, device_id):
                return {"support": {"applications": {"OoklaSpeedTest": {"supported": True, "driver": {"ref": "driver"}}}}, "application_status": {"applications": {"OoklaSpeedTest": {"state": "OK"}}}, "actions": {"applications": {"OoklaSpeedTest": {"pendingSync": False}}}, "data": {}}
        self.app.mosaic_api = FakeApi()
        records = [
            {"fields": {"subscriberCode": "10014", "subscriberId": "1", "deviceId": "2", "model": "Router A", "fullName": "Test", "disposition": "MANAGED_DEVICE", "lastInform": "2099-01-01T00:00:00+00:00"}},
            {"fields": {"subscriberCode": "10014", "subscriberId": "1", "deviceId": "3", "model": "Router B", "fullName": "Test", "disposition": "MANAGED_DEVICE", "lastInform": "2099-01-01T00:00:00+00:00"}},
        ]
        rows = asyncio.run(self.app.mosaic_candidates_for_records({"name": "10014 Test"}, 0, records))
        self.assertEqual({row["device_id"] for row in rows}, {"2", "3"})
        self.assertTrue(all(row["match"] == "multiple_devices" for row in rows))
        self.assertTrue(all(row["eligible"] for row in rows))

    def test_speed_test_controls_use_clear_labels_without_confirmation_box(self):
        self.assertTrue(hasattr(self.app, "mosaic_search_entry"))
        self.assertEqual(self.app.mosaic_search_button.cget("text"), "Search & add")
        self.assertEqual(self.app.mosaic_reconcile_button.cget("text"), "Check uncertain tests")
        self.assertEqual(self.app.mosaic_tree.heading("state")["text"], "Test status")
        self.assertFalse(hasattr(self.app, "mosaic_confirmation"))
        import inspect
        self.assertNotIn("RUN SPEED TESTS", inspect.getsource(toolkit.App.run_selected_mosaic_tests))


    def test_auto_retry_controls_are_bounded_and_off_by_default(self):
        self.assertFalse(self.app.mosaic_auto_retry.get())
        self.assertEqual(tuple(self.app.mosaic_retry_count.cget("values")), ("1", "2", "3"))
        self.assertEqual(self.app.mosaic_retry_count.get(), "1")
        self.assertEqual(self.app.mosaic_retry_failed_button.cget("text"), "Retry failed selected")

    def test_auto_retry_retries_only_explicit_retryable_failure(self):
        self.app.mosaic_api=object();candidate={"key":"2","device_id":"2","subscriber_code":"10014","model":"SDG","eligible":True,"record":{"fields":{}},"state":"ready","customer":"Customer A"};self.app.mosaic_candidates=[candidate];self.app.mosaic_checked={"2"};self.app.mosaic_auto_retry.set(True);self.app.mosaic_retry_count.set("2")
        outcomes=[{"state":"failed","retryable":True,"detail":"Mosaic diagnostic failed"},{"state":"verified","metrics":{"download_mbps":50},"cleanup":{}}]
        async def execute(*args,**kwargs):return outcomes.pop(0)
        captured={}
        def synchronous(coro,callback):captured["result"]=asyncio.run(coro);callback(captured["result"],None)
        with mock.patch.object(toolkit,"execute_journaled_ookla",side_effect=execute) as run, mock.patch.object(toolkit.asyncio,"sleep",new=mock.AsyncMock()), mock.patch.object(self.app,"bg",side_effect=synchronous), mock.patch.object(self.app,"start_mosaic_progress_poll"):
            self.app.run_selected_mosaic_tests()
        self.assertEqual(run.call_count,2)
        self.assertEqual(captured["result"][0][1]["attempts"],2)
        self.assertEqual(captured["result"][0][1]["state"],"verified")

    def test_auto_retry_never_retries_unknown_outcome(self):
        self.app.mosaic_api=object();candidate={"key":"2","device_id":"2","subscriber_code":"10014","model":"SDG","eligible":True,"record":{"fields":{}},"state":"ready","customer":"Customer A"};self.app.mosaic_candidates=[candidate];self.app.mosaic_checked={"2"};self.app.mosaic_auto_retry.set(True);self.app.mosaic_retry_count.set("3")
        async def execute(*args,**kwargs):return {"state":"unknown","retryable":False,"detail":"ambiguous PUT"}
        captured={}
        def synchronous(coro,callback):captured["result"]=asyncio.run(coro);callback(captured["result"],None)
        with mock.patch.object(toolkit,"execute_journaled_ookla",side_effect=execute) as run, mock.patch.object(self.app,"bg",side_effect=synchronous), mock.patch.object(self.app,"start_mosaic_progress_poll"):
            self.app.run_selected_mosaic_tests()
        self.assertEqual(run.call_count,1)
        self.assertEqual(captured["result"][0][1]["attempts"],1)

    def test_retry_failed_selected_runs_only_retryable_failed_rows(self):
        self.app.mosaic_api=object()
        retryable={"key":"2","eligible":True,"state":"failed","retryable":True};ordinary={"key":"3","eligible":True,"state":"failed","retryable":False};self.app.mosaic_candidates=[retryable,ordinary];self.app.mosaic_checked={"2","3"}
        with mock.patch.object(self.app,"start_mosaic_test_batch") as start:
            self.app.retry_failed_mosaic_tests()
        start.assert_called_once_with([retryable])

    def test_progress_queue_updates_row_message_and_overall_bar(self):
        candidate={"key":"2","customer":"10014 Customer A","eligible":True,"state":"ready"}
        self.app.mosaic_candidates=[candidate];self.app.mosaic_running=True;self.app.mosaic_progress.configure(maximum=2,value=0)
        self.app.mosaic_progress_events.put({"key":"2","index":1,"total":2,"stage":"Waiting for router"})
        with mock.patch.object(self.app,"after",return_value="after-id"):
            self.app.drain_mosaic_progress()
        self.assertEqual(candidate["state"],"Waiting for router")
        self.assertIn("Customer 1 of 2",self.app.mosaic_action_status.get())
        self.assertIn("Waiting for router",self.app.mosaic_action_status.get())
        self.assertEqual(float(self.app.mosaic_progress["value"]),0.0)

    def test_terminal_progress_applies_result_and_advances_bar(self):
        candidate={"key":"2","customer":"10014 Customer A","eligible":True,"state":"ready","stale_pending":False}
        self.app.mosaic_candidates=[candidate];self.app.mosaic_running=True;self.app.mosaic_progress.configure(maximum=2,value=0)
        outcome={"state":"verified","metrics":{"download_mbps":50.0,"upload_mbps":10.0,"test_time_utc":"2026-08-21 01:00:00 UTC"},"cleanup":{"cleared":True}}
        self.app.mosaic_progress_events.put({"key":"2","index":1,"total":2,"stage":"Verified","outcome":outcome})
        with mock.patch.object(self.app,"after",return_value="after-id"):
            self.app.drain_mosaic_progress()
        self.assertEqual(candidate["state"],"verified")
        self.assertEqual(candidate["download_mbps"],50.0)
        self.assertEqual(float(self.app.mosaic_progress["value"]),1.0)

    def test_speed_test_progress_bar_exists(self):
        self.assertTrue(self.app.mosaic_progress.winfo_exists())

    def test_repeated_run_clicks_start_only_one_worker(self):
        self.app.mosaic_api = object()
        self.app.mosaic_candidates = [{"key": "2", "device_id": "2", "subscriber_code": "10014", "model": "SDG", "eligible": True, "record": {"fields": {}}}]
        self.app.mosaic_checked = {"2"}
        def hold(coroutine, callback): coroutine.close()
        with mock.patch.object(self.app, "bg", side_effect=hold) as background:
            self.app.run_selected_mosaic_tests()
            self.app.run_selected_mosaic_tests()
        self.assertEqual(background.call_count, 1)
        self.assertEqual(str(self.app.mosaic_run_button.cget("state")), "disabled")
        self.app.mosaic_running = False
        if self.app.mosaic_progress_after:
            self.app.after_cancel(self.app.mosaic_progress_after);self.app.mosaic_progress_after=None

    def test_ui_passes_match_record_to_fresh_preflight(self):
        import inspect
        source = inspect.getsource(toolkit.App.start_mosaic_test_batch)
        self.assertIn("record=candidate['record']", source)

    def test_mosaic_worker_does_not_call_tk_after_from_background_coroutine(self):
        import inspect
        source = inspect.getsource(toolkit.App.run_selected_mosaic_tests)
        self.assertNotIn("self.after(", source)

    def test_close_clears_mosaic_session_and_password(self):
        class FakeClient:
            def __init__(self): self.closed = False
            def clear_session(self): self.closed = True
        fake = FakeClient()
        self.app.mosaic_api = fake
        self.app.mosaic_password.set("sensitive")
        with mock.patch.object(self.app, "destroy") as destroy:
            self.app.close_app()
        self.assertTrue(fake.closed)
        self.assertEqual(self.app.mosaic_password.get(), "")
        destroy.assert_called_once()


    def test_minimum_size_keeps_back_navigation_visible(self):
        self.app.deiconify()
        self.app.geometry("1120x700+0+0")
        self.app.show_page("mosaic_speed_test")
        self.app.update_idletasks()
        self.app.update()
        self.assertLessEqual(
            self.app.mosaic_back_button.winfo_rootx() + self.app.mosaic_back_button.winfo_width(),
            self.app.winfo_rootx() + self.app.winfo_width(),
        )

    def test_minimum_size_keeps_retry_and_export_controls_visible(self):
        self.app.deiconify();self.app.geometry("1120x700+0+0");self.app.show_page("mosaic_speed_test");self.app.update_idletasks();self.app.update()
        right=self.app.winfo_rootx()+self.app.winfo_width()
        for control in (self.app.mosaic_auto_retry_check,self.app.mosaic_retry_count,self.app.mosaic_retry_failed_button,self.app.mosaic_export_button):
            self.assertLessEqual(control.winfo_rootx()+control.winfo_width(),right)

    def test_unsupported_candidate_displays_reason_and_cannot_be_selected(self):
        self.app.mosaic_candidates = [{
            "key": "8588", "selected": False, "customer": "999999999 Test", "subscriber_code": "999999999",
            "subscriber_id": "6230", "device_id": "8588", "model": "C0 - SR905acv", "match": "Exact code",
            "eligible": False, "eligibility": "SR905 routers do not support Mosaic speed tests", "state": "unsupported",
            "download_mbps": None, "upload_mbps": None, "latency_ms": None, "jitter_ms": None,
        }]
        self.app.render_mosaic_candidates()
        item = self.app.mosaic_tree.item("8588", "values")
        self.assertIn("SR905", item[7])
        self.app.toggle_mosaic_candidate("8588")
        self.assertFalse(self.app.mosaic_candidates[0]["selected"])


if __name__ == "__main__":
    unittest.main()
