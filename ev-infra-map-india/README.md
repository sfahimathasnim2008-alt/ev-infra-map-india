# EV Infra Map India

Flask + SQLite + Leaflet dashboard for EV charging stations and EV registrations.

## Run (Windows)

```powershell
cd ev-infra-map-india
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000. On first start the app loads the bundled data from `data/seed/` into `data/ev_infra.db`.
To reload the bundled data from scratch: `python -c "import app; app.seed_database(force=True)"`.
Internet access is needed for the map tiles and the Leaflet libraries.

## What is loaded

| File in `data/seed/` | Content | Notes |
|---|---|---|
| `stations_kerala_tn_india.csv` | 855 connector rows | Merged into 499 stations; 18 state names corrected; 1 row skipped (blank state). No district column, so district stays "Not recorded". |
| `stations_tn_manual.csv` | 1 Tamil Nadu station | From `tamil_nadu_ev_stations.csv.xlsx`. |
| `pcs_by_state.csv` | Operational public charging stations per state | This is the former `ev_count_state_wise.csv`. It counts chargers, not EVs. Reporting date not recorded in the file. |
| `ev_state_totals.csv` | Registered EVs per state, with date and source | PIB / MoRTH e-Vahan (3 Aug 2023) for AP, Karnataka, Kerala, Odisha, Tamil Nadu; Tamil Nadu FY 2025-26 cumulative as reported by the press. |
| `ev_count_district_template.csv` | 139 districts (TN, Kerala, Karnataka, AP, Odisha) | **All `ev_count` blank = unavailable.** No verified district counts exist yet. |
| `ev_count_district_audit.csv` | Audit table for the above | Status UNAVAILABLE for every row. |

## Data rules

- Blank `ev_count` is stored as NULL and shown as "Data unavailable", never as zero.
- District is never inferred from a city name or coordinates.
- State totals are never split into district values.
- District analysis runs only for districts with a verified count.

## Importing

Use the *Data & import* tab. District CSV format: `state,district,ev_count`. Download the current table in that format from the *Districts* tab; unavailable counts stay blank.
If a downstream importer accepts only numbers, filter out blank rows or store them as NULL; do not substitute 0 or -1.

## Tests

```powershell
python -m unittest discover -s tests -v
```

## Structure

- `app.py` routes, importers, SQLite, planning calculation
- `templates/index.html` dashboard (Overview, States, Districts, Data & import, Methodology)
- `static/css/style.css`, `static/js/app.js`
- `data/seed/` bundled data, `docs/METHODOLOGY.md`, `tests/test_app.py`
- `legacy/` the original static HTML version
