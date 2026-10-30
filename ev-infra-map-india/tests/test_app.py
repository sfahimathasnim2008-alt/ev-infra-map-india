import io
import tempfile
import unittest
from pathlib import Path

import app as project


def upload(client, dataset, text, name='data.csv'):
    return client.post('/upload', data={'dataset': dataset, 'file': (io.BytesIO(text.encode()), name)},
                       content_type='multipart/form-data', follow_redirects=True)


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_db, self._orig_seed = project.DB_PATH, project.SEED_DIR
        project.DB_PATH = Path(self.tmp.name) / 'test.db'
        project.SEED_DIR = Path(self.tmp.name) / 'no-seed'   # start every test from an empty database
        project.init_db()
        self.client = project.app.test_client()

    def tearDown(self):
        project.DB_PATH, project.SEED_DIR = self._orig_db, self._orig_seed
        self.tmp.cleanup()

    def test_home_loads_with_empty_database(self):
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'EV Infra Map India', r.data)

    def test_station_template_download(self):
        r = self.client.get('/download-template/stations')
        self.assertIn(b'latitude,longitude', r.data)

    def test_bee_style_csv_without_district_imports_and_merges_connectors(self):
        csv_data = ('Station Name,City,State,Latitude,Longitude,Operator,Usage Type,Connector Type,Power (kW)\n'
                    'Site A,Kochi,Keral,10.01,76.31,Tata Power,Public,CCS (Type 2),60\n'
                    'Site A,Kochi,Kerala ,10.01,76.31,Tata Power,Public,CHAdeMO,30\n'
                    'Site B,Chennai,Tamil Nadu,13.08,80.27,GO EC,Public,CCS (Type 2),30\n')
        saved, skipped, notes = project.import_stations(
            type('F', (), {'stream': io.BytesIO(csv_data.encode()), 'filename': 'x.csv'})())
        self.assertEqual((saved, skipped), (2, 0))
        self.assertEqual(notes['state_names_corrected'], 1)  # 'Keral'; plain whitespace is trimmed silently
        a = [s for s in project.get_stations('Kerala')][0]
        self.assertEqual(a['connectors'], 2)
        self.assertEqual(a['power_kw'], 60)
        self.assertEqual(a['district'], project.NOT_RECORDED)

    def test_invalid_rows_are_skipped_with_reasons(self):
        csv_data = ('Station Name,State,Latitude,Longitude\n'
                    'Bad coords,Tamil Nadu,0,0\n'
                    'No state,,13.0,80.0\n')
        saved, skipped, notes = project.import_stations(
            type('F', (), {'stream': io.BytesIO(csv_data.encode()), 'filename': 'bad.csv'})())
        self.assertEqual((saved, skipped), (0, 2))
        self.assertEqual(len(notes['skipped_reasons']), 2)

    def test_state_name_normalisation(self):
        self.assertEqual(project.normalize_state('Lerala')[0], 'Kerala')
        self.assertEqual(project.normalize_state(' uttar pradesh ')[0], 'Uttar Pradesh')
        self.assertEqual(project.normalize_state('Atlantis')[0], None)

    def test_blank_ev_count_is_unavailable_not_zero(self):
        upload(self.client, 'ev', 'state,district,ev_count\nTamil Nadu,Chennai,\nTamil Nadu,Madurai,1200\n')
        rows = {r['district']: r['ev_count'] for r in project.get_ev_rows('Tamil Nadu')}
        self.assertIsNone(rows['Chennai'])
        self.assertEqual(rows['Madurai'], 1200)
        gaps = {r['district']: r for r in project.make_gap_rows('Tamil Nadu', 100)}
        self.assertEqual(gaps['Chennai']['status'], 'EV count unavailable')
        self.assertIsNone(gaps['Chennai']['estimated_additional_units'])
        self.assertEqual(gaps['Madurai']['target_units'], 12)

    def test_negative_ev_count_skipped(self):
        saved, skipped, _ = project.import_ev(
            type('F', (), {'stream': io.BytesIO(b'state,district,ev_count\nKerala,Idukki,-5\n'), 'filename': 'e.csv'})())
        self.assertEqual((saved, skipped), (0, 1))

    def test_gap_calculation_uses_district_stations(self):
        with project.db() as con:
            con.execute('INSERT INTO ev_districts(state,district,ev_count,imported_at) VALUES(?,?,?,?)',
                        ('Tamil Nadu', 'Chennai', 1000, 'now'))
            con.execute('''INSERT INTO stations(station_id,station_name,state,district,latitude,longitude,connectors,imported_at)
                           VALUES('S1','Test','Tamil Nadu','Chennai',13.08,80.27,2,'now')''')
        row = project.make_gap_rows('Tamil Nadu', 100)[0]
        self.assertEqual(row['target_units'], 10)
        self.assertEqual(row['estimated_additional_units'], 8)

    def test_stations_without_district_are_not_matched_to_districts(self):
        with project.db() as con:
            con.execute('INSERT INTO ev_districts(state,district,ev_count,imported_at) VALUES(?,?,?,?)',
                        ('Kerala', 'Idukki', 500, 'now'))
            con.execute('''INSERT INTO stations(station_id,station_name,state,district,latitude,longitude,connectors,imported_at)
                           VALUES('S2','Somewhere','Kerala',?,9.8,76.9,1,'now')''', (project.NOT_RECORDED,))
        self.assertEqual(project.make_gap_rows('Kerala', 100)[0]['station_count'], 0)

    def test_ev_download_keeps_blank_for_unavailable(self):
        upload(self.client, 'ev', 'state,district,ev_count\nKerala,Idukki,\nKerala,Thrissur,900\n')
        text = self.client.get('/download/ev-districts?state=Kerala').data.decode()
        self.assertEqual(text.splitlines()[0], 'state,district,ev_count')
        self.assertIn('Kerala,Idukki,', text.replace('\r', ''))
        self.assertIn('Kerala,Thrissur,900', text)

    def test_indian_number_format(self):
        self.assertEqual(project.inr(552214), '5,52,214')
        self.assertEqual(project.inr(173152), '1,73,152')
        self.assertEqual(project.inr(None), '\u2014')


class SeedTests(unittest.TestCase):
    def test_bundled_seed_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            orig = project.DB_PATH
            project.DB_PATH = Path(tmp) / 'seed.db'
            try:
                project.seed_database(force=True)
                self.assertEqual(len(project.get_stations()), 500)
                self.assertEqual(len(project.get_ev_rows()), 139)
                self.assertTrue(all(r['ev_count'] is None for r in project.get_ev_rows()))
                r = project.app.test_client().get('/')
                self.assertEqual(r.status_code, 200)
                self.assertIn(b'Data unavailable', r.data)
            finally:
                project.DB_PATH = orig


if __name__ == '__main__':
    unittest.main()
