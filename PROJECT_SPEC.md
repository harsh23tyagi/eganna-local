# Project Goal: eGANNA iOS Personal Tracker (FastAPI + Mobile PWA)

## Context & Problem
The Uttar Pradesh Sugarcane Department's official mobile app (`eGANNA CANE UP`, Android package `com.sgk.eganna.in`) is Android-only. The official web portal (`https://enquiry.caneup.in/`) suffers from frequent ASP.NET session/CAPTCHA validation failures and poor mobile UX.

This project builds a lightweight, personal iOS-friendly Progressive Web App (PWA) backed by a FastAPI proxy that queries the eGANNA backend directly so family sugarcane supply tickets (Purchy), Ganna Calendar, and Weighment (Taul) status can be tracked from an iPhone.

## Core Objectives
1. **API Extraction / Data Layer:**
   - Support extracting/calling the upstream mobile API endpoints used by `com.sgk.eganna.in` (or scraping/proxying `enquiry.caneup.in` / NIC IP `164.100.181.16`).
   - Bypass SSL certificate chain issues (`verify=False` in `httpx`) and spoof the Android app's headers (`User-Agent: okhttp/4.9.3`).
   - Include a clean adapter interface so exact decompiled APK endpoints (via `jadx`) can be dropped in while providing working mock data out of the box for UI testing.

2. **FastAPI Backend (`app.py`):**
   - Expose a single unified endpoint: `GET /api/cane/{ugc}?force_refresh=bool` (plus support for `{district, factory, village, grower_id}` lookup).
   - Implement an in-memory/SQLite TTL cache (15–30 mins) so the app loads instantaneously on iOS even when upstream UP NIC servers are slow or down.
   - Normalize upstream data into three clean JSON models:
     1. `FarmerProfile`: Grower Name, Father's Name, Village, Factory, Basic Quota, Total Bonding, Total Purchy Allotted, Purchy Issued Count, Current Mill Running Pakhwada (1–12) & Running Column (1–5).
     2. `SupplyTickets` (सप्लाई टिकट / गन्ना तौल): List of issued purchies with Purchy No., Pakhwada/Column slot, Issue Date, Expiry Date, Mode (Gate/Centre), Status (Active/Weighed/Expired), and Net Weight in Quintals.
     3. `GannaCalendar` (गन्ना कैलेंडर & अतिरिक्त सट्टा कैलेंडर): A 12 Pakhwada (Fortnight) × 5 Column (60-slot) matrix showing scheduled purchies (`0` or count), highlighting passed/issued slots, the mill's current running slot, and upcoming slots.

3. **Next Purchy ETA Calculator:**
   - Calculate the estimated days until the next purchy is issued based on the distance in slots between the factory's current running `(pakhwada, column)` and the farmer's next scheduled calendar slot (assuming ~3 days per column / ~15 days per pakhwada).

4. **iOS Standalone PWA Frontend (`index.html` + `manifest.json`):**
   - Mobile-first, dark-mode UI built with Tailwind CSS, configured with Apple WebView meta tags (`apple-mobile-web-app-capable`) so "Add to Home Screen" in iOS Safari runs full-screen like a native app.
   - **Multi-Profile Switcher:** Store multiple family UGC codes / Grower profiles in `localStorage` with a 1-tap dropdown switcher at the top (solving the official app's single-account limitation).
   - **Top KPI Cards:** Issued vs. Total Purchies, Mill Running Slot (`P# • Col #`), and Next Purchy ETA (`~X days`).
   - **Tabbed Views:**
     - *Tab 1: Supply Tickets & Taul* — Cards showing active and weighed purchies.
     - *Tab 2: 60-Slot Ganna Calendar Grid* — Color-coded 12×5 matrix (Pink = Issued, Amber = Mill Running Slot, Emerald = Upcoming Scheduled Purchy).

## Deliverables & Subtasks
- [ ] **Subtask 1: Project Setup & Dependencies (`requirements.txt`)**
  - Define Python dependencies (`fastapi`, `uvicorn[standard]`, `httpx`, `pydantic`, `beautifulsoup4`).
- [ ] **Subtask 2: Data Models, ETA Engine & SQLite TTL Cache (`app.py`)**
  - Define Pydantic schemas (`FarmerProfile`, `SupplyTicket`, `CalendarSlot`, `GannaCalendar`, `NextPurchyETA`, `CaneDashboardResponse`).
  - Build SQLite-backed TTL cache (default 20 minutes) with `force_refresh` bypass and stale-if-error fallback.
  - Implement the 60-slot linear index ETA calculation (`slot_index = (pakhwada - 1) * 5 + column`, `delta_slots * 3` days).
- [ ] **Subtask 3: Pluggable Upstream Adapter & Rich Family Mock Profiles (`app.py`)**
  - Build `EGannaUpstreamAdapter` with `httpx.AsyncClient(verify=False, headers={"User-Agent": "okhttp/4.9.3", ...})`.
  - Support lookup by `ugc` or `{district, factory, village, grower_id}` query parameters.
  - Provide realistic multi-member family mock profiles out of the box so the UI works immediately and switches smoothly between profiles.
- [ ] **Subtask 4: iOS PWA Frontend & Web Manifest (`static/index.html`, `static/manifest.json`)**
  - Configure iOS standalone PWA meta tags, safe-area padding, and dark-mode Tailwind layout.
  - Implement `localStorage`-backed multi-profile switcher + modal to add by UGC or District/Factory/Village/Grower ID.
  - Render Top KPI summary cards (Issued/Allotted, Mill Running Slot, Next Purchy ETA, Total Weighed Quintals).
  - Render Tab 1 (Supply Tickets & Taul filterable cards) and Tab 2 (12×5 60-slot color-coded Ganna Calendar matrix + Raw/Additional bonding toggle).
- [ ] **Subtask 5: APK Endpoint Extraction Script (`scripts/extract_endpoints.sh`)**
  - Create a shell script using `jadx` and `rg`/`grep` to extract Retrofit annotations, base URLs, and Volley endpoints from `eganna.apk`.
- [ ] **Subtask 6: End-to-End Verification**
  - Launch the local FastAPI server and verify all API routes, caching behavior, ETA computation, and frontend rendering.
