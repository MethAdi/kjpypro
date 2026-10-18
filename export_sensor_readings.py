#! C:\Users\Aditya\AppData\Local\Programs\Python\Python310\python.exe
"""Export the app's sensor_readings table as a CSV snapshot."""

import csv
from pathlib import Path

from proj import Database


def main():
    db = Database()
    try:
        with db.conn.cursor() as cursor:
            cursor.execute("SELECT * FROM sensor_readings ORDER BY id")
            columns = [item[0] for item in cursor.description]
            rows = cursor.fetchall()

        output = Path(__file__).parent / "data" / "sensor_readings.csv"
        output.parent.mkdir(exist_ok=True)
        with output.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(dict(row) for row in rows)

        print(f"Exported {len(rows)} rows to {output}")
    finally:
        db.conn.close()


if __name__ == "__main__":
    main()
