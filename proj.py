#! C:\Users\Aditya\AppData\Local\Programs\Python\Python310\python.exe
"""
KJS-CES-02  AI-Enabled Micro-channel Battery Thermal Management System
=======================================================================
Python mini project: OOP + abstract classes + Tkinter GUI + PostgreSQL (pgAdmin).

SETUP (once):  pip install -r requirements.txt
               PostgreSQL password is requested securely at startup, or may be
               supplied with the PGPASSWORD environment variable.

Run the GUI :   py proj.py
Run self-test:  py proj.py --test (drops and recreates the "tms_test" schema)

Sections
  1. Exceptions            5. Thermal models (ABC + 4 concrete classes)
  2. Validation helpers    6. Recommendation engine
  3. Sensors / BatteryCell 7. Database (PostgreSQL CRUD)
  4. Micro-channel analysis 8. Manager, simulator, self-test, GUI

IMPORTANT: values labelled MEASURED come from sensors. Everything from the
micro-channel analysis, the models and the rule engine is an ESTIMATE.
"""
import math
import getpass
import os
import random
import re
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import psycopg2
from psycopg2.extras import DictCursor

# ---- PostgreSQL connection settings: EDIT THE PASSWORD ----------------------
DB_CONFIG = {"dbname": "battery_tms", "user": "postgres",
             "password": os.environ.get("PGPASSWORD") or
                         getpass.getpass("PostgreSQL password for postgres: "),
             "host": "localhost", "port": "5433"}


# ---------------------------------------------------------------- 1. EXCEPTIONS
class InvalidSensorValueError(Exception):
    """Raised when a sensor value is missing, non-numeric or out of range."""


class DatabaseError(Exception):
    """Raised when a database operation fails."""


class RecordNotFoundError(Exception):
    """Raised when a requested database record does not exist."""


# ----------------------------------------------------------- 2. VALIDATION HELPERS
STATUSES = ("NORMAL", "ELEVATED", "CRITICAL", "LOW_FLOW", "FAULT")
TASK_STATUSES = ("PENDING", "IN_PROGRESS", "DONE")

ELEVATED_TEMP = 40.0   # deg C
CRITICAL_TEMP = 50.0   # deg C
LOW_FLOW = 0.30        # L/min


def validate_battery_id(raw):
    """'bat-7', 'BAT 07', 'bat_007' -> 'BAT-007'. Raises ValueError if invalid."""
    m = re.fullmatch(r"BAT[-_ ]?(\d{1,3})", str(raw).strip().upper())
    if not m:
        raise ValueError(f"Invalid battery ID '{raw}'. Use format BAT-001 (1-3 digits).")
    return f"BAT-{int(m.group(1)):03d}"


def format_status(raw, allowed=STATUSES):
    """' low flow ' -> 'LOW_FLOW'. Raises ValueError if not an allowed status."""
    s = str(raw).strip().upper().replace(" ", "_")
    if s not in allowed:
        raise ValueError(f"Invalid status '{raw}'. Allowed: {', '.join(allowed)}")
    return s


def classify(temperature, flow):
    """Return the status string for a validated measurement."""
    if temperature >= CRITICAL_TEMP:
        return "CRITICAL"
    if flow < LOW_FLOW:
        return "LOW_FLOW"
    if temperature >= ELEVATED_TEMP:
        return "ELEVATED"
    return "NORMAL"


# --------------------------------------------------------- 3. SENSORS / BATTERY CELL
class Sensor:
    """Generic sensor. Subclasses only set key, unit and valid range."""
    key, unit, low, high = "value", "", 0.0, 1.0

    def __init__(self, sensor_id):
        self.sensor_id = sensor_id
        self.last_value = None

    def read(self, raw):
        """Validate a raw value and store it. Raises InvalidSensorValueError."""
        try:
            v = float(raw)
        except (TypeError, ValueError):
            raise InvalidSensorValueError(f"{self.key}: non-numeric value '{raw}'")
        if math.isnan(v) or not (self.low <= v <= self.high):
            raise InvalidSensorValueError(
                f"{self.key}: {raw} outside valid range {self.low}..{self.high} {self.unit}")
        self.last_value = v
        return v


class TemperatureSensor(Sensor):
    key, unit, low, high = "temperature", "C", -20.0, 120.0


class InletTempSensor(Sensor):
    key, unit, low, high = "inlet", "C", -10.0, 60.0


class FlowSensor(Sensor):
    key, unit, low, high = "flow", "L/min", 0.0, 5.0


class CurrentSensor(Sensor):
    key, unit, low, high = "current", "A", 0.0, 200.0


class BatteryCell:
    """A battery cell that owns a LIST of sensor objects."""

    def __init__(self, battery_id, capacity_ah=50.0):
        self.battery_id = validate_battery_id(battery_id)
        self.capacity_ah = capacity_ah
        self.status = "UNKNOWN"
        self.sensors = [TemperatureSensor(f"{self.battery_id}-T"),
                        FlowSensor(f"{self.battery_id}-F"),
                        InletTempSensor(f"{self.battery_id}-IN"),
                        CurrentSensor(f"{self.battery_id}-I")]

    def read_all(self, raw):
        """Process every sensor in the list. Returns {key: float}.
        Raises one InvalidSensorValueError listing ALL faulty sensors."""
        values, errors = {}, []
        for sensor in self.sensors:
            try:
                values[sensor.key] = sensor.read(raw.get(sensor.key))
            except InvalidSensorValueError as e:
                errors.append(str(e))
        if errors:
            raise InvalidSensorValueError("; ".join(errors))
        return values

    def __str__(self):
        return f"{self.battery_id} [{self.status}]"


# ------------------------------------------------------ 4. MICRO-CHANNEL ANALYSIS
class MicrochannelAnalysis:
    """Simplified analytical stand-in for a CFD run (laminar flow, rectangular
    channels). All outputs are ESTIMATES, not measurements."""
    N, W, H, L = 20, 0.6e-3, 2.0e-3, 0.15      # channels, width, height, length (m)
    RHO, CP, MU, K = 1000.0, 4180.0, 1.0e-3, 0.6  # water-glycol-like coolant
    NU = 4.8                                    # laminar Nusselt number (const)
    R_INT = 0.015                               # cell heat gen: Q = R_INT * I^2 (W)

    def __init__(self):
        self.dh = 2 * self.W * self.H / (self.W + self.H)
        self.area = self.N * 2 * (self.W + self.H) * self.L  # wetted area (m^2)

    def heat_generated(self, current):
        return self.R_INT * current ** 2

    def convective_resistance(self):
        return 1.0 / (self.NU * self.K / self.dh * self.area)

    def analyse(self, flow_lpm, current, inlet):
        flow = max(flow_lpm, 0.01)
        q = flow / 60000.0                               # m^3/s
        v = q / self.N / (self.W * self.H)               # velocity per channel
        re = self.RHO * v * self.dh / self.MU
        h = self.NU * self.K / self.dh
        dp = (57.0 / re) * (self.L / self.dh) * self.RHO * v * v / 2.0 / 1000.0  # kPa
        heat = self.heat_generated(current)
        d_cool = heat / (self.RHO * q * self.CP)
        return {"reynolds": re, "nusselt": self.NU, "h_coeff": h,
                "pressure_drop": dp, "heat_w": heat, "coolant_outlet": inlet + d_cool}


# ------------------------------------------- 5. THERMAL MODELS (ABSTRACT + CONCRETE)
class ThermalModel(ABC):
    """Abstract base class. Every model returns an ESTIMATED cell temperature."""
    name = "Abstract"

    @abstractmethod
    def predict(self, current, inlet, flow):
        """Return predicted cell temperature in deg C."""

    def __str__(self):
        return self.name


class HeatBalanceModel(ThermalModel):
    """Physics: T = Tin + dT_coolant/2 + Q * (R_contact + R_convective)."""
    name = "HeatBalance"
    R_CONTACT = 0.35

    def __init__(self):
        self.mc = MicrochannelAnalysis()

    def predict(self, current, inlet, flow):
        r = self.mc.analyse(flow, current, inlet)
        rth = self.R_CONTACT + self.mc.convective_resistance()
        return inlet + (r["coolant_outlet"] - inlet) / 2 + r["heat_w"] * rth


class LinearEmpiricalModel(ThermalModel):
    """Fixed-coefficient linear fit: T = Tin + a*I + b - c*flow."""
    name = "LinearEmpirical"

    def predict(self, current, inlet, flow):
        return inlet + 0.42 * current - 3.0 - 1.0 * flow


class RuleBasedModel(ThermalModel):
    """Lookup bands on load, with a coolant-flow correction."""
    name = "RuleBased"

    def predict(self, current, inlet, flow):
        rise = 3 if current < 20 else 7 if current < 35 else 13 if current < 45 else 20
        if flow < LOW_FLOW:
            rise += 8
        elif flow >= 1.5:
            rise -= 2
        return inlet + rise


class RegressionModel(ThermalModel):
    """'Trained' model: least-squares fit on stored readings.
    Features: [1, I^2, Tin, 1/(flow+0.1)].  Uses defaults until trained."""
    name = "Regression"

    def __init__(self):
        self.coef = [2.0, 0.0065, 0.95, 0.5]
        self.trained = False

    @staticmethod
    def _features(current, inlet, flow):
        return [1.0, current ** 2, inlet, 1.0 / (flow + 0.1)]

    def fit(self, rows):
        """rows: iterable of (current, inlet, flow, temperature)."""
        X = [self._features(c, i, f) for c, i, f, _ in rows]
        y = [t for *_, t in rows]
        if len(X) < 8:
            raise ValueError("Need at least 8 valid readings to train the model.")
        n = 4
        A = [[sum(r[i] * r[j] for r in X) + (1e-3 if i == j and i else 0.0)
              for j in range(n)] for i in range(n)]
        b = [sum(r[i] * t for r, t in zip(X, y)) for i in range(n)]
        for col in range(n):                      # Gaussian elimination
            piv = max(range(col, n), key=lambda r: abs(A[r][col]))
            if abs(A[piv][col]) < 1e-12:
                raise ValueError("Training data too uniform to fit a model.")
            A[col], A[piv], b[col], b[piv] = A[piv], A[col], b[piv], b[col]
            for r in range(col + 1, n):
                k = A[r][col] / A[col][col]
                A[r] = [a - k * c for a, c in zip(A[r], A[col])]
                b[r] -= k * b[col]
        coef = [0.0] * n
        for i in reversed(range(n)):
            coef[i] = (b[i] - sum(A[i][j] * coef[j] for j in range(i + 1, n))) / A[i][i]
        self.coef, self.trained = coef, True

    def predict(self, current, inlet, flow):
        return sum(c * f for c, f in zip(self.coef, self._features(current, inlet, flow)))


# --------------------------------------------------- 6. RECOMMENDATION ENGINE
class RecommendationEngine:
    """Rule-based. Returns (kind, text); kind is MEASURED or ESTIMATE."""

    def recommend(self, status, temperature, flow, inlet, mean_pred):
        recs = []
        if status == "FAULT":
            return [("MEASURED", "Sensor fault detected: inspect wiring/replace sensor. "
                                 "Do not trust this reading; verify with a spare sensor.")]
        if status == "CRITICAL":
            recs.append(("MEASURED", f"Temperature {temperature:.1f} C is critical: "
                                     "stop charging, cut load, run pump at maximum."))
        elif status == "LOW_FLOW":
            recs.append(("MEASURED", f"Coolant flow {flow:.2f} L/min is below {LOW_FLOW}: "
                                     "check pump and look for channel blockage/air lock."))
        elif status == "ELEVATED":
            recs.append(("MEASURED", f"Temperature {temperature:.1f} C is elevated: "
                                     "raise coolant flow and reduce charge/discharge rate."))
        else:
            recs.append(("MEASURED", "Operating normally. No action needed."))
        if inlet > 35:
            recs.append(("MEASURED", f"Coolant inlet {inlet:.1f} C is warm: "
                                     "check chiller/radiator performance."))
        if mean_pred is not None and mean_pred >= CRITICAL_TEMP and temperature < CRITICAL_TEMP:
            recs.append(("ESTIMATE", f"Models estimate about {mean_pred:.1f} C: "
                                     "temperature may reach critical level, act early."))
        return recs


# ------------------------------------------------------------------ 7. DATABASE
class Database:
    """PostgreSQL storage (view/query it in pgAdmin). Every SQL statement goes
    through _run(), so any psycopg2 error becomes our own DatabaseError."""
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS sensor_readings(
        id SERIAL PRIMARY KEY, battery_id TEXT NOT NULL,
        temperature DOUBLE PRECISION, flow DOUBLE PRECISION, inlet DOUBLE PRECISION,
        current DOUBLE PRECISION, status TEXT NOT NULL, note TEXT DEFAULT '',
        timestamp TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS cfd_results(
        id SERIAL PRIMARY KEY,
        reading_id INTEGER NOT NULL REFERENCES sensor_readings(id) ON DELETE CASCADE,
        reynolds DOUBLE PRECISION, nusselt DOUBLE PRECISION, h_coeff DOUBLE PRECISION,
        pressure_drop DOUBLE PRECISION, heat_w DOUBLE PRECISION,
        coolant_outlet DOUBLE PRECISION, kind TEXT DEFAULT 'ESTIMATE', timestamp TEXT);
    CREATE TABLE IF NOT EXISTS predictions(
        id SERIAL PRIMARY KEY,
        reading_id INTEGER NOT NULL REFERENCES sensor_readings(id) ON DELETE CASCADE,
        model_name TEXT, predicted DOUBLE PRECISION, observed DOUBLE PRECISION,
        error DOUBLE PRECISION, abs_error DOUBLE PRECISION,
        kind TEXT DEFAULT 'ESTIMATE', timestamp TEXT);
    CREATE TABLE IF NOT EXISTS recommendations(
        id SERIAL PRIMARY KEY,
        reading_id INTEGER NOT NULL REFERENCES sensor_readings(id) ON DELETE CASCADE,
        kind TEXT, text TEXT, timestamp TEXT);
    CREATE TABLE IF NOT EXISTS maintenance_records(
        id SERIAL PRIMARY KEY, battery_id TEXT NOT NULL,
        task TEXT NOT NULL, status TEXT NOT NULL, notes TEXT DEFAULT '', date TEXT);
    """

    def __init__(self, schema="public", reset=False, **override):
        cfg = {**DB_CONFIG, **override}
        try:
            self._ensure_database(cfg)
            self.conn = psycopg2.connect(cursor_factory=DictCursor, **cfg)
            with self.conn, self.conn.cursor() as cur:
                if schema != "public":                 # used by the self-test only
                    if reset:
                        cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
                    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
                cur.execute(f"SET search_path TO {schema}")
                cur.execute(self.SCHEMA)
        except (psycopg2.Error, UnicodeDecodeError) as e:
            raise DatabaseError(
                "Cannot connect to PostgreSQL. Check that the server is running and "
                "that DB_CONFIG (password, port) is correct.\nDetails: "
                f"{str(e).strip()[:300]}") from e

    @staticmethod
    def _ensure_database(cfg):
        """Create the database (e.g. 'battery_tms') if it does not exist yet."""
        admin = psycopg2.connect(**{**cfg, "dbname": "postgres"})
        try:
            admin.autocommit = True
            with admin.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (cfg["dbname"],))
                if cur.fetchone() is None:
                    cur.execute(f'CREATE DATABASE "{cfg["dbname"]}"')
        finally:
            admin.close()

    def _run(self, sql, params=(), fetch=None):
        """The single place where SQL runs (commits on success, rolls back on error)."""
        try:
            with self.conn, self.conn.cursor() as cur:
                cur.execute(sql, params)
                if fetch == "all":
                    return cur.fetchall()
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "id":
                    return cur.fetchone()[0]
        except psycopg2.Error as e:
            raise DatabaseError(f"Database operation failed: {str(e).strip()[:300]}") from e

    @staticmethod
    def now():
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ---- sensor_readings: full CRUD
    def add_reading(self, battery_id, temperature, flow, inlet, current, status, note=""):
        return self._run(
            "INSERT INTO sensor_readings(battery_id,temperature,flow,inlet,current,"
            "status,note,timestamp) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (battery_id, temperature, flow, inlet, current, format_status(status),
             note, self.now()), fetch="id")

    def get_reading(self, rid):
        row = self._run("SELECT * FROM sensor_readings WHERE id=%s", (rid,), "one")
        if row is None:
            raise RecordNotFoundError(f"Reading #{rid} not found.")
        return row

    def list_readings(self, battery_id=None, limit=500):
        if battery_id and battery_id != "ALL":
            return self._run("SELECT * FROM sensor_readings WHERE battery_id=%s "
                             "ORDER BY id DESC LIMIT %s", (battery_id, limit), "all")
        return self._run("SELECT * FROM sensor_readings ORDER BY id DESC LIMIT %s",
                         (limit,), "all")

    def update_reading(self, rid, temperature, flow, inlet, current, status, note=""):
        self.get_reading(rid)  # raises RecordNotFoundError if missing
        self._run("UPDATE sensor_readings SET temperature=%s,flow=%s,inlet=%s,current=%s,"
                  "status=%s,note=%s WHERE id=%s",
                  (temperature, flow, inlet, current, format_status(status), note, rid))

    def delete_reading(self, rid):
        self.get_reading(rid)
        self._run("DELETE FROM sensor_readings WHERE id=%s", (rid,))  # cascades

    def delete_derived(self, rid):
        for t in ("cfd_results", "predictions", "recommendations"):
            self._run(f"DELETE FROM {t} WHERE reading_id=%s", (rid,))

    def battery_ids(self):
        return [r[0] for r in self._run(
            "SELECT DISTINCT battery_id FROM sensor_readings ORDER BY 1", fetch="all")]

    def training_rows(self):
        return [tuple(r) for r in self._run(
            "SELECT current,inlet,flow,temperature FROM sensor_readings "
            "WHERE temperature IS NOT NULL AND status!='FAULT'", fetch="all")]

    # ---- cfd_results
    def add_cfd(self, rid, r):
        self._run("INSERT INTO cfd_results(reading_id,reynolds,nusselt,h_coeff,"
                  "pressure_drop,heat_w,coolant_outlet,timestamp) "
                  "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                  (rid, r["reynolds"], r["nusselt"], r["h_coeff"], r["pressure_drop"],
                   r["heat_w"], r["coolant_outlet"], self.now()))

    def list_cfd(self, limit=500):
        return self._run("SELECT c.*, s.battery_id FROM cfd_results c JOIN "
                         "sensor_readings s ON s.id=c.reading_id ORDER BY c.id DESC LIMIT %s",
                         (limit,), "all")

    # ---- predictions
    def add_prediction(self, rid, model, predicted, observed):
        err = predicted - observed
        self._run("INSERT INTO predictions(reading_id,model_name,predicted,observed,"
                  "error,abs_error,timestamp) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                  (rid, model, predicted, observed, err, abs(err), self.now()))

    def list_predictions(self, limit=500):
        return self._run("SELECT p.*, s.battery_id FROM predictions p JOIN "
                         "sensor_readings s ON s.id=p.reading_id ORDER BY p.id DESC LIMIT %s",
                         (limit,), "all")

    def model_errors(self):
        return self._run("SELECT model_name, COUNT(*) AS n, AVG(abs_error) AS mae, "
                         "MAX(abs_error) AS worst FROM predictions GROUP BY model_name",
                         fetch="all")

    # ---- recommendations
    def add_recommendation(self, rid, kind, text):
        self._run("INSERT INTO recommendations(reading_id,kind,text,timestamp) "
                  "VALUES(%s,%s,%s,%s)", (rid, kind, text, self.now()))

    def list_recommendations(self, limit=500):
        return self._run("SELECT r.*, s.battery_id FROM recommendations r JOIN "
                         "sensor_readings s ON s.id=r.reading_id ORDER BY r.id DESC LIMIT %s",
                         (limit,), "all")

    # ---- maintenance_records: full CRUD
    def add_maintenance(self, battery_id, task, status="PENDING", notes=""):
        return self._run("INSERT INTO maintenance_records(battery_id,task,status,notes,date) "
                         "VALUES(%s,%s,%s,%s,%s) RETURNING id",
                         (battery_id, task, format_status(status, TASK_STATUSES), notes,
                          self.now()[:10]), fetch="id")

    def get_maintenance(self, mid):
        row = self._run("SELECT * FROM maintenance_records WHERE id=%s", (mid,), "one")
        if row is None:
            raise RecordNotFoundError(f"Maintenance record #{mid} not found.")
        return row

    def list_maintenance(self):
        return self._run("SELECT * FROM maintenance_records ORDER BY id DESC", fetch="all")

    def update_maintenance(self, mid, status, notes=None):
        row = self.get_maintenance(mid)
        self._run("UPDATE maintenance_records SET status=%s, notes=%s WHERE id=%s",
                  (format_status(status, TASK_STATUSES),
                   row["notes"] if notes is None else notes, mid))

    def delete_maintenance(self, mid):
        self.get_maintenance(mid)
        self._run("DELETE FROM maintenance_records WHERE id=%s", (mid,))


# -------------------------------------------------- 8a. MANAGER (application logic)
class ThermalManager:
    """Glue: validates readings, stores them, runs analysis + all models."""

    def __init__(self, db):
        self.db = db
        self.cells = {}
        self.analysis = MicrochannelAnalysis()
        self.regression = RegressionModel()
        # polymorphism: one list, every model used through the same interface
        self.models = [HeatBalanceModel(), LinearEmpiricalModel(),
                       RuleBasedModel(), self.regression]
        self.engine = RecommendationEngine()

    def cell(self, battery_id):
        bid = validate_battery_id(battery_id)
        if bid not in self.cells:
            self.cells[bid] = BatteryCell(bid)
        return self.cells[bid]

    def process(self, battery_id, raw):
        """Validate + store one reading. Sensor faults are stored as FAULT rows.
        Returns dict(id, battery_id, status, values, recs)."""
        cell = self.cell(battery_id)            # may raise ValueError (bad ID)
        try:
            v = cell.read_all(raw)
            status = classify(v["temperature"], v["flow"])
            rid = self.db.add_reading(cell.battery_id, v["temperature"], v["flow"],
                                      v["inlet"], v["current"], status)
        except InvalidSensorValueError as e:
            v, status = None, "FAULT"
            rid = self.db.add_reading(cell.battery_id, None, None, None, None,
                                      "FAULT", note=str(e))
        cell.status = status
        recs = self._derive(rid, status, v)
        return {"id": rid, "battery_id": cell.battery_id, "status": status,
                "values": v, "recs": recs}

    def _derive(self, rid, status, v):
        """Compute and store micro-channel results, predictions, recommendations."""
        mean_pred = None
        if v is not None:
            self.db.add_cfd(rid, self.analysis.analyse(v["flow"], v["current"], v["inlet"]))
            preds = []
            for m in self.models:                       # polymorphic call
                p = m.predict(v["current"], v["inlet"], v["flow"])
                preds.append(p)
                self.db.add_prediction(rid, m.name, p, v["temperature"])
            mean_pred = sum(preds) / len(preds)
        recs = self.engine.recommend(
            status, v["temperature"] if v else 0, v["flow"] if v else 0,
            v["inlet"] if v else 0, mean_pred)
        for kind, text in recs:
            self.db.add_recommendation(rid, kind, text)
        return recs

    def update_reading(self, rid, raw):
        """Edit a stored reading: re-validate, re-classify, recompute derived rows."""
        row = self.db.get_reading(rid)
        v = self.cell(row["battery_id"]).read_all(raw)   # raises if invalid
        status = classify(v["temperature"], v["flow"])
        self.db.update_reading(rid, v["temperature"], v["flow"], v["inlet"],
                               v["current"], status)
        self.db.delete_derived(rid)
        return self._derive(rid, status, v)

    def train_model(self):
        self.regression.fit(self.db.training_rows())
        return self.regression.coef


# ------------------------------------------------------ 8b. SIMULATED DATASETS
class Simulator:
    """Generates raw readings for the four required test scenarios.
    'Observed' temperature = physics with slightly different constants + noise,
    so the models show non-zero prediction error."""
    SCENARIOS = ("Normal", "Elevated", "Low Flow", "Sensor Fault")

    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.mc = MicrochannelAnalysis()

    def _observed(self, current, inlet, flow):
        q_heat = 0.016 * current ** 2
        r = self.mc.analyse(flow, current, inlet)
        t = inlet + (r["coolant_outlet"] - inlet) / 2 + q_heat * (
            0.42 + self.mc.convective_resistance())
        if flow < LOW_FLOW:            # local hot-spot effect the models don't capture
            t += 6.0 * (1 - flow / LOW_FLOW)
        return t + self.rng.gauss(0, 0.6)

    def generate(self, scenario):
        u = self.rng.uniform
        if scenario == "Normal":
            i, tin, f = u(15, 35), u(22, 28), u(1.0, 1.6)
        elif scenario == "Elevated":
            i, tin, f = u(42, 52), u(30, 36), u(0.8, 1.2)
        elif scenario == "Low Flow":
            i, tin, f = u(30, 45), u(25, 32), u(0.05, 0.25)
        elif scenario == "Sensor Fault":
            i, tin, f = u(15, 35), u(22, 28), u(1.0, 1.6)
        else:
            raise ValueError(f"Unknown scenario '{scenario}'")
        raw = {"temperature": round(self._observed(i, tin, f), 2),
               "flow": round(f, 3), "inlet": round(tin, 2), "current": round(i, 1)}
        if scenario == "Sensor Fault":
            key = self.rng.choice(list(raw))
            raw[key] = self.rng.choice([-999, 250, "ERR", None, float("nan")])
        return raw


# ------------------------------------------------------------ 8c. SELF-TEST
def run_self_test():
    db = Database(schema="tms_test", reset=True)   # throw-away schema
    mgr = ThermalManager(db)
    sim = Simulator(seed=42)
    results = {}
    for sc in Simulator.SCENARIOS:
        results[sc] = [mgr.process(f"BAT-00{k % 3 + 1}", sim.generate(sc))["status"]
                       for k in range(15)]
        counts = {s: results[sc].count(s) for s in set(results[sc])}
        print(f"{sc:13s} -> {counts}")
    assert results["Normal"].count("NORMAL") >= 13
    assert all(s in ("ELEVATED", "CRITICAL") for s in results["Elevated"])
    assert all(s in ("LOW_FLOW", "CRITICAL") for s in results["Low Flow"])
    assert all(s == "FAULT" for s in results["Sensor Fault"])
    # exception handling + validation + CRUD
    for bad in ("X1", "BAT-12345", ""):
        try:
            validate_battery_id(bad)
            raise AssertionError("bad id accepted")
        except ValueError:
            pass
    assert validate_battery_id("bat 7") == "BAT-007" and format_status(" low flow ") == "LOW_FLOW"
    try:
        db.get_reading(99999)
        raise AssertionError("missing record not detected")
    except RecordNotFoundError:
        pass
    rid = db.list_readings("BAT-001")[-1]["id"]
    mgr.update_reading(rid, {"temperature": 55, "flow": 1, "inlet": 30, "current": 40})
    assert db.get_reading(rid)["status"] == "CRITICAL"
    db.delete_reading(rid)
    assert not [r for r in db.list_predictions(10000) if r["reading_id"] == rid]
    mid = db.add_maintenance("BAT-001", "Flush channels")
    db.update_maintenance(mid, "done")
    assert db.get_maintenance(mid)["status"] == "DONE"
    db.delete_maintenance(mid)
    print("Coefficients after training:", [round(c, 4) for c in mgr.train_model()])
    for r in db.model_errors():
        print(f"  {r['model_name']:16s} n={r['n']:3d}  MAE={r['mae']:.2f} C  max={r['worst']:.2f} C")
    print("ALL SELF-TESTS PASSED")


# ----------------------------------------------------------------- 8d. TKINTER GUI
def run_gui():
    import tkinter as tk
    from tkinter import ttk, messagebox

    BG, CARD, NAVY, ACCENT, LINE = "#f3f5f9", "#ffffff", "#14213d", "#1d6fd1", "#dfe3eb"
    FONT = "Segoe UI"
    COLORS = {"NORMAL": "#2e9e4f", "ELEVATED": "#e69500", "CRITICAL": "#d62828",
              "LOW_FLOW": "#1d6fd1", "FAULT": "#7a7a7a", "UNKNOWN": "#9aa3b2"}
    TAG_BG = {"NORMAL": "#e6f6ea", "ELEVATED": "#fff1d6", "CRITICAL": "#fbdcdc",
              "LOW_FLOW": "#dce9fb", "FAULT": "#e4e4e4"}

    def fmt(v, d=2):
        return "-" if v is None else f"{v:.{d}f}"

    def card(parent, title=None):
        f = tk.Frame(parent, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        if title:
            tk.Label(f, text=title, bg=CARD, fg=NAVY,
                     font=(FONT, 10, "bold")).pack(anchor="w", padx=12, pady=(10, 4))
        return f

    def make_tree(parent, cols, widths):
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=8)
        for c, w in zip(cols, widths):
            tree.heading(c, text=c)
            tree.column(c, width=w, anchor="center")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for s, bg in TAG_BG.items():
            tree.tag_configure(s, background=bg)
        return frame, tree

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("Battery Thermal Management System")
            self.geometry("1280x880")
            self.minsize(1100, 720)
            self.configure(bg=BG)
            self._style()
            try:
                self.db = Database()
            except DatabaseError as e:
                messagebox.showerror("Database", str(e))
                raise SystemExit(1)
            self.mgr = ThermalManager(self.db)
            self.sim = Simulator()
            self.vars = {k: tk.StringVar() for k in
                         ("battery", "temperature", "flow", "inlet", "current")}
            self.vars["battery"].set("BAT-001")
            self.filter_var = tk.StringVar(value="ALL")
            self._build_header()
            body = tk.Frame(self, bg=BG)
            body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
            body.columnconfigure(1, weight=1)
            body.rowconfigure(0, weight=1)
            self._build_sidebar(body)
            self._build_main(body)
            self.bind("<Return>", lambda e: self.submit())
            self.refresh_all()

        # ------------------------------------------------------------ styling
        def _style(self):
            s = ttk.Style(self)
            s.theme_use("clam")
            s.configure("TFrame", background=BG)
            s.configure("TLabel", background=CARD, font=(FONT, 9))
            s.configure("TButton", font=(FONT, 9), padding=6)
            s.configure("Accent.TButton", background=ACCENT, foreground="white",
                        font=(FONT, 10, "bold"), padding=8)
            s.map("Accent.TButton", background=[("active", "#1558a8")])
            s.configure("Treeview", rowheight=26, font=(FONT, 9), fieldbackground=CARD)
            s.configure("Treeview.Heading", font=(FONT, 9, "bold"), background="#e8ecf4",
                        padding=5)
            s.map("Treeview", background=[("selected", ACCENT)],
                  foreground=[("selected", "white")])
            s.configure("TNotebook", background=BG)
            s.configure("TNotebook.Tab", font=(FONT, 10), padding=(14, 7))
            s.map("TNotebook.Tab", background=[("selected", CARD)])

        # ------------------------------------------------------------- header
        def _build_header(self):
            h = tk.Frame(self, bg=NAVY)
            h.pack(fill="x", pady=(0, 10))
            left = tk.Frame(h, bg=NAVY)
            left.pack(side="left", padx=18, pady=10)
            tk.Label(left, text="Battery Thermal Management System", bg=NAVY, fg="white",
                     font=(FONT, 17, "bold")).pack(anchor="w")
            tk.Label(left, text="Micro-channel cooling monitor  |  AI temperature prediction  |"
                                "  KJS-CES-02", bg=NAVY, fg="#a9b4cc",
                     font=(FONT, 9)).pack(anchor="w")
            right = tk.Frame(h, bg=NAVY)
            right.pack(side="right", padx=22)
            self.light = tk.Canvas(right, width=40, height=40, bg=NAVY, highlightthickness=0)
            self.bulb = self.light.create_oval(4, 4, 36, 36, fill=COLORS["UNKNOWN"], outline="white", width=2)
            self.light.pack(side="left")
            box = tk.Frame(right, bg=NAVY)
            box.pack(side="left", padx=10)
            self.status_lbl = tk.Label(box, text="NO DATA", bg=NAVY, fg="white",
                                       font=(FONT, 16, "bold"))
            self.status_lbl.pack(anchor="w")
            self.status_sub = tk.Label(box, text="status indicator", bg=NAVY, fg="#a9b4cc",
                                       font=(FONT, 9))
            self.status_sub.pack(anchor="w")

        # ------------------------------------------------------------ sidebar
        def _build_sidebar(self, body):
            side = tk.Frame(body, bg=BG, width=290)
            side.grid(row=0, column=0, sticky="ns", padx=(0, 12))
            side.grid_propagate(False)

            entry = card(side, "Data Entry  (measured values)")
            entry.pack(fill="x")
            self.batt_cb = ttk.Combobox(entry, textvariable=self.vars["battery"],
                                        values=["BAT-001", "BAT-002", "BAT-003"])
            fields = [("Battery ID", None), ("Cell temperature (C)", "temperature"),
                      ("Coolant flow (L/min)", "flow"), ("Inlet temperature (C)", "inlet"),
                      ("Current (A)", "current")]
            for txt, key in fields:
                tk.Label(entry, text=txt, bg=CARD, fg="#4a5568", font=(FONT, 9)).pack(
                    anchor="w", padx=12, pady=(4, 0))
                w = self.batt_cb if key is None else ttk.Entry(
                    entry, textvariable=self.vars[key], validate="key",
                    validatecommand=(self.register(self._numeric_ok), "%P"))
                w.pack(fill="x", padx=12)
            ttk.Button(entry, text="Submit Reading", style="Accent.TButton",
                       command=self.submit).pack(fill="x", padx=12, pady=(10, 4))
            ttk.Button(entry, text="Clear fields", command=self.clear_entry).pack(
                fill="x", padx=12, pady=(0, 10))

            sim = card(side, "Simulated Datasets")
            sim.pack(fill="x", pady=8)
            self.sc_var = tk.StringVar(value=Simulator.SCENARIOS[0])
            ttk.Combobox(sim, textvariable=self.sc_var, values=Simulator.SCENARIOS,
                         state="readonly").pack(fill="x", padx=12)
            row = tk.Frame(sim, bg=CARD)
            row.pack(fill="x", padx=12, pady=6)
            tk.Label(row, text="Readings:", bg=CARD, font=(FONT, 9)).pack(side="left")
            self.count_var = tk.IntVar(value=10)
            ttk.Spinbox(row, from_=1, to=200, textvariable=self.count_var,
                        width=6).pack(side="left", padx=6)
            ttk.Button(sim, text="Run Scenario", command=self.run_scenario).pack(
                fill="x", padx=12, pady=2)
            ttk.Button(sim, text="Run All 4 Scenarios", command=self.run_all).pack(
                fill="x", padx=12, pady=2)
            ttk.Button(sim, text="Train Regression Model", command=self.train).pack(
                fill="x", padx=12, pady=(2, 10))

            key = card(side, "Status Thresholds")
            key.pack(fill="x")
            for st, txt in (("NORMAL", "below 40 C"), ("ELEVATED", "40 to 50 C"),
                            ("CRITICAL", "50 C or above"), ("LOW_FLOW", "flow below 0.3 L/min"),
                            ("FAULT", "invalid sensor value")):
                r = tk.Frame(key, bg=CARD)
                r.pack(fill="x", padx=12, pady=1)
                dot = tk.Canvas(r, width=14, height=14, bg=CARD, highlightthickness=0)
                dot.create_oval(2, 2, 12, 12, fill=COLORS[st], outline="")
                dot.pack(side="left")
                tk.Label(r, text=f"{st}", bg=CARD, font=(FONT, 9, "bold"), width=9,
                         anchor="w").pack(side="left", padx=4)
                tk.Label(r, text=txt, bg=CARD, fg="#4a5568", font=(FONT, 9)).pack(side="left")
            tk.Frame(key, bg=CARD, height=4).pack()

        # --------------------------------------------------------------- main
        def _build_main(self, body):
            main = tk.Frame(body, bg=BG)
            main.grid(row=0, column=1, sticky="nsew")
            main.columnconfigure(0, weight=1)
            main.rowconfigure(3, weight=1)

            kp = tk.Frame(main, bg=BG)
            kp.grid(row=0, column=0, sticky="ew")
            self.kpi, self.kpi_title = {}, {}
            for i, (key, title) in enumerate((("temp", "LATEST TEMP (MEASURED)"),
                                              ("flow", "FLOW (MEASURED)"),
                                              ("count", "READINGS / FAULTS"),
                                              ("model", "BEST MODEL (AVG ERROR)"))):
                kp.columnconfigure(i, weight=1, uniform="k")
                c = card(kp)
                c.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
                self.kpi_title[key] = tk.Label(c, text=title, bg=CARD, fg="#6b7686",
                                               font=(FONT, 8, "bold"))
                self.kpi_title[key].pack(anchor="w", padx=12, pady=(10, 0))
                self.kpi[key] = tk.Label(c, text="-", bg=CARD, fg=NAVY,
                                         font=(FONT, 20, "bold"))
                self.kpi[key].pack(anchor="w", padx=12, pady=(0, 10))

            self.banner = tk.Label(main, text="No readings yet. Enter a reading or run a "
                                              "simulated dataset.", bg=COLORS["UNKNOWN"],
                                   fg="white", font=(FONT, 11, "bold"), anchor="w",
                                   padx=14, pady=10, wraplength=900, justify="left")
            self.banner.grid(row=1, column=0, sticky="ew", pady=8)

            ch = card(main)
            ch.grid(row=2, column=0, sticky="ew")
            top = tk.Frame(ch, bg=CARD)
            top.pack(fill="x")
            tk.Label(top, text="Temperature trend", bg=CARD, fg=NAVY,
                     font=(FONT, 10, "bold")).pack(side="left", padx=12, pady=(10, 2))
            self.filter_cb = ttk.Combobox(top, textvariable=self.filter_var, width=10,
                                          state="readonly", values=["ALL"])
            self.filter_cb.pack(side="right", padx=12, pady=(8, 0))
            tk.Label(top, text="Battery:", bg=CARD, font=(FONT, 9)).pack(side="right")
            self.filter_cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_all())
            self.chart = tk.Canvas(ch, height=210, bg=CARD, highlightthickness=0)
            self.chart.pack(fill="x", padx=6, pady=(0, 6))
            self.chart.bind("<Configure>", lambda e: self.draw_chart())

            nb = ttk.Notebook(main)
            nb.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
            # Alerts
            t0 = ttk.Frame(nb); nb.add(t0, text="  Alerts  ")
            self.alerts = tk.Text(t0, height=6, state="disabled", wrap="word", bg=CARD,
                                  relief="flat", font=(FONT, 10), padx=10, pady=8)
            self.alerts.pack(fill="both", expand=True)
            for s, c in COLORS.items():
                self.alerts.tag_configure(s, foreground=c, font=(FONT, 10, "bold"))
            self.alerts.tag_configure("ESTIMATE", foreground="#6a1b9a")
            # Readings
            t1 = ttk.Frame(nb); nb.add(t1, text="  Readings  ")
            bar = ttk.Frame(t1); bar.pack(fill="x", pady=4)
            ttk.Button(bar, text="Edit Selected", command=self.edit_reading).pack(side="left", padx=4)
            ttk.Button(bar, text="Delete Selected", command=self.delete_reading).pack(side="left")
            ttk.Button(bar, text="Refresh", command=self.refresh_all).pack(side="left", padx=4)
            f, self.t_read = make_tree(
                t1, ("ID", "Battery", "Time", "Temp C (measured)", "Flow L/min",
                     "Inlet C", "Current A", "Status", "Note"),
                (45, 80, 140, 125, 85, 70, 80, 90, 280))
            f.pack(fill="both", expand=True)
            # Predictions
            t2 = ttk.Frame(nb); nb.add(t2, text="  Predicted vs Observed  ")
            self.err_lbl = ttk.Label(t2, text="", background=BG, font=(FONT, 10, "bold"))
            self.err_lbl.pack(anchor="w", padx=6, pady=4)
            f, self.t_pred = make_tree(
                t2, ("Reading", "Battery", "Model", "Predicted C (ESTIMATE)",
                     "Observed C (MEASURED)", "Error C", "Abs Error C"),
                (65, 80, 130, 170, 170, 90, 100))
            f.pack(fill="both", expand=True)
            # Micro-channel
            t3 = ttk.Frame(nb); nb.add(t3, text="  Micro-channel  ")
            ttk.Label(t3, background=BG, text="All values are analytical ESTIMATES "
                      "(simplified CFD-style calculation), not measurements.").pack(
                anchor="w", padx=6, pady=4)
            f, self.t_cfd = make_tree(
                t3, ("Reading", "Battery", "Reynolds", "Nusselt", "h W/m2K",
                     "dP kPa", "Heat W", "Coolant out C", "Kind"),
                (65, 80, 80, 70, 90, 80, 80, 110, 90))
            f.pack(fill="both", expand=True)
            # Recommendations
            t4 = ttk.Frame(nb); nb.add(t4, text="  Recommendations  ")
            f, self.t_rec = make_tree(t4, ("Reading", "Battery", "Time", "Basis", "Recommendation"),
                                      (65, 80, 140, 90, 700))
            f.pack(fill="both", expand=True)
            # Maintenance
            t5 = ttk.Frame(nb); nb.add(t5, text="  Maintenance  ")
            bar = ttk.Frame(t5); bar.pack(fill="x", pady=4)
            self.m_batt, self.m_task, self.m_notes = (tk.StringVar(value="BAT-001"),
                                                      tk.StringVar(), tk.StringVar())
            self.m_stat = tk.StringVar(value="PENDING")
            for txt, var, w in (("Battery", self.m_batt, 9), ("Task", self.m_task, 26),
                                ("Notes", self.m_notes, 26)):
                ttk.Label(bar, text=txt, background=BG).pack(side="left", padx=4)
                ttk.Entry(bar, textvariable=var, width=w).pack(side="left")
            ttk.Combobox(bar, textvariable=self.m_stat, values=TASK_STATUSES, width=12,
                         state="readonly").pack(side="left", padx=6)
            bar2 = ttk.Frame(t5); bar2.pack(fill="x", pady=2)
            ttk.Button(bar2, text="Add", command=self.m_add).pack(side="left", padx=4)
            ttk.Button(bar2, text="Update Selected (status/notes)",
                       command=self.m_update).pack(side="left", padx=4)
            ttk.Button(bar2, text="Delete Selected", command=self.m_delete).pack(side="left")
            f, self.t_main = make_tree(t5, ("ID", "Battery", "Task", "Status", "Notes", "Date"),
                                       (45, 80, 260, 100, 320, 100))
            f.pack(fill="both", expand=True)

        # ------------------------------------------------------------ helpers
        def set_indicator(self, status, battery=""):
            col = COLORS.get(status, COLORS["UNKNOWN"])
            self.light.itemconfig(self.bulb, fill=col)
            self.status_lbl.config(text=status.replace("_", " "))
            self.status_sub.config(text=battery or "status indicator")
            self.banner.config(bg=col)

        def show_alert(self, res):
            v, st = res["values"], res["status"]
            head = (f"{st}  |  {res['battery_id']}  |  " + (
                f"{v['temperature']:.1f} C measured, flow {v['flow']:.2f} L/min"
                if v else "invalid sensor values"))
            self.alerts.config(state="normal")
            self.alerts.insert("1.0", "\n")
            for kind, text in reversed(res["recs"]):
                self.alerts.insert("1.0", f"      ({kind}) {text}\n",
                                   "ESTIMATE" if kind == "ESTIMATE" else "")
            self.alerts.insert("1.0", head + "\n", st)
            self.alerts.config(state="disabled")
            self.set_indicator(st, res["battery_id"])
            self.banner.config(text=head + "\n" + res["recs"][0][1])

        @staticmethod
        def _numeric_ok(text):
            """Allow only characters that can form a number (digits, one dot, leading -)."""
            return text == "" or re.fullmatch(r"-?\d*\.?\d*", text) is not None

        def _check_input(self, battery, raw):
            """Reject bad manual input BEFORE saving (faults come only from sensors/simulator)."""
            self.mgr.cell(battery).read_all(raw)   # ValueError / InvalidSensorValueError
            return True

        def clear_entry(self):
            for k in ("temperature", "flow", "inlet", "current"):
                self.vars[k].set("")

        def guard(self, fn, *a):
            """Run a DB/logic action and show friendly errors."""
            try:
                return fn(*a)
            except ValueError as e:
                messagebox.showwarning("Invalid input", str(e))
            except InvalidSensorValueError as e:
                messagebox.showwarning("Invalid sensor value", str(e))
            except RecordNotFoundError as e:
                messagebox.showinfo("Not found", str(e))
                self.refresh_all()
            except DatabaseError as e:
                messagebox.showerror("Database error", str(e))

        # ------------------------------------------------------------ actions
        def submit(self):
            raw = {k: self.vars[k].get().strip() for k in
                   ("temperature", "flow", "inlet", "current")}
            if not all(raw.values()):
                messagebox.showwarning("Missing data", "Fill in all four sensor fields.")
                return
            if not self.guard(self._check_input, self.vars["battery"].get(), raw):
                return                              # warning already shown, nothing saved
            res = self.guard(self.mgr.process, self.vars["battery"].get(), raw)
            if res:
                self.show_alert(res)
                self.refresh_all()

        def _run(self, scenario, n):
            last = None
            for k in range(n):
                last = self.mgr.process(f"BAT-00{k % 3 + 1}", self.sim.generate(scenario))
            return last

        def run_scenario(self):
            res = self.guard(self._run, self.sc_var.get(), self.count_var.get())
            if res:
                self.show_alert(res)
                self.refresh_all()

        def run_all(self):
            res = None
            for sc in Simulator.SCENARIOS:
                res = self.guard(self._run, sc, self.count_var.get()) or res
            if res:
                self.show_alert(res)
                self.refresh_all()

        def train(self):
            coef = self.guard(self.mgr.train_model)
            if coef:
                messagebox.showinfo("Regression trained",
                                    "Coefficients [1, I^2, Tin, 1/(flow+0.1)]:\n"
                                    + ", ".join(f"{c:.4f}" for c in coef)
                                    + "\n\nNew readings will use the trained model.")
                self.refresh_all()

        def selected_id(self, tree):
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("Select a row", "Select a row in the table first.")
                return None
            return int(tree.item(sel[0])["values"][0])

        def edit_reading(self):
            rid = self.selected_id(self.t_read)
            if rid is None:
                return
            row = self.guard(self.db.get_reading, rid)
            if row is None:
                return
            win = tk.Toplevel(self)
            win.title(f"Edit reading #{rid}")
            win.configure(bg=CARD)
            vs = {}
            for r, k in enumerate(("temperature", "flow", "inlet", "current")):
                tk.Label(win, text=k.capitalize(), bg=CARD).grid(row=r, column=0, padx=12, pady=5, sticky="w")
                vs[k] = tk.StringVar(value="" if row[k] is None else str(row[k]))
                ttk.Entry(win, textvariable=vs[k], validate="key",
                          validatecommand=(self.register(self._numeric_ok), "%P")
                          ).grid(row=r, column=1, padx=12)

            def save():
                if self.guard(self.mgr.update_reading, rid, {k: v.get() for k, v in vs.items()}):
                    win.destroy()
                    self.refresh_all()
            ttk.Button(win, text="Save", style="Accent.TButton", command=save).grid(
                row=4, column=0, columnspan=2, pady=10)

        def delete_reading(self):
            rid = self.selected_id(self.t_read)
            if rid is not None and messagebox.askyesno("Delete", f"Delete reading #{rid}?"):
                self.guard(self.db.delete_reading, rid)
                self.refresh_all()

        def m_add(self):
            def do():
                bid = validate_battery_id(self.m_batt.get())
                if not self.m_task.get().strip():
                    raise ValueError("Task description is required.")
                self.db.add_maintenance(bid, self.m_task.get().strip(),
                                        self.m_stat.get(), self.m_notes.get())
            self.guard(do)
            self.load_maintenance()

        def m_update(self):
            mid = self.selected_id(self.t_main)
            if mid is not None:
                self.guard(self.db.update_maintenance, mid, self.m_stat.get(),
                           self.m_notes.get() or None)
                self.load_maintenance()

        def m_delete(self):
            mid = self.selected_id(self.t_main)
            if mid is not None:
                self.guard(self.db.delete_maintenance, mid)
                self.load_maintenance()

        # ------------------------------------------------------------- loaders
        def refresh_all(self):
            ids = self.guard(self.db.battery_ids) or []
            self.filter_cb["values"] = ["ALL"] + ids
            self.batt_cb["values"] = sorted(set(ids) | {"BAT-001", "BAT-002", "BAT-003"})
            self.load_readings()
            self.load_predictions()
            self.load_cfd()
            self.load_recs()
            self.load_maintenance()
            self.load_kpis()
            self.draw_chart()

        def _fill(self, tree, rows):
            tree.delete(*tree.get_children())
            for values, tag in rows:
                tree.insert("", "end", values=values, tags=(tag,) if tag else ())

        def load_readings(self):
            rows = self.guard(self.db.list_readings, self.filter_var.get()) or []
            self._fill(self.t_read, [((r["id"], r["battery_id"], r["timestamp"],
                                       fmt(r["temperature"], 1), fmt(r["flow"]),
                                       fmt(r["inlet"], 1), fmt(r["current"], 1),
                                       r["status"], r["note"]), r["status"]) for r in rows])

        def load_predictions(self):
            rows = self.guard(self.db.list_predictions) or []
            self._fill(self.t_pred, [((r["reading_id"], r["battery_id"], r["model_name"],
                                       fmt(r["predicted"], 2), fmt(r["observed"], 2),
                                       f"{r['error']:+.2f}", fmt(r["abs_error"], 2)), None)
                                     for r in rows])
            stats = self.guard(self.db.model_errors) or []
            self.err_lbl.config(text=("Mean absolute error:   " + "   |   ".join(
                f"{s['model_name']}: {s['mae']:.2f} C" for s in stats)) if stats else "")

        def load_cfd(self):
            rows = self.guard(self.db.list_cfd) or []
            self._fill(self.t_cfd, [((r["reading_id"], r["battery_id"], fmt(r["reynolds"], 0),
                                      fmt(r["nusselt"], 1), fmt(r["h_coeff"], 0),
                                      fmt(r["pressure_drop"], 3), fmt(r["heat_w"], 1),
                                      fmt(r["coolant_outlet"], 2), r["kind"]), None)
                                    for r in rows])

        def load_recs(self):
            rows = self.guard(self.db.list_recommendations) or []
            self._fill(self.t_rec, [((r["reading_id"], r["battery_id"], r["timestamp"],
                                      r["kind"], r["text"]), None) for r in rows])

        def load_maintenance(self):
            rows = self.guard(self.db.list_maintenance) or []
            self._fill(self.t_main, [((r["id"], r["battery_id"], r["task"], r["status"],
                                       r["notes"], r["date"]), None) for r in rows])

        def load_kpis(self):
            rows = self.guard(self.db.list_readings, self.filter_var.get(), 100000) or []
            valid = [r for r in rows if r["temperature"] is not None]
            if valid:
                r = valid[0]
                self.kpi["temp"].config(text=f"{r['temperature']:.1f} C",
                                        fg=COLORS.get(r["status"], NAVY))
                self.kpi["flow"].config(text=f"{r['flow']:.2f} L/min",
                                        fg=COLORS["LOW_FLOW"] if r["flow"] < LOW_FLOW else NAVY)
            else:
                self.kpi["temp"].config(text="-", fg=NAVY)
                self.kpi["flow"].config(text="-", fg=NAVY)
            faults = sum(1 for r in rows if r["status"] == "FAULT")
            self.kpi["count"].config(text=f"{len(rows)}  /  {faults}",
                                     fg=COLORS["FAULT"] if faults else NAVY)
            stats = self.guard(self.db.model_errors) or []
            if stats:
                best = min(stats, key=lambda s: s["mae"])
                self.kpi["model"].config(text=f"\u00b1{best['mae']:.2f} C")
                self.kpi_title["model"].config(text=f"BEST: {best['model_name'].upper()}")
            else:
                self.kpi["model"].config(text="-")
                self.kpi_title["model"].config(text="BEST MODEL (AVG ERROR)")

        # --------------------------------------------------------------- chart
        def draw_chart(self):
            c = self.chart
            c.delete("all")
            w, h = c.winfo_width(), c.winfo_height()
            if w < 120:
                return
            rows = list(reversed(self.guard(self.db.list_readings,
                                            self.filter_var.get(), 40) or []))
            temps = [r["temperature"] for r in rows if r["temperature"] is not None]
            if not rows:
                c.create_text(w / 2, h / 2, text="No data yet", fill="#9aa3b2",
                              font=(FONT, 12))
                return
            L, R, T, B = 48, 20, 12, 30
            lo = min(20, int(min(temps)) - 2) if temps else 20
            hi = max(60, int(max(temps)) + 3) if temps else 60
            pw, ph = w - L - R, h - T - B
            y = lambda t: T + (hi - t) / (hi - lo) * ph
            x = lambda i: L + (i / (len(rows) - 1) * pw if len(rows) > 1 else pw / 2)
            for t in range((lo // 10 + 1) * 10, hi + 1, 10):
                c.create_line(L, y(t), w - R, y(t), fill="#eef1f6")
                c.create_text(L - 8, y(t), text=f"{t}", anchor="e", fill="#6b7686",
                              font=(FONT, 8))
            for t, col, name in ((ELEVATED_TEMP, COLORS["ELEVATED"], "Elevated 40 C"),
                                 (CRITICAL_TEMP, COLORS["CRITICAL"], "Critical 50 C")):
                c.create_line(L, y(t), w - R, y(t), fill=col, dash=(5, 4), width=1)
                c.create_text(L + 6, y(t) - 8, text=name, anchor="w", fill=col,
                              font=(FONT, 8, "bold"))
            c.create_text(14, T + ph / 2, text="C", fill="#6b7686", font=(FONT, 9, "bold"))
            c.create_text(L + pw / 2, h - 8, text="oldest  ->  newest reading",
                          fill="#6b7686", font=(FONT, 8))
            # estimated line from the best model (clearly dashed + purple)
            stats = self.guard(self.db.model_errors) or []
            best = min(stats, key=lambda s: s["mae"])["model_name"] if stats else None
            if best:
                pred = {p["reading_id"]: p["predicted"] for p in
                        (self.guard(self.db.list_predictions, 5000) or [])
                        if p["model_name"] == best}
                run = []
                for i, r in enumerate(rows + [{"id": None}]):
                    if r["id"] in pred:
                        run.append((x(i), y(pred[r["id"]])))
                    else:
                        if len(run) > 1:
                            c.create_line(*[v for p in run for v in p], fill="#8e44ad",
                                          dash=(4, 3), width=2)
                        run = []
            # measured line + status coloured points
            seg = []
            for i, r in enumerate(rows):
                if r["temperature"] is None:
                    if len(seg) > 3:   # need at least 2 points (4 coords)
                        c.create_line(*seg, fill=ACCENT, width=2)
                    seg = []
                    c.create_text(x(i), T + ph - 8, text="x", fill=COLORS["FAULT"],
                                  font=(FONT, 11, "bold"))
                else:
                    seg += [x(i), y(r["temperature"])]
            if len(seg) > 3:
                c.create_line(*seg, fill=ACCENT, width=2)
            for i, r in enumerate(rows):
                if r["temperature"] is not None:
                    c.create_oval(x(i) - 4, y(r["temperature"]) - 4, x(i) + 4,
                                  y(r["temperature"]) + 4,
                                  fill=COLORS.get(r["status"], ACCENT), outline="white")
            c.create_text(w - R, T, anchor="ne", fill="#4a5568", font=(FONT, 8),
                          text="solid = MEASURED     dashed = ESTIMATE" + (
                              f" ({best})" if best else "") + "     x = sensor fault")

    App().mainloop()


if __name__ == "__main__":
    if "--test" in sys.argv:
        run_self_test()
    else:
        run_gui()   
