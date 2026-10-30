# EV Infra Map India — Methodology Note

## 1. Purpose

The prototype brings station-level charging data and district-level EV registration counts into one interface. It provides a map of imported stations and a transparent, configurable demand-screening calculation to help prioritize further investigation.

## 2. Data layers and provenance

| Layer | Suggested source | Use | Important limitation |
|---|---|---|---|
| Charging station locations | Tamil Nadu Government charger map; BEE public station list; documented CPO/open data | Individual map markers and district station counts | A station list may not expose a bulk CSV or coordinates for every entry; validate status and duplicates |
| EV registrations | VAHAN Public Dashboard or official downloadable reports | EV demand by state/district | Record registration period, vehicle category and retrieval date; dashboard access may not mean an open API is available |
| State-level deployment context | Ministry of Heavy Industries / PIB | Cross-check broad totals | Historical deployment totals are not a live operational station count |
| District boundaries / roads | Official state GIS, Survey of India / other licensed GIS sources, OpenStreetMap under its terms | Future geographic coverage and road-distance analysis | Not used to invent or infer station locations in the current build |

## 2a. Changes in this version

- Station imports no longer require a district. Rows that describe the same site are merged and the row count becomes the connector count.
- Obvious state-name typos (for example "Keral", "Lerala") are corrected to the official name and counted in the import log.
- A blank `ev_count` is stored as unavailable. District rows without a count never receive a computed gap.
- State-level totals (EVs and operational public charging stations) are kept in separate tables with their own dates and are never divided into districts.

## 3. Import validation

Station CSV imports require station ID, station name, state, district, latitude and longitude. Coordinates are checked for numeric values, legal latitude/longitude ranges and a broad India bounding box. Rows missing required fields or failing coordinate validation are skipped and counted. A repeated station ID updates the existing row. These checks do not prove that a station is real or currently operational; source provenance and manual verification remain necessary.

EV district imports require state, district and a non-negative integer EV count. The data date and source are retained where supplied. A missing district row is unknown, not zero. State/district labels should be normalized before analysis.

## 4. Demand-based screening

Let E be the uploaded district EV count, R the configurable planning ratio (EVs per comparable charging unit), and U the existing comparable units.

- Target units = ceiling(E / R)
- Estimated additional units = maximum(0, target units − U)

For the current prototype, U sums the `connectors` field if available; if it is missing, one unit per station record is used as a provisional proxy. This can be inaccurate where a station has multiple EVSEs or connectors. A final study should define whether its unit is a site, station, EVSE, charger or connector and convert sources consistently. The default ratio of 100 is an example assumption, not an official planning standard.

## 5. Geographic coverage interpretation

The map displays the coordinates of uploaded records only. The district screening reports districts with no station records in the loaded dataset, which must not be interpreted as proof of no real stations. If sourced district point coordinates are supplied in the EV CSV, the app calculates straight-line distance from that point to the nearest station and flags points beyond a configurable threshold. A district point is only a proxy, not full district-wide coverage. True geographic coverage requires verified station coordinates plus district boundaries and preferably road-network distances and a population/traffic demand layer. Future work should calculate distances from populated cells/settlements to the nearest compatible operational station and evaluate charger power, utilization, road access, land availability and grid capacity.

## 6. Decision support and limits

The table is a screening aid, not a final siting recommendation. High estimated demand gaps can be caused by incomplete station data, mismatched district names or a planning ratio that does not reflect vehicle mix and usage. Results should show source, reporting period, missing-data rate and sensitivity to alternative ratios. Recommendations must be validated with local authorities and operators before implementation.

## 7. Suggested evaluation plan

1. Validate a sample of station records against the official source.
2. Compare imported district station totals with a source table from the same date and definition.
3. Test duplicate, missing, invalid and out-of-range records.
4. Run low/base/high planning-ratio scenarios and compare rankings.
5. Inspect candidate coverage gaps with district/road maps and local knowledge.
6. Record limitations and data refresh dates in the final demonstration.
