"""EV Infra Map India - Flask backend.

Data policy (unchanged from the original prototype):
  * nothing is invented: unknown values stay NULL and are shown as "Data unavailable";
  * a blank ev_count means "unavailable", never zero;
  * district is never inferred from a city name or from coordinates.
"""
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file
from pathlib import Path
import sqlite3, csv, io, os, math, re, hashlib, difflib, json
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / 'data' / 'ev_infra.db'
SEED_DIR = BASE_DIR / 'data' / 'seed'

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-only-change-this-key')
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

STATION_FIELDS = ['station_id', 'station_name', 'state', 'district', 'city', 'latitude', 'longitude',
                  'operator', 'usage_type', 'charger_type', 'status', 'power_kw', 'connectors',
                  'source', 'source_date']
EV_FIELDS = ['state', 'district', 'ev_count', 'data_date', 'source', 'latitude', 'longitude']
NOT_RECORDED = 'Not recorded'

CANONICAL_STATES = [
    'Andaman and Nicobar Islands', 'Andhra Pradesh', 'Arunachal Pradesh', 'Assam', 'Bihar', 'Chandigarh',
    'Chhattisgarh', 'Delhi', 'Goa', 'Gujarat', 'Haryana', 'Himachal Pradesh', 'Jammu and Kashmir',
    'Jharkhand', 'Karnataka', 'Kerala', 'Ladakh', 'Lakshadweep', 'Madhya Pradesh', 'Maharashtra',
    'Manipur', 'Meghalaya', 'Mizoram', 'Nagaland', 'Odisha', 'Puducherry', 'Punjab', 'Rajasthan',
    'Sikkim', 'Tamil Nadu', 'Telangana', 'Tripura', 'Uttar Pradesh', 'Uttarakhand', 'West Bengal',
    'Dadra and Nagar Haveli and Daman and Diu']
_STATE_LOOKUP = {s.casefold(): s for s in CANONICAL_STATES}
_STATE_LOOKUP.update({'andaman and nicobar': 'Andaman and Nicobar Islands', 'orissa': 'Odisha',
                      'pondicherry': 'Puducherry', 'nct of delhi': 'Delhi'})


@app.template_filter('inr')
def inr(value):
    """Indian digit grouping: 552214 -> 5,52,214. None -> em dash."""
    if value is None or value == '':
        return '\u2014'
    n = int(value)
    sign, s = ('-' if n < 0 else ''), str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:]); head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ','.join(parts + [tail])
    return sign + s


# --------------------------------------------------------------------------- database
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _ensure_columns(con, table, columns):
    existing = {r['name'] for r in con.execute(f'PRAGMA table_info({table})')}
    for name, ddl in columns.items():
        if name not in existing:
            con.execute(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}')


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS stations (
            station_id TEXT PRIMARY KEY, station_name TEXT NOT NULL, state TEXT NOT NULL,
            district TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
            charger_type TEXT, status TEXT, power_kw REAL, connectors INTEGER,
            source TEXT, source_date TEXT, imported_at TEXT NOT NULL)''')
        # Upgrade databases created by the first prototype.
        _ensure_columns(con, 'stations', {'city': 'TEXT', 'operator': 'TEXT', 'usage_type': 'TEXT'})
        con.execute('''CREATE TABLE IF NOT EXISTS ev_districts (
            state TEXT NOT NULL, district TEXT NOT NULL, ev_count INTEGER,
            data_date TEXT, source TEXT, latitude REAL, longitude REAL, imported_at TEXT NOT NULL,
            PRIMARY KEY(state, district))''')
        con.execute('''CREATE TABLE IF NOT EXISTS ev_state_totals (
            state TEXT NOT NULL, ev_count INTEGER NOT NULL, data_date TEXT NOT NULL,
            source TEXT, definition TEXT, imported_at TEXT NOT NULL,
            PRIMARY KEY(state, data_date))''')
        con.execute('''CREATE TABLE IF NOT EXISTS pcs_state (
            state TEXT PRIMARY KEY, operational_pcs INTEGER NOT NULL, source TEXT, data_date TEXT,
            imported_at TEXT NOT NULL)''')
        con.execute('''CREATE TABLE IF NOT EXISTS import_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, dataset TEXT, filename TEXT,
            imported_rows INTEGER, skipped_rows INTEGER, imported_at TEXT, note TEXT)''')


# --------------------------------------------------------------------------- parsing helpers
def clean(v):
    return str(v).strip() if v is not None else ''


def normalize_header(value):
    return re.sub(r'[^a-z0-9]+', '_', clean(value).lower()).strip('_')


def parse_float(v):
    text = clean(v).replace(',', '')
    if text == '':
        return None
    match = re.search(r'-?\d+(?:\.\d+)?', text)
    if not match:
        raise ValueError('Not a numeric value')
    return float(match.group(0))


def parse_int(v):
    value = parse_float(v)
    return None if value is None else int(value)


def first_value(row, *keys):
    for key in keys:
        value = clean(row.get(key, ''))
        if value:
            return value
    return ''


def normalize_state(raw):
    """Return (canonical_name, was_corrected). Fixes whitespace/case and obvious typos
    (e.g. 'Keral', 'Lerala'); returns (None, False) when no confident match exists."""
    text = clean(raw)
    if not text:
        return None, False
    key = text.casefold()
    if key in _STATE_LOOKUP:
        return _STATE_LOOKUP[key], text != _STATE_LOOKUP[key]
    match = difflib.get_close_matches(key, list(_STATE_LOOKUP), n=1, cutoff=0.8)
    if match:
        return _STATE_LOOKUP[match[0]], True
    return None, False


def read_csv_text(file_storage):
    try:
        return file_storage.stream.read().decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('Could not read this CSV as UTF-8. Save it as CSV UTF-8 and upload again.') from exc


def _log(con, dataset, filename, inserted, skipped, now, note):
    con.execute('INSERT INTO import_log(dataset,filename,imported_rows,skipped_rows,imported_at,note) VALUES(?,?,?,?,?,?)',
                (dataset, filename, inserted, skipped, now, note))


# --------------------------------------------------------------------------- importers
def import_stations(file_storage):
    """Import station rows. Accepts the project template and the BEE/OpenChargeMap-style
    export (Station Name, City, State, Latitude, Longitude, Operator, Usage Type,
    Connector Type, Power (kW)).

    Rows that describe the same site (same state, name and coordinates) are one station:
    each extra row is counted as another connector and the highest power is kept.
    District is optional and is never inferred.
    Returns (stations_saved, rows_skipped, notes_dict).
    """
    reader = csv.DictReader(io.StringIO(read_csv_text(file_storage)))
    if not reader.fieldnames:
        raise ValueError('The CSV is empty or does not contain a header row.')
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    source_name = clean(getattr(file_storage, 'filename', '')) or 'Uploaded CSV'

    sites, skipped_reasons, corrected = {}, {}, 0

    def skip(reason):
        skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1

    for original in reader:
        r = {normalize_header(k): clean(v) for k, v in original.items() if k}
        try:
            state, fixed = normalize_state(first_value(r, 'state'))
            if not state:
                skip('missing or unrecognised state'); continue
            corrected += fixed
            district = first_value(r, 'district') or NOT_RECORDED
            city = first_value(r, 'city_village', 'city', 'village')
            location = first_value(r, 'location', 'station_location', 'address')
            cpo = first_value(r, 'operator', 'cpo_name', 'charging_point_operator')
            name = first_value(r, 'station_name', 'charging_station_name') or location or city or cpo
            lat = parse_float(first_value(r, 'latitude', 'lat'))
            lon = parse_float(first_value(r, 'longitude', 'long', 'lon', 'lng'))
            if not name:
                skip('missing station name'); continue
            if lat is None or lon is None:
                skip('missing coordinates'); continue
            if not (6 <= lat <= 38 and 67 <= lon <= 98):
                skip('coordinates outside India'); continue

            charger = first_value(r, 'charger_type', 'types_of_chargers_installed_connector',
                                  'type_of_charger', 'connector_type', 'charger_connector_type')
            power_text = first_value(r, 'power_kw', 'charger_rating', 'connector_rating')
            power = parse_float(power_text) if power_text else None
            conn_text = first_value(r, 'connectors', 'no_of_connector', 'number_of_connectors')
            explicit_connectors = parse_int(conn_text) if conn_text else None
            sid = first_value(r, 'station_id', 'station_code', 'evse_id', 'charging_station_id')
            identity = '|'.join([state.casefold(), name.casefold(), f'{lat:.5f}', f'{lon:.5f}'])
            sid = sid or 'EV-' + hashlib.sha1(identity.encode('utf-8')).hexdigest()[:12].upper()

            site = sites.get(sid)
            if site is None:
                sites[sid] = dict(
                    station_id=sid, station_name=name, state=state, district=district, city=city,
                    latitude=lat, longitude=lon, operator=cpo,
                    usage_type=first_value(r, 'usage_type'), types=[charger] if charger else [],
                    status=first_value(r, 'status', 'charging_status') or 'Unknown', power_kw=power,
                    rows=1, explicit_connectors=explicit_connectors,
                    source=first_value(r, 'source', 'data_source') or source_name,
                    source_date=first_value(r, 'source_date', 'data_date', 'date'))
            else:
                site['rows'] += 1
                if charger and charger not in site['types']:
                    site['types'].append(charger)
                if power is not None and (site['power_kw'] is None or power > site['power_kw']):
                    site['power_kw'] = power
        except (ValueError, TypeError):
            skip('non-numeric value in a numeric column')

    with db() as con:
        for s in sites.values():
            connectors = s['explicit_connectors'] if s['explicit_connectors'] is not None else s['rows']
            con.execute("""INSERT INTO stations(station_id,station_name,state,district,latitude,longitude,
                    charger_type,status,power_kw,connectors,source,source_date,imported_at,city,operator,usage_type)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(station_id) DO UPDATE SET station_name=excluded.station_name,
                    state=excluded.state,district=excluded.district,latitude=excluded.latitude,
                    longitude=excluded.longitude,charger_type=excluded.charger_type,status=excluded.status,
                    power_kw=excluded.power_kw,connectors=excluded.connectors,source=excluded.source,
                    source_date=excluded.source_date,imported_at=excluded.imported_at,city=excluded.city,
                    operator=excluded.operator,usage_type=excluded.usage_type""",
                        (s['station_id'], s['station_name'], s['state'], s['district'], s['latitude'],
                         s['longitude'], ' / '.join(s['types']), s['status'], s['power_kw'], connectors,
                         s['source'], s['source_date'], now, s['city'], s['operator'], s['usage_type']))
        skipped = sum(skipped_reasons.values())
        notes = {'state_names_corrected': corrected, 'skipped_reasons': skipped_reasons,
                 'district_not_recorded': sum(1 for s in sites.values() if s['district'] == NOT_RECORDED)}
        _log(con, 'stations', source_name, len(sites), skipped, now, json.dumps(notes))
    return len(sites), skipped, notes


def import_ev(file_storage):
    """Import district EV counts. A blank ev_count is stored as NULL = data unavailable."""
    raw = read_csv_text(file_storage)
    reader = csv.DictReader(io.StringIO(raw))
    headers = {normalize_header(h) for h in (reader.fieldnames or [])}
    if not {'state', 'district', 'ev_count'}.issubset(headers):
        raise ValueError('EV CSV must include: state, district, ev_count')
    inserted = skipped = unavailable = 0
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with db() as con:
        for original in reader:
            r = {normalize_header(k): clean(v) for k, v in original.items() if k}
            try:
                state, _ = normalize_state(r.get('state', ''))
                district = r.get('district', '')
                if not state or not district:
                    raise ValueError()
                ev_count = parse_int(r.get('ev_count'))
                if ev_count is not None and ev_count < 0:
                    raise ValueError()
                lat, lon = parse_float(r.get('latitude')), parse_float(r.get('longitude'))
                if lat is not None and not (-90 <= lat <= 90): raise ValueError()
                if lon is not None and not (-180 <= lon <= 180): raise ValueError()
                con.execute('''INSERT INTO ev_districts(state,district,ev_count,data_date,source,latitude,longitude,imported_at)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(state,district) DO UPDATE SET ev_count=excluded.ev_count,
                    data_date=excluded.data_date,source=excluded.source,latitude=excluded.latitude,
                    longitude=excluded.longitude,imported_at=excluded.imported_at''',
                            (state, district, ev_count, r.get('data_date', ''), r.get('source', ''), lat, lon, now))
                inserted += 1
                unavailable += ev_count is None
            except (ValueError, TypeError):
                skipped += 1
        _log(con, 'ev_districts', getattr(file_storage, 'filename', '') or 'Uploaded CSV', inserted, skipped, now,
             json.dumps({'count_unavailable': unavailable}))
    return inserted, skipped, unavailable


def import_state_totals(file_storage):
    reader = csv.DictReader(io.StringIO(read_csv_text(file_storage)))
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    n = 0
    with db() as con:
        for original in reader:
            r = {normalize_header(k): clean(v) for k, v in original.items() if k}
            state, _ = normalize_state(r.get('state', ''))
            count = parse_int(r.get('ev_count'))
            if not state or count is None or not r.get('data_date'):
                continue
            con.execute('''INSERT INTO ev_state_totals(state,ev_count,data_date,source,definition,imported_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(state,data_date) DO UPDATE SET ev_count=excluded.ev_count,
                source=excluded.source,definition=excluded.definition,imported_at=excluded.imported_at''',
                        (state, count, r['data_date'], r.get('source', ''), r.get('definition', ''), now))
            n += 1
        _log(con, 'ev_state_totals', getattr(file_storage, 'filename', '') or 'seed', n, 0, now, '')
    return n


def import_pcs(file_storage):
    """Operational public charging stations (PCS) per state, as published by BEE."""
    reader = csv.DictReader(io.StringIO(read_csv_text(file_storage)))
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    n = 0
    with db() as con:
        for original in reader:
            r = {normalize_header(k): clean(v) for k, v in original.items() if k}
            state, _ = normalize_state(r.get('state_ut') or r.get('state'))
            count = parse_int(r.get('no_of_operational_pcs') or r.get('operational_pcs'))
            if not state or count is None:
                continue  # skips the 'Total' row
            con.execute('''INSERT INTO pcs_state(state,operational_pcs,source,data_date,imported_at) VALUES(?,?,?,?,?)
                ON CONFLICT(state) DO UPDATE SET operational_pcs=excluded.operational_pcs,
                source=excluded.source,data_date=excluded.data_date,imported_at=excluded.imported_at''',
                        (state, count, 'State-wise operational PCS table supplied with the project (pcs_by_state.csv)',
                         '', now))
            n += 1
        _log(con, 'pcs_state', getattr(file_storage, 'filename', '') or 'seed', n, 0, now, '')
    return n


class _LocalFile:
    """Lets the importers read a file from disk the same way they read an upload."""
    def __init__(self, path):
        self.filename = Path(path).name
        self.stream = open(path, 'rb')

    def close(self):
        self.stream.close()


def seed_database(force=False):
    """Load bundled data from data/seed on first run (or when force=True)."""
    init_db()
    with db() as con:
        if force:
            for t in ('stations', 'ev_districts', 'ev_state_totals', 'pcs_state', 'import_log'):
                con.execute(f'DELETE FROM {t}')
        empty = con.execute('SELECT COUNT(*) FROM stations').fetchone()[0] == 0
    if not (force or empty) or not SEED_DIR.exists():
        return
    jobs = [('stations_kerala_tn_india.csv', import_stations), ('stations_tn_manual.csv', import_stations),
            ('ev_count_district_template.csv', import_ev), ('ev_state_totals.csv', import_state_totals),
            ('pcs_by_state.csv', import_pcs)]
    for filename, fn in jobs:
        path = SEED_DIR / filename
        if path.exists():
            f = _LocalFile(path)
            try:
                fn(f)
            finally:
                f.close()


# --------------------------------------------------------------------------- queries
def _state_clause(state):
    return ('', ()) if not state or state == 'All India' else (' WHERE lower(state)=lower(?)', (state,))


def get_stations(state=''):
    where, args = _state_clause(state)
    with db() as con:
        rows = con.execute(f'SELECT * FROM stations{where} ORDER BY state, station_name', args).fetchall()
    return [dict(r) for r in rows]


def get_ev_rows(state=''):
    where, args = _state_clause(state)
    with db() as con:
        rows = con.execute(f'SELECT * FROM ev_districts{where} ORDER BY state, district', args).fetchall()
    return [dict(r) for r in rows]


def get_state_overview():
    """One row per state combining stations on the map, BEE operational PCS and EV totals."""
    with db() as con:
        stations = {r['state']: dict(r) for r in con.execute(
            'SELECT state, COUNT(*) n, COALESCE(SUM(connectors),0) conn FROM stations GROUP BY state')}
        pcs = {r['state']: r['operational_pcs'] for r in con.execute('SELECT state, operational_pcs FROM pcs_state')}
        totals = {}
        for r in con.execute('SELECT * FROM ev_state_totals ORDER BY state, data_date DESC'):
            totals.setdefault(r['state'], []).append(dict(r))
        district_rows = {r['state']: dict(r) for r in con.execute(
            'SELECT state, COUNT(*) total, SUM(ev_count IS NOT NULL) available FROM ev_districts GROUP BY state')}
    names = sorted(set(stations) | set(pcs) | set(totals) | set(district_rows))
    out = []
    for s in names:
        st = stations.get(s, {})
        dr = district_rows.get(s, {})
        out.append({'state': s, 'stations_mapped': st.get('n', 0), 'connectors_mapped': st.get('conn', 0),
                    'operational_pcs': pcs.get(s), 'ev_totals': totals.get(s, []),
                    'districts_total': dr.get('total', 0), 'districts_with_count': dr.get('available') or 0})
    return out


def haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0, 1 - a)))


def make_gap_rows(state='Tamil Nadu', target_ratio=100, radius_km=10):
    """District screening. Districts without a verified EV count are reported as unavailable,
    never as zero, and never receive a computed gap."""
    evrows = get_ev_rows(state)
    stations = get_stations(state)
    by_district = {}
    for s in stations:
        if s['district'] and s['district'] != NOT_RECORDED:
            by_district.setdefault((s['state'].casefold(), s['district'].casefold()), []).append(s)
    rows = []
    for ev in evrows:
        ss = by_district.get((ev['state'].casefold(), ev['district'].casefold()), [])
        units = sum((s.get('connectors') or 1) for s in ss)
        base = {'state': ev['state'], 'district': ev['district'], 'ev_count': ev['ev_count'],
                'station_count': len(ss), 'units': units, 'ev_source': ev.get('source', ''),
                'ev_date': ev.get('data_date', ''), 'ev_per_unit': None, 'target_units': None,
                'estimated_additional_units': None, 'nearest_station_km': None}
        if ev['ev_count'] is None:
            base['status'] = 'EV count unavailable'
            rows.append(base)
            continue
        target = math.ceil(ev['ev_count'] / target_ratio) if target_ratio > 0 else None
        gap = max(0, target - units) if target is not None else None
        nearest = None
        if ev.get('latitude') is not None and ev.get('longitude') is not None and stations:
            nearest = min(haversine_km(ev['latitude'], ev['longitude'], s['latitude'], s['longitude'])
                          for s in stations)
        if nearest is not None and nearest > radius_km:
            status = f'Geographic gap > {radius_km:g} km'
        elif not ss and nearest is None:
            status = 'No station records / coordinates'
        elif gap and gap > 0:
            status = 'Demand gap'
        elif nearest is not None:
            status = 'Within distance threshold'
        else:
            status = 'Review coverage'
        base.update({'ev_per_unit': round(ev['ev_count'] / units, 1) if units else None, 'target_units': target,
                     'estimated_additional_units': gap,
                     'nearest_station_km': round(nearest, 1) if nearest is not None else None, 'status': status})
        rows.append(base)
    rows.sort(key=lambda r: (r['ev_count'] is not None, r['estimated_additional_units'] or 0, r['ev_count'] or 0),
              reverse=True)
    return rows


def recent_imports(limit=8):
    with db() as con:
        rows = con.execute('SELECT * FROM import_log ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d['notes'] = json.loads(d.get('note') or '{}')
        except ValueError:
            d['notes'] = {}
        out.append(d)
    return out


# --------------------------------------------------------------------------- routes
@app.route('/')
def index():
    state = request.args.get('state', 'All India')
    with db() as con:
        states = [r[0] for r in con.execute(
            'SELECT state FROM stations UNION SELECT state FROM ev_districts UNION SELECT state FROM pcs_state '
            'UNION SELECT state FROM ev_state_totals ORDER BY 1')]
        n_dist = con.execute('SELECT COUNT(*), COALESCE(SUM(ev_count IS NOT NULL),0) FROM ev_districts').fetchone()
    return render_template('index.html', state=state, states=states, state_overview=get_state_overview(),
                           n_districts=n_dist[0], n_district_counts=n_dist[1], imports=recent_imports(),
                           station_fields=STATION_FIELDS, ev_fields=EV_FIELDS)


@app.route('/upload', methods=['POST'])
def upload():
    kind = request.form.get('dataset')
    f = request.files.get('file')
    if not f or not f.filename:
        flash('Choose a CSV file before uploading.', 'error'); return redirect(url_for('index') + '#data')
    if not f.filename.lower().endswith('.csv'):
        flash('Please upload a .csv file. Convert Excel files to CSV first.', 'error'); return redirect(url_for('index') + '#data')
    try:
        if kind == 'stations':
            saved, skipped, notes = import_stations(f)
            extra = f" {notes['state_names_corrected']} state name(s) corrected." if notes['state_names_corrected'] else ''
            flash(f'Stations imported: {saved} station(s) saved, {skipped} row(s) skipped.{extra}', 'success')
        else:
            saved, skipped, unavailable = import_ev(f)
            flash(f'District EV counts imported: {saved} row(s) saved ({unavailable} marked unavailable), {skipped} skipped.', 'success')
    except Exception as exc:
        flash(f'Import failed: {exc}', 'error')
    return redirect(url_for('index') + '#data')


@app.route('/api/stations')
def api_stations():
    return jsonify(get_stations(request.args.get('state', 'All India')))


@app.route('/api/states')
def api_states():
    return jsonify(get_state_overview())


@app.route('/api/gaps')
def api_gaps():
    state = request.args.get('state', 'All India')
    try: ratio = max(1, min(10000, int(request.args.get('target_ratio', 100))))
    except ValueError: ratio = 100
    try: radius = max(1, min(500, float(request.args.get('radius_km', 10))))
    except ValueError: radius = 10
    return jsonify(make_gap_rows(state, ratio, radius))


@app.route('/download-template/<kind>')
def download_template(kind):
    if kind == 'stations':
        content, filename = ','.join(STATION_FIELDS) + '\n', 'charging_stations_template.csv'
    elif kind == 'ev':
        content, filename = ','.join(EV_FIELDS) + '\n', 'ev_district_template.csv'
    else:
        return 'Unknown template', 404
    return send_file(io.BytesIO(content.encode()), mimetype='text/csv', as_attachment=True, download_name=filename)


@app.route('/download/ev-districts')
def download_ev_districts():
    """CSV in the exact state,district,ev_count shape; unavailable counts stay blank."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['state', 'district', 'ev_count'])
    for r in get_ev_rows(request.args.get('state', 'All India')):
        w.writerow([r['state'], r['district'], '' if r['ev_count'] is None else r['ev_count']])
    return send_file(io.BytesIO(out.getvalue().encode()), mimetype='text/csv', as_attachment=True,
                     download_name='ev_count.csv')


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


seed_database()
if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_DEBUG', '1') == '1')
