"""AP association reporting built from the cnMaestro Statistics API.

cnMaestro exposes the serving AP MAC only through the Statistics API
(``ap_mac`` on PMP/ePMP/cnVision subscriber modules), not through the
Devices inventory endpoint.  This module collects one paginated pass of
AP statistics and one of SM statistics, then joins them locally so a
report can be produced without per-device requests.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

import httpx

STATISTICS_PATH = "/api/v2/devices/statistics"
DEVICES_PATH = "/api/v2/devices"
AP_FIELDS = "mac,name,network,tower,status,status_time,connected_sms,type,mode"
SM_FIELDS = "mac,name,ap_mac,network,tower,status,status_time,distance,type,mode,last_sync"
ALL_FIELDS = "mac,name,network,tower,status,status_time,connected_sms,ap_mac,distance,type,mode,last_sync"
PAGE_SIZE = 100
MAX_PAGES = 500
UNASSIGNED_LABEL = "Unassigned - no AP reported"
UNKNOWN_AP_SUFFIX = "(not in inventory)"
SUMMARY_FIELDS = (
    "AP Name",
    "AP MAC",
    "Network",
    "Tower",
    "SMs in Report",
    "Online SMs",
    "Connected SMs (AP reported)",
    "Count Check",
    "Offline SMs",
    "SM MACs",
    "SM Names",
    "Report Generated (UTC)",
)
DETAIL_FIELDS = (
    "SM Name",
    "SM MAC",
    "AP MAC",
    "AP Name",
    "Network",
    "Tower",
    "SM State",
    "SM Age",
    "Distance (km)",
    "Last Sync",
    "Report Generated (UTC)",
)


class AssociationReportCancelled(RuntimeError):
    """The operator cancelled the report while statistics pages were loading."""


class AssociationReportError(RuntimeError):
    """cnMaestro rejected the report query and the fallback did not rescue it."""


def normalize_mac(value) -> str:
    """Return a canonical ``AA:BB:CC:DD:EE:FF`` MAC, or ``""`` when not a MAC."""
    digits = re.sub(r"[^0-9A-F]", "", str(value or "").upper())
    if len(digits) != 12:
        return ""
    return ":".join(digits[index:index + 2] for index in range(0, 12, 2))


def format_age(seconds) -> str:
    """Match the Speed Manager age text for a status interval in seconds."""
    try:
        value = int(seconds or 0)
    except (TypeError, ValueError):
        return "Unknown"
    if value < 3600:
        return f"{value // 60}m"
    if value < 86400:
        return f"{value / 3600:.1f}h"
    return f"{value / 86400:.1f}d"


def _integer(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _online(row: dict):
    status = str(row.get("status") or "").strip().lower()
    if status == "online":
        return True
    if status in ("offline", "claimed", "waiting", "onboarding"):
        return False
    return None


def _state_text(online) -> str:
    if online is True:
        return "Online"
    if online is False:
        return "Offline"
    return ""


def _count_check(group: dict) -> str:
    """Compare the AP's own connected-SM count with our online SM count."""
    reported = group.get("reported")
    if reported is None:
        return "Unknown"
    return "Yes" if reported == group.get("online_sms") else "No"


def _row_has_mode(row: dict, expected: str) -> bool:
    mode = str(row.get("mode") or "").strip().lower()
    return not mode or mode == expected


def _group_from_row(row: dict, mac: str, kind: str) -> dict:
    name = str(row.get("name") or "").strip() or mac
    return {
        "kind": kind,
        "mac": mac,
        "name": name,
        "network": str(row.get("network") or "").strip(),
        "tower": str(row.get("tower") or "").strip(),
        "online": _online(row),
        "status_time": _integer(row.get("status_time")),
        "reported": _integer(row.get("connected_sms")),
        "sms": [],
    }


def build_association_report(sm_rows, ap_rows, generated=None) -> dict:
    """Join SM statistics to AP inventory by the SM's serving ``ap_mac``."""
    moment = generated or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    stamp = moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    groups: dict[str, dict] = {}
    for ap in ap_rows or []:
        if not _row_has_mode(ap, "ap"):
            continue
        device_type = str(ap.get("type") or "").strip().lower()
        if device_type and device_type != "pmp":
            continue
        mac = normalize_mac(ap.get("mac"))
        if not mac:
            continue
        groups[mac] = _group_from_row(ap, mac, "ap")

    sms = []
    for sm in sm_rows or []:
        if not _row_has_mode(sm, "sm"):
            continue
        device_type = str(sm.get("type") or "").strip().lower()
        if device_type and device_type != "pmp":
            continue
        mac = normalize_mac(sm.get("mac"))
        if not mac:
            continue
        sms.append({
            "mac": mac,
            "name": str(sm.get("name") or "").strip() or mac,
            "ap_mac": normalize_mac(sm.get("ap_mac")),
            "online": _online(sm),
            "status_time": _integer(sm.get("status_time")),
            "network": str(sm.get("network") or "").strip(),
            "tower": str(sm.get("tower") or "").strip(),
            "distance": sm.get("distance"),
            "last_sync": str(sm.get("last_sync") or "").strip(),
        })

    unassigned = []
    for sm in sms:
        if not sm["ap_mac"]:
            unassigned.append(sm)
            continue
        group = groups.get(sm["ap_mac"])
        if group is None:
            group = {
                "kind": "unknown",
                "mac": sm["ap_mac"],
                "name": f"{sm['ap_mac']} {UNKNOWN_AP_SUFFIX}",
                "network": sm["network"],
                "tower": sm["tower"],
                "online": None,
                "status_time": None,
                "reported": None,
                "sms": [],
            }
            groups[sm["ap_mac"]] = group
        group["sms"].append(sm)

    if unassigned:
        groups[""] = {
            "kind": "unassigned",
            "mac": "",
            "name": UNASSIGNED_LABEL,
            "network": "",
            "tower": "",
            "online": None,
            "status_time": None,
            "reported": None,
            "sms": unassigned,
        }

    kind_order = {"ap": 0, "unknown": 1, "unassigned": 2}
    ordered = sorted(
        groups.values(),
        key=lambda group: (kind_order.get(group["kind"], 9), group["name"].casefold(), group["mac"]),
    )
    for group in ordered:
        group["sms"].sort(key=lambda sm: (sm["name"].casefold(), sm["mac"]))
        group["online_sms"] = sum(1 for sm in group["sms"] if sm["online"] is True)

    all_sms = [sm for group in ordered for sm in group["sms"]]
    known = [group for group in ordered if group["kind"] == "ap"]
    summary = {
        "ap_count": len(known),
        "unknown_ap_count": len([group for group in ordered if group["kind"] == "unknown"]),
        "sm_count": len(all_sms),
        "assigned": len(all_sms) - len(unassigned),
        "unassigned": len(unassigned),
        "online_sms": sum(1 for sm in all_sms if sm["online"] is True),
        "offline_sms": sum(1 for sm in all_sms if sm["online"] is False),
        "unknown_sms": sum(1 for sm in all_sms if sm["online"] is None),
        "mismatch_count": sum(1 for group in known if _count_check(group) == "No"),
    }
    return {"generated": stamp, "groups": ordered, "summary": summary}


def summary_text(report: dict) -> str:
    """One-line status for the report header."""
    summary = report.get("summary") or {}
    parts = [
        f"{summary.get('ap_count', 0)} APs",
        f"{summary.get('sm_count', 0)} SMs",
        f"{summary.get('assigned', 0)} assigned",
    ]
    if summary.get("unassigned"):
        parts.append(f"{summary['unassigned']} unassigned")
    if summary.get("unknown_ap_count"):
        parts.append(f"{summary['unknown_ap_count']} AP MAC(s) not in inventory")
    if summary.get("mismatch_count"):
        parts.append(f"{summary['mismatch_count']} AP count check(s) differ")
    if report.get("degraded"):
        parts.append("per-device fallback")
    parts.append(str(report.get("generated") or "").strip())
    return " | ".join(part for part in parts if part)


def filter_groups(report: dict, query: str = "", online_only: bool = False) -> list[dict]:
    """Return shallow copies of the groups matching the operator's view filters."""
    needle = str(query or "").strip().casefold()
    mac_needle = normalize_mac(query).casefold()

    def matches(*values) -> bool:
        if not needle:
            return True
        for value in values:
            text = str(value or "").casefold()
            if needle and needle in text:
                return True
            if mac_needle and mac_needle in text:
                return True
        return False

    selected_groups = []
    for group in report.get("groups", []):
        group_matches = matches(group.get("name"), group.get("mac"), group.get("network"), group.get("tower"))
        selected = []
        for sm in group.get("sms", []):
            if online_only and sm.get("online") is not True:
                continue
            if group_matches or matches(sm.get("name"), sm.get("mac"), sm.get("ap_mac")):
                selected.append(sm)
        if not group_matches and not selected:
            continue
        copy = {key: value for key, value in group.items() if key != "sms"}
        copy["sms"] = selected
        selected_groups.append(copy)
    return selected_groups


def summary_csv_rows(report: dict, groups=None) -> list[dict]:
    """One row per AP (plus the unassigned bucket) for a director-facing export."""
    stamp = report.get("generated", "")
    rows = []
    for group in report.get("groups", []) if groups is None else groups:
        sms = group.get("sms", [])
        rows.append({
            "AP Name": group.get("name", ""),
            "AP MAC": group.get("mac", ""),
            "Network": group.get("network", ""),
            "Tower": group.get("tower", ""),
            "SMs in Report": len(sms),
            "Online SMs": sum(1 for sm in sms if sm.get("online") is True),
            "Connected SMs (AP reported)": "" if group.get("reported") is None else group["reported"],
            "Count Check": _count_check({**group, "online_sms": sum(1 for sm in sms if sm.get("online") is True)}),
            "Offline SMs": sum(1 for sm in sms if sm.get("online") is False),
            "SM MACs": "; ".join(sm.get("mac", "") for sm in sms),
            "SM Names": "; ".join(sm.get("name", "") for sm in sms),
            "Report Generated (UTC)": stamp,
        })
    return rows


def detail_csv_rows(report: dict, groups=None) -> list[dict]:
    """One row per subscriber module for pivot tables and per-customer lookups."""
    stamp = report.get("generated", "")
    rows = []
    for group in report.get("groups", []) if groups is None else groups:
        for sm in group.get("sms", []):
            rows.append({
                "SM Name": sm.get("name", ""),
                "SM MAC": sm.get("mac", ""),
                "AP MAC": sm.get("ap_mac", ""),
                "AP Name": "" if group.get("kind") == "unassigned" else group.get("name", ""),
                "Network": group.get("network") or sm.get("network", ""),
                "Tower": group.get("tower") or sm.get("tower", ""),
                "SM State": _state_text(sm.get("online")),
                "SM Age": format_age(sm.get("status_time")),
                "Distance (km)": "" if sm.get("distance") is None else sm.get("distance"),
                "Last Sync": sm.get("last_sync", ""),
                "Report Generated (UTC)": stamp,
            })
    return rows


async def _collect(api, path: str, params: dict, page_size: int, label: str, progress, cancel) -> list[dict]:
    client = await api.client()
    items: list[dict] = []
    offset = 0
    pages = 0
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise AssociationReportCancelled("Report collection cancelled.")
            query = dict(params)
            query["limit"] = page_size
            query["offset"] = offset
            response = await api.req(client, "GET", api.base + path, params=query)
            payload = response.json()
            page = payload.get("data") if isinstance(payload, dict) else payload
            page = list(page or [])
            items.extend(page)
            pages += 1
            paging = payload.get("paging") if isinstance(payload, dict) else None
            total = _integer((paging or {}).get("total"))
            if progress:
                progress(label, len(items), total if total is not None else len(items))
            if not page:
                break
            if total is not None:
                if len(items) >= total:
                    break
            elif len(page) < page_size:
                break
            if pages >= MAX_PAGES:
                raise RuntimeError("cnMaestro pagination did not complete.")
            offset += len(page)
    finally:
        await client.aclose()
    return items


def _error_detail(payload) -> str:
    if isinstance(payload, dict):
        parts = []
        for key in ("error", "message", "cause", "detail", "description"):
            value = payload.get(key)
            if isinstance(value, dict):
                parts.extend(str(item) for item in value.values() if item)
            elif value:
                parts.append(str(value))
        return " - ".join(dict.fromkeys(parts))
    if isinstance(payload, list):
        return "; ".join(str(item) for item in payload[:3])
    return str(payload or "")


def http_error_text(error: Exception) -> str:
    """Include the server's own message; httpx alone only reports the status line."""
    response = getattr(error, "response", None)
    if response is None:
        return str(error)
    detail = ""
    try:
        detail = _error_detail(response.json())
    except Exception:
        detail = (getattr(response, "text", "") or "").strip()
    detail = " ".join(detail.split())[:400]
    status = getattr(response, "status_code", "?")
    return f"cnMaestro returned HTTP {status}: {detail}" if detail else f"cnMaestro returned HTTP {status}"


def _partition_statistics(rows) -> tuple[list[dict], list[dict]]:
    ap_rows = []
    sm_rows = []
    for row in rows or []:
        mode = str(row.get("mode") or "").strip().lower()
        if mode == "ap":
            ap_rows.append(row)
        elif mode == "sm":
            sm_rows.append(row)
    return ap_rows, sm_rows


async def _bulk_statistics(api, page_size: int, progress, cancel) -> list[dict]:
    # The documented statistics call uses fields only. Several cnMaestro builds
    # answer 400 Bad Request when `mode` is supplied, so partition locally and
    # only add a projection after the plain call is rejected.
    last_error = None
    for params in ({}, {"fields": ALL_FIELDS}):
        try:
            return await _collect(api, STATISTICS_PATH, params, page_size, "AP/SM statistics", progress, cancel)
        except httpx.HTTPStatusError as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status not in (400, 404, 405, 422):
                raise
            last_error = error
    raise last_error


async def _per_device_statistics(api, progress, cancel) -> list[dict]:
    """Last resort: one statistics read per SM from the working Devices API."""
    devices = await api.inventory()
    total = len(devices)
    rows: list[dict] = []
    done = 0
    client = await api.client()
    semaphore = asyncio.Semaphore(4)

    async def one(device):
        nonlocal done
        async with semaphore:
            if cancel is not None and cancel.is_set():
                raise AssociationReportCancelled("Report collection cancelled.")
            mac = str(device.get("mac") or "").strip()
            if not mac:
                return
            response = await api.req(client, "GET", api.base + f"/api/v2/devices/{mac}/statistics")
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else payload
            row = data[0] if isinstance(data, list) and data else data
            if isinstance(row, dict):
                row = dict(row)
                row.setdefault("mac", mac)
                row.setdefault("name", device.get("name"))
                row.setdefault("type", device.get("type") or "pmp")
                row.setdefault("mode", "sm")
                rows.append(row)
            done += 1
            if progress:
                progress("SM statistics (per device)", done, total)

    try:
        await asyncio.gather(*(one(device) for device in devices))
    finally:
        await client.aclose()
    return rows


async def _fallback_report(api, page_size: int, progress, cancel, generated, reason: str) -> dict:
    try:
        devices = await _collect(api, DEVICES_PATH, {"type": "pmp"}, page_size, "AP inventory", progress, cancel)
        ap_rows = [row for row in devices if str(row.get("mode") or "").strip().lower() == "ap"]
    except Exception:
        ap_rows = []
    sm_rows = await _per_device_statistics(api, progress, cancel)
    report = build_association_report(sm_rows, ap_rows, generated=generated)
    report["degraded"] = True
    report["source"] = "per-device statistics"
    report["bulk_error"] = reason
    return report


async def fetch_association_report(api, page_size: int = PAGE_SIZE, progress=None, cancel=None, generated=None) -> dict:
    """Collect AP and SM statistics through the connected cnMaestro API object."""
    reason = ""
    try:
        rows = await _bulk_statistics(api, page_size, progress, cancel)
    except AssociationReportCancelled:
        raise
    except httpx.HTTPStatusError as error:
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status not in (400, 404, 405, 422):
            raise AssociationReportError(http_error_text(error)) from error
        reason = http_error_text(error)
    else:
        ap_rows, sm_rows = _partition_statistics(rows)
        if ap_rows or sm_rows:
            return build_association_report(sm_rows, ap_rows, generated=generated)
        reason = "cnMaestro statistics response did not include device modes."

    try:
        return await _fallback_report(api, page_size, progress, cancel, generated, reason)
    except AssociationReportCancelled:
        raise
    except Exception as error:
        detail = http_error_text(error) if isinstance(error, httpx.HTTPStatusError) else str(error)
        raise AssociationReportError(f"{reason} Per-device fallback also failed: {detail}") from error
