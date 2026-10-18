# Battery Thermal Management System

A Tkinter application for recording battery sensor readings, estimating thermal behavior, and viewing recommendations. PostgreSQL stores submitted readings and related analysis data.

## Requirements

- Python 3.10 or newer
- PostgreSQL
- Python dependency: `psycopg2-binary`

Install the dependency with:

```powershell
py -m pip install -r requirements.txt
```

## Database connection

The application connects to database `battery_tms` as user `postgres` on `localhost:5433`. Change the connection settings in `proj.py` if your PostgreSQL server uses a different host or port.

The password is requested securely when the application starts, unless `PGPASSWORD` is already set in the environment. Do not put your password in this repository.

On startup, the application creates the database if needed and creates these tables in the `public` schema if they do not already exist:

- `sensor_readings`
- `cfd_results`
- `predictions`
- `recommendations`
- `maintenance_records`

## Run

From this directory, run:

```powershell
py proj.py
```

Use the **Readings** tab in the app to view submitted readings. The simulator controls create generated readings only when you run them.

## Export readings

Run `py export_sensor_readings.py` to export the current `sensor_readings` table to `data/sensor_readings.csv`. The export includes every column and row in that table, and the script prompts for the PostgreSQL password securely. The CSV is a snapshot; run the export again to refresh it.

The PostgreSQL data directory and local database files are intentionally excluded from Git. The database schema is created by the application at startup.
