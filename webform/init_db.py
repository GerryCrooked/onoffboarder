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
    hardware TEXT,
    comments TEXT,
    referenceuser TEXT,
    process_type TEXT,
    status TEXT,
    role TEXT,
    account_info TEXT,
    hardware_status TEXT,
    key_status TEXT,
    key_confirmation TEXT
)
''')

conn.commit()
conn.close()
print("✅ Tabelle 'requests' mit aktuellem Schema initialisiert")
