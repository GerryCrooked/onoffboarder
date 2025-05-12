import smtplib
import os
import sqlite3
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from dotenv import load_dotenv, find_dotenv
from ad_utils import build_ou_tree, parse_exclude_ous, get_users_in_ou, get_supervisors_in_ou, get_ad_connection
from ldap3 import Server, Connection, ALL, SUBTREE
from datetime import datetime
from email.mime.text import MIMEText


load_dotenv(find_dotenv())

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "secret123")
print("🚀 Starte app.py neu!")

@app.route("/", methods=["GET", "POST"])
def form():
    if request.method == "POST":
        lastname = request.form.get("lastname")
        firstname = request.form.get("firstname")
        department = request.form.get("department")
        department_dn = request.form.get("department_dn")
        supervisor = request.form.get("supervisor", "")
        startdate = request.form.get("startdate") or datetime.today().strftime('%Y-%m-%d')
        enddate = request.form.get("enddate")
        comments = request.form.get("comments", "")
        hardware = request.form.get("hardware", "")
        referenceuser = request.form.get("referenceuser", "")
        process_type = request.form.get("process_type", "onboarding")
        status = "offen"
        role = "user"

        conn = sqlite3.connect("db/onoffboarding.db")
        c = conn.cursor()
        c.execute('''INSERT INTO requests
                     (lastname, firstname, department, supervisor, startdate, enddate, hardware, comments, referenceuser, process_type, status, role, department_dn)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                  (lastname, firstname, department, supervisor, startdate, enddate, hardware, comments, referenceuser, process_type, status, role, department_dn))
        conn.commit()
        conn.close()
        # Sende Genehmigungs-E-Mail
        send_approval_mail(supervisor, firstname, lastname, process_type, c.lastrowid)
        flash("Antrag gespeichert und zur Freigabe versendet.")
        return redirect(url_for("form"))

    return render_template("form.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    from ldap3 import Server, Connection, ALL, SUBTREE

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        ldap_server = os.getenv("AD_SERVER")
        ldap_base_dn = os.getenv("AD_SEARCH_BASE")
        group_name = os.getenv("AD_GROUP")  # z.B. "onoffboardingadmin"

        print("🔐 Form-Login Debug")
        print("Username:", username)
        print("Password empty?", not password)
        print("AD_SERVER:", ldap_server)
        print("AD_SEARCH_BASE:", ldap_base_dn)

        if not ldap_server or not ldap_base_dn or not username or not password:
            flash("❌ Fehlende Umgebungsvariablen oder Eingabefehler.")
            return render_template("login.html")

        try:
            server = Server(ldap_server, get_info=ALL)
            domain = ".".join(part.split("=")[1] for part in ldap_base_dn.split(",") if part.startswith("DC="))
            user_dn = f"{username}@{domain}"

            print("🔎 Versuch Bind mit:", user_dn)
            conn = Connection(server, user=user_dn, password=password, authentication="SIMPLE", auto_bind=True)

            # Suche Benutzer-DN für Mitgliedschaftsprüfung
            conn.search(
                search_base=ldap_base_dn,
                search_filter=f"(sAMAccountName={username})",
                search_scope=SUBTREE,
                attributes=["distinguishedName"]
            )
            if not conn.entries:
                flash("❌ Benutzer nicht gefunden.")
                return render_template("login.html")

            user_entry_dn = conn.entries[0].distinguishedName.value
            print("✔ Benutzer-DN:", user_entry_dn)

            # Nested Group Check mit LDAP_MATCHING_RULE_IN_CHAIN
            conn.search(
                search_base=ldap_base_dn,
                search_filter=f"(member:1.2.840.113556.1.4.1941:={user_entry_dn})",
                search_scope=SUBTREE,
                attributes=["cn"]
            )

            groups = [entry.cn.value for entry in conn.entries]
            print("🧾 Alle Gruppen (rekursiv):", groups)

            if group_name in groups:
                session["user"] = username
                flash(f"✅ Willkommen, {username}")
                return redirect(url_for("admin"))
            else:
                flash("❌ Keine Berechtigung für Admin-Zugang.")
        except Exception as e:
            print("❗️ LDAP Login Fehler:", str(e))
            flash(f"Fehler beim Login: {e}")

    return render_template("login.html")

@app.route("/logout")
def logout():
    session.pop("user", None)
    flash("Abgemeldet.")
    return redirect(url_for("login"))

@app.route("/admin")
def admin():
    if "user" not in session:
        return redirect(url_for("login"))

    conn = sqlite3.connect("db/onoffboarding.db")
    c = conn.cursor()
    c.execute("SELECT * FROM requests WHERE status = 'offen' ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    return render_template("admin.html", requests=rows)

@app.route("/ou_tree")
def ou_tree():
    base_dn = os.getenv("AD_SEARCH_BASE")
    exclude = parse_exclude_ous()
    tree = build_ou_tree(base_dn, exclude_paths=exclude)
    return jsonify(tree)

@app.route("/ou_users")
def ou_users():
    dn = request.args.get("dn")
    if not dn:
        return jsonify([]), 400
    return jsonify(get_users_in_ou(dn))


@app.route("/supervisors")
def supervisors():
    dn = request.args.get("dn")
    if not dn:
        return jsonify([]), 400
    return jsonify(get_supervisors_in_ou(dn))


def send_approval_mail(to_address, firstname, lastname, process_type, request_id):
    server = os.getenv("SMTP_SERVER")
    port = int(os.getenv("SMTP_PORT", "25"))
    sender = os.getenv("SENDER")
    webserver = os.getenv("LOCALWEBSERVER")

    subject = f"Genehmigung erforderlich: {process_type.upper()} für {lastname}, {firstname}"
    link = f"http://{webserver}/view/{request_id}"
    body = f"""Sehr geehrte Vorgesetzte,

es liegt ein neuer Antrag zur Genehmigung vor:

🧾 Vorgang: {process_type}
👤 Name: {firstname} {lastname}

👉 Zur Ansicht und Genehmigung:
{link}

Mit freundlichen Grüßen
Ihr On-/Offboarding-System
"""

    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = to_address

    try:
        with smtplib.SMTP(server, port) as smtp:
            smtp.sendmail(sender, [to_address], msg.as_string())
        print(f"✅ Genehmigungs-E-Mail an {to_address} gesendet.")
    except Exception as e:
        print(f"❌ Fehler beim E-Mail-Versand: {e}")


@app.route("/supervisors_all")
def supervisors_all():
    from ad_utils import get_all_supervisors
    return jsonify(get_all_supervisors())


@app.route("/reject/<int:request_id>")
def reject_request(request_id):
    if "user" not in session:
        return redirect(url_for("login"))

    conn = sqlite3.connect("db/onoffboarding.db")
    c = conn.cursor()
    c.execute("UPDATE requests SET status = 'abgelehnt' WHERE id = ?", (request_id,))
    conn.commit()
    conn.close()
    flash("Antrag abgelehnt.")
    return redirect(url_for("admin"))


@app.route("/view/<int:request_id>")
def view_request(request_id):
    print("📥 view_request() wurde betreten")
    print("📦 Session:", session)
    print("👤 Session-User:", session.get("user"))

    conn = sqlite3.connect("db/onoffboarding.db")
    c = conn.cursor()
    c.execute("SELECT * FROM requests WHERE id = ?", (request_id,))
    request_data = c.fetchone()
    conn.close()

    can_approve = False

    if "user" in session:
        username = session["user"]
        supervisor_group = os.getenv("SUPERVISOR_GROUP", "vorgesetzter")
        search_base = os.getenv("AD_SEARCH_BASE")

        print("🔍 Benutzer-Session:", username)
        print("🔍 Prüfe Gruppe:", supervisor_group)

        try:
            ad_conn = get_ad_connection()
            ad_conn.search(
                search_base=search_base,
                search_filter=f"(&(objectClass=group)(cn={supervisor_group}))",
                attributes=["member"]
            )

            if not ad_conn.entries:
                print("❌ Gruppe nicht gefunden.")
            else:
                members = ad_conn.entries[0]["member"]
                print(f"📋 {len(members)} Mitglieder gefunden.")

                for dn in members:
                    ad_conn.search(
                        search_base=str(dn),
                        search_filter="(objectClass=user)",
                        search_scope="BASE",
                        attributes=["sAMAccountName"]
                    )
                    if ad_conn.entries:
                        sam = ad_conn.entries[0]["sAMAccountName"].value
                        print("👤 Gruppenmitglied:", sam)
                        print("🧪 Vergleich:", sam, "==", username)
                        if sam.strip().lower() == username.strip().lower():
                            print("✅ User ist Gruppenmitglied!")
                            can_approve = True
                            break
        except Exception as e:
            print("❗️ LDAP-Gruppenprüfung fehlgeschlagen:", e)

    print("👤 Eingeloggter Benutzer:", session.get("user"))
    print("✅ Darf genehmigen?", can_approve)

    return render_template("view.html", request=request_data, can_approve=can_approve)


@app.route("/approve/<int:request_id>", methods=["POST", "GET"])
def approve_request(request_id):
    if "user" not in session:
        return redirect(url_for("login"))

    conn = sqlite3.connect("db/onoffboarding.db")
    c = conn.cursor()

    # Antrag genehmigen
    c.execute("UPDATE requests SET status = 'genehmigt' WHERE id = ?", (request_id,))
    conn.commit()

    # Daten abrufen
    c.execute("SELECT * FROM requests WHERE id = ?", (request_id,))
    row = c.fetchone()
    conn.close()

    # Referenzbenutzer-DN via LDAP auflösen
    referenceuser_dn = ""
    try:
        ref_user = row[10]  # referenceuser
        ad_conn = get_ad_connection()
        search_base = os.getenv("AD_SEARCH_BASE")

        ad_conn.search(
            search_base=search_base,
            search_filter=f"(sAMAccountName={ref_user})",
            search_scope=SUBTREE,
            attributes=["distinguishedName"]
        )

        if ad_conn.entries:
            referenceuser_dn = ad_conn.entries[0]["distinguishedName"].value
            print("📌 Referenceuser DN:", referenceuser_dn)
        else:
            print("⚠️ Kein DN für referenceuser gefunden.")
    except Exception as e:
        print(f"❗ Fehler beim Referenceuser-DN-Lookup: {e}")

    # Webhook an n8n senden
    try:
        webhook_url = os.getenv("N8N_WEBHOOK_APPROVED", "http://192.168.89.113:5689/webhook-test/onoffboarding-approved")
        data = {
            "id": row[0],
            "firstname": row[1],
            "lastname": row[2],
            "department": row[3],
            "department_dn": row[12],
            "startdate": row[6],
            "enddate": row[7],
            "hardware": row[8],
            "comments": row[9],
            "referenceuser": row[10],
            "referenceuser_dn": referenceuser_dn,
            "process_type": row[11],
            "status": "genehmigt"
        }
        res = requests.post(webhook_url, json=data)
        print(f"📤 Webhook an n8n gesendet – Status: {res.status_code}")
    except Exception as e:
        print(f"❗ Fehler beim Senden des Webhooks: {e}")

    flash("Antrag genehmigt und an n8n übergeben.")
    return redirect(url_for("admin"))


@app.route("/archived")
def archived():
    if "user" not in session:
        return redirect(url_for("login"))

    conn = sqlite3.connect("db/onoffboarding.db")
    c = conn.cursor()
    c.execute("SELECT * FROM requests WHERE status != 'offen' ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    return render_template("archived.html", requests=rows)

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000)
