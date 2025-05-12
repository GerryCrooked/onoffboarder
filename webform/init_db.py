import sqlite3
import os

os.makedirs("db", exist_ok=True)

conn = sqlite3.connect("db/onoffboarding.db")
c = conn.cursor()

c.execute('''
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lastname TEXT,
    firstname TEXT,
    department TEXT,
    department_dn TEXT,
    supervisor TEXT,
    startdate TEXT,
    enddate TEXT,
    comments TEXT,
    referenceuser TEXT,
    process_type TEXT,
    status TEXT,
    role TEXT,
    hardware_computer TEXT,
    hardware_monitor TEXT,
    hardware_accessories TEXT,
    hardware_mobile TEXT,
    key_required TEXT,
    required_windows INTEGER
)
''')

conn.commit()
conn.close()
print("✅ Tabelle 'requests' mit aktuellem Schema initialisiert")
