from __future__ import annotations

import base64
import hashlib
import html as html_lib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
CACHE_DB_PATH = Path(os.getenv("EGANNA_CACHE_DB", str(BASE_DIR / "cache.db")))
DEFAULT_CACHE_TTL_SECONDS = int(os.getenv("EGANNA_CACHE_TTL", "1200"))  # 20 minutes
EGANNA_MODE = os.getenv("EGANNA_MODE", "mock").lower()  # "mock" or "live"
EGANNA_BASE_URL = os.getenv("EGANNA_BASE_URL", "https://164.100.181.16")

ANDROID_HEADERS = {
    "User-Agent": "okhttp/4.9.3",
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip",
    "Connection": "Keep-Alive",
}


# ---------------------------------------------------------------------------
# Normalized JSON Models
# ---------------------------------------------------------------------------


class FarmerProfile(BaseModel):
    ugc: str
    grower_id: str
    grower_name: str
    father_name: str
    village: str
    village_code: str
    factory: str
    factory_code: str
    district: str
    season: str = "2026-27"
    basic_quota_qtl: float
    total_bonding_qtl: float
    total_purchy_allotted: int
    purchy_issued_count: int
    total_weighed_qtl: float
    current_running_pakhwada: int = Field(ge=1, le=12)
    current_running_column: int = Field(ge=1, le=15)
    current_running_slot: int = Field(default=1, ge=1, le=180)


class SupplyTicket(BaseModel):
    purchy_no: str
    pakhwada: int = Field(ge=1, le=12)
    column: int = Field(ge=1, le=15)
    slot_number: int = Field(default=1, ge=1, le=180)
    issue_date: str
    expiry_date: str
    mode: Literal["Gate", "Centre"]
    centre_name: str
    status: Literal["Active", "Weighed", "Expired"]
    net_weight_qtl: Optional[float] = None
    weighment_date: Optional[str] = None
    vehicle_type: Optional[str] = None
    coupled_base_count: int = 1
    coupled_slots: List[str] = Field(default_factory=list)


class CalendarSlot(BaseModel):
    pakhwada: int = Field(ge=1, le=12)
    column: int = Field(ge=1, le=15)
    slot_number: int = Field(ge=1, le=180)
    main_count: int = 0
    additional_count: int = 0
    total_count: int = 0
    issued_count: int = 0
    early_count: int = 0
    general_count: int = 0
    rejected_count: int = 0
    state: Literal["issued", "running", "upcoming", "passed_empty", "future_empty"]


class CalendarSummary(BaseModel):
    total_base_purchy: int = 0
    issued_base_purchy: int = 0
    remaining_base_purchy: int = 0
    early_count: int = 0
    general_count: int = 0
    rejected_count: int = 0
    over_weight: int = 0


class GannaCalendar(BaseModel):
    running_pakhwada: int = Field(ge=1, le=12)
    running_column: int = Field(ge=1, le=15)
    running_slot_number: int = Field(ge=1, le=180)
    summary: CalendarSummary = Field(default_factory=CalendarSummary)
    slots: List[CalendarSlot]


class NextPurchyETA(BaseModel):
    next_pakhwada: Optional[int] = None
    next_column: Optional[int] = None
    next_slot_number: Optional[int] = None
    slots_away: Optional[int] = None
    estimated_days: Optional[int] = None
    purchy_count_in_slot: int = 0
    status_label: str


class CaneDashboardResponse(BaseModel):
    profile: FarmerProfile
    supply_tickets: List[SupplyTicket]
    ganna_calendar: GannaCalendar
    next_purchy_eta: NextPurchyETA
    source: Literal["mock", "live"]
    cache_hit: bool
    cache_status: Literal["fresh_upstream", "cache_hit", "stale_fallback"]
    cached_at_iso: str
    cache_age_seconds: int = 0


class PresetProfile(BaseModel):
    label: str
    ugc: str
    district: str
    factory: str
    village: str
    grower_id: str


class CaptchaInitResponse(BaseModel):
    session_id: str
    captcha_data_url: str


class CaptchaVerifyRequest(BaseModel):
    session_id: str
    captcha: str


class CaptchaVerifyResponse(BaseModel):
    verified: bool
    session_id: str
    message: str
    unlocked_controls: List[str] = []


class WatchProfileRequest(BaseModel):
    ugc: str
    label: str = ""


class NotificationConfigRequest(BaseModel):
    ntfy_topic: str = ""
    public_url: str = ""


# ---------------------------------------------------------------------------
# SQLite TTL Cache
# ---------------------------------------------------------------------------


class SQLiteTTLCache:
    """Thread-safe SQLite TTL cache with stale-if-error fallback."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cane_cache (
                    cache_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watched_profiles (
                    ugc TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    added_at REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_purchies (
                    ugc TEXT NOT NULL,
                    purchy_no TEXT NOT NULL,
                    notified_at REAL NOT NULL,
                    PRIMARY KEY (ugc, purchy_no)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM notification_settings WHERE key = ?",
                (key,),
            ).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO notification_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            self._conn.commit()

    def list_watched_profiles(self) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ugc, label, added_at FROM watched_profiles ORDER BY added_at DESC"
            ).fetchall()
        return [{"ugc": r[0], "label": r[1], "added_at": r[2]} for r in rows]

    def add_watched_profile(self, ugc: str, label: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO watched_profiles (ugc, label, added_at) VALUES (?, ?, ?)
                ON CONFLICT(ugc) DO UPDATE SET label = excluded.label
                """,
                (ugc, label or ugc, time.time()),
            )
            self._conn.commit()

    def remove_watched_profile(self, ugc: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM watched_profiles WHERE ugc = ?", (ugc,))
            self._conn.commit()

    def is_purchy_seen(self, ugc: str, purchy_no: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM seen_purchies WHERE ugc = ? AND purchy_no = ?",
                (ugc, purchy_no),
            ).fetchone()
        return row is not None

    def mark_purchy_seen(self, ugc: str, purchy_no: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO seen_purchies (ugc, purchy_no, notified_at)
                VALUES (?, ?, ?)
                """,
                (ugc, purchy_no, time.time()),
            )
            self._conn.commit()

    def get(self, key: str, ttl_seconds: int) -> Optional[Tuple[dict, int]]:
        """Return (payload_dict, age_seconds) if present and within TTL."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json, updated_at FROM cane_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        if not row:
            return None
        payload_json, updated_at = row
        age = max(0, int(time.time() - updated_at))
        if age > ttl_seconds:
            return None
        return json.loads(payload_json), age

    def get_stale(self, key: str) -> Optional[Tuple[dict, int]]:
        """Return (payload_dict, age_seconds) regardless of TTL for error fallback."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json, updated_at FROM cane_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        if not row:
            return None
        payload_json, updated_at = row
        age = max(0, int(time.time() - updated_at))
        return json.loads(payload_json), age

    def set(self, key: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO cane_cache (cache_key, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(payload), time.time()),
            )
            self._conn.commit()


# ---------------------------------------------------------------------------
# Next Purchy ETA Engine
# ---------------------------------------------------------------------------


def slot_to_linear(pakhwada: int, column: int) -> int:
    return (pakhwada - 1) * 15 + column


def calculate_next_purchy_eta(calendar: GannaCalendar) -> NextPurchyETA:
    """
    Calculate estimated days until next purchy is issued based on linear
    distance in the 180-slot (12 Fortnight x 15 Column) matrix (~1 day/column).
    """
    running_slot = calendar.running_slot_number
    for slot in calendar.slots:
        if slot.slot_number < running_slot:
            continue
        remaining_in_slot = max(0, slot.total_count - slot.issued_count)
        if remaining_in_slot > 0:
            slots_away = slot.slot_number - running_slot
            estimated_days = slots_away
            if slots_away == 0:
                label = f"Due in Current Slot (FN {slot.pakhwada} • Col {slot.column})"
            else:
                label = f"~{estimated_days} days (FN {slot.pakhwada} • Col {slot.column})"
            return NextPurchyETA(
                next_pakhwada=slot.pakhwada,
                next_column=slot.column,
                next_slot_number=slot.slot_number,
                slots_away=slots_away,
                estimated_days=estimated_days,
                purchy_count_in_slot=remaining_in_slot,
                status_label=label,
            )

    return NextPurchyETA(
        next_pakhwada=None,
        next_column=None,
        next_slot_number=None,
        slots_away=None,
        estimated_days=None,
        purchy_count_in_slot=0,
        status_label="All scheduled purchies issued",
    )


# ---------------------------------------------------------------------------
# Calendar Builder & Mock Family Profiles
# ---------------------------------------------------------------------------


def _build_calendar(
    running_pakhwada: int,
    running_column: int,
    main_schedule: Dict[Tuple[int, int], int],
    additional_schedule: Dict[Tuple[int, int], int],
    tickets: List[SupplyTicket],
    variety_counts: Optional[Dict[Tuple[int, int], Tuple[int, int, int]]] = None,
    summary_override: Optional[CalendarSummary] = None,
) -> GannaCalendar:
    running_slot_number = slot_to_linear(running_pakhwada, running_column)
    issued_by_slot: Dict[Tuple[int, int], int] = {}
    for t in tickets:
        key = (t.pakhwada, t.column)
        issued_by_slot[key] = issued_by_slot.get(key, 0) + max(1, t.coupled_base_count)

    slots: List[CalendarSlot] = []
    total_early = 0
    total_general = 0
    total_rejected = 0
    total_base = 0
    issued_base = 0

    for p in range(1, 13):
        for c in range(1, 16):
            slot_num = slot_to_linear(p, c)
            main_cnt = main_schedule.get((p, c), 0)
            add_cnt = additional_schedule.get((p, c), 0)
            total_cnt = main_cnt + add_cnt
            issued_cnt = issued_by_slot.get((p, c), 0)
            ear, gen, rej = (variety_counts or {}).get((p, c), (main_cnt, 0, 0))

            total_base += total_cnt
            total_early += ear
            total_general += gen
            total_rejected += rej
            if issued_cnt > 0:
                issued_base += min(total_cnt, issued_cnt) if total_cnt > 0 else issued_cnt

            if slot_num == running_slot_number:
                state: Literal[
                    "issued", "running", "upcoming", "passed_empty", "future_empty"
                ] = "running"
            elif issued_cnt > 0 or (slot_num < running_slot_number and total_cnt > 0):
                state = "issued"
            elif total_cnt > 0 and slot_num > running_slot_number:
                state = "upcoming"
            elif slot_num < running_slot_number:
                state = "passed_empty"
            else:
                state = "future_empty"

            slots.append(
                CalendarSlot(
                    pakhwada=p,
                    column=c,
                    slot_number=slot_num,
                    main_count=main_cnt,
                    additional_count=add_cnt,
                    total_count=total_cnt,
                    issued_count=issued_cnt,
                    early_count=ear,
                    general_count=gen,
                    rejected_count=rej,
                    state=state,
                )
            )

    summary = summary_override or CalendarSummary(
        total_base_purchy=total_base,
        issued_base_purchy=issued_base,
        remaining_base_purchy=max(0, total_base - issued_base),
        early_count=total_early,
        general_count=total_general,
        rejected_count=total_rejected,
        over_weight=0,
    )

    return GannaCalendar(
        running_pakhwada=running_pakhwada,
        running_column=running_column,
        running_slot_number=running_slot_number,
        summary=summary,
        slots=slots,
    )


def _preset_mock_profiles() -> Dict[str, dict]:
    """Three realistic family grower profiles for immediate UI & multi-profile testing."""
    return {
        "13200458912": {
            "grower_id": "0412",
            "grower_name": "Rajendra Singh",
            "father_name": "Late Harpal Singh",
            "village": "Tissa",
            "village_code": "114",
            "factory": "Khatauli (Triveni)",
            "factory_code": "042",
            "district": "Muzaffarnagar",
            "basic_quota_qtl": 420.0,
            "total_bonding_qtl": 485.0,
            "running_pakhwada": 4,
            "running_column": 2,
            "main_schedule": {
                (1, 2): 1,
                (2, 1): 1,
                (2, 4): 1,
                (3, 2): 1,
                (3, 5): 1,
                (4, 4): 1,
                (5, 2): 1,
                (5, 5): 1,
                (6, 3): 1,
                (7, 1): 1,
                (7, 4): 1,
                (8, 2): 1,
                (9, 1): 1,
                (10, 3): 1,
            },
            "additional_schedule": {
                (4, 4): 1,
                (6, 5): 1,
                (8, 4): 1,
            },
            "tickets": [
                SupplyTicket(
                    purchy_no="KH26-0412-001",
                    pakhwada=1,
                    column=2,
                    issue_date="2026-10-04",
                    expiry_date="2026-10-07",
                    mode="Gate",
                    centre_name="Mill Gate Khatauli",
                    status="Weighed",
                    net_weight_qtl=44.60,
                    weighment_date="2026-10-06",
                    vehicle_type="Tractor Trolley",
                ),
                SupplyTicket(
                    purchy_no="KH26-0412-002",
                    pakhwada=2,
                    column=1,
                    issue_date="2026-10-16",
                    expiry_date="2026-10-19",
                    mode="Gate",
                    centre_name="Mill Gate Khatauli",
                    status="Weighed",
                    net_weight_qtl=46.15,
                    weighment_date="2026-10-18",
                    vehicle_type="Tractor Trolley",
                ),
                SupplyTicket(
                    purchy_no="KH26-0412-003",
                    pakhwada=2,
                    column=4,
                    issue_date="2026-10-25",
                    expiry_date="2026-10-28",
                    mode="Gate",
                    centre_name="Mill Gate Khatauli",
                    status="Weighed",
                    net_weight_qtl=43.80,
                    weighment_date="2026-10-27",
                    vehicle_type="Tractor Trolley",
                ),
                SupplyTicket(
                    purchy_no="KH26-0412-004",
                    pakhwada=3,
                    column=2,
                    issue_date="2026-11-03",
                    expiry_date="2026-11-06",
                    mode="Gate",
                    centre_name="Mill Gate Khatauli",
                    status="Weighed",
                    net_weight_qtl=45.25,
                    weighment_date="2026-11-05",
                    vehicle_type="Tractor Trolley",
                ),
                SupplyTicket(
                    purchy_no="KH26-0412-005",
                    pakhwada=3,
                    column=5,
                    issue_date="2026-11-12",
                    expiry_date="2026-11-15",
                    mode="Gate",
                    centre_name="Mill Gate Khatauli",
                    status="Active",
                    net_weight_qtl=None,
                    weighment_date=None,
                    vehicle_type="Tractor Trolley",
                ),
            ],
        },
        "13200458915": {
            "grower_id": "0415",
            "grower_name": "Virendra Singh",
            "father_name": "Late Harpal Singh",
            "village": "Tissa",
            "village_code": "114",
            "factory": "Khatauli (Triveni)",
            "factory_code": "042",
            "district": "Muzaffarnagar",
            "basic_quota_qtl": 310.0,
            "total_bonding_qtl": 340.0,
            "running_pakhwada": 4,
            "running_column": 2,
            "main_schedule": {
                (1, 3): 1,
                (2, 2): 1,
                (3, 1): 1,
                (3, 4): 1,
                (4, 2): 1,
                (5, 1): 1,
                (6, 2): 1,
                (7, 3): 1,
                (8, 1): 1,
                (9, 4): 1,
            },
            "additional_schedule": {
                (5, 4): 1,
                (7, 5): 1,
            },
            "tickets": [
                SupplyTicket(
                    purchy_no="KH26-0415-001",
                    pakhwada=1,
                    column=3,
                    issue_date="2026-10-07",
                    expiry_date="2026-10-10",
                    mode="Gate",
                    centre_name="Mill Gate Khatauli",
                    status="Weighed",
                    net_weight_qtl=41.90,
                    weighment_date="2026-10-09",
                    vehicle_type="Tractor Trolley",
                ),
                SupplyTicket(
                    purchy_no="KH26-0415-002",
                    pakhwada=2,
                    column=2,
                    issue_date="2026-10-19",
                    expiry_date="2026-10-22",
                    mode="Gate",
                    centre_name="Mill Gate Khatauli",
                    status="Weighed",
                    net_weight_qtl=43.10,
                    weighment_date="2026-10-21",
                    vehicle_type="Tractor Trolley",
                ),
                SupplyTicket(
                    purchy_no="KH26-0415-003",
                    pakhwada=3,
                    column=1,
                    issue_date="2026-10-31",
                    expiry_date="2026-11-03",
                    mode="Gate",
                    centre_name="Mill Gate Khatauli",
                    status="Expired",
                    net_weight_qtl=None,
                    weighment_date=None,
                    vehicle_type="Tractor Trolley",
                ),
                SupplyTicket(
                    purchy_no="KH26-0415-004",
                    pakhwada=3,
                    column=4,
                    issue_date="2026-11-09",
                    expiry_date="2026-11-12",
                    mode="Gate",
                    centre_name="Mill Gate Khatauli",
                    status="Weighed",
                    net_weight_qtl=42.45,
                    weighment_date="2026-11-11",
                    vehicle_type="Tractor Trolley",
                ),
            ],
        },
        "13200891044": {
            "grower_id": "1044",
            "grower_name": "Devendra Tyagi",
            "father_name": "Mahendra Pal Tyagi",
            "village": "Bopara",
            "village_code": "208",
            "factory": "Mansurpur (DSM)",
            "factory_code": "051",
            "district": "Muzaffarnagar",
            "basic_quota_qtl": 265.0,
            "total_bonding_qtl": 290.0,
            "running_pakhwada": 5,
            "running_column": 1,
            "main_schedule": {
                (1, 4): 1,
                (2, 3): 1,
                (3, 3): 1,
                (4, 1): 1,
                (4, 5): 1,
                (5, 4): 1,
                (6, 4): 1,
                (7, 2): 1,
                (8, 3): 1,
            },
            "additional_schedule": {
                (6, 1): 1,
                (9, 2): 1,
            },
            "tickets": [
                SupplyTicket(
                    purchy_no="MN26-1044-001",
                    pakhwada=1,
                    column=4,
                    issue_date="2026-10-10",
                    expiry_date="2026-10-13",
                    mode="Centre",
                    centre_name="Bopara-A Centre",
                    status="Weighed",
                    net_weight_qtl=19.80,
                    weighment_date="2026-10-12",
                    vehicle_type="Bullock Cart / Buggi",
                ),
                SupplyTicket(
                    purchy_no="MN26-1044-002",
                    pakhwada=2,
                    column=3,
                    issue_date="2026-10-22",
                    expiry_date="2026-10-25",
                    mode="Centre",
                    centre_name="Bopara-A Centre",
                    status="Weighed",
                    net_weight_qtl=20.45,
                    weighment_date="2026-10-24",
                    vehicle_type="Bullock Cart / Buggi",
                ),
                SupplyTicket(
                    purchy_no="MN26-1044-003",
                    pakhwada=3,
                    column=3,
                    issue_date="2026-11-06",
                    expiry_date="2026-11-09",
                    mode="Centre",
                    centre_name="Bopara-A Centre",
                    status="Weighed",
                    net_weight_qtl=21.10,
                    weighment_date="2026-11-08",
                    vehicle_type="Bullock Cart / Buggi",
                ),
                SupplyTicket(
                    purchy_no="MN26-1044-004",
                    pakhwada=4,
                    column=1,
                    issue_date="2026-11-11",
                    expiry_date="2026-11-14",
                    mode="Centre",
                    centre_name="Bopara-A Centre",
                    status="Active",
                    net_weight_qtl=None,
                    weighment_date=None,
                    vehicle_type="Bullock Cart / Buggi",
                ),
            ],
        },
    }


def _build_deterministic_custom_profile(
    ugc: str,
    district: Optional[str] = None,
    factory: Optional[str] = None,
    village: Optional[str] = None,
    grower_id: Optional[str] = None,
) -> dict:
    """Generate a deterministic realistic profile when a user enters a new UGC or lookup."""
    seed = int(hashlib.sha256(ugc.encode("utf-8")).hexdigest()[:8], 16)
    running_p = 3 + (seed % 3)  # 3..5
    running_c = 1 + (seed % 5)  # 1..5
    main_schedule: Dict[Tuple[int, int], int] = {}
    for p in range(1, 11):
        c1 = 1 + ((seed + p * 3) % 5)
        main_schedule[(p, c1)] = 1
        if p % 2 == 0:
            c2 = 1 + ((c1 + 2) % 5)
            main_schedule[(p, c2)] = 1

    additional_schedule = {(running_p + 1, 3): 1, (running_p + 3, 2): 1}
    tickets: List[SupplyTicket] = []
    running_linear = slot_to_linear(running_p, running_c)
    idx = 1
    for (p, c), cnt in sorted(main_schedule.items()):
        if slot_to_linear(p, c) < running_linear:
            is_latest = idx == 3
            mode_val: Literal["Gate", "Centre"] = "Gate" if seed % 2 == 0 else "Centre"
            tickets.append(
                SupplyTicket(
                    purchy_no=f"UP26-{ugc[-4:]}-{idx:03d}",
                    pakhwada=p,
                    column=c,
                    slot_number=slot_to_linear(p, c),
                    issue_date=f"2026-10-{min(28, 3 + idx * 5):02d}",
                    expiry_date=f"2026-10-{min(31, 6 + idx * 5):02d}",
                    mode=mode_val,
                    centre_name=f"{village or 'Village'} Gate",
                    status="Active" if is_latest else "Weighed",
                    net_weight_qtl=None if is_latest else round(38.0 + (idx * 1.7), 2),
                    weighment_date=None if is_latest else f"2026-10-{min(30, 5 + idx * 5):02d}",
                    vehicle_type="Tractor Trolley",
                    coupled_base_count=2 if mode_val == "Gate" else 1,
                    coupled_slots=[f"FN {p} • Col {c} (Slot #{slot_to_linear(p, c)})"],
                )
            )
            idx += 1

    return {
        "grower_id": grower_id or ugc[-4:],
        "grower_name": f"Grower {grower_id or ugc[-4:]}",
        "father_name": "UP Cane Farmer",
        "village": village or "Custom Village",
        "village_code": "101",
        "factory": factory or "UP Sugar Mill",
        "factory_code": "040",
        "district": district or "Muzaffarnagar",
        "basic_quota_qtl": 300.0,
        "total_bonding_qtl": 345.0,
        "running_pakhwada": running_p,
        "running_column": running_c,
        "main_schedule": main_schedule,
        "additional_schedule": additional_schedule,
        "tickets": tickets,
    }


# ---------------------------------------------------------------------------
# Live enquiry.caneup.in Session & HTML Parser
# ---------------------------------------------------------------------------


class _TableParser(HTMLParser):
    """Lightweight HTML table extractor using Python's built-in html.parser."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: List[Tuple[str, List[List[str]]]] = []
        self._in_table = False
        self._table_id = ""
        self._current_rows: List[List[str]] = []
        self._in_row = False
        self._current_cells: List[str] = []
        self._in_cell = False
        self._cell_buf: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attr_map = dict(attrs)
        t = tag.lower()
        if t == "table":
            self._in_table = True
            self._table_id = attr_map.get("id") or ""
            self._current_rows = []
        elif t == "tr" and self._in_table:
            self._in_row = True
            self._current_cells = []
        elif t in ("td", "th") and self._in_row:
            self._in_cell = True
            self._cell_buf = []

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("td", "th") and self._in_cell:
            self._in_cell = False
            text = " ".join("".join(self._cell_buf).split())
            self._current_cells.append(text)
        elif t == "tr" and self._in_row:
            self._in_row = False
            if self._current_cells:
                self._current_rows.append(self._current_cells)
        elif t == "table" and self._in_table:
            self._in_table = False
            if self._current_rows:
                self.tables.append((self._table_id, self._current_rows))


def _extract_hidden_inputs(page_html: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for tag in re.findall(r"<input[^>]*type=[\"']hidden[\"'][^>]*>", page_html, re.I):
        name_m = re.search(r'name=["\']([^"\']+)["\']', tag, re.I)
        if not name_m:
            continue
        val_m = re.search(r'value=["\']([^"\']*)["\']', tag, re.I)
        fields[name_m.group(1)] = html_lib.unescape(val_m.group(1)) if val_m else ""
    return fields


def _extract_span_text(page_html: str, element_id: str) -> str:
    m = re.search(
        rf'id=["\']{re.escape(element_id)}["\'][^>]*>(.*?)</(?:span|label|div)>',
        page_html,
        re.I | re.S,
    )
    if not m:
        return ""
    return " ".join(re.sub(r"<[^>]+>", " ", html_lib.unescape(m.group(1))).split())


class CaneUpSession:
    def __init__(self, session_id: str, client: httpx.AsyncClient) -> None:
        self.session_id = session_id
        self.client = client
        self.hidden_fields: Dict[str, str] = {}
        self.verified = False
        self.last_html = ""
        self.created_at = time.time()


class CaneUpSessionManager:
    """Manages ASP.NET sessions with enquiry.caneup.in for user-solved CAPTCHAs."""

    def __init__(self) -> None:
        self._sessions: Dict[str, CaneUpSession] = {}

    def _cleanup(self) -> None:
        now = time.time()
        expired = [sid for sid, s in self._sessions.items() if now - s.created_at > 1800]
        for sid in expired:
            self._sessions.pop(sid, None)

    async def init_captcha(self) -> CaptchaInitResponse:
        self._cleanup()
        sid = str(uuid.uuid4())
        client = httpx.AsyncClient(
            base_url="https://enquiry.caneup.in",
            verify=False,
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        page_resp = await client.get("/")
        page_resp.raise_for_status()
        cap_resp = await client.get("/CreateCaptcha.aspx?New=1")
        cap_resp.raise_for_status()

        session = CaneUpSession(session_id=sid, client=client)
        session.hidden_fields = _extract_hidden_inputs(page_resp.text)
        session.last_html = page_resp.text
        self._sessions[sid] = session

        b64 = base64.b64encode(cap_resp.content).decode("ascii")
        return CaptchaInitResponse(
            session_id=sid,
            captcha_data_url=f"data:image/gif;base64,{b64}",
        )

    def get_session(self, session_id: str) -> Optional[CaneUpSession]:
        self._cleanup()
        return self._sessions.get(session_id)

    async def verify_captcha(self, session_id: str, captcha: str) -> CaptchaVerifyResponse:
        session = self.get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="CAPTCHA session expired. Please reload CAPTCHA.")

        form_data = dict(session.hidden_fields)
        form_data["txtCaptcha"] = captcha.strip()
        form_data["BtnLogin"] = "View"

        resp = await session.client.post(
            "/",
            data=form_data,
            headers={
                "Origin": "https://enquiry.caneup.in",
                "Referer": "https://enquiry.caneup.in/",
            },
        )
        resp.raise_for_status()
        session.last_html = resp.text
        session.hidden_fields = _extract_hidden_inputs(resp.text)

        # Check if CAPTCHA gate is still blocking
        if 'id="BtnLogin"' in resp.text or 'id="txtCaptcha"' in resp.text:
            err_msg = _extract_span_text(resp.text, "Lblcaptchainfo") or "Invalid CAPTCHA. Please try again."
            raise HTTPException(status_code=400, detail=err_msg)

        session.verified = True
        controls = sorted(
            set(re.findall(r'(?:id|name)=["\']([^"\']*(?:ugc|dist|fact|vill|grow|btn|txt|ddl)[^"\']*)["\']', resp.text, re.I))
        )
        return CaptchaVerifyResponse(
            verified=True,
            session_id=session_id,
            message="Connected to enquiry.caneup.in!",
            unlocked_controls=controls[:30],
        )


caneup_sessions = CaneUpSessionManager()


async def _fetch_live_caneup_by_ugc(
    session: CaneUpSession,
    ugc: str,
) -> Tuple[FarmerProfile, List[SupplyTicket], GannaCalendar, Literal["mock", "live"]]:
    """Query an authenticated enquiry.caneup.in session by UGC and parse HTML tables."""
    if not session.verified:
        raise ValueError("enquiry.caneup.in session is not CAPTCHA-verified.")

    page_html = session.last_html
    # Locate UGC input & submit button on the unlocked ASP.NET page
    ugc_field = None
    for m in re.finditer(r"<input[^>]+>", page_html, re.I):
        tag = m.group(0)
        if re.search(r'type=["\'](?:hidden|submit|button)["\']', tag, re.I):
            continue
        name_m = re.search(r'name=["\']([^"\']+)["\']', tag, re.I)
        if name_m and "ugc" in name_m.group(1).lower():
            ugc_field = name_m.group(1)
            break

    submit_name = None
    submit_val = "View"
    for m in re.finditer(r'<input[^>]*type=["\']submit["\'][^>]*>', page_html, re.I):
        tag = m.group(0)
        name_m = re.search(r'name=["\']([^"\']+)["\']', tag, re.I)
        val_m = re.search(r'value=["\']([^"\']*)["\']', tag, re.I)
        if name_m:
            submit_name = name_m.group(1)
            if val_m:
                submit_val = val_m.group(1)
            if "ugc" in submit_name.lower() or "view" in submit_val.lower():
                break

    if not ugc_field or not submit_name:
        discovered = sorted(set(re.findall(r'name=["\']([^"\']+)["\']', page_html, re.I)))
        raise ValueError(
            f"Could not locate UGC input/submit on unlocked enquiry.caneup.in page. "
            f"Discovered form fields: {', '.join(discovered[:25])}"
        )

    post_data = dict(session.hidden_fields)
    post_data[ugc_field] = ugc
    post_data[submit_name] = submit_val
    resp = await session.client.post("/", data=post_data)
    resp.raise_for_status()
    result_html = resp.text
    session.last_html = result_html
    session.hidden_fields = _extract_hidden_inputs(result_html)

    # Parse profile fields
    hidden = session.hidden_fields
    grower_name = _extract_span_text(result_html, "LblGrowerName") or hidden.get("HdGrowName") or f"UGC {ugc}"
    village = _extract_span_text(result_html, "LblVillage") or "—"
    factory = _extract_span_text(result_html, "LblFact") or hidden.get("HdFactory") or "—"
    grower_code = hidden.get("HdGrowCode") or ugc[-4:]
    village_code = hidden.get("HdVillCode") or "0"

    def _to_float(val: str) -> float:
        try:
            return float(re.sub(r"[^0-9.]", "", val) or "0")
        except ValueError:
            return 0.0

    basic_quota = _to_float(hidden.get("HdBQuota", "0"))
    total_bonding = _to_float(hidden.get("HdTotalBonding", "0"))

    # Parse HTML tables for 12x5 calendar and supply tickets
    parser = _TableParser()
    parser.feed(result_html)

    main_schedule: Dict[Tuple[int, int], int] = {}
    additional_schedule: Dict[Tuple[int, int], int] = {}
    variety_counts: Dict[Tuple[int, int], Tuple[int, int, int]] = {}
    tickets_by_no: Dict[str, SupplyTicket] = {}

    # Try to detect running pakhwada/column (1..12 x 1..15) from page text
    running_p = 1
    running_c = 1
    run_m = re.search(
        r"(?:Running|Current)\s*(?:Pakhwada|Fortnight)[^0-9]{1,20}(\d{1,2}).{0,40}?(?:Col(?:umn)?)[^0-9]{1,20}(\d{1,2})",
        result_html,
        re.I | re.S,
    )
    if run_m:
        rp, rc = int(run_m.group(1)), int(run_m.group(2))
        if 1 <= rp <= 12 and 1 <= rc <= 15:
            running_p, running_c = rp, rc

    for tbl_id, rows in parser.tables:
        header_row = rows[0] if rows else []
        tid_lower = tbl_id.lower()

        # Skip Last Year, SMS Log, Plot/Survey tables completely
        if any(
            skip in tid_lower
            for skip in ("lastyr", "lastyear", "smslog", "gvsms", "grdplot", "grdsurvey", "precalender")
        ):
            continue

        # 1. Parse 12x15 Ganna Calendar (Main & Additional)
        if "grdcalender" in tid_lower or "addnlcalender" in tid_lower or ("calender" in tid_lower and len(rows) >= 12):
            # Locate column indices for '01'..'15' from header row
            col_indices: List[int] = []
            for target_num in range(1, 16):
                found_idx = next(
                    (
                        idx
                        for idx, h in enumerate(header_row)
                        if h.strip().isdigit() and int(h.strip()) == target_num
                    ),
                    None,
                )
                if found_idx is not None:
                    col_indices.append(found_idx)
            if len(col_indices) != 15:
                # Fallback: take up to 15 trailing columns
                width = max((len(r) for r in rows), default=17)
                start_col = max(1, width - 15)
                col_indices = list(range(start_col, min(width, start_col + 15)))

            target = additional_schedule if "add" in tid_lower else main_schedule
            current_pakh: Optional[int] = None
            for r in rows[1:]:
                if not r:
                    continue
                first_cell = r[0].strip()
                if re.fullmatch(r"(?:P|FN)?\s*0?([1-9]|1[0-2])", first_cell, re.I):
                    current_pakh = int(re.sub(r"[^0-9]", "", first_cell))
                if current_pakh is None:
                    continue

                row_label = " ".join(r[:2]).lower()
                is_gen = any(k in row_label for k in ("सामान्य", "gen"))
                is_rej = any(k in row_label for k in ("अनुपयुक्त", "rej"))

                for col_pos, cell_idx in enumerate(col_indices, start=1):
                    if cell_idx >= len(r):
                        continue
                    cell_val = r[cell_idx].strip()
                    if cell_val.isdigit():
                        num = int(cell_val)
                        cnt = 1 if num >= 100000 else (num if num <= 25 else 0)
                        if cnt > 0:
                            key = (current_pakh, col_pos)
                            target[key] = target.get(key, 0) + cnt
                            ear, gen, rej = variety_counts.get(key, (0, 0, 0))
                            if is_gen:
                                gen += cnt
                            elif is_rej:
                                rej += cnt
                            else:
                                ear += cnt
                            variety_counts[key] = (ear, gen, rej)
            continue

        # 2. Parse Current Season Supply Tickets (tcMasterInfo_tplSupply_grdSupply)
        if "grdsupply" in tid_lower and "lastyear" not in tid_lower:
            print(f"[grdSupply_sample] head={header_row} first_row={rows[1] if len(rows) > 1 else []}")
            for r in rows[1:]:
                joined = " ".join(r)
                p_match = re.search(r"\b(\d{10,14})\b", joined)
                if not p_match:
                    continue
                p_no = p_match.group(1)

                t_pakh, t_col = running_p, running_c
                pc_match = re.search(r"\b0?([1-9]|1[0-2])\s*[/\-]\s*0?([1-9]|1[0-5])\b", joined)
                if pc_match:
                    t_pakh, t_col = int(pc_match.group(1)), int(pc_match.group(2))

                dates = re.findall(r"\b\d{2}[-/]\d{2}[-/]\d{4}\b", joined)
                status: Literal["Active", "Weighed", "Expired"] = "Active"
                if any(w in joined.lower() for w in ("expir", "निरस्त", "समाप्त")):
                    status = "Expired"

                mode: Literal["Gate", "Centre"] = "Centre" if any(w in joined.lower() for w in ("cent", "केंद्र", "केन्द्र")) else "Gate"
                coupled_cnt = 2 if mode == "Gate" else 1
                slot_num = slot_to_linear(t_pakh, t_col)
                tickets_by_no[p_no] = SupplyTicket(
                    purchy_no=p_no,
                    pakhwada=t_pakh,
                    column=t_col,
                    slot_number=slot_num,
                    issue_date=dates[0] if len(dates) >= 1 else "—",
                    expiry_date=dates[1] if len(dates) >= 2 else "—",
                    mode=mode,
                    centre_name=factory,
                    status=status,
                    net_weight_qtl=None,
                    weighment_date=None,
                    coupled_base_count=coupled_cnt,
                    coupled_slots=[f"FN {t_pakh} • Col {t_col} (Slot #{slot_num})"],
                )
            continue

        # 3. Parse Current Season Weighment / Taul Receipts (tcMasterInfo_tplCane_grdCane)
        if "grdcane" in tid_lower:
            print(f"[grdCane_sample] head={header_row} first_row={rows[1] if len(rows) > 1 else []}")
            for r in rows[1:]:
                joined = " ".join(r)
                p_match = re.search(r"\b(\d{10,14})\b", joined)
                if not p_match:
                    continue
                p_no = p_match.group(1)
                floats = [float(x) for x in re.findall(r"\b(\d+\.\d{1,3})\b", joined) if 0.5 <= float(x) <= 250.0]
                net_wt = floats[-1] if floats else None
                dates = re.findall(r"\b\d{2}[-/]\d{2}[-/]\d{4}\b", joined)

                t_pakh, t_col = running_p, running_c
                pc_match = re.search(r"\b0?([1-9]|1[0-2])\s*[/\-]\s*0?([1-9]|1[0-5])\b", joined)
                if pc_match:
                    t_pakh, t_col = int(pc_match.group(1)), int(pc_match.group(2))

                existing = tickets_by_no.get(p_no)
                if existing:
                    if net_wt is not None and net_wt > 0:
                        existing.net_weight_qtl = net_wt
                        existing.status = "Weighed"
                    if dates and not existing.weighment_date:
                        existing.weighment_date = dates[0]
                    if pc_match and existing.pakhwada == 1 and existing.column == 1:
                        existing.pakhwada = t_pakh
                        existing.column = t_col
                        existing.slot_number = slot_to_linear(t_pakh, t_col)
                else:
                    mode_val: Literal["Gate", "Centre"] = "Centre" if any(w in joined.lower() for w in ("cent", "केंद्र", "केन्द्र")) else "Gate"
                    slot_num = slot_to_linear(t_pakh, t_col)
                    tickets_by_no[p_no] = SupplyTicket(
                        purchy_no=p_no,
                        pakhwada=t_pakh,
                        column=t_col,
                        slot_number=slot_num,
                        issue_date=dates[0] if dates else "—",
                        expiry_date="—",
                        mode=mode_val,
                        centre_name=factory,
                        status="Weighed" if net_wt else "Active",
                        net_weight_qtl=net_wt,
                        weighment_date=dates[0] if dates else None,
                        coupled_base_count=2 if mode_val == "Gate" else 1,
                        coupled_slots=[f"FN {t_pakh} • Col {t_col} (Slot #{slot_num})"],
                    )

    tickets = list(tickets_by_no.values())
    if not run_m and tickets:
        latest_slot = max((slot_to_linear(t.pakhwada, t.column), t.pakhwada, t.column) for t in tickets)
        running_p, running_c = latest_slot[1], latest_slot[2]

    calendar = _build_calendar(
        running_pakhwada=running_p,
        running_column=running_c,
        main_schedule=main_schedule,
        additional_schedule=additional_schedule,
        tickets=tickets,
        variety_counts=variety_counts,
    )
    total_allotted = sum(s.total_count for s in calendar.slots)
    total_weighed = round(sum(t.net_weight_qtl for t in tickets if t.net_weight_qtl is not None), 2)

    profile = FarmerProfile(
        ugc=ugc,
        grower_id=grower_code,
        grower_name=grower_name,
        father_name="—",
        village=village,
        village_code=village_code,
        factory=factory,
        factory_code="0",
        district="UP",
        basic_quota_qtl=basic_quota,
        total_bonding_qtl=total_bonding,
        total_purchy_allotted=total_allotted,
        purchy_issued_count=len(tickets),
        total_weighed_qtl=total_weighed,
        current_running_pakhwada=running_p,
        current_running_column=running_c,
        current_running_slot=slot_to_linear(running_p, running_c),
    )
    return profile, tickets, calendar, "live"


# ---------------------------------------------------------------------------
# Pluggable Upstream Adapter (com.sgk.eganna.in / enquiry.caneup.in)
# ---------------------------------------------------------------------------


class EGannaUpstreamAdapter:
    """
    Adapter for UP Sugarcane Department eGANNA APIs.

    Configured with `verify=False` to bypass incomplete UP NIC SSL certificate
    chains and spoofs `User-Agent: okhttp/4.9.3` matching `com.sgk.eganna.in`.
    """

    def __init__(self, mode: str = EGANNA_MODE, base_url: str = EGANNA_BASE_URL) -> None:
        self.mode = mode
        self.base_url = base_url.rstrip("/")

    def _create_http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers=ANDROID_HEADERS,
            verify=False,
            timeout=httpx.Timeout(15.0, connect=8.0),
            follow_redirects=True,
        )

    async def fetch_by_ugc(
        self,
        ugc: str,
        district: Optional[str] = None,
        factory: Optional[str] = None,
        village: Optional[str] = None,
        grower_id: Optional[str] = None,
    ) -> Tuple[FarmerProfile, List[SupplyTicket], GannaCalendar, Literal["mock", "live"]]:
        if self.mode == "live":
            return await self._fetch_live(
                ugc=ugc,
                district=district,
                factory=factory,
                village=village,
                grower_id=grower_id,
            )
        return self._fetch_mock(
            ugc=ugc,
            district=district,
            factory=factory,
            village=village,
            grower_id=grower_id,
        )

    async def _fetch_live(
        self,
        ugc: str,
        district: Optional[str],
        factory: Optional[str],
        village: Optional[str],
        grower_id: Optional[str],
    ) -> Tuple[FarmerProfile, List[SupplyTicket], GannaCalendar, Literal["mock", "live"]]:
        """
        Plug in exact decompiled APK endpoints from `scripts/extract_endpoints.sh` here.

        Example Retrofit flow from `com.sgk.eganna.in`:
          1. GET / POST grower details by UGC or (district, factory, village, grower_id)
          2. GET / POST Ganna Calendar 12x5 matrix
          3. GET / POST Supply Tickets & Weighment (Taul) list
        """
        async with self._create_http_client() as client:
            # Replace with your extracted endpoint path & payload, e.g.:
            # resp = await client.post("/api/Grower/GetGrowerDetails", json={"UGC": ugc})
            # resp.raise_for_status()
            # raw_data = resp.json()
            _ = (client, ugc, district, factory, village, grower_id)
            raise NotImplementedError(
                "Live endpoints not yet configured. Run scripts/extract_endpoints.sh "
                "against eganna.apk and map the JSON fields in _fetch_live()."
            )

    def _fetch_mock(
        self,
        ugc: str,
        district: Optional[str],
        factory: Optional[str],
        village: Optional[str],
        grower_id: Optional[str],
    ) -> Tuple[FarmerProfile, List[SupplyTicket], GannaCalendar, Literal["mock", "live"]]:
        presets = _preset_mock_profiles()
        raw = presets.get(ugc) or _build_deterministic_custom_profile(
            ugc=ugc,
            district=district,
            factory=factory,
            village=village,
            grower_id=grower_id,
        )

        tickets: List[SupplyTicket] = raw["tickets"]
        for t in tickets:
            t.slot_number = slot_to_linear(t.pakhwada, t.column)
            t.coupled_base_count = 2 if t.mode == "Gate" else 1
            if not t.coupled_slots:
                t.coupled_slots = [f"FN {t.pakhwada} • Col {t.column} (Slot #{t.slot_number})"]

        calendar = _build_calendar(
            running_pakhwada=raw["running_pakhwada"],
            running_column=raw["running_column"],
            main_schedule=raw["main_schedule"],
            additional_schedule=raw["additional_schedule"],
            tickets=tickets,
        )

        total_allotted = sum(s.total_count for s in calendar.slots)
        total_weighed = round(
            sum(t.net_weight_qtl for t in tickets if t.net_weight_qtl is not None), 2
        )

        profile = FarmerProfile(
            ugc=ugc,
            grower_id=raw["grower_id"],
            grower_name=raw["grower_name"],
            father_name=raw["father_name"],
            village=raw["village"],
            village_code=raw["village_code"],
            factory=raw["factory"],
            factory_code=raw["factory_code"],
            district=raw["district"],
            basic_quota_qtl=raw["basic_quota_qtl"],
            total_bonding_qtl=raw["total_bonding_qtl"],
            total_purchy_allotted=total_allotted,
            purchy_issued_count=len(tickets),
            total_weighed_qtl=total_weighed,
            current_running_pakhwada=raw["running_pakhwada"],
            current_running_column=raw["running_column"],
            current_running_slot=slot_to_linear(raw["running_pakhwada"], raw["running_column"]),
        )
        return profile, tickets, calendar, "mock"


# ---------------------------------------------------------------------------
# FastAPI App & Routes
# ---------------------------------------------------------------------------

app = FastAPI(
    title="eGANNA iOS Personal Tracker API",
    version="1.0.0",
    description="FastAPI proxy, TTL cache, and 60-slot ETA engine for UP eGANNA.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

cache = SQLiteTTLCache(CACHE_DB_PATH)
adapter = EGannaUpstreamAdapter()


async def _resolve_cane_dashboard(
    cache_key: str,
    ugc: str,
    force_refresh: bool,
    district: Optional[str] = None,
    factory: Optional[str] = None,
    village: Optional[str] = None,
    grower_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> CaneDashboardResponse:
    if not force_refresh and not session_id:
        cached = cache.get(cache_key, DEFAULT_CACHE_TTL_SECONDS)
        if cached is not None:
            payload, age = cached
            payload["cache_hit"] = True
            payload["cache_status"] = "cache_hit"
            payload["cache_age_seconds"] = age
            return CaneDashboardResponse(**payload)

    try:
        if session_id:
            live_session = caneup_sessions.get_session(session_id)
            if not live_session or not live_session.verified:
                raise HTTPException(
                    status_code=401,
                    detail="Live enquiry.caneup.in session expired or not verified. Please solve CAPTCHA again.",
                )
            profile, tickets, calendar, source = await _fetch_live_caneup_by_ugc(
                session=live_session,
                ugc=ugc,
            )
        else:
            profile, tickets, calendar, source = await adapter.fetch_by_ugc(
                ugc=ugc,
                district=district,
                factory=factory,
                village=village,
                grower_id=grower_id,
            )
    except HTTPException:
        raise
    except Exception as exc:
        stale = cache.get_stale(cache_key)
        if stale is not None and not session_id:
            payload, age = stale
            payload["cache_hit"] = True
            payload["cache_status"] = "stale_fallback"
            payload["cache_age_seconds"] = age
            return CaneDashboardResponse(**payload)
        raise HTTPException(status_code=502, detail=f"Upstream eGANNA error: {exc}") from exc

    eta = calculate_next_purchy_eta(calendar)
    now_iso = datetime.now(timezone.utc).isoformat()
    response = CaneDashboardResponse(
        profile=profile,
        supply_tickets=tickets,
        ganna_calendar=calendar,
        next_purchy_eta=eta,
        source=source,
        cache_hit=False,
        cache_status="fresh_upstream",
        cached_at_iso=now_iso,
        cache_age_seconds=0,
    )
    cache.set(cache_key, response.model_dump())
    return response


@app.get("/api/health")
async def health_check() -> dict:
    return {
        "status": "ok",
        "mode": adapter.mode,
        "cache_ttl_seconds": DEFAULT_CACHE_TTL_SECONDS,
    }


@app.get("/api/presets", response_model=List[PresetProfile])
async def get_preset_profiles() -> List[PresetProfile]:
    presets = _preset_mock_profiles()
    return [
        PresetProfile(
            label=f"{data['grower_name']} ({data['factory'].split()[0]})",
            ugc=ugc,
            district=data["district"],
            factory=data["factory"],
            village=data["village"],
            grower_id=data["grower_id"],
        )
        for ugc, data in presets.items()
    ]


@app.get("/api/caneup/captcha", response_model=CaptchaInitResponse)
async def get_caneup_captcha() -> CaptchaInitResponse:
    try:
        return await caneup_sessions.init_captcha()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to load enquiry.caneup.in CAPTCHA: {exc}") from exc


@app.post("/api/caneup/verify", response_model=CaptchaVerifyResponse)
async def verify_caneup_captcha(body: CaptchaVerifyRequest) -> CaptchaVerifyResponse:
    return await caneup_sessions.verify_captcha(body.session_id, body.captcha)


@app.get("/api/cane/{ugc}", response_model=CaneDashboardResponse)
async def get_cane_by_ugc(
    ugc: str,
    force_refresh: bool = Query(default=False),
    session_id: Optional[str] = Query(default=None),
    district: Optional[str] = Query(default=None),
    factory: Optional[str] = Query(default=None),
    village: Optional[str] = Query(default=None),
    grower_id: Optional[str] = Query(default=None),
) -> CaneDashboardResponse:
    clean_ugc = ugc.strip()
    if not clean_ugc:
        raise HTTPException(status_code=400, detail="UGC code cannot be empty")
    cache_key = f"ugc:{clean_ugc}"
    return await _resolve_cane_dashboard(
        cache_key=cache_key,
        ugc=clean_ugc,
        force_refresh=force_refresh,
        district=district,
        factory=factory,
        village=village,
        grower_id=grower_id,
        session_id=session_id,
    )


@app.get("/api/cane-lookup", response_model=CaneDashboardResponse)
async def get_cane_by_lookup(
    district: str = Query(..., min_length=1),
    factory: str = Query(..., min_length=1),
    village: str = Query(..., min_length=1),
    grower_id: str = Query(..., min_length=1),
    force_refresh: bool = Query(default=False),
) -> CaneDashboardResponse:
    composite_key = (
        f"lookup:{district.strip().lower()}:{factory.strip().lower()}:"
        f"{village.strip().lower()}:{grower_id.strip().lower()}"
    )
    synthetic_ugc = f"LK{abs(hash(composite_key)) % 10**9:09d}"
    return await _resolve_cane_dashboard(
        cache_key=composite_key,
        ugc=synthetic_ugc,
        force_refresh=force_refresh,
        district=district.strip(),
        factory=factory.strip(),
        village=village.strip(),
        grower_id=grower_id.strip(),
    )


# ---------------------------------------------------------------------------
# Daily 10:00 AM Check & Push Notifications (ntfy.sh)
# ---------------------------------------------------------------------------


async def send_push_notification(
    title: str,
    message: str,
    click_url: Optional[str] = None,
) -> Dict[str, str]:
    """Send push notification via ntfy.sh if configured."""
    ntfy_topic = os.getenv("NTFY_TOPIC", "").strip() or cache.get_setting("ntfy_topic").strip()
    if not ntfy_topic:
        return {"status": "ntfy_topic_not_configured"}

    ntfy_server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": title, "Priority": "high", "Tags": "herb,bell"}
    if click_url:
        headers["Click"] = click_url

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{ntfy_server}/{ntfy_topic}",
            content=message.encode("utf-8"),
            headers=headers,
        )
    return {"ntfy": f"HTTP {resp.status_code}"}


@app.get("/api/notifications/status")
async def get_notification_status() -> dict:
    ntfy_topic = os.getenv("NTFY_TOPIC", "").strip() or cache.get_setting("ntfy_topic").strip()
    public_url = os.getenv("PUBLIC_APP_URL", "").strip() or cache.get_setting("public_url").strip()
    return {
        "ntfy_topic": ntfy_topic,
        "public_url": public_url,
        "watched_profiles": cache.list_watched_profiles(),
    }


@app.post("/api/notifications/config")
async def update_notification_config(body: NotificationConfigRequest) -> dict:
    if body.ntfy_topic is not None:
        cache.set_setting("ntfy_topic", body.ntfy_topic.strip())
    if body.public_url is not None:
        cache.set_setting("public_url", body.public_url.strip())
    return await get_notification_status()


@app.post("/api/notifications/watch")
async def add_watched_profile(body: WatchProfileRequest) -> dict:
    clean_ugc = body.ugc.strip()
    if not clean_ugc:
        raise HTTPException(status_code=400, detail="UGC cannot be empty")
    cache.add_watched_profile(clean_ugc, body.label.strip() or clean_ugc)
    return {"watched_profiles": cache.list_watched_profiles()}


@app.delete("/api/notifications/watch/{ugc}")
async def remove_watched_profile(ugc: str) -> dict:
    cache.remove_watched_profile(ugc.strip())
    return {"watched_profiles": cache.list_watched_profiles()}


@app.post("/api/notifications/test")
async def send_test_notification() -> dict:
    public_url = os.getenv("PUBLIC_APP_URL", "").strip() or cache.get_setting("public_url").strip() or None
    delivery = await send_push_notification(
        title="eGANNA Tracker Test Alert",
        message="Daily 10:00 AM Purchy notifications are connected to your iPhone!",
        click_url=public_url,
    )
    return {"sent": True, "delivery": delivery}


@app.post("/api/notifications/run-now")
async def run_daily_check_now(
    session_id: Optional[str] = Query(default=None),
    cron_secret: Optional[str] = Query(default=None),
) -> dict:
    expected_secret = os.getenv("CRON_SECRET", "").strip()
    if expected_secret and cron_secret != expected_secret:
        raise HTTPException(status_code=403, detail="Invalid cron_secret")

    watched = cache.list_watched_profiles()
    if not watched:
        return {"checked": 0, "new_purchies": [], "note": "No watched profiles registered."}

    public_url = os.getenv("PUBLIC_APP_URL", "").strip() or cache.get_setting("public_url").strip() or None
    live_session = caneup_sessions.get_session(session_id) if session_id else None
    if not live_session:
        # Check if any active verified session exists in memory
        live_session = next(
            (s for s in caneup_sessions._sessions.values() if s.verified),
            None,
        )

    new_alerts: List[dict] = []
    if adapter.mode != "live" and (not live_session or not live_session.verified):
        # No active CAPTCHA session for enquiry.caneup.in -> send 1-tap reminder
        delivery = await send_push_notification(
            title="eGANNA Daily Check: CAPTCHA Needed",
            message=f"Tap to solve CAPTCHA once and check today's purchies for {len(watched)} family profile(s).",
            click_url=public_url,
        )
        return {
            "checked": len(watched),
            "status": "captcha_required",
            "new_purchies": [],
            "delivery": delivery,
        }

    for wp in watched:
        ugc = wp["ugc"]
        label = wp["label"]
        try:
            if live_session and live_session.verified:
                profile, tickets, _, _ = await _fetch_live_caneup_by_ugc(live_session, ugc)
            else:
                profile, tickets, _, _ = await adapter.fetch_by_ugc(ugc=ugc)
        except Exception as exc:
            new_alerts.append({"ugc": ugc, "error": str(exc)})
            continue

        for t in tickets:
            if not cache.is_purchy_seen(ugc, t.purchy_no):
                cache.mark_purchy_seen(ugc, t.purchy_no)
                if t.status == "Active":
                    msg = (
                        f"{profile.grower_name} ({label}): New Purchy #{t.purchy_no} "
                        f"• {t.mode} • Valid till {t.expiry_date}"
                    )
                    delivery = await send_push_notification(
                        title="New eGANNA Purchy Issued!",
                        message=msg,
                        click_url=public_url,
                    )
                    new_alerts.append({"ugc": ugc, "purchy_no": t.purchy_no, "delivery": delivery})

    return {"checked": len(watched), "status": "ok", "new_purchies": new_alerts}


@app.get("/", include_in_schema=False)
async def serve_pwa_index() -> FileResponse:
    return FileResponse(BASE_DIR / "index.html", media_type="text/html")


@app.get("/manifest.json", include_in_schema=False)
async def serve_manifest() -> FileResponse:
    return FileResponse(BASE_DIR / "manifest.json", media_type="application/manifest+json")
