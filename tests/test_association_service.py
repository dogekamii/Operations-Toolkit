import asyncio
import threading
import unittest
from datetime import datetime, timezone

import httpx

import association_service as service


GENERATED = datetime(2026, 11, 9, 14, 2, 31, tzinfo=timezone.utc)


def ap_row(**overrides):
    row = {
        "mac": "AA:BB:CC:00:00:01",
        "name": "North Sector A",
        "network": "Rural",
        "tower": "North",
        "status": "online",
        "status_time": 50000,
        "connected_sms": 2,
        "type": "pmp",
        "mode": "ap",
    }
    row.update(overrides)
    return row


def sm_row(**overrides):
    row = {
        "mac": "11:22:33:44:55:01",
        "name": "10014 Johnson",
        "ap_mac": "aa-bb-cc-00-00-01",
        "network": "Rural",
        "status": "online",
        "status_time": 120,
        "distance": 3.42,
        "type": "pmp",
        "mode": "sm",
        "last_sync": "2026-11-09T14:01:00+00:00",
    }
    row.update(overrides)
    return row


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


class FakeApi:
    """Serves canned cnMaestro pages per endpoint and records every query."""

    def __init__(self, statistics_rows=(), device_rows=(), per_device_rows=(), inventory_rows=(),
                 page_size=2, require_fields=False, reject_fields=False, bulk_status=None,
                 per_device_status=None, bulk_error_body=None, cancel_after_first=None):
        self.base = "https://cnmaestro.test"
        self.statistics_rows = list(statistics_rows)
        self.device_rows = list(device_rows)
        self.per_device_rows = list(per_device_rows)
        self.inventory_rows = list(inventory_rows)
        self.page_size = page_size
        self.require_fields = require_fields
        self.reject_fields = reject_fields
        self.bulk_status = bulk_status
        self.per_device_status = per_device_status
        self.bulk_error_body = bulk_error_body if bulk_error_body is not None else {"error": "invalid query"}
        self.cancel_after_first = cancel_after_first
        self.calls = []
        self.clients = []

    async def client(self):
        client = FakeClient()
        self.clients.append(client)
        return client

    async def inventory(self):
        return list(self.inventory_rows)

    def _raise(self, url, status, body):
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError(f"HTTP {status}", request=request, response=httpx.Response(status, json=body, request=request))

    def _page(self, rows, url, params):
        offset = int(params.get("offset", 0))
        # Simulate a server-side page cap: never return more than page_size rows.
        limit = min(int(params.get("limit", self.page_size)), self.page_size)
        page = rows[offset:offset + limit]
        if self.cancel_after_first is not None and offset == 0:
            self.cancel_after_first.set()
        return FakeResponse({"paging": {"total": len(rows)}, "data": page})

    async def req(self, client, method, url, **kwargs):
        params = dict(kwargs.get("params") or {})
        self.calls.append((method, url, params))
        if url.endswith("/devices/statistics"):
            if self.bulk_status is not None:
                self._raise(url, self.bulk_status, self.bulk_error_body)
            if self.require_fields and "fields" not in params:
                self._raise(url, 400, {"error": "fields required"})
            if self.reject_fields and "fields" in params:
                self._raise(url, 422, {"error": "fields unsupported"})
            return self._page(self.statistics_rows, url, params)
        if "/devices/" in url and url.endswith("/statistics"):
            if self.per_device_status is not None:
                self._raise(url, self.per_device_status, {"error": "forbidden"})
            mac = url.split("/devices/")[1].split("/")[0]
            return FakeResponse({"data": [row for row in self.per_device_rows if row.get("mac") == mac]})
        if url.endswith("/devices"):
            return self._page(self.device_rows, url, params)
        raise AssertionError(f"unexpected URL {url}")


class MacNormalizationTests(unittest.TestCase):
    def test_normalizes_common_mac_formats(self):
        for value in ("aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "aabb.ccdd.eeff", "AABBCCDDEEFF", " aa:bb:cc:dd:ee:ff "):
            self.assertEqual(service.normalize_mac(value), "AA:BB:CC:DD:EE:FF", value)

    def test_rejects_non_mac_values(self):
        for value in ("", None, "not-a-mac", "AA:BB:CC", "12345"):
            self.assertEqual(service.normalize_mac(value), "", value)

    def test_accepts_whitespace_and_punctuation_separators(self):
        self.assertEqual(service.normalize_mac("aa bb cc dd ee ff"), "AA:BB:CC:DD:EE:FF")


class AgeFormattingTests(unittest.TestCase):
    def test_matches_speed_manager_text(self):
        self.assertEqual(service.format_age(0), "0m")
        self.assertEqual(service.format_age(120), "2m")
        self.assertEqual(service.format_age(7200), "2.0h")
        self.assertEqual(service.format_age(900000), "10.4d")
        self.assertEqual(service.format_age(None), "0m")
        self.assertEqual(service.format_age("bad"), "Unknown")


class BuildReportTests(unittest.TestCase):
    def build(self, sm_rows=None, ap_rows=None):
        return service.build_association_report(
            sm_rows if sm_rows is not None else [sm_row()],
            ap_rows if ap_rows is not None else [ap_row()],
            generated=GENERATED,
        )

    def test_groups_sms_under_their_serving_ap(self):
        report = self.build()
        self.assertEqual(len(report["groups"]), 1)
        group = report["groups"][0]
        self.assertEqual((group["kind"], group["mac"], group["name"]), ("ap", "AA:BB:CC:00:00:01", "North Sector A"))
        self.assertEqual([sm["name"] for sm in group["sms"]], ["10014 Johnson"])
        self.assertEqual(report["generated"], "2026-11-09 14:02:31 UTC")

    def test_ap_without_sms_is_still_reported(self):
        report = self.build(sm_rows=[], ap_rows=[ap_row(connected_sms=0)])
        self.assertEqual(len(report["groups"]), 1)
        self.assertEqual(report["groups"][0]["sms"], [])
        self.assertEqual(report["summary"]["ap_count"], 1)
        self.assertEqual(report["summary"]["sm_count"], 0)

    def test_unknown_ap_mac_gets_its_own_group_not_unassigned(self):
        report = self.build(sm_rows=[sm_row(ap_mac="FF:FF:CC:00:00:09")], ap_rows=[])
        kinds = [group["kind"] for group in report["groups"]]
        self.assertEqual(kinds, ["unknown"])
        self.assertIn("not in inventory", report["groups"][0]["name"])
        self.assertEqual(report["summary"]["unassigned"], 0)
        self.assertEqual(report["summary"]["unknown_ap_count"], 1)

    def test_blank_ap_mac_is_bucketed_as_unassigned_last(self):
        report = self.build(sm_rows=[sm_row(), sm_row(mac="11:22:33:44:55:03", name="10016 Diaz", ap_mac="")])
        self.assertEqual([group["kind"] for group in report["groups"]], ["ap", "unassigned"])
        self.assertEqual(report["groups"][-1]["name"], service.UNASSIGNED_LABEL)
        self.assertEqual([sm["name"] for sm in report["groups"][-1]["sms"]], ["10016 Diaz"])
        self.assertEqual(report["summary"]["assigned"], 1)
        self.assertEqual(report["summary"]["unassigned"], 1)

    def test_groups_are_ordered_ap_then_unknown_then_unassigned(self):
        report = self.build(sm_rows=[
            sm_row(),
            sm_row(mac="11:22:33:44:55:04", name="A unknown", ap_mac="FF:FF:CC:00:00:09"),
            sm_row(mac="11:22:33:44:55:03", name="Z unassigned", ap_mac=""),
        ], ap_rows=[ap_row(), ap_row(mac="AA:BB:CC:00:00:02", name="Zulu Sector")])
        self.assertEqual([group["kind"] for group in report["groups"]], ["ap", "ap", "unknown", "unassigned"])
        self.assertEqual([group["name"] for group in report["groups"][:2]], ["North Sector A", "Zulu Sector"])

    def test_count_check_compares_reported_with_online_sms_only(self):
        report = self.build(sm_rows=[
            sm_row(),
            sm_row(mac="11:22:33:44:55:02", name="10015 Flores", status="offline", status_time=90000),
        ], ap_rows=[ap_row(connected_sms=1)])
        group = report["groups"][0]
        self.assertEqual(group["online_sms"], 1)
        self.assertEqual(report["summary"]["mismatch_count"], 0)
        self.assertEqual(report["summary"]["offline_sms"], 1)

    def test_count_check_flags_a_real_disagreement(self):
        report = self.build(sm_rows=[sm_row()], ap_rows=[ap_row(connected_sms=4)])
        self.assertEqual(report["summary"]["mismatch_count"], 1)

    def test_non_pmp_sms_are_excluded(self):
        report = self.build(sm_rows=[sm_row(), sm_row(mac="11:22:33:44:55:09", type="epmp")])
        self.assertEqual(report["summary"]["sm_count"], 1)

    def test_non_pmp_aps_are_excluded(self):
        report = self.build(ap_rows=[ap_row(), ap_row(mac="AA:BB:CC:00:00:09", name="ePMP AP", type="epmp")])
        self.assertEqual([group["name"] for group in report["groups"]], ["North Sector A"])
        self.assertEqual(report["summary"]["ap_count"], 1)

    def test_rows_with_the_wrong_mode_are_ignored(self):
        report = self.build(sm_rows=[sm_row(), ap_row(mac="AA:BB:CC:00:00:77", name="Leaked AP row")],
                            ap_rows=[ap_row(), sm_row(mac="11:22:33:44:55:88", name="Leaked SM row")])
        self.assertEqual([group["name"] for group in report["groups"]], ["North Sector A"])
        self.assertEqual(report["summary"]["sm_count"], 1)

    def test_naive_generated_timestamp_is_treated_as_utc(self):
        report = service.build_association_report([sm_row()], [ap_row()], generated=datetime(2026, 11, 9, 6, 0, 0))
        self.assertEqual(report["generated"], "2026-11-09 06:00:00 UTC")

    def test_summary_text_reports_buckets_and_checks(self):
        report = self.build(sm_rows=[sm_row(ap_mac=""), sm_row(mac="11:22:33:44:55:04", ap_mac="FF:FF:CC:00:00:09")],
                            ap_rows=[ap_row(connected_sms=9)])
        text = service.summary_text(report)
        self.assertIn("1 APs", text)
        self.assertIn("2 SMs", text)
        self.assertIn("1 unassigned", text)
        self.assertIn("1 AP MAC(s) not in inventory", text)
        self.assertIn("1 AP count check(s) differ", text)
        self.assertIn("2026-11-09 14:02:31 UTC", text)


class FilteringTests(unittest.TestCase):
    def setUp(self):
        self.report = service.build_association_report(
            [
                sm_row(),
                sm_row(mac="11:22:33:44:55:02", name="10015 Flores", status="offline", status_time=90000),
                sm_row(mac="11:22:33:44:55:04", name="10017 Stone", ap_mac="FF:FF:CC:00:00:09"),
                sm_row(mac="11:22:33:44:55:03", name="10016 Diaz", ap_mac=""),
            ],
            [ap_row(), ap_row(mac="AA:BB:CC:00:00:02", name="South Sector B", tower="South", connected_sms=0, status="offline", status_time=900000)],
            generated=GENERATED,
        )

    def test_empty_query_keeps_every_group(self):
        self.assertEqual(len(service.filter_groups(self.report)), 4)

    def test_ap_name_query_keeps_all_of_that_aps_sms(self):
        groups = service.filter_groups(self.report, "north sector")
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["sms"]), 2)

    def test_sm_query_narrows_to_the_matching_sm(self):
        groups = service.filter_groups(self.report, "flores")
        self.assertEqual(len(groups), 1)
        self.assertEqual([sm["name"] for sm in groups[0]["sms"]], ["10015 Flores"])

    def test_mac_query_accepts_dashes_and_case(self):
        groups = service.filter_groups(self.report, "aa-bb-cc-00-00-02")
        self.assertEqual([group["name"] for group in groups], ["South Sector B"])

    def test_online_only_drops_offline_sms_but_keeps_ap_groups(self):
        groups = service.filter_groups(self.report, online_only=True)
        self.assertEqual(len(groups), 4)
        self.assertEqual([sm["name"] for sm in groups[0]["sms"]], ["10014 Johnson"])

    def test_online_only_and_query_without_matches_returns_nothing(self):
        self.assertEqual(service.filter_groups(self.report, "10015 Flores", online_only=True), [])

    def test_filtering_does_not_mutate_the_report(self):
        service.filter_groups(self.report, "flores")
        self.assertEqual(len(self.report["groups"][0]["sms"]), 2)


class CsvRowTests(unittest.TestCase):
    def setUp(self):
        self.report = service.build_association_report(
            [sm_row(), sm_row(mac="11:22:33:44:55:03", name="10016 Diaz", ap_mac="")],
            [ap_row(connected_sms=1)],
            generated=GENERATED,
        )

    def test_summary_rows_are_one_per_ap_and_include_the_unassigned_bucket(self):
        rows = service.summary_csv_rows(self.report)
        self.assertEqual(len(rows), 2)
        self.assertEqual(tuple(rows[0]), service.SUMMARY_FIELDS)
        self.assertEqual(rows[0]["SM MACs"], "11:22:33:44:55:01")
        self.assertEqual(rows[0]["Count Check"], "Yes")
        self.assertEqual(rows[1]["AP Name"], service.UNASSIGNED_LABEL)
        self.assertEqual(rows[1]["SMs in Report"], 1)
        self.assertEqual(rows[1]["Count Check"], "Unknown")
        self.assertEqual(rows[0]["Report Generated (UTC)"], "2026-11-09 14:02:31 UTC")

    def test_summary_rows_join_multiple_sms(self):
        report = service.build_association_report(
            [sm_row(), sm_row(mac="11:22:33:44:55:02", name="10015 Flores")],
            [ap_row(connected_sms=2)],
            generated=GENERATED,
        )
        row = service.summary_csv_rows(report)[0]
        self.assertEqual(row["SM MACs"], "11:22:33:44:55:01; 11:22:33:44:55:02")
        self.assertEqual(row["SM Names"], "10014 Johnson; 10015 Flores")

    def test_detail_rows_are_one_per_sm_with_ap_context(self):
        rows = service.detail_csv_rows(self.report)
        self.assertEqual(len(rows), 2)
        self.assertEqual(tuple(rows[0]), service.DETAIL_FIELDS)
        self.assertEqual(rows[0]["AP MAC"], "AA:BB:CC:00:00:01")
        self.assertEqual(rows[0]["AP Name"], "North Sector A")
        self.assertEqual(rows[0]["Tower"], "North")
        self.assertEqual(rows[0]["Distance (km)"], 3.42)
        self.assertEqual(rows[1]["AP MAC"], "")
        self.assertEqual(rows[1]["AP Name"], "")

    def test_rows_can_be_limited_to_the_filtered_view(self):
        groups = service.filter_groups(self.report, "diaz")
        self.assertEqual(len(service.summary_csv_rows(self.report, groups)), 1)
        self.assertEqual(len(service.detail_csv_rows(self.report, groups)), 1)


class FetchReportTests(unittest.TestCase):
    def test_paginates_when_the_server_caps_page_size(self):
        rows = [ap_row(mac=f"AA:BB:CC:00:00:{index:02X}", name=f"AP {index}") for index in range(5)]
        api = FakeApi(statistics_rows=rows, page_size=2)
        report = asyncio.run(service.fetch_association_report(api, page_size=5, generated=GENERATED))
        # The fake caps every page at two rows even though five were requested.
        self.assertEqual(report["summary"]["ap_count"], 5)
        self.assertEqual([call[2]["offset"] for call in api.calls], [0, 2, 4])
        self.assertTrue(all(client.closed for client in api.clients))

    def test_bulk_call_omits_mode_and_partitions_locally(self):
        api = FakeApi(statistics_rows=[ap_row(), sm_row()])
        report = asyncio.run(service.fetch_association_report(api, generated=GENERATED))
        self.assertEqual(len(api.calls), 1)
        method, url, params = api.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/api/v2/devices/statistics"))
        self.assertNotIn("mode", params)
        self.assertNotIn("fields", params)
        self.assertEqual(report["summary"]["ap_count"], 1)
        self.assertEqual(report["summary"]["sm_count"], 1)

    def test_fields_projection_is_retried_when_the_plain_call_is_rejected(self):
        api = FakeApi(statistics_rows=[ap_row()], require_fields=True)
        report = asyncio.run(service.fetch_association_report(api, generated=GENERATED))
        self.assertEqual(report["summary"]["ap_count"], 1)
        self.assertNotIn("fields", api.calls[0][2])
        self.assertIn("ap_mac", api.calls[1][2]["fields"])

    def test_cancel_event_stops_collection_and_closes_the_client(self):
        rows = [ap_row(mac=f"AA:BB:CC:00:00:{index:02X}") for index in range(4)]
        cancel = threading.Event()
        api = FakeApi(statistics_rows=rows, page_size=2, cancel_after_first=cancel)
        with self.assertRaises(service.AssociationReportCancelled):
            asyncio.run(service.fetch_association_report(api, page_size=2, cancel=cancel, generated=GENERATED))
        self.assertTrue(api.clients[0].closed)

    def test_progress_reports_running_totals(self):
        rows = [ap_row(mac=f"AA:BB:CC:00:00:{index:02X}") for index in range(3)]
        api = FakeApi(statistics_rows=rows, page_size=2)
        events = []
        asyncio.run(service.fetch_association_report(api, page_size=2, progress=lambda label, done, total: events.append((label, done, total)), generated=GENERATED))
        self.assertEqual(events[0], ("AP/SM statistics", 2, 3))
        self.assertEqual(events[-1], ("AP/SM statistics", 3, 3))

    def test_bulk_rejection_falls_back_to_per_device_statistics(self):
        api = FakeApi(
            device_rows=[ap_row()],
            inventory_rows=[{"mac": "11:22:33:44:55:01", "name": "10014 Johnson", "type": "pmp", "mode": "sm"}],
            per_device_rows=[sm_row()],
            bulk_status=400,
            bulk_error_body={"error": "Bad Request"},
        )
        report = asyncio.run(service.fetch_association_report(api, generated=GENERATED))
        self.assertTrue(report["degraded"])
        self.assertEqual(report["source"], "per-device statistics")
        self.assertIn("HTTP 400", report["bulk_error"])
        self.assertEqual(report["summary"]["ap_count"], 1)
        self.assertEqual(report["summary"]["sm_count"], 1)
        self.assertEqual([group["name"] for group in report["groups"]], ["North Sector A"])
        self.assertTrue(any(call[1].endswith("/devices/11:22:33:44:55:01/statistics") for call in api.calls))

    def test_missing_device_modes_falls_back_instead_of_returning_an_empty_report(self):
        api = FakeApi(
            statistics_rows=[{"mac": "AA:BB:CC:00:00:01", "name": "No mode", "type": "pmp"}],
            device_rows=[ap_row()],
            inventory_rows=[{"mac": "11:22:33:44:55:01", "name": "10014 Johnson", "type": "pmp", "mode": "sm"}],
            per_device_rows=[sm_row()],
        )
        report = asyncio.run(service.fetch_association_report(api, generated=GENERATED))
        self.assertTrue(report["degraded"])
        self.assertEqual(report["summary"]["sm_count"], 1)

    def test_fallback_failure_reports_both_cnmaestro_messages(self):
        api = FakeApi(
            device_rows=[],
            inventory_rows=[{"mac": "11:22:33:44:55:01", "name": "10014 Johnson", "type": "pmp", "mode": "sm"}],
            bulk_status=400,
            bulk_error_body={"error": {"message": "mode not supported"}},
            per_device_status=403,
        )
        with self.assertRaises(service.AssociationReportError) as caught:
            asyncio.run(service.fetch_association_report(api, generated=GENERATED))
        message = str(caught.exception)
        self.assertIn("HTTP 400", message)
        self.assertIn("mode not supported", message)
        self.assertIn("fallback also failed", message)
        self.assertIn("HTTP 403", message)

    def test_http_error_text_extracts_nested_messages(self):
        request = httpx.Request("GET", "https://cnmaestro.test/api/v2/devices/statistics")
        response = httpx.Response(400, json={"error": {"message": "invalid mode", "cause": "AP"}}, request=request)
        text = service.http_error_text(httpx.HTTPStatusError("bad", request=request, response=response))
        self.assertEqual(text, "cnMaestro returned HTTP 400: invalid mode - AP")


if __name__ == "__main__":
    unittest.main()
