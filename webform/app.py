import smtplib
import os
import sqlite3
import requests
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from dotenv import load_dotenv, find_dotenv
# Stelle sicher, dass ALLE benötigten Funktionen importiert werden
from ad_utils import (
    build_ou_tree, parse_exclude_ous, get_users_in_ou,
    get_supervisors_in_ou, get_all_supervisors, get_ad_connection
)
from ldap3 import Server, Connection, ALL, SUBTREE
from datetime import datetime
from email.mime.text import MIMEText
import logging  # Importiere das logging-Modul
import sys  # Importiere sys
import ldap3.core.exceptions # Wird für spezifischere Fehlerbehandlung benötigt
import re  # Importiere das Modul für reguläre Ausdrücke

load_dotenv(find_dotenv())

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "secret123")

# Konfiguriere das Logging für Docker
logging.basicConfig(
    level=logging.INFO,  # Setze das Log-Level (z.B. DEBUG, INFO, WARNING, ERROR)
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout  # Leite das Logging nach stdout um
)
print("🚀 Starte app.py neu!")

# Konstanten für Umgebungsvariablen
AD_SERVER = os.getenv("AD_SERVER")
AD_SEARCH_BASE = os.getenv("AD_SEARCH_BASE")
AD_GROUP = os.getenv("AD_GROUP") # Admin Gruppe für Login
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", "25"))
SENDER = os.getenv("SENDER")
WEB_SERVER = os.getenv("LOCALWEBSERVER")
N8N_WEBHOOK_APPROVED = os.getenv("N8N_WEBHOOK_APPROVED", "http://onoffboarder-n8n-1:5678/webhook-test/onoffboarding-approved")
# Name der AD-Gruppe, die Vorgesetzte enthält (aus .env geladen)
SUPERVISOR_GROUP = os.getenv("SUPERVISOR_GROUP", "vorgesetzter")
MAIL_USER = os.getenv("MAIL_USER")  # Fallback E-Mail Empfänger
# Regulärer Ausdruck für die E-Mail-Validierung (einfach)
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def is_valid_email(email):
    """
    Überprüft, ob eine E-Mail-Adresse gültig ist.

    Args:
        email (str): Die zu überprüfende E-Mail-Adresse.

    Returns:
        bool: True, wenn die E-Mail-Adresse gültig ist, False sonst.
    """
    if not isinstance(email, str):
        return False
    return EMAIL_REGEX.match(email) is not None


@app.route("/", methods=["GET", "POST"])
def form():
    today = datetime.today().strftime('%Y-%m-%d')
    if request.method == "POST":
        lastname = request.form.get("lastname")
        firstname = request.form.get("firstname")
        startdate = request.form.get("startdate") or datetime.today().strftime('%Y-%m-%d')
        enddate = request.form.get("enddate")
        department = request.form.get("department")
        department_dn = request.form.get("department_dn")
        supervisor = request.form.get("supervisor", "") # Holt die ausgewählte Supervisor E-Mail/DN
        comments = request.form.get("comments", "")
        hardware_computer = request.form.get("hardware_computer", "")
        hardware_monitor = request.form.get("hardware_monitor", "")
        hardware_accessories = request.form.getlist("hardware_accessories[]")
        hardware_mobile = request.form.getlist("hardware_mobile[]")
        referenceuser = request.form.get("referenceuser", "")
        process_type = request.form.get("process_type", "onboarding")
        status = "offen"
        role = "user"
        key_required = request.form.get("key_required") == "true"
        required_windows = request.form.get("needs_windows_user") == "true"
        hardware_required = request.form.get("needs_hardware") == "true"

        # Validierung (Beispiel: Vorname/Nachname dürfen nicht leer sein, wenn Windows-Konto benötigt)
        if required_windows and (not firstname or not lastname):
             flash("Vor- und Nachname sind erforderlich, wenn ein Windows Benutzerkonto benötigt wird.", "danger")
             # Lade Formulardaten erneut für die Anzeige
             ou_tree_data, all_supervisors_data = load_form_dependencies()
             return render_template("form.html", today=today, ou_tree=ou_tree_data, all_supervisors=all_supervisors_data, form_data=request.form) # Formdaten zurückgeben

        # Validierung Vorgesetzter (wenn Windows benötigt)
        if required_windows and not supervisor:
             flash("Ein Vorgesetzter muss ausgewählt werden, wenn ein Windows Benutzerkonto benötigt wird.", "danger")
             ou_tree_data, all_supervisors_data = load_form_dependencies()
             return render_template("form.html", today=today, ou_tree=ou_tree_data, all_supervisors=all_supervisors_data, form_data=request.form)


        conn = sqlite3.connect("db/onoffboarding.db")
        c = conn.cursor()
        # Spaltennamen explizit angeben für bessere Lesbarkeit und Wartbarkeit
        sql_insert = '''INSERT INTO requests
               (lastname, firstname, startdate, enddate, department, supervisor,
                hardware_computer, hardware_monitor, hardware_accessories, hardware_mobile,
                comments, referenceuser, process_type, status, role, department_dn,
                key_required, required_windows, hardware_required)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'''
        values = (
            lastname, firstname, startdate, enddate, department, supervisor,
            hardware_computer if hardware_required else "",
            hardware_monitor if hardware_required else "",
            ",".join(hardware_accessories) if hardware_accessories and hardware_required else "",
            ",".join(hardware_mobile) if hardware_mobile else "", # Mobile Geräte unabhängig?
            comments, referenceuser, process_type, status, role, department_dn,
            key_required, required_windows, hardware_required
        )
        try:
            c.execute(sql_insert, values)
            conn.commit()
            request_id = c.lastrowid # Korrekte Methode, um die ID zu bekommen
            conn.close()
            logging.info(f"💾 Anfrage in Datenbank gespeichert mit ID: {request_id}")
        except sqlite3.Error as e:
            logging.error(f"❌ SQLite Fehler beim Speichern des Antrags: {e}")
            flash(f"Fehler beim Speichern des Antrags in der Datenbank: {e}", "danger")
            if conn: conn.close()
            ou_tree_data, all_supervisors_data = load_form_dependencies()
            return render_template("form.html", today=today, ou_tree=ou_tree_data, all_supervisors=all_supervisors_data, form_data=request.form)


        # Bedingte Logik für E-Mail/Webhook basierend auf 'required_windows'
        if required_windows:
            # Fall A: Windows-Konto benötigt -> E-Mail senden
            try:
                # 'supervisor' Variable enthält hier die E-Mail oder den DN des Vorgesetzten
                send_approval_mail(supervisor, firstname, lastname, process_type, request_id)
                flash("Antrag gespeichert und zur Freigabe versendet.", "success")
            except ValueError as e: # Fehler bei Adressvalidierung
                 logging.error(f"Fehler beim Mailversand (Adresse) für Antrag {request_id}: {e}")
                 flash(f"Antrag gespeichert, aber Fehler bei Genehmigungs-E-Mail: {e}. Bitte Admin kontaktieren.", "warning")
                 # Optional: Status in DB auf Fehler setzen?
            except Exception as e: # Andere Fehler (SMTP etc.)
                logging.error(f"Fehler beim initialen Mailversand für Antrag {request_id}: {e}")
                flash(f"Antrag gespeichert, aber Fehler beim Senden der Genehmigungs-E-Mail: {e}. Bitte Admin kontaktieren.", "warning")
                # Optional: Status in DB auf Fehler setzen?
        else:
            # Fall B: Kein Windows-Konto benötigt -> Direkt Webhook auslösen
            logging.info(f"ℹ️ Antrag {request_id}: E-Mail-Benachrichtigung übersprungen (kein Windows-Konto). Starte direkte Verarbeitung.")
            try:
                # Setze den Status direkt auf 'genehmigt'
                conn = sqlite3.connect("db/onoffboarding.db")
                c = conn.cursor()
                c.execute("UPDATE requests SET status = 'genehmigt' WHERE id = ?", (request_id,))
                conn.commit()
                conn.close()
                logging.info(f"✅ Antrag {request_id} automatisch genehmigt.")
                # Löse den Webhook aus
                trigger_n8n_webhook(request_id) # Diese Funktion sollte Fehler intern loggen/behandeln
                flash("Antrag gespeichert und direkt an die Verarbeitung übergeben.", "success")
            except sqlite3.Error as e:
                logging.error(f"❌ SQLite Fehler beim automatischen Genehmigen von Antrag {request_id}: {e}")
                flash(f"Antrag gespeichert, aber Fehler bei der Datenbankaktualisierung: {e}", "danger")
                if conn: conn.close()
            except Exception as e:
                 logging.error(f"Fehler bei der direkten Verarbeitung/Webhook für Antrag {request_id}: {e}")
                 flash(f"Antrag gespeichert, aber Fehler bei der direkten Verarbeitung: {e}", "danger")
                 # Status ist hier noch 'offen', da Update fehlgeschlagen ist oder Webhook-Problem

        return redirect(url_for("form"))
        # Ende des POST-Blocks

    # --- GET Request: Formular anzeigen ---
    ou_tree_data, all_supervisors_data = load_form_dependencies()
    return render_template("form.html", today=today, ou_tree=ou_tree_data, all_supervisors=all_supervisors_data)

def load_form_dependencies():
    """Lädt OU-Baum und globale Vorgesetztenliste für das Formular."""
    ou_tree_data = []
    all_supervisors_data = []
    try:
       exclude = parse_exclude_ous()
       # Lade OU Baum - Fehler hier sollte abgefangen werden
       try:
           ou_tree_data = build_ou_tree(AD_SEARCH_BASE, exclude_paths=exclude)
       except Exception as e_ou:
           logging.error(f"Fehler beim Laden des OU-Baums: {e_ou}")
           flash("Fehler beim Laden der Abteilungsstruktur.", "warning")

       # Lade ALLE Supervisoren initial für den Fall, dass keine OU gewählt wird
       # oder als Fallback, falls die OU-spezifische Suche fehlschlägt
       try:
           all_supervisors_data = get_all_supervisors()
       except Exception as e_sup:
            logging.error(f"Fehler beim Laden der globalen Vorgesetztenliste: {e_sup}")
            flash("Fehler beim Laden der Vorgesetztenliste.", "warning")

    except Exception as e:
       # Allgemeiner Fehler (z.B. in parse_exclude_ous)
       logging.error(f"Genereller Fehler beim Laden der Formularabhängigkeiten: {e}")
       flash("Ein Fehler ist beim Laden der Formulardaten aufgetreten.", "danger")
    return ou_tree_data, all_supervisors_data


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        logging.debug("🔑 Form-Login Versuch für Benutzer: %s", username)
        logging.debug("AD_SERVER: %s", AD_SERVER)
        logging.debug("AD_SEARCH_BASE: %s", AD_SEARCH_BASE)
        logging.debug("AD_GROUP (Admin): %s", AD_GROUP)


        if not AD_SERVER or not AD_SEARCH_BASE or not username or not password:
            flash("⚠️ Fehlende Konfiguration oder Eingabefehler.", "warning")
            return render_template("login.html")

        conn = None # Initialisiere conn hier
        try:
            server = Server(AD_SERVER, get_info=ALL)
            domain_parts = [part.split('=')[1] for part in AD_SEARCH_BASE.split(',') if part.strip().startswith('DC=')]
            domain = '.'.join(domain_parts)
            user_principal_name = f"{username}@{domain}"

            logging.debug(f"➡️ Versuch Bind mit UPN: {user_principal_name}")
            conn = Connection(server, user=user_principal_name, password=password, authentication="SIMPLE", auto_bind=True)
            logging.info(f"✅ Bind mit UPN für '{username}' erfolgreich.")

            # Suche nach dem sAMAccountName des Benutzers (wird oft für die Session verwendet)
            conn.search(
                search_base=AD_SEARCH_BASE,
                search_filter=f"(userPrincipalName={user_principal_name})",
                search_scope=SUBTREE,
                attributes=['distinguishedName', 'sAMAccountName']
            )

            if not conn.entries:
                 # UPN nicht gefunden, könnte an Konfiguration liegen oder falsche Eingabe
                 # Ein erneuter Versuch mit sAMAccountName ist hier nicht sinnvoll für den *Bind*
                 logging.warning(f"⚠️ UPN '{user_principal_name}' nicht gefunden im AD.")
                 flash("⚠️ Benutzer nicht gefunden oder UPN-Format inkorrekt.", "warning")
                 if conn.bound: conn.unbind()
                 return render_template("login.html")

            user_entry_dn = conn.entries[0].distinguishedName.value
            actual_sam_account_name = conn.entries[0].sAMAccountName.value if 'sAMAccountName' in conn.entries[0] else username
            logging.debug(f"Benutzer DN: {user_entry_dn}")
            logging.debug(f"Benutzer sAMAccountName: {actual_sam_account_name}")

            # --- Prüfung auf Admin-Gruppenmitgliedschaft (AD_GROUP) ---
            if not AD_GROUP:
                logging.error("🚫 Admin-Gruppe (AD_GROUP) ist nicht in .env konfiguriert!")
                flash("🚫 Admin-Konfiguration unvollständig.", "danger")
                if conn.bound: conn.unbind()
                return render_template("login.html")

            # Finde zuerst den DN der Admin-Gruppe
            conn.search(AD_SEARCH_BASE, f"(&(objectClass=group)(cn={AD_GROUP}))", attributes=['distinguishedName'])
            if not conn.entries:
                logging.error(f"🚫 Admin-Gruppe '{AD_GROUP}' nicht im AD gefunden!")
                flash(f"🚫 Admin-Gruppe '{AD_GROUP}' nicht gefunden.", "danger")
                if conn.bound: conn.unbind()
                return render_template("login.html")
            admin_group_dn = conn.entries[0].distinguishedName.value
            logging.debug(f"DN der Admin-Gruppe '{AD_GROUP}': {admin_group_dn}")

            # Prüfe rekursive Mitgliedschaft des Benutzers in der Admin-Gruppe
            is_admin = conn.search(
                 search_base=user_entry_dn, # Suche beim User-Objekt
                 search_filter=f"(memberOf:1.2.840.113556.1.4.1941:={admin_group_dn})",
                 search_scope='base',
                 attributes=['cn'] # Minimales Attribut anfordern
            )
            logging.debug(f"Prüfung auf Mitgliedschaft in '{AD_GROUP}' (rekursiv): {'Ja' if is_admin else 'Nein'}")

            if is_admin:
                session["user"] = actual_sam_account_name # sAMAccountName in Session speichern
                session["user_dn"] = user_entry_dn # Optional: DN auch speichern
                session["is_admin"] = True # Flag setzen
                flash(f"✅ Willkommen, {actual_sam_account_name}!", "success")
                conn.unbind() # Verbindung explizit schließen nach Erfolg
                return redirect(url_for("admin")) # Weiterleitung zum Admin-Bereich
            else:
                flash("🚫 Sie haben keine Berechtigung für den Admin-Zugang.", "danger")
                conn.unbind() # Verbindung schließen
                return render_template("login.html")

        except ldap3.core.exceptions.LDAPBindError:
             # Passwort falsch oder Benutzer/UPN falsch formatiert
             logging.warning(f"❌ LDAP Bind Fehler beim Login für '{username}'. Ungültige Anmeldedaten.")
             flash("Fehler beim Login: Ungültiger Benutzername oder Passwort.", "danger")
        except ldap3.core.exceptions.LDAPSocketOpenError as e:
            logging.error(f"❌ LDAP Verbindungsproblem zum Server '{AD_SERVER}': {e}")
            flash(f"Fehler beim Login: Keine Verbindung zum Verzeichnisdienst möglich.", "danger")
        except ldap3.core.exceptions.LDAPError as e:
            # Andere LDAP-Fehler (z.B. Suche fehlgeschlagen)
            logging.error(f"❌ Allgemeiner LDAP Fehler beim Login für '{username}': {e}")
            flash("Fehler beim Login: Ein Problem mit dem Verzeichnisdienst ist aufgetreten.", "danger")
        except Exception as e:
            # Unerwartete Python-Fehler
            logging.error(f"❌ Unerwarteter Fehler beim Login für '{username}': {e}", exc_info=True) # Traceback loggen
            flash("Ein unerwarteter interner Fehler ist aufgetreten.", "danger")
        finally:
            # Stelle sicher, dass die Verbindung immer geschlossen wird, falls sie existiert und gebunden ist
             if conn and conn.bound:
                 try:
                     conn.unbind()
                     logging.debug("🔒 LDAP Verbindung nach Login-Versuch geschlossen.")
                 except Exception as unbind_e:
                     logging.error(f"Fehler beim Schließen der LDAP-Verbindung nach Login-Versuch: {unbind_e}")

    # GET Request oder fehlgeschlagener POST
    return render_template("login.html")


@app.route("/logout")
def logout():
    user = session.get('user', 'Unbekannt')
    session.clear() # Alle Session-Daten löschen
    flash("Sie wurden erfolgreich abgemeldet.", "info")
    logging.info(f"👤 Benutzer '{user}' abgemeldet.")
    return redirect(url_for("login"))


@app.route("/admin")
def admin():
    # Strenge Prüfung: Nur eingeloggte User, die auch Admins sind
    if "user" not in session or not session.get("is_admin"):
        flash("Zugriff verweigert. Bitte als Administrator anmelden.", "warning")
        return redirect(url_for("login"))

    conn = sqlite3.connect("db/onoffboarding.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    # Zeige nur offene Anträge im Haupt-Admin-Bereich
    c.execute("SELECT * FROM requests WHERE status = 'offen' ORDER BY id DESC")
    open_requests = c.fetchall()
    conn.close()

    processed_requests = []
    for req in open_requests:
        req_dict = dict(req)
        # Konvertiere kommagetrennte Strings in Listen für die Anzeige
        req_dict['hardware_accessories'] = req_dict['hardware_accessories'].split(',') if req_dict['hardware_accessories'] else []
        req_dict['hardware_mobile'] = req_dict['hardware_mobile'].split(',') if req_dict['hardware_mobile'] else []
        processed_requests.append(req_dict)

    return render_template("admin.html", requests=processed_requests)


@app.route("/ou_tree")
def ou_tree():
    # Zugriffsschutz: Nur für eingeloggte Benutzer
    if "user" not in session:
        return jsonify({"error": "Nicht authentifiziert"}), 401
    try:
        exclude = parse_exclude_ous()
        tree = build_ou_tree(AD_SEARCH_BASE, exclude_paths=exclude)
        return jsonify(tree)
    except Exception as e:
        logging.error(f"Fehler beim Erstellen des OU-Baums: {e}")
        return jsonify({"error": "Konnte OU-Baum nicht laden"}), 500


@app.route("/ou_users")
def ou_users():
    # Zugriffsschutz
    if "user" not in session:
        return jsonify({"error": "Nicht authentifiziert"}), 401
    dn = request.args.get("dn")
    if not dn:
        return jsonify({"error": "Parameter 'dn' fehlt"}), 400
    try:
        users = get_users_in_ou(dn) # Aus ad_utils.py
        return jsonify(users)
    except Exception as e:
        logging.error(f"Fehler beim Laden der Benutzer für OU '{dn}': {e}")
        return jsonify({"error": "Konnte Benutzer nicht laden"}), 500

# --- NEUE VERSION DER /supervisors ROUTE ---
# In app.py -> innerhalb der /supervisors Route

@app.route("/supervisors")
def supervisors():
    # Zugriffsschutz
    if "user" not in session:
        logging.warning("Zugriffsversuch auf /supervisors ohne Session.") # Logge den Versuch
        return jsonify({"error": "Nicht authentifiziert"}), 401

    logging.info("--- /supervisors Route aufgerufen ---") # Start der Funktion loggen
    dn = request.args.get("dn")
    logging.info(f"Empfangener DN: {dn}") # Geloggter DN

    final_supervisor_list = []

    if not SUPERVISOR_GROUP:
        logging.error("SUPERVISOR_GROUP ist nicht konfiguriert!")
        return jsonify({"error": "Vorgesetzten-Gruppe nicht konfiguriert."}), 500

    try:
        if dn:
            logging.info(f"Versuche Vorgesetzte für spezifische OU: {dn}")
            # --- HIER STARTET DER KRITISCHE TEIL ---
            logging.debug("Rufe get_supervisors_in_ou auf...")
            ou_specific_supervisors = get_supervisors_in_ou(dn) # <-- Mögliche Fehlerquelle 1
            logging.debug(f"Ergebnis von get_supervisors_in_ou: {ou_specific_supervisors}") # Logge das Ergebnis

            if ou_specific_supervisors:
                logging.info(f"✅ {len(ou_specific_supervisors)} Vorgesetzte in OU '{dn}' gefunden.")
                final_supervisor_list = ou_specific_supervisors
            else:
                logging.info(f"ℹ️ Keine Vorgesetzten in OU '{dn}'. Rufe get_all_supervisors (Fallback)...")
                final_supervisor_list = get_all_supervisors() # <-- Mögliche Fehlerquelle 2
                logging.debug(f"Ergebnis von get_all_supervisors (Fallback): {final_supervisor_list}") # Logge das Ergebnis
        else:
            logging.info("Keine spezifische OU angegeben. Rufe get_all_supervisors...")
            final_supervisor_list = get_all_supervisors() # <-- Mögliche Fehlerquelle 3
            logging.debug(f"Ergebnis von get_all_supervisors (ohne DN): {final_supervisor_list}") # Logge das Ergebnis

        logging.info(f"Gebe {len(final_supervisor_list)} Vorgesetzte als JSON zurück.") # Loggen vor dem Return
        return jsonify(final_supervisor_list)

    except ldap3.core.exceptions.LDAPError as e:
        # Logge spezifische LDAP Fehler detaillierter
        logging.error(f"LDAP Fehler in /supervisors (DN: {dn}, Gruppe: '{SUPERVISOR_GROUP}'): {e}", exc_info=True) # Mit Traceback
        return jsonify({"error": f"Verzeichnisdienstfehler: {e}"}), 500
    except Exception as e:
        # Fange alle anderen Fehler ab und logge sie!
        logging.error(f"Unerwarteter Fehler in /supervisors (DN: {dn}, Gruppe: '{SUPERVISOR_GROUP}'): {e}", exc_info=True) # Mit Traceback!
        # Gib eine generische Fehlermeldung zurück, aber logge den spezifischen Fehler
        return jsonify({"error": "Ein interner Serverfehler ist aufgetreten."}), 500
# --- ENDE DER NEUEN /supervisors ROUTE ---


def send_approval_mail(to_address, firstname, lastname, process_type, request_id):
    """
    Sendet eine E-Mail an den Vorgesetzten zur Genehmigung.
    Verwendet MAIL_USER als Fallback, wenn to_address ungültig ist.
    Args:
        to_address (str): E-Mail oder DN des Vorgesetzten aus dem Formular.
        ... (andere Args) ...
    Raises:
        ValueError: Wenn keine gültige Empfängeradresse gefunden/konfiguriert wurde.
        smtplib.SMTPException: Bei SMTP-Fehlern.
        Exception: Bei anderen Fehlern.
    """
    subject = f"Genehmigung erforderlich: {process_type.capitalize()} für {lastname}, {firstname} (ID: {request_id})"
    base_url = WEB_SERVER if WEB_SERVER and WEB_SERVER.startswith(('http://', 'https://')) else f"http://{WEB_SERVER}"
    link = f"{base_url}/view/{request_id}" # Link zur Detailansicht

    body = f"""Sehr geehrte/r Vorgesetzte/r,

ein neuer Antrag ({process_type}) für '{firstname} {lastname}' erfordert Ihre Genehmigung.

Details und Genehmigungsoptionen finden Sie unter folgendem Link:
{link}

(Antrags-ID: {request_id})

Mit freundlichen Grüßen,
Ihr On-/Offboarding-System
"""

    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = subject
    msg['From'] = SENDER

    final_recipient = None
    recipient_source = "unbekannt" # Für Logging

    # 1. Prüfe Absender
    if not SENDER or not is_valid_email(SENDER):
        logging.error(f"❌ Ungültige oder fehlende Absenderadresse (SENDER): {SENDER}")
        raise ValueError("Ungültige Absenderadresse konfiguriert.")

    # 2. Prüfe primären Empfänger (Vorgesetzter aus Formular)
    # Annahme: `to_address` könnte eine E-Mail oder ein DN sein.
    # Wenn es ein DN ist, müssen wir die E-Mail noch auflösen.
    # Einfache Annahme hier: Es ist bereits eine E-Mail.
    # TODO: Erweitere dies ggf. um DN-zu-E-Mail-Auflösung via LDAP, falls 'supervisor' im Formular ein DN sein kann.
    if to_address and is_valid_email(to_address):
        final_recipient = to_address
        recipient_source = "Vorgesetzter (Formular)"
        logging.info(f"Primärer Empfänger (aus Formular) ist gültige E-Mail: {final_recipient}")
    else:
        logging.warning(f"⚠️ Primäre Empfängeradresse '{to_address}' ist ungültig oder fehlt. Prüfe Fallback MAIL_USER ('{MAIL_USER}').")
        # 3. Prüfe Fallback Empfänger (MAIL_USER)
        if MAIL_USER and is_valid_email(MAIL_USER):
            final_recipient = MAIL_USER
            recipient_source = "Fallback (MAIL_USER)"
            logging.warning(f"⚠️ Sende E-Mail an Fallback-Adresse: {final_recipient}")
            msg['Subject'] = f"[Fallback] {subject}" # Betreff anpassen
        else:
            logging.error(f"❌ Weder primäre Adresse ('{to_address}') noch Fallback MAIL_USER ('{MAIL_USER}') sind gültig/konfiguriert.")
            raise ValueError("Keine gültige Empfängeradresse für Genehmigungs-E-Mail gefunden.")

    msg['To'] = final_recipient

    try:
        logging.info(f"📧 Sende E-Mail von '{SENDER}' an '{final_recipient}' ({recipient_source}) für Antrag {request_id}.")
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as smtp: # Timeout hinzufügen
            # Optional: Debugging, TLS, Login (siehe vorherige Versionen)
            # smtp.set_debuglevel(1)
            # if SMTP_USE_TLS: smtp.starttls() # Env Var hinzufügen
            # if SMTP_USER and SMTP_PASSWORD: smtp.login(SMTP_USER, SMTP_PASSWORD) # Env Vars hinzufügen
            smtp.sendmail(SENDER, [final_recipient], msg.as_string())
        logging.info(f"✅ Genehmigungs-E-Mail für Antrag {request_id} an {final_recipient} gesendet.")
    except smtplib.SMTPException as e:
        logging.error(f"❌ SMTP Fehler beim Senden an {final_recipient} für Antrag {request_id}: {e}")
        raise # Fehler weitergeben
    except Exception as e:
        logging.error(f"❌ Unerwarteter Fehler beim Senden an {final_recipient} für Antrag {request_id}: {e}", exc_info=True)
        raise # Fehler weitergeben


@app.route("/reject/<int:request_id>", methods=["POST"])
def reject_request(request_id):
    # Zugriffsschutz: Nur eingeloggte Admins oder berechtigte User dürfen ablehnen
    # Hier vereinfacht: Nur Admins (gleiche Logik wie /approve)
    if "user" not in session or not session.get("is_admin"):
        flash("Zugriff verweigert. Nur Administratoren können Anträge ablehnen.", "warning")
        return redirect(url_for("login"))

    current_user = session['user']
    logging.info(f"➡️ Ablehnungsversuch für Antrag ID: {request_id} durch Admin '{current_user}'")

    try:
        conn = sqlite3.connect("db/onoffboarding.db")
        c = conn.cursor()
        c.execute("SELECT status FROM requests WHERE id = ?", (request_id,))
        result = c.fetchone()

        if result and result[0] == 'offen':
            # Status auf 'abgelehnt' setzen
            c.execute("UPDATE requests SET status = 'abgelehnt' WHERE id = ?", (request_id,))
            conn.commit()
            flash(f"Antrag {request_id} wurde abgelehnt.", "info")
            logging.info(f"🚫 Antrag {request_id} durch Admin '{current_user}' abgelehnt.")
            # Optional: Benachrichtigung an Ersteller/Vorgesetzten?
        elif result:
            flash(f"Antrag {request_id} konnte nicht abgelehnt werden (Aktueller Status: {result[0]}).", "warning")
            logging.warning(f"Ablehnung für Antrag {request_id} fehlgeschlagen, Status ist bereits '{result[0]}'.")
        else:
            flash(f"Antrag {request_id} nicht gefunden.", "danger")
            logging.warning(f"Ablehnungsversuch für nicht existierenden Antrag ID: {request_id}")

        conn.close()
    except sqlite3.Error as e:
        logging.error(f"❌ SQLite Fehler beim Ablehnen von Antrag {request_id}: {e}")
        flash(f"Datenbankfehler beim Ablehnen des Antrags: {e}", "danger")
        if conn: conn.close()
    except Exception as e:
        logging.error(f"❌ Allgemeiner Fehler beim Ablehnen von Antrag {request_id}: {e}", exc_info=True)
        flash(f"Unerwarteter Fehler beim Ablehnen des Antrags.", "danger")

    # Leite zum Admin-Bereich zurück
    return redirect(url_for("admin"))


@app.route("/view/<int:request_id>")
def view_request(request_id):
    """
    Zeigt die Details eines bestimmten Antrags an.
    Prüft, ob der aktuell eingeloggte Benutzer genehmigen darf.
    """
    logging.info(f"👀 Zeige Details für Antrag ID: {request_id}")

    conn = sqlite3.connect("db/onoffboarding.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM requests WHERE id = ?", (request_id,))
    request_data = c.fetchone()
    conn.close()

    if not request_data:
        flash(f"Antrag {request_id} nicht gefunden.", "danger")
        return redirect(url_for("admin"))

    can_approve = False
    current_user = session.get("user") # sAMAccountName des eingeloggten Benutzers
    is_admin_session = session.get("is_admin", False) # Ist der User Admin laut Login?

    # Genehmigung möglich, wenn:
    # 1. Benutzer eingeloggt ist UND
    # 2. Antrag noch 'offen' ist UND
    # 3. Benutzer entweder Admin ist ODER Mitglied der SUPERVISOR_GROUP
    if current_user and request_data['status'] == 'offen':
        logging.debug(f"Prüfe Genehmigungsrecht für Antrag {request_id} durch Benutzer '{current_user}'. Status: {request_data['status']}")

        if is_admin_session:
             can_approve = True
             logging.info(f"✅ Benutzer '{current_user}' ist Admin und darf genehmigen.")
        elif SUPERVISOR_GROUP:
            logging.debug(f"Prüfe Mitgliedschaft von '{current_user}' in Gruppe '{SUPERVISOR_GROUP}'")
            # Prüfe Gruppenmitgliedschaft via LDAP
            ad_conn = None
            try:
                ad_conn = get_ad_connection()
                # Finde DN der SUPERVISOR_GROUP
                ad_conn.search(AD_SEARCH_BASE, f"(&(objectClass=group)(cn={SUPERVISOR_GROUP}))", attributes=['distinguishedName'])
                if ad_conn.entries:
                    group_dn = ad_conn.entries[0].distinguishedName.value
                    # Finde DN des aktuellen Benutzers
                    ad_conn.search(AD_SEARCH_BASE, f"(sAMAccountName={current_user})", attributes=['distinguishedName'])
                    if ad_conn.entries:
                        user_dn = ad_conn.entries[0].distinguishedName.value
                        # Prüfe rekursive Mitgliedschaft
                        is_member = ad_conn.search(
                            search_base=user_dn,
                            search_filter=f"(memberOf:1.2.840.113556.1.4.1941:={group_dn})",
                            search_scope='base', attributes=['cn']
                        )
                        if is_member:
                            can_approve = True
                            logging.info(f"✅ Benutzer '{current_user}' ist Mitglied von '{SUPERVISOR_GROUP}' und darf genehmigen.")
                        else:
                            logging.info(f"ℹ️ Benutzer '{current_user}' ist kein Mitglied von '{SUPERVISOR_GROUP}'.")
                    else:
                        logging.warning(f"⚠️ Konnte DN für Benutzer '{current_user}' nicht finden (für Gruppenprüfung).")
                else:
                    logging.warning(f"⚠️ Vorgesetzten-Gruppe '{SUPERVISOR_GROUP}' nicht im AD gefunden.")

            except ldap3.core.exceptions.LDAPError as e:
                logging.error(f"❌ LDAP-Fehler bei der Gruppenprüfung für '{current_user}' in '{SUPERVISOR_GROUP}': {e}")
            except Exception as e:
                logging.error(f"❌ Unerwarteter Fehler bei der Gruppenprüfung für '{current_user}': {e}", exc_info=True)
            finally:
                 if ad_conn and ad_conn.bound:
                      try: ad_conn.unbind()
                      except: pass
        else:
             logging.warning("Keine SUPERVISOR_GROUP konfiguriert, nur Admins können genehmigen.")

    logging.info(f"Genehmigungsrecht für '{current_user}' bei Antrag {request_id}: {can_approve}")

    # Bereite Daten für die Anzeige vor
    request_dict = dict(request_data)
    request_dict['hardware_accessories'] = request_dict['hardware_accessories'].split(',') if request_dict['hardware_accessories'] else []
    request_dict['hardware_mobile'] = request_dict['hardware_mobile'].split(',') if request_dict['hardware_mobile'] else []

    return render_template("view.html", request=request_dict, can_approve=can_approve)


def trigger_n8n_webhook(request_id):
    """
    Ruft Daten ab und sendet sie an den N8N_WEBHOOK_APPROVED.
    Behandelt Fehler intern und loggt sie.
    """
    logging.info(f"🚀 Trigger n8n Webhook für Antrag ID: {request_id}")
    webhook_url = N8N_WEBHOOK_APPROVED
    if not webhook_url:
         logging.error(f"❌ N8N_WEBHOOK_APPROVED ist nicht konfiguriert. Webhook für Antrag {request_id} kann nicht gesendet werden.")
         # Setze Status in DB auf Fehler, damit es nachverfolgt werden kann
         try:
             conn = sqlite3.connect("db/onoffboarding.db")
             c = conn.cursor()
             # Verwende spezifischen Status oder füge Kommentar hinzu
             c.execute("UPDATE requests SET status = 'fehler_webhook_url' WHERE id = ?", (request_id,))
             conn.commit()
             conn.close()
         except sqlite3.Error as db_err:
             logging.error(f"❌ Konnte Status nicht auf 'fehler_webhook_url' setzen für Antrag {request_id}: {db_err}")
         return # Beende Funktion

    conn = None
    row = None
    try:
        conn = sqlite3.connect("db/onoffboarding.db")
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM requests WHERE id = ?", (request_id,))
        row = c.fetchone()
        conn.close() # Schließe DB-Verbindung so früh wie möglich
        conn = None # Setze auf None, um doppeltes Schließen im finally zu vermeiden

        if not row:
            logging.error(f"❌ Konnte Daten für Antrag {request_id} nicht aus DB abrufen für Webhook.")
            # Hier ist der Status wahrscheinlich schon 'genehmigt', kein Update nötig.
            return

        logging.debug(f"ℹ️ Abgerufene DB-Daten für Webhook (Antrag {request_id}): {dict(row)}")

        # Referenzbenutzer-DN auflösen (falls vorhanden)
        referenceuser_dn = ""
        ref_user_sam = row["referenceuser"]
        if ref_user_sam:
            ad_conn = None
            try:
                logging.info(f" LDAP Suche nach DN für Referenzbenutzer '{ref_user_sam}' (Antrag {request_id})")
                ad_conn = get_ad_connection()
                ad_conn.search(
                    search_base=AD_SEARCH_BASE,
                    search_filter=f"(sAMAccountName={ref_user_sam})",
                    search_scope=SUBTREE,
                    attributes=["distinguishedName"]
                )
                if ad_conn.entries:
                    referenceuser_dn = ad_conn.entries[0]["distinguishedName"].value
                    logging.info(f"✅ DN für Referenzbenutzer '{ref_user_sam}' gefunden: {referenceuser_dn}")
                else:
                    logging.warning(f"⚠️ Kein DN für Referenzbenutzer '{ref_user_sam}' gefunden (Antrag {request_id}).")
            except ldap3.core.exceptions.LDAPError as e:
                logging.error(f"❌ LDAP Fehler beim Referenceuser-DN-Lookup für '{ref_user_sam}': {e}")
            except Exception as e:
                logging.error(f"❌ Unerwarteter Fehler beim Referenceuser-DN-Lookup für '{ref_user_sam}': {e}", exc_info=True)
            finally:
                if ad_conn and ad_conn.bound:
                    try: ad_conn.unbind()
                    except: pass

        # Daten für Webhook zusammenstellen
        data = {
            "id": row["id"], "firstname": row["firstname"], "lastname": row["lastname"],
            "startdate": row["startdate"], "enddate": row["enddate"],
            "department": row["department"], "department_dn": row["department_dn"],
            "supervisor": row["supervisor"], # E-Mail/DN wie gespeichert
            "hardware_computer": row["hardware_computer"], "hardware_monitor": row["hardware_monitor"],
            "hardware_accessories": row["hardware_accessories"].split(',') if row["hardware_accessories"] else [],
            "hardware_mobile": row["hardware_mobile"].split(',') if row["hardware_mobile"] else [],
            "comments": row["comments"],
            "referenceuser": ref_user_sam, "referenceuser_dn": referenceuser_dn,
            "process_type": row["process_type"], "status": row["status"], # Status sollte 'genehmigt' sein
            "key_required": bool(row["key_required"]),
            "required_windows": bool(row["required_windows"]),
            "hardware_required": bool(row["hardware_required"]),
            "created_at": row["created_at"] # Zeitstempel hinzufügen
        }

        logging.info(f"➡️ Sende Webhook für Antrag {request_id} an: {webhook_url}")
        logging.debug(f"Webhook Daten (Antrag {request_id}): {data}")
        res = requests.post(webhook_url, json=data, headers={"Content-Type": "application/json"}, timeout=20) # Erhöhtes Timeout
        res.raise_for_status() # Fehler bei 4xx/5xx

        logging.info(f"✅ Webhook erfolgreich gesendet für Antrag {request_id}. Status: {res.status_code}")
        # Optional: Status auf 'an_n8n_übergeben' setzen
        # update_request_status(request_id, 'an_n8n_uebergeben')

    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Fehler beim Senden des Webhooks (Request) für Antrag {request_id} an {webhook_url}: {e}")
        update_request_status(request_id, 'fehler_webhook') # Setze Fehlerstatus
    except sqlite3.Error as e:
        logging.error(f"❌ SQLite Fehler beim Abrufen der Daten für Webhook (Antrag {request_id}): {e}")
        # Status nicht ändern, da Problem beim Lesen war
    except Exception as e:
        logging.error(f"❌ Unerwarteter Fehler beim Senden des Webhooks für Antrag {request_id}: {e}", exc_info=True)
        update_request_status(request_id, 'fehler_webhook_unbekannt') # Setze Fehlerstatus
    finally:
        # Stelle sicher, dass die DB-Verbindung geschlossen wird, falls sie noch offen ist
        if conn:
            try: conn.close()
            except: pass

def update_request_status(request_id, new_status):
    """Hilfsfunktion zum Aktualisieren des Status eines Antrags in der DB."""
    conn = None
    try:
        conn = sqlite3.connect("db/onoffboarding.db")
        c = conn.cursor()
        c.execute("UPDATE requests SET status = ? WHERE id = ?", (new_status, request_id))
        conn.commit()
        logging.info(f"DB Status für Antrag {request_id} auf '{new_status}' aktualisiert.")
    except sqlite3.Error as e:
        logging.error(f"❌ Konnte DB Status für Antrag {request_id} nicht auf '{new_status}' setzen: {e}")
    except Exception as e:
         logging.error(f"❌ Unerwarteter Fehler beim Update des DB Status für Antrag {request_id} auf '{new_status}': {e}", exc_info=True)
    finally:
        if conn:
            try: conn.close()
            except: pass


@app.route("/approve/<int:request_id>", methods=["POST"])
def approve_request(request_id):
    # Zugriffsschutz: Nur eingeloggte Admins oder berechtigte User (Vorgesetzte)
    if "user" not in session:
        flash("Zugriff verweigert. Bitte anmelden.", "warning")
        return redirect(url_for("login"))

    # Hier sollte die Berechtigungsprüfung aus /view wiederholt oder übernommen werden.
    # Vereinfacht: Wir nehmen an, der Klick kommt von einem berechtigten User aus /view.
    # TODO: Füge hier eine robuste Berechtigungsprüfung hinzu, falls /approve direkt aufgerufen werden kann.
    current_user = session['user']
    logging.info(f"➡️ Genehmigungsversuch für Antrag ID: {request_id} durch Benutzer '{current_user}'")

    conn = None
    try:
        conn = sqlite3.connect("db/onoffboarding.db")
        c = conn.cursor()
        c.execute("SELECT status FROM requests WHERE id = ?", (request_id,))
        result = c.fetchone()

        if result and result[0] == 'offen':
            # Antrag genehmigen
            c.execute("UPDATE requests SET status = 'genehmigt' WHERE id = ?", (request_id,))
            conn.commit()
            logging.info(f"✅ Antrag {request_id} durch '{current_user}' genehmigt.")
            conn.close() # Schließe DB Verbindung vor dem Webhook-Aufruf
            conn = None

            # Erst nach erfolgreicher DB-Änderung den Webhook auslösen
            trigger_n8n_webhook(request_id)

            flash(f"Antrag {request_id} genehmigt und zur Verarbeitung weitergeleitet.", "success")
        elif result:
            flash(f"Antrag {request_id} konnte nicht genehmigt werden (Status: {result[0]}).", "warning")
            logging.warning(f"Genehmigung für Antrag {request_id} fehlgeschlagen, Status ist bereits '{result[0]}'.")
        else:
            flash(f"Antrag {request_id} nicht gefunden.", "danger")
            logging.warning(f"Genehmigungsversuch für nicht existierenden Antrag ID: {request_id}")

    except sqlite3.Error as e:
        logging.error(f"❌ SQLite Fehler beim Genehmigen von Antrag {request_id}: {e}")
        flash(f"Datenbankfehler beim Genehmigen des Antrags: {e}", "danger")
    except Exception as e:
        logging.error(f"❌ Allgemeiner Fehler beim Genehmigen von Antrag {request_id}: {e}", exc_info=True)
        flash(f"Unerwarteter Fehler beim Genehmigen des Antrags.", "danger")
    finally:
        if conn: # Falls im Fehlerfall noch offen
             try: conn.close()
             except: pass

    # Leite zum Admin-Dashboard zurück
    return redirect(url_for("admin"))


@app.route("/archived")
def archived():
    # Zugriffsschutz
    if "user" not in session or not session.get("is_admin"):
         flash("Zugriff auf das Archiv verweigert.", "warning")
         return redirect(url_for("login"))

    conn = sqlite3.connect("db/onoffboarding.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    # Zeige alle Anträge, die NICHT 'offen' sind
    c.execute("SELECT * FROM requests WHERE status != 'offen' ORDER BY id DESC")
    archived_requests = c.fetchall()
    conn.close()

    # Bereite Daten für die Anzeige vor
    processed_requests = []
    for req in archived_requests:
        req_dict = dict(req)
        req_dict['hardware_accessories'] = req_dict['hardware_accessories'].split(',') if req_dict['hardware_accessories'] else []
        req_dict['hardware_mobile'] = req_dict['hardware_mobile'].split(',') if req_dict['hardware_mobile'] else []
        processed_requests.append(req_dict)

    return render_template("archived.html", requests=processed_requests)


# Initialisiere DB beim Start
def init_db():
    """Erstellt die Datenbankdatei und die Tabelle, falls sie nicht existieren,
       und fügt fehlende Spalten hinzu."""
    db_dir = 'db'
    db_file = os.path.join(db_dir, 'onoffboarding.db')
    conn = None
    try:
        if not os.path.exists(db_dir):
            os.makedirs(db_dir)
            logging.info(f"Verzeichnis '{db_dir}' erstellt.")

        conn = sqlite3.connect(db_file)
        c = conn.cursor()

        # Tabelle erstellen, falls nicht vorhanden
        c.execute('''CREATE TABLE IF NOT EXISTS requests (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        lastname TEXT NOT NULL,
                        firstname TEXT NOT NULL,
                        startdate TEXT,
                        enddate TEXT,
                        department TEXT,
                        supervisor TEXT,
                        hardware_computer TEXT,
                        hardware_monitor TEXT,
                        hardware_accessories TEXT,
                        hardware_mobile TEXT,
                        comments TEXT,
                        referenceuser TEXT,
                        process_type TEXT NOT NULL DEFAULT 'onboarding',
                        status TEXT NOT NULL DEFAULT 'offen',
                        role TEXT,
                        department_dn TEXT,
                        key_required BOOLEAN DEFAULT 0,
                        required_windows BOOLEAN DEFAULT 0,
                        hardware_required BOOLEAN DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )''')
        logging.info("Tabelle 'requests' geprüft/erstellt.")

        # Prüfen und Hinzufügen fehlender Spalten (robustere Methode)
        c.execute("PRAGMA table_info(requests)")
        columns = [info[1] for info in c.fetchall()]
        required_columns = {
             'key_required': 'BOOLEAN DEFAULT 0',
             'required_windows': 'BOOLEAN DEFAULT 0',
             'hardware_required': 'BOOLEAN DEFAULT 0',
             'department_dn': 'TEXT',
             'created_at': 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'
        }
        for col, col_type in required_columns.items():
            if col not in columns:
                try:
                    c.execute(f"ALTER TABLE requests ADD COLUMN {col} {col_type}")
                    logging.info(f"Spalte '{col}' zur Tabelle 'requests' hinzugefügt.")
                except sqlite3.OperationalError as e:
                     # Kann passieren, wenn Spalte doch schon existiert (Race Condition, etc.)
                     logging.warning(f"Konnte Spalte '{col}' nicht hinzufügen (möglicherweise schon vorhanden): {e}")


        conn.commit()
        logging.info("✅ Datenbank erfolgreich initialisiert/geprüft.")

    except sqlite3.Error as e:
        logging.error(f"❌ SQLite Fehler bei der Datenbankinitialisierung ({db_file}): {e}")
        sys.exit(f"Kritischer DB Fehler: {e}") # Beenden, wenn DB nicht initialisiert werden kann
    except Exception as e:
        logging.error(f"❌ Allgemeiner Fehler bei der Datenbankinitialisierung: {e}", exc_info=True)
        sys.exit(f"Kritischer Initialisierungsfehler: {e}")
    finally:
        if conn:
            try: conn.close()
            except: pass


if __name__ == '__main__':
    init_db() # Datenbank beim Start initialisieren/prüfen
    # Debug-Modus aus Umgebungsvariable oder Default False
    flask_debug = os.getenv("FLASK_DEBUG", "False").lower() in ("true", "1", "t")
    app.run(host="0.0.0.0", port=5000, debug=flask_debug)
