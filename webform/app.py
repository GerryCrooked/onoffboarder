import smtplib
import os
import sqlite3
import requests
import pdf_generator
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, send_from_directory, send_file, current_app
from dotenv import load_dotenv, find_dotenv
from werkzeug.utils import secure_filename

from ldap3 import Server, Connection, ALL, SUBTREE, BASE, LEVEL
from ldap3.core.exceptions import LDAPException, LDAPBindError, LDAPSocketOpenError

from datetime import datetime, date
from email.mime.text import MIMEText
import logging
import sys
import re
from functools import wraps

from markupsafe import Markup, escape

load_dotenv(find_dotenv())

logging.basicConfig(
    level=os.getenv("FLASK_LOG_LEVEL", "INFO").upper(),
    format='%(asctime)s - %(name)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

import ad_utils

# Schlüssel für Umgebungsvariablen der Gruppen
ENV_MAIN_ACCESS_GROUP = "MAIN_ACCESS_GROUP"
ENV_ADMIN_MAIN_GROUP = "AD_GROUP"
ENV_SUPERVISOR_GROUP = "SUPERVISOR_GROUP"
ENV_HR_GROUP = "HR_GROUP"
ENV_BAUAMT_GROUP = "BAUAMT_GROUP"
ENV_IT_GROUP = "IT_GROUP"
ENV_OFFICE_GROUP = "OFFICE_GROUP"
ENV_PRINT_GROUP = "PRINT_GROUP"
ENV_RIS_GROUP = "RIS_RIS_GROUP" # Korrigiert, falls es ein Tippfehler war, sonst anpassen

# --- BEGINN: Hilfsfunktionen & Decorator ---
def nl2br_filter(s):
    if not s: return ""
    return Markup(re.sub(r'\r\n|\r|\n', '<br>\n', str(escape(s))))

def format_datetime_filter(value, format_str='%d.%m.%Y %H:%M'):
    if value is None or value == "": return ""
    try:
        if isinstance(value, str):
            try: return datetime.strptime(value.split('.')[0], '%Y-%m-%d %H:%M:%S').strftime(format_str)
            except ValueError:
                try: return datetime.strptime(value, '%Y-%m-%dT%H:%M').strftime(format_str)
                except ValueError: return datetime.strptime(value, '%Y-%m-%d').strftime('%d.%m.%Y')
        elif isinstance(value, datetime): return value.strftime(format_str)
        elif isinstance(value, date): return value.strftime('%d.%m.%Y')
    except ValueError: return value
    return value

def get_hardware_details_for_display(request_item_dict):
    details, item = [], dict(request_item_dict) if isinstance(request_item_dict, sqlite3.Row) else request_item_dict
    hw_computer = item.get('hardware_computer', '')
    # hardware_accessories und hardware_mobile sollten bereits Listen sein durch get_request_item_as_dict
    accessories = item.get('hardware_accessories', [])
    mobiles = item.get('hardware_mobile', [])
    processed_accessories = set()

    if hw_computer and hw_computer.lower() not in ['computer bereits vorhanden', '']:
        details.append(f"Computer: {hw_computer}")
        if hw_computer.lower() == 'laptop' and accessories and 'Dockingstation' in accessories:
            other_laptop_acc = [acc for acc in accessories if acc and acc.lower() != 'dockingstation']
            acc_text = "Dockingstation" + (", " + ', '.join(other_laptop_acc) if other_laptop_acc else "")
            details.append(f"Zubehör (Laptop): {acc_text}")
            processed_accessories.add('Dockingstation')
            if other_laptop_acc: processed_accessories.update(filter(None, other_laptop_acc))

    hw_monitor = item.get('hardware_monitor', '')
    if hw_monitor and hw_monitor.lower() not in ['monitor(e) bereits vorhanden', '']:
        details.append(f"Monitor: {hw_monitor}")

    if accessories:
        remaining_acc = [acc for acc in accessories if acc and acc not in processed_accessories]
        if remaining_acc: details.append(f"Weiteres Zubehör: {', '.join(remaining_acc)}")

    if mobiles:
        filtered_mobiles = [m for m in mobiles if m]
        if filtered_mobiles: details.append(f"Mobiles Gerät: {', '.join(filtered_mobiles)}")

    if item.get('needs_fixed_phone'): details.append("Festarbeitsplatztelefon: Ja")

    return "- " + "\n- ".join(details) if details else "Keine spezifische neue Hardware/Telefon angefordert oder alles als 'bereits vorhanden' markiert."

def is_valid_email(email):
    if not isinstance(email, str): return False
    return EMAIL_REGEX.match(email) is not None

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def load_form_dependencies():
    ou_tree_data, all_supervisors_data = [], []
    try:
        # Stellt sicher, dass AD_SEARCH_BASE definiert ist, bevor es verwendet wird
        if AD_SEARCH_BASE:
            ou_tree_data = ad_utils.build_ou_tree(AD_SEARCH_BASE)
            all_supervisors_data = ad_utils.get_all_supervisors()
        else:
            logger.warning("AD_SEARCH_BASE nicht definiert. OU-Baum und Supervisoren können nicht geladen werden.")
    except Exception as e:
        logger.error(f"Fehler beim Laden der Formularabhängigkeiten: {e}", exc_info=True)
        flash("Fehler beim Laden der Abteilungs- oder Vorgesetztendaten.", "danger")
    return ou_tree_data, all_supervisors_data

def get_request_item_as_dict(request_id):
    conn_db = None
    try:
        conn_db = sqlite3.connect("db/onoffboarding.db"); conn_db.row_factory = sqlite3.Row; c = conn_db.cursor()
        c.execute("SELECT * FROM requests WHERE id = ?", (request_id,)); row = c.fetchone()
        if row:
            item = dict(row)
            item['hardware_accessories'] = [a.strip() for a in item.get('hardware_accessories','').split(',')] if item.get('hardware_accessories') else []
            item['hardware_mobile'] = [m.strip() for m in item.get('hardware_mobile','').split(',')] if item.get('hardware_mobile') else []
            return item
    except Exception as e: logger.error(f"Fehler get_request_item_as_dict für ID {request_id}: {e}", exc_info=True)
    finally:
        if conn_db: conn_db.close()
    return None

def send_mail(to_address, subject, body_content, from_address=None, is_html=False):
    actual_sender = from_address or SENDER
    if not actual_sender or not is_valid_email(actual_sender):
        logger.error(f"❌ Ungültige oder fehlende Absenderadresse: {actual_sender}")
        raise ValueError("Ungültige oder fehlende Absenderadresse (SENDER) konfiguriert.")

    final_recipient = None
    if to_address and is_valid_email(to_address): final_recipient = to_address
    elif MAIL_USER and is_valid_email(MAIL_USER):
        final_recipient = MAIL_USER
        subject = f"[Fallback an {MAIL_USER}] {subject}"
        logger.warning(f"Ungültige primäre E-Mail-Adresse '{to_address}'. Sende an Fallback MAIL_USER: {MAIL_USER}")
    else:
        logger.error(f"❌ Weder primäre Adresse '{to_address}' noch Fallback MAIL_USER ('{MAIL_USER}') gültig/konfiguriert für Betreff: {subject}")
        raise ValueError("Keine gültige Empfängeradresse für E-Mail gefunden.")

    msg = MIMEText(body_content, 'html' if is_html else 'plain', 'utf-8')
    msg['Subject'] = subject; msg['From'] = actual_sender; msg['To'] = final_recipient
    try:
        logger.info(f"📧 Sende E-Mail '{subject}' von '{actual_sender}' an '{final_recipient}'. HTML: {is_html}")
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as smtp:
            smtp.set_debuglevel(0)
            smtp.sendmail(actual_sender, [final_recipient], msg.as_string())
        logger.info(f"✅ E-Mail '{subject}' an {final_recipient} gesendet.")
    except smtplib.SMTPException as e:
        logger.error(f"❌ SMTP Fehler Senden an {final_recipient} für Betreff '{subject}': {e}", exc_info=True); raise
    except Exception as e:
        logger.error(f"❌ Unerwarteter Fehler Senden an {final_recipient} für Betreff '{subject}': {e}", exc_info=True); raise

def send_approval_mail(to_address_supervisor, firstname, lastname, process_type, request_id):
    subject = f"Genehmigung erforderlich: {process_type.capitalize()} für {lastname}, {firstname} (ID: {request_id})"
    base = WEB_SERVER if WEB_SERVER and WEB_SERVER.startswith(('http://', 'https://')) else f"http://{WEB_SERVER or 'localhost:5000'}"
    link = f"{base}/view/{request_id}"
    body_content = f"""Sehr geehrte/r Vorgesetzte/r,
ein neuer Antrag ({process_type}) für '{firstname} {lastname}' erfordert Ihre Genehmigung.
Details und Genehmigungsoptionen finden Sie unter folgendem Link:
{link}
(Antrags-ID: {request_id})
Mit freundlichen Grüßen,
Ihr On-/Offboarding-System"""
    send_mail(to_address_supervisor, subject, body_content)

def update_request_status(request_id, new_status, expected_current_status=None):
    conn_db = None; success = False
    try:
        conn_db = sqlite3.connect("db/onoffboarding.db"); c = conn_db.cursor()
        if expected_current_status:
            c.execute("SELECT status FROM requests WHERE id = ?", (request_id,)); current_status_row = c.fetchone()
            if not current_status_row:
                logger.error(f"Antrag {request_id} nicht gefunden für Status-Update.")
                flash(f"Antrag {request_id} nicht gefunden.", "danger")
                return False
            if current_status_row[0] != expected_current_status:
                logger.warning(f"Status für Antrag {request_id} ist '{current_status_row[0]}', erwartet '{expected_current_status}'. Update zu '{new_status}' nicht durchgeführt.")
                if new_status != current_status_row[0]: # Nur flashen, wenn es ein echter Konflikt ist
                    flash(f"Aktion für Antrag {request_id} nicht möglich (Aktueller Status: {current_status_row[0]}, Erwartet: {expected_current_status}).", "warning")
                return False
        c.execute("UPDATE requests SET status = ? WHERE id = ?", (new_status, request_id))
        if c.rowcount == 0 and not (expected_current_status and new_status == expected_current_status): # Kein Update, wenn Status schon der neue Status ist
            logger.warning(f"Kein Datensatz mit ID {request_id} für Status-Update auf '{new_status}' gefunden/geändert oder Status war bereits '{new_status}'.")
            if not (expected_current_status and new_status == expected_current_status): # Flash nur, wenn es nicht nur ein "Status ist schon richtig" Fall ist
                 flash(f"Antrag {request_id} nicht gefunden oder Status bereits '{new_status}'.", "warning")
            return False
        conn_db.commit(); logger.info(f"DB Status Antrag {request_id} auf '{new_status}' aktualisiert."); success = True
    except sqlite3.Error as e:
        logger.error(f"❌ Konnte DB Status Antrag {request_id} nicht auf '{new_status}' setzen: {e}", exc_info=True)
        flash("Datenbankfehler beim Aktualisieren des Antragsstatus.", "danger")
    except Exception as e:
        logger.error(f"❌ Unerwarteter Fehler Update DB Status Antrag {request_id} auf '{new_status}': {e}", exc_info=True)
        flash("Allgemeiner Fehler beim Aktualisieren des Antragsstatus.", "danger")
    finally:
        if conn_db: conn_db.close()
    return success

def trigger_n8n_webhook(request_id):
    logger.info(f"🚀 Trigger n8n Webhook für Antrag ID: {request_id}")
    webhook_url = os.getenv("N8N_WEBHOOK_APPROVED")
    if not webhook_url:
        logger.error(f"❌ N8N_WEBHOOK_APPROVED nicht konfiguriert. Webhook Antrag {request_id} nicht gesendet.")
        update_request_status(request_id, 'fehler_webhook_url') # Stiller Fehlerstatus
        return

    row_dict = get_request_item_as_dict(request_id)
    if not row_dict:
        logger.error(f"❌ Konnte Daten für Antrag {request_id} nicht aus DB laden für Webhook.")
        return

    referenceuser_dn = ""; ref_user_sam = row_dict.get("referenceuser")
    if ref_user_sam:
        ad_conn = None
        try:
            logger.info(f" LDAP Suche DN für Referenzuser '{ref_user_sam}' (Antrag {request_id})")
            ad_conn = ad_utils.get_ad_connection()
            user_details = ad_utils.get_user_details_by_samaccountname(ad_conn, ref_user_sam, attributes=['distinguishedName'])
            if user_details and user_details.get('dn'):
                referenceuser_dn = user_details['dn']
                logger.info(f"✅ DN für Ref-User '{ref_user_sam}': {referenceuser_dn}")
            else:
                logger.warning(f"⚠️ Kein DN für Ref-User '{ref_user_sam}' (Antrag {request_id}) gefunden via ad_utils.")
        except Exception as e:
            logger.error(f"❌ Fehler Ref-User DN Lookup '{ref_user_sam}': {e}", exc_info=True)
        finally:
            if ad_conn and ad_conn.bound: ad_conn.unbind()

    data_to_send = row_dict.copy()
    for key in ['key_required', 'required_windows', 'hardware_required', 'email_account_required',
                'needs_fixed_phone', 'needs_ris_access', 'needs_cipkom_access', 'needs_office_notification',
                'workplace_needs_new_table', 'workplace_needs_new_chair', 'workplace_needs_monitor_arms',
                'workplace_no_new_equipment']:
        data_to_send[key] = bool(data_to_send.get(key))
    data_to_send['referenceuser_dn'] = referenceuser_dn

    try:
        logger.info(f"➡️ Sende Webhook Antrag {request_id} an: {webhook_url} mit Status '{data_to_send['status']}'")
        res = requests.post(webhook_url, json=data_to_send, headers={"Content-Type": "application/json"}, timeout=20)
        res.raise_for_status()
        logger.info(f"✅ Webhook erfolgreich Antrag {request_id}. Status: {res.status_code}")
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Fehler Senden Webhook (Request) Antrag {request_id} an {webhook_url}: {e}", exc_info=True)
        update_request_status(request_id, 'fehler_webhook')
    except Exception as e:
        logger.error(f"❌ Unerwarteter Fehler Senden Webhook Antrag {request_id}: {e}", exc_info=True)
        update_request_status(request_id, 'fehler_webhook_unbekannt')

def check_all_subprocesses_done(request_item):
    if not request_item: return False, ["Antrag nicht gefunden"]
    pending_tasks, all_done = [], True
    def add_pending(condition, task_name, log_msg):
        nonlocal all_done
        if condition:
            pending_tasks.append(task_name); all_done = False; logger.debug(f"Pending (Antrag {request_item.get('id')}): {log_msg}")

    add_pending(request_item.get('required_windows') and request_item.get('n8n_ad_creation_status') != 'success', "Windows Account Erstellung", "Windows Account")
    add_pending(request_item.get('email_account_required') and not request_item.get('email_created_address'), "E-Mail Adresse Erfassung", "E-Mail Adresse")
    add_pending(request_item.get('hardware_required') and not request_item.get('hw_status_setup_done_at'), "Hardware Setup (vollständig)", "Hardware Setup")
    add_pending(request_item.get('needs_fixed_phone') and not request_item.get('phone_status_setup_at'), "Telefon Einrichtung", "Telefon Einrichtung")
    # Schlüsselvorbereitung bleibt im Bauamt, Schlüsselausgabe geht zu HR
    add_pending(request_item.get('key_required') and not request_item.get('key_status_prepared_at'), "Bauamt: Schlüssel vorbereitet", "Schlüssel vorbereitet") # Bauamt
    add_pending(request_item.get('key_required') and not request_item.get('key_status_issued_at'), "HR: Schlüsselausgabe", "Schlüsselausgabe") # HR
    add_pending(request_item.get('needs_ris_access') and not request_item.get('ris_access_status_granted_at'), "RIS Zugang", "RIS Zugang")
    add_pending(request_item.get('needs_cipkom_access') and not request_item.get('cipkom_access_status_granted_at'), "CIPKOM Zugang", "CIPKOM Zugang")

    if not request_item.get('workplace_no_new_equipment'):
        add_pending(request_item.get('workplace_needs_new_table') and not request_item.get('workplace_table_setup_at'), "Bauamt: Neuer Tisch Aufbau", "Bauamt Tisch")
        add_pending(request_item.get('workplace_needs_new_chair') and not request_item.get('workplace_chair_setup_at'), "Bauamt: Neuer Stuhl Aufbau", "Bauamt Stuhl")
        add_pending(request_item.get('workplace_needs_monitor_arms') and not request_item.get('workplace_monitor_arms_setup_at'), "Bauamt: Monitorarme Montage", "Bauamt Monitorarme")

    if request_item.get('process_type') == 'onboarding':
        hr_tasks = [
            (not request_item.get('hr_dienstvereinbarung_at'), "HR Dienstvereinbarung", "HR Dienstvereinbarung"),
            (not request_item.get('hr_datenschutz_at'), "HR Datenschutzblatt", "HR Datenschutz"),
            (not request_item.get('hr_dsgvo_informed_at'), "HR: DSGVO informiert", "HR DSGVO"),
            (not request_item.get('hr_it_directive_at'), "HR: Dienstanweisung IT", "HR IT Dienstanweisung"),
            (not request_item.get('hr_payroll_sheet_at'), "HR: Personaldatenblatt Abrechnung", "HR Personaldatenblatt"),
            (not request_item.get('hr_security_guidelines_at'), "HR: Leitlinien Informationssicherheit", "HR Leitlinien InfoSi"),
            (not request_item.get('aida_access_created_at'), "AIDA: Zugang erstellt", "AIDA Zugang"),
            (not request_item.get('aida_key_registered_at'), "AIDA: Schlüssel aufgenommen", "AIDA Schlüssel")]
        for condition, task_name, log_msg in hr_tasks: add_pending(condition, task_name, log_msg)

    if request_item.get('needs_office_notification'):
        office_tasks = [
            (not request_item.get('office_outlook_contact_at'), "Vorzimmer: Outlook-Kontakt", "VZ Outlook"),
            (not request_item.get('office_distribution_lists_at'), "Vorzimmer: Verteilerlisten", "VZ Verteiler"),
            (not request_item.get('office_phone_list_at'), "Vorzimmer: Telefonliste", "VZ Telefonliste"),
            (not request_item.get('office_birthday_calendar_at'), "Vorzimmer: Geburtstagskalender", "VZ Geburtstag"),
            (not request_item.get('office_welcome_gift_at'), "Vorzimmer: Begrüßungsgeschenk", "VZ Geschenk"),
            (not request_item.get('office_mayor_appt_confirmed_at'), "Vorzimmer: Termin Bürgermeister", "VZ Termin BM"),
            (not request_item.get('office_business_cards_at'), "Vorzimmer: Visitenkarten", "VZ Visitenkarten"),
            (not request_item.get('office_organigram_at'), "Vorzimmer: Organigramm", "VZ Organigramm"),
            (not request_item.get('office_homepage_updated_at'), "Vorzimmer: Homepage Update", "VZ Homepage")]
        for condition, task_name, log_msg in office_tasks: add_pending(condition, task_name, log_msg)

    logger.info(f"Abschlussprüfung für Antrag {request_item.get('id')}: All done = {all_done}, Pending = {pending_tasks if pending_tasks else 'Keine'}")
    return all_done, pending_tasks

def require_ad_group(group_name_env_var_key_or_keys):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if "user" not in session:
                flash("Bitte zuerst anmelden, um auf diese Seite zuzugreifen.", "warning")
                return redirect(url_for("login", next=request.url))
            current_user_sam = session["user"]
            allowed = False
            group_keys_to_check = group_name_env_var_key_or_keys if isinstance(group_name_env_var_key_or_keys, list) else [group_name_env_var_key_or_keys]
            checked_group_cns_for_flash = []

            for group_key in group_keys_to_check:
                required_group_cn = os.getenv(group_key)
                if not required_group_cn:
                    logger.error(f"Env-Var '{group_key}' für Gruppe nicht gesetzt. Zugriff für User '{current_user_sam}' auf '{f.__name__}' verweigert.")
                    continue
                checked_group_cns_for_flash.append(required_group_cn)
                try:
                    if ad_utils.is_user_member_of_group_by_env_var(current_user_sam, group_key): # env_var_key übergeben
                        allowed = True; logger.debug(f"User '{current_user_sam}' in Gruppe '{required_group_cn}' (via ENV {group_key}) für '{f.__name__}'."); break
                except ValueError as ve: logger.error(f"Konfig-Fehler Gruppenprüfung '{required_group_cn}': {ve}")
                except Exception as e_gc: logger.error(f"Fehler Gruppenprüfung '{required_group_cn}': {e_gc}", exc_info=True)

            if not allowed:
                groups_str = ", ".join(checked_group_cns_for_flash) or "erforderliche Gruppe(n)"
                if not checked_group_cns_for_flash and any(not os.getenv(gk) for gk in group_keys_to_check):
                    flash("Zugriff verweigert: Sicherheitskonfiguration unvollständig.", "danger")
                else:
                    flash(f"Zugriff verweigert. Erfordert Mitgliedschaft in: {groups_str}.", "danger")
                logger.warning(f"User '{current_user_sam}' Zugriff auf '{f.__name__}' verweigert. Benötigt: {groups_str} (Keys: {group_keys_to_check}).")
                return redirect(url_for("admin"))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "secret123")
app.jinja_env.globals['hasattr'] = hasattr # hasattr global verfügbar machen
app.jinja_env.filters['nl2br'] = nl2br_filter
app.jinja_env.filters['format_datetime'] = format_datetime_filter
app.jinja_env.globals['datetime'] = datetime
app.jinja_env.globals['date'] = date
app.jinja_env.globals['get_hardware_details_for_display'] = get_hardware_details_for_display

# Kontextprozessor für Umgebungsvariablen-Schlüssel in Templates
@app.context_processor
def utility_processor():
    return dict(
        is_user_in_group_for_template=lambda gk: ad_utils.is_user_member_of_group_by_env_var(session.get("user"), gk) if "user" in session else False,
        ENV_MAIN_ACCESS_GROUP=ENV_MAIN_ACCESS_GROUP, ENV_ADMIN_MAIN_GROUP=ENV_ADMIN_MAIN_GROUP,
        ENV_SUPERVISOR_GROUP=ENV_SUPERVISOR_GROUP, ENV_HR_GROUP=ENV_HR_GROUP,
        ENV_BAUAMT_GROUP=ENV_BAUAMT_GROUP, ENV_IT_GROUP=ENV_IT_GROUP,
        ENV_OFFICE_GROUP=ENV_OFFICE_GROUP, ENV_PRINT_GROUP=ENV_PRINT_GROUP, ENV_RIS_GROUP=ENV_RIS_GROUP
    )

UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'webform/uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 # 16 MB
ALLOWED_EXTENSIONS = {'pdf'}
app_root_path_for_upload = os.path.dirname(os.path.abspath(__file__))
concrete_upload_folder = os.path.join(app_root_path_for_upload, app.config['UPLOAD_FOLDER'])
if not os.path.exists(concrete_upload_folder):
    try: os.makedirs(concrete_upload_folder); logger.info(f"Upload-Ordner erstellt: {concrete_upload_folder}")
    except OSError as e: logger.error(f"Fehler Erstellen Upload-Ordner {concrete_upload_folder}: {e}")

AD_SERVER = os.getenv("AD_SERVER")
AD_SEARCH_BASE = os.getenv("AD_SEARCH_BASE")
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", "25"))
SENDER = os.getenv("SENDER")
WEB_SERVER = os.getenv("LOCALWEBSERVER")
N8N_WEBHOOK_APPROVED = os.getenv("N8N_WEBHOOK_APPROVED")
N8N_WEBHOOK_CREATE_USER_ACTION = os.getenv("N8N_WEBHOOK_CREATE_USER_ACTION")
N8N_FLASK_TOKEN = os.getenv("N8N_FLASK_TOKEN")
MAIL_USER = os.getenv("MAIL_USER")
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

# Routen Definitionen
# (Hier folgen alle Ihre Routen, wie in Antwort #47 definiert, mit den @require_ad_group Decorators)
# Die Implementierung der Routen selbst bleibt großteils gleich, nur die @require_ad_group(...) Zeilen werden angepasst/hinzugefügt.

@app.route("/dashboard") # NEUE Route für nicht-Admins
@require_ad_group(ENV_MAIN_ACCESS_GROUP)
def user_dashboard():
    conn_db = None
    current_requests_raw = []
    current_user_sam = session.get("user")

    try:
        conn_db = sqlite3.connect("db/onoffboarding.db")
        conn_db.row_factory = sqlite3.Row
        c = conn_db.cursor()
        # Nur Anträge im Status 'offen' oder 'in_bearbeitung' anzeigen
        try:
            c.execute("SELECT * FROM requests WHERE status NOT IN ('abgeschlossen', 'abgelehnt') ORDER BY created_at DESC, id DESC")
        except sqlite3.OperationalError:
            logger.warning("Spalte 'created_at' nicht in DB gefunden für Sortierung in /dashboard, sortiere nach ID.")
            c.execute("SELECT * FROM requests WHERE status NOT IN ('abgeschlossen', 'abgelehnt') ORDER BY id DESC")
        current_requests_raw = c.fetchall()
    except sqlite3.Error as e:
        logger.error(f"SQLite Fehler beim Laden aktueller Anträge für Dashboard: {e}", exc_info=True)
        flash("Fehler beim Laden der Anträge.", "danger")
    finally:
        if conn_db: conn_db.close()

    requests_for_dashboard = []
    for req_raw in current_requests_raw:
        req_dict = dict(req_raw)
        
        # NEU: Prüfen, ob alle Teilaufgaben für diesen Antrag abgeschlossen sind
        all_subprocesses_completed, _ = check_all_subprocesses_done(req_dict)
        if all_subprocesses_completed:
            logger.debug(f"Antrag {req_dict['id']} ist vollständig abgeschlossen, wird nicht auf Dashboard angezeigt.")
            continue # Diesen Antrag überspringen, da er fertig ist

        action_links = []

        # Prüfen, welche Links der Benutzer sehen darf, basierend auf Gruppe UND Antrags-Flags
        if ad_utils.is_user_member_of_group_by_env_var(current_user_sam, ENV_HR_GROUP):
            # HR-Gruppe: Link anzeigen, wenn HR-Aufgaben relevant sind ODER Schlüssel relevant ist
            # check_all_subprocesses_done gibt uns bereits die pending_tasks, wir können diese hier nutzen
            # oder spezifisch die Flags prüfen
            if req_dict.get('process_type') == 'onboarding' or (req_dict.get('key_required') and not req_dict.get('key_status_issued_at')):
                 action_links.append({'label': 'HR & AIDA bearbeiten', 'url': url_for('hr_update_status', request_id=req_dict['id'])})

        if ad_utils.is_user_member_of_group_by_env_var(current_user_sam, ENV_BAUAMT_GROUP):
            # Bauamt-Gruppe: Link anzeigen, wenn Schlüsselvorbereitung oder Arbeitsplatzausstattung relevant ist
            if (req_dict.get('key_required') and not req_dict.get('key_status_prepared_at')) or \
               (req_dict.get('workplace_needs_new_table') and not req_dict.get('workplace_table_setup_at')) or \
               (req_dict.get('workplace_needs_new_chair') and not req_dict.get('workplace_chair_setup_at')) or \
               (req_dict.get('workplace_needs_monitor_arms') and not req_dict.get('workplace_monitor_arms_setup_at')) or \
               (not req_dict.get('workplace_no_new_equipment') and not req_dict.get('workplace_needs_new_table') and \
                not req_dict.get('workplace_needs_new_chair') and not req_dict.get('workplace_needs_monitor_arms')):
                action_links.append({'label': 'Bauamt bearbeiten', 'url': url_for('update_bauamt_status', request_id=req_dict['id'])})

        if ad_utils.is_user_member_of_group_by_env_var(current_user_sam, ENV_IT_GROUP):
            # IT-Gruppe: Links nur anzeigen, wenn die spezifische IT-Aufgabe im Antrag angefordert wurde
            if req_dict.get('hardware_required') or req_dict.get('needs_fixed_phone'):
                if not (req_dict.get('hw_status_setup_done_at') and req_dict.get('phone_status_setup_at')): # Nur anzeigen, wenn Hardware/Telefon nicht komplett fertig
                    action_links.append({'label': 'IT Hardware/Telefon bearbeiten', 'url': url_for('update_hardware_status', request_id=req_dict['id'])})
            
            if req_dict.get('email_account_required') and not req_dict.get('email_created_address'):
                action_links.append({'label': 'IT E-Mail bearbeiten', 'url': url_for('update_email_status', request_id=req_dict['id'])})
            
            if (req_dict.get('needs_ris_access') and not req_dict.get('ris_access_status_granted_at')) or \
               (req_dict.get('needs_cipkom_access') and not req_dict.get('cipkom_access_status_granted_at')) or \
               (req_dict.get('other_software_notes')): # Link anzeigen, wenn RIS/CIPKOM offen oder sonstige Software da ist
                action_links.append({'label': 'IT Software bearbeiten', 'url': url_for('update_software_status', request_id=req_dict['id'])})

        if ad_utils.is_user_member_of_group_by_env_var(current_user_sam, ENV_OFFICE_GROUP):
            # Vorzimmer-Gruppe: Link nur anzeigen, wenn 'needs_office_notification' True ist UND noch offene Aufgaben existieren
            if req_dict.get('needs_office_notification'):
                # Überprüfen, ob noch offene Vorzimmer-Aufgaben existieren
                office_tasks_pending = False
                if not req_dict.get('office_outlook_contact_at') or \
                   not req_dict.get('office_distribution_lists_at') or \
                   not req_dict.get('office_phone_list_at') or \
                   not req_dict.get('office_birthday_calendar_at') or \
                   not req_dict.get('office_welcome_gift_at') or \
                   not req_dict.get('office_mayor_appt_confirmed_at') or \
                   not req_dict.get('office_business_cards_at') or \
                   not req_dict.get('office_organigram_at') or \
                   not req_dict.get('office_homepage_updated_at'):
                   office_tasks_pending = True
                
                if office_tasks_pending:
                    action_links.append({'label': 'Vorzimmer bearbeiten', 'url': url_for('update_office_status', request_id=req_dict['id'])})


        # Nur Anträge hinzufügen, für die der Benutzer mindestens einen Bearbeitungslink hat
        if action_links:
            req_dict['action_links'] = action_links
            requests_for_dashboard.append(req_dict)

    return render_template("user_dashboard.html", username=session.get("user"), requests=requests_for_dashboard)

@app.route("/", methods=["GET", "POST"])
@require_ad_group(ENV_ADMIN_MAIN_GROUP)
def form():
    today = datetime.today().strftime('%Y-%m-%d')
    form_data_on_error = {}
    if request.method == "POST":
        form_data_on_error = request.form.copy()
        lastname = request.form.get("lastname")
        firstname = request.form.get("firstname")
        birthdate = request.form.get("birthdate")
        job_title = request.form.get("job_title", "")
        startdate = request.form.get("startdate") or today
        enddate = request.form.get("enddate")
        department = request.form.get("department")
        department_dn = request.form.get("department_dn")
        supervisor_sam = request.form.get("supervisor", "")
        supervisor_email_for_mail = supervisor_sam
        if supervisor_sam:
            sup_ad_conn = None
            try:
                sup_ad_conn = ad_utils.get_ad_connection()
                sup_details = ad_utils.get_user_details_by_samaccountname(sup_ad_conn, supervisor_sam, attributes=['mail'])
                if sup_details and sup_details.get('mail'):
                    supervisor_email_for_mail = sup_details.get('mail')
                    logger.info(f"E-Mail für Supervisor '{supervisor_sam}' gefunden: {supervisor_email_for_mail}")
                else:
                    logger.warning(f"Keine E-Mail für Supervisor '{supervisor_sam}' im AD gefunden. Verwende '{supervisor_sam}' oder Fallback ({MAIL_USER}).")
            except AttributeError as ae:
                 logger.error(f"Funktion get_user_details_by_samaccountname nicht in ad_utils gefunden oder fehlerhaft: {ae}")
            except Exception as e_sup_mail: logger.error(f"Fehler Supervisor E-Mail für '{supervisor_sam}': {e_sup_mail}")
            finally:
                if sup_ad_conn and sup_ad_conn.bound: sup_ad_conn.unbind()
        comments = request.form.get("comments", ""); hardware_computer = request.form.get("hardware_computer", ""); hardware_monitor = request.form.get("hardware_monitor", "")
        hardware_accessories = request.form.getlist("hardware_accessories[]"); hardware_mobile = request.form.getlist("hardware_mobile[]")
        referenceuser = request.form.get("referenceuser", ""); process_type = request.form.get("process_type", "onboarding"); status = "offen"; role = "user"
        key_required = request.form.get("key_required") == "true"; required_windows = request.form.get("needs_windows_user") == "true"
        hardware_required = request.form.get("needs_hardware") == "true"; email_account_required = request.form.get("needs_email_account") == "true"
        needs_fixed_phone = request.form.get("needs_fixed_phone") == "true"; needs_ris_access = request.form.get("needs_ris_access") == "true"
        needs_cipkom_access = request.form.get("needs_cipkom_access") == "true"; other_software_notes = request.form.get("other_software_notes", "")
        cipkom_reference_user = request.form.get("cipkom_reference_user", ""); room_number = request.form.get("room_number", "")
        workplace_needs_new_table = request.form.get("workplace_needs_new_table") == "true"; workplace_needs_new_chair = request.form.get("workplace_needs_new_chair") == "true"
        workplace_needs_monitor_arms = request.form.get("workplace_needs_monitor_arms") == "true"; workplace_no_new_equipment = request.form.get("workplace_no_new_equipment") == "true"
        needs_office_notification = request.form.get("needs_office_notification") == "true"
        if not firstname or not lastname:
            flash("Vor- und Nachname sind Pflichtfelder.", "danger"); o,a = load_form_dependencies(); return render_template("form.html", today=today, ou_tree=o, all_supervisors=a, form_data=form_data_on_error)
        if required_windows and not supervisor_sam:
            flash("Vorgesetzter ist Pflichtfeld bei Windows Konto.", "danger"); o,a = load_form_dependencies(); return render_template("form.html", today=today, ou_tree=o, all_supervisors=a, form_data=form_data_on_error)
        if required_windows and not department_dn:
            flash("Abteilung ist Pflichtfeld bei Windows Konto.", "danger"); o,a = load_form_dependencies(); return render_template("form.html", today=today, ou_tree=o, all_supervisors=a, form_data=form_data_on_error)
        conn_db = None; request_id = None
        try:
            conn_db = sqlite3.connect("db/onoffboarding.db"); c = conn_db.cursor()
            sql_insert = '''INSERT INTO requests (lastname, firstname, birthdate, job_title, startdate, enddate, department, supervisor, hardware_computer, hardware_monitor, hardware_accessories, hardware_mobile, comments, referenceuser, process_type, status, role, department_dn, key_required, required_windows, hardware_required, email_account_required, needs_fixed_phone, needs_ris_access, needs_cipkom_access, other_software_notes, cipkom_reference_user, room_number, workplace_needs_new_table, workplace_needs_new_chair, workplace_needs_monitor_arms, workplace_no_new_equipment, needs_office_notification) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'''
            values = (lastname, firstname, birthdate, job_title, startdate, enddate, department, supervisor_sam, hardware_computer if hardware_required else "", hardware_monitor if hardware_required else "", ",".join(hardware_accessories) if hardware_accessories and hardware_required else "", ",".join(hardware_mobile) if hardware_mobile else "", comments, referenceuser, process_type, status, role, department_dn, key_required, required_windows, hardware_required, email_account_required, needs_fixed_phone, needs_ris_access, needs_cipkom_access, other_software_notes, cipkom_reference_user, room_number, workplace_needs_new_table, workplace_needs_new_chair, workplace_needs_monitor_arms, workplace_no_new_equipment, needs_office_notification)
            c.execute(sql_insert, values); conn_db.commit(); request_id = c.lastrowid
            logger.info(f"Anfrage ID: {request_id} gespeichert. Status: {status}")
        except sqlite3.Error as e:
            logger.error(f"SQLite Fehler beim Speichern: {e}", exc_info=True); flash("DB-Fehler.", "danger"); o,a = load_form_dependencies(); return render_template("form.html", today=today, ou_tree=o, all_supervisors=a, form_data=form_data_on_error)
        finally:
            if conn_db: conn_db.close()
        if request_id:
            if required_windows and supervisor_sam:
                try: send_approval_mail(supervisor_email_for_mail, firstname, lastname, process_type, request_id); flash("Antrag gespeichert und zur Freigabe versendet.", "success")
                except Exception as e: logger.error(f"Mailversand-Fehler Antrag {request_id} an '{supervisor_email_for_mail}': {e}", exc_info=True); flash(f"Antrag gespeichert, aber Fehler bei Genehmigungs-E-Mail.", "warning")
            else:
                logger.info(f"Antrag {request_id}: Keine Mail-Genehmigung nötig/möglich. Direkte Bearbeitung.")
                if update_request_status(request_id, "in_bearbeitung", "offen"): trigger_n8n_webhook(request_id); flash("Antrag gespeichert und direkt zur Bearbeitung weitergeleitet.", "success")
            return redirect(url_for("form"))
        else: flash("Antrag konnte nicht erstellt werden.", "danger"); o,a = load_form_dependencies(); return render_template("form.html", today=today, ou_tree=o, all_supervisors=a, form_data=form_data_on_error)
    ou_tree_data, all_supervisors_data = load_form_dependencies()
    return render_template("form.html", today=today, ou_tree=ou_tree_data, all_supervisors=all_supervisors_data, form_data={}) # Immer leeres form_data bei GET

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        logger.debug(f"🔑 Login Versuch für Benutzer: {username}")

        # Umgebungsvariablen für Gruppen-CNs abrufen
        main_access_group_env_key = ENV_MAIN_ACCESS_GROUP # Der Key für die .env-Variable
        admin_specific_group_env_key = ENV_ADMIN_MAIN_GROUP # Der Key für die .env-Variable

        main_access_group_name = os.getenv(main_access_group_env_key)
        admin_specific_group_name = os.getenv(admin_specific_group_env_key)

        # Konfiguration und Eingaben prüfen
        required_configs = {
            "AD_SERVER": AD_SERVER,
            "AD_SEARCH_BASE": AD_SEARCH_BASE,
            main_access_group_env_key: main_access_group_name, # Prüft, ob die Env-Var für die Hauptgruppe gesetzt ist
            admin_specific_group_env_key: admin_specific_group_name # Prüft, ob die Env-Var für die Admin-Gruppe gesetzt ist
        }
        if not username or not password:
            flash("Benutzername und Passwort sind erforderlich.", "danger")
            return render_template("login.html")

        missing_env_vars = [k for k, v in required_configs.items() if not v]
        if missing_env_vars:
            logger.error(f"Login-Konfiguration unvollständig. Fehlende Umgebungsvariablen: {', '.join(missing_env_vars)}")
            flash("⚠️ Login nicht möglich: Wichtige Systemkonfigurationen fehlen. Bitte Admin kontaktieren.", "danger")
            return render_template("login.html")

        ad_conn_login = None
        try:
            server_uri = AD_SERVER
            use_ssl = os.getenv("AD_USE_SSL", "false").lower() == "true"
            prefix = "ldaps://" if use_ssl else "ldap://"
            if not server_uri.lower().startswith(("ldap://", "ldaps://")):
                server_uri = prefix + server_uri

            server = Server(server_uri, get_info=ALL, use_ssl=use_ssl)

            # UPN-Erstellung für den Bind-Versuch
            dc_parts = [part.split('=')[1] for part in AD_SEARCH_BASE.split(',') if part.strip().upper().startswith('DC=')]
            domain = '.'.join(dc_parts)
            user_principal_name = f"{username}@{domain}"

            logger.debug(f"➡️ Versuch Bind mit UPN: {user_principal_name}")
            # Verbindung mit den Benutzer-Credentials herstellen
            ad_conn_login = Connection(server, user=user_principal_name, password=password, authentication="SIMPLE", auto_bind=True, raise_exceptions=True)
            logger.info(f"✅ Bind mit UPN für '{user_principal_name}' erfolgreich.")

            # sAMAccountName und DN des Benutzers abrufen (mit der gerade etablierten Verbindung)
            # Suche nach UPN oder sAMAccountName, um Flexibilität bei der Eingabe zu ermöglichen
            ad_conn_login.search(search_base=AD_SEARCH_BASE,
                                   search_filter=f"(|(userPrincipalName={user_principal_name})(sAMAccountName={username}))",
                                   search_scope=SUBTREE,
                                   attributes=['distinguishedName', 'sAMAccountName'])

            if not ad_conn_login.entries:
                logger.warning(f"⚠️ Keine AD-Einträge für '{username}' (UPN: '{user_principal_name}') nach erfolgreichem Bind gefunden. Dies sollte nicht passieren.")
                flash("⚠️ Benutzerdetails nicht gefunden nach erfolgreicher Authentifizierung. Bitte Admin kontaktieren.", "warning")
                # raise LDAPBindError ausgelöst von Connection(..., raise_exceptions=True) sollte dies eigentlich schon verhindern.
                # Zur Sicherheit hier trotzdem behandeln.
                return render_template("login.html")

            user_entry = ad_conn_login.entries[0]
            # WICHTIGE KORREKTUR: Zuweisung aufteilen
            actual_sam_account_name = getattr(user_entry.sAMAccountName, "value", username) # Fallback auf eingegebenen Username
            user_entry_dn = user_entry.distinguishedName.value
            logger.debug(f"Benutzer DN: {user_entry_dn}, sAMAccountName: {actual_sam_account_name}")

            # Schritt 1: Prüfen, ob Benutzer Mitglied der Haupt-Zugriffsgruppe ist
            # Für diese Prüfung verwenden wir die Funktion aus ad_utils, die den Service-Account nutzt.
            if not ad_utils.is_user_member_of_group_by_env_var(actual_sam_account_name, ENV_MAIN_ACCESS_GROUP):
                logger.warning(f"🚫 Benutzer '{actual_sam_account_name}' ist kein Mitglied der Hauptgruppe '{main_access_group_name}'. Login verweigert.")
                flash(f"Zugriff verweigert. Sie sind nicht berechtigt, diese Anwendung zu nutzen.", "danger")
                return render_template("login.html")
            logger.info(f"✅ Benutzer '{actual_sam_account_name}' ist Mitglied der Haupt-Zugriffsgruppe '{main_access_group_name}'.")

            # Schritt 2: Prüfen, ob Benutzer Mitglied der spezifischen Admin-Gruppe ist
            is_specific_admin = ad_utils.is_user_member_of_group_by_env_var(actual_sam_account_name, ENV_ADMIN_MAIN_GROUP)
            session["is_admin"] = is_specific_admin
            if is_specific_admin:
                logger.info(f"✅ Benutzer '{actual_sam_account_name}' ist Mitglied der Admin-Gruppe '{admin_specific_group_name}'.")
            else:
                logger.info(f"ℹ️ Benutzer '{actual_sam_account_name}' ist KEIN Mitglied der Admin-Gruppe '{admin_specific_group_name}'.")

            # Session für erfolgreichen Login (Mitglied der Hauptgruppe) erstellen
            session["user"] = actual_sam_account_name
            session["user_dn"] = user_entry_dn # Kann nützlich sein für andere AD-Operationen

            flash(f"✅ Willkommen, {actual_sam_account_name}!", "success")

            # Weiterleitungslogik
            next_url = request.args.get('next')
            if next_url:
                # Sicherheitscheck: Ist die `next_url` sicher und auf der eigenen Domain? (Optional, aber empfohlen)
                # Hier eine einfache Prüfung, ob es zur Admin-Seite geht und der User kein Admin ist
                if url_for('admin') in next_url and not session.get("is_admin"):
                    logger.info(f"User '{actual_sam_account_name}' (kein Admin) versuchte via 'next' zu '{next_url}', wird zu '/dashboard' umgeleitet.")
                    return redirect(url_for("user_dashboard"))
                logger.info(f"Leite User '{actual_sam_account_name}' zu 'next' URL: {next_url}")
                return redirect(next_url)

            # Standard-Weiterleitung nach Login
            if session.get("is_admin"):
                return redirect(url_for("admin"))
            else:
                # Wenn User in MAIN_ACCESS_GROUP aber kein Admin ist, leite zu einem generischen Dashboard
                return redirect(url_for("user_dashboard"))

        except LDAPBindError:
            logger.warning(f"❌ LDAP Bind Fehler für '{username}'. Ungültige Anmeldedaten oder UPN/Passwort-Kombination.")
            flash("Login fehlgeschlagen: Benutzername oder Passwort ungültig.", "danger")
        except LDAPSocketOpenError as e:
            logger.error(f"❌ LDAP Verbindungsproblem Server '{AD_SERVER}': {e}", exc_info=True)
            flash("Login fehlgeschlagen: Keine Verbindung zum Verzeichnisdienst.", "danger")
        except ValueError as e: # Fängt ValueErrors von ad_utils (fehlende Konfig) oder eigener Logik
            logger.error(f"❌ Konfigurationsfehler (ValueError) beim Login oder Gruppenprüfung: {e}", exc_info=True)
            flash(f"Login fehlgeschlagen: {e}", "danger") # Zeige die spezifische Fehlermeldung aus ValueError
        except LDAPException as e:
            logger.error(f"❌ Allgemeiner LDAP Fehler Login '{username}': {e}", exc_info=True)
            flash("Login fehlgeschlagen: Problem mit dem Verzeichnisdienst.", "danger")
        except Exception as e:
            logger.error(f"❌ Unerwarteter Fehler Login '{username}': {e}", exc_info=True)
            flash("Ein unerwarteter interner Fehler ist aufgetreten.", "danger")
        finally:
            if ad_conn_login and ad_conn_login.bound:
                try:
                    ad_conn_login.unbind()
                    logger.debug("🔒 LDAP Verbindung (Login-User) geschlossen.")
                except Exception as unbind_e:
                    logger.error(f"Fehler beim Schließen der LDAP-Verbindung (Login-User): {unbind_e}")
        # Wenn ein Fehler auftrat, wird das Login-Template erneut gerendert
        return render_template("login.html")

    # Für GET Requests das Login-Template anzeigen
    return render_template("login.html")

@app.route("/logout")
def logout():
    user = session.pop('user', 'Unbekannt'); session.pop('user_dn', None); session.pop('is_admin', None)
    flash("Sie wurden erfolgreich abgemeldet.", "info"); logger.info(f"👋 Benutzer '{user}' abgemeldet.")
    return redirect(url_for("login"))

@app.route("/admin")
@require_ad_group(ENV_ADMIN_MAIN_GROUP)
def admin():
    conn_db = None
    current_requests_raw = []
    try:
        conn_db = sqlite3.connect("db/onoffboarding.db")
        conn_db.row_factory = sqlite3.Row
        c = conn_db.cursor()
        # Sortierung nach created_at, falls Spalte existiert, sonst nach ID
        try:
            c.execute("SELECT * FROM requests WHERE status NOT IN ('abgeschlossen', 'abgelehnt') ORDER BY created_at DESC, id DESC")
        except sqlite3.OperationalError:
            logger.warning("Spalte 'created_at' nicht in DB gefunden für Sortierung in /admin, sortiere nach ID.")
            c.execute("SELECT * FROM requests WHERE status NOT IN ('abgeschlossen', 'abgelehnt') ORDER BY id DESC")
        current_requests_raw = c.fetchall()
    except sqlite3.Error as e:
        logger.error(f"SQLite Fehler beim Laden aktueller Anträge: {e}", exc_info=True)
        flash("Fehler beim Laden der Anträge.", "danger")
        # current_requests_raw bleibt leer
    finally:
        if conn_db: conn_db.close()

    current_requests = []
    for req_raw in current_requests_raw: # req_raw ist ein sqlite3.Row Objekt
        req_dict = dict(req_raw) # Konvertiere sqlite3.Row zu einem Python Dictionary

        # Verarbeite hardware_accessories
        acc_str = req_dict.get('hardware_accessories', '') # .get() ist jetzt sicher auf req_dict
        req_dict['hardware_accessories'] = [a.strip() for a in acc_str.split(',')] if acc_str else []

        # Verarbeite hardware_mobile
        mob_str = req_dict.get('hardware_mobile', '') # .get() ist jetzt sicher auf req_dict
        req_dict['hardware_mobile'] = [m.strip() for m in mob_str.split(',')] if mob_str else []

        current_requests.append(req_dict)

    return render_template("admin.html", requests=current_requests)

@app.route("/archived")
@require_ad_group(ENV_ADMIN_MAIN_GROUP)
def archived():
    conn_db = None
    archived_requests_raw = []
    try:
        conn_db = sqlite3.connect("db/onoffboarding.db")
        conn_db.row_factory = sqlite3.Row
        c = conn_db.cursor()
        c.execute("SELECT * FROM requests WHERE status IN ('abgeschlossen', 'abgelehnt') ORDER BY id DESC")
        archived_requests_raw = c.fetchall()
    except sqlite3.Error as e:
        logger.error(f"SQLite Fehler Laden Archiv: {e}", exc_info=True)
        flash("Fehler beim Laden des Archivs.", "danger")
        # archived_requests_raw bleibt leer
    finally:
        if conn_db: conn_db.close()

    processed_requests = []
    for req_raw in archived_requests_raw: # req_raw ist ein sqlite3.Row Objekt
        req_dict = dict(req_raw) # Konvertiere sqlite3.Row zu einem Python Dictionary

        # Verarbeite hardware_accessories
        acc_str = req_dict.get('hardware_accessories', '') # .get() ist jetzt sicher auf req_dict
        req_dict['hardware_accessories'] = [a.strip() for a in acc_str.split(',')] if acc_str else []

        # Verarbeite hardware_mobile
        mob_str = req_dict.get('hardware_mobile', '') # .get() ist jetzt sicher auf req_dict
        req_dict['hardware_mobile'] = [m.strip() for m in mob_str.split(',')] if mob_str else []

        processed_requests.append(req_dict)

    return render_template("archived.html", requests=processed_requests)

@app.route("/ou_tree")
@require_ad_group(ENV_MAIN_ACCESS_GROUP)
def ou_tree():
    try: tree = ad_utils.build_ou_tree(AD_SEARCH_BASE); return jsonify(tree)
    except Exception as e: logger.error(f"Fehler Erstellen OU-Baum: {e}", exc_info=True); return jsonify({"error": "Konnte OU-Baum nicht laden"}), 500

@app.route("/ou_users")
@require_ad_group(ENV_MAIN_ACCESS_GROUP)
def ou_users():
    dn = request.args.get("dn")
    if not dn: return jsonify({"error": "Parameter 'dn' fehlt"}), 400
    try: users = ad_utils.get_users_in_ou(dn); return jsonify(users)
    except Exception as e: logger.error(f"Fehler Laden Benutzer für OU '{dn}': {e}", exc_info=True); return jsonify({"error": "Konnte Benutzer nicht laden"}), 500

@app.route("/supervisors")
@require_ad_group(ENV_MAIN_ACCESS_GROUP)
def supervisors():
    logger.info(f"--- /supervisors Route aufgerufen ---"); dn = request.args.get("dn")
    if not os.getenv(ENV_SUPERVISOR_GROUP): logger.error(f"{ENV_SUPERVISOR_GROUP} nicht konfiguriert!"); return jsonify({"error": "Vorgesetzten-Gruppe nicht konfiguriert."}), 500
    try:
        supervisor_list = ad_utils.get_supervisors_in_ou(dn) if dn else ad_utils.get_all_supervisors()
        return jsonify(supervisor_list)
    except Exception as e: logger.error(f"Fehler in /supervisors: {e}", exc_info=True); return jsonify({"error": "Interner Serverfehler."}), 500

@app.route("/reject/<int:request_id>", methods=["POST"])
@require_ad_group(ENV_ADMIN_MAIN_GROUP)
def reject_request(request_id):
    current_user = session['user']; logger.info(f"Ablehnung Antrag ID: {request_id} durch User '{current_user}'")
    if not update_request_status(request_id, "abgelehnt", "offen"):
        update_request_status(request_id, "abgelehnt")
    flash(f"Antrag {request_id} als abgelehnt markiert.", "info")
    return redirect(url_for("admin"))

@app.route("/view/<int:request_id>")
@require_ad_group(ENV_ADMIN_MAIN_GROUP)
def view_request(request_id):
    logger.info(f"Zeige Details für Antrag ID: {request_id}")
    request_dict = get_request_item_as_dict(request_id)
    if not request_dict: flash(f"Antrag {request_id} nicht gefunden.", "danger"); return redirect(url_for("admin"))
    can_approve, all_subprocesses_completed, pending_subprocesses = False, False, []
    current_user_sam = session.get("user")
    if current_user_sam and request_dict.get('status') == 'offen':
        is_supervisor = ad_utils.is_user_member_of_group_by_env_var(current_user_sam, ENV_SUPERVISOR_GROUP)
        if is_supervisor or session.get("is_admin", False): can_approve = True
        logger.info(f"User '{current_user_sam}' Genehmigungsrecht für Antrag {request_id}: {can_approve} (Supervisor: {is_supervisor}, Admin: {session.get('is_admin', False)}).")
    if request_dict.get('status') == 'in_bearbeitung':
        all_subprocesses_completed, pending_subprocesses = check_all_subprocesses_done(request_dict)
        request_dict['pending_subprocesses'] = pending_subprocesses
    return render_template("view.html", request=request_dict, can_approve=can_approve, all_subprocesses_completed=all_subprocesses_completed)

@app.route("/approve/<int:request_id>", methods=["POST"])
@require_ad_group([ENV_SUPERVISOR_GROUP, ENV_ADMIN_MAIN_GROUP])
def approve_request(request_id):
    current_user = session['user']
    logger.info(f"Genehmigung Antrag ID: {request_id} durch User '{current_user}'")
    if update_request_status(request_id, "in_bearbeitung", "offen"):
        logger.info(f"Antrag {request_id} durch '{current_user}' genehmigt und auf 'in_bearbeitung' gesetzt.")
        trigger_n8n_webhook(request_id); flash(f"Antrag {request_id} genehmigt und zur Bearbeitung weitergeleitet.", "success")
    return redirect(url_for("admin"))

@app.route("/manual_username/<int:request_id>", methods=["GET", "POST"])
@require_ad_group("IT_GROUP")
def manual_username_input(request_id):
    request_item = get_request_item_as_dict(request_id)
    if not request_item: flash(f"Antrag {request_id} nicht gefunden.", "danger"); return redirect(url_for("admin"))
    if request.method == "POST":
        new_username = request.form.get("new_username", "").strip(); admin_user_performing_action = session.get("user")
        if not new_username: flash("Bitte geben Sie einen neuen Benutzernamen ein.", "danger"); return render_template("manual_username.html", request_item=request_item)
        payload_to_n8n = {
            "usernameToCreate": new_username, "originalRequestId": request_item["id"],
            "firstname": request_item["firstname"], "lastname": request_item["lastname"],
            "referenceUserSAM": request_item["referenceuser"], "departmentDN": request_item["department_dn"],
            "manualAdminOverride": admin_user_performing_action }
        n8n_webhook_url = N8N_WEBHOOK_CREATE_USER_ACTION
        if not n8n_webhook_url: logger.error("N8N_WEBHOOK_CREATE_USER_ACTION ist nicht konfiguriert!"); flash("Fehler: Workflow zur User-Erstellung (manuell) nicht konfiguriert.", "danger"); return render_template("manual_username.html", request_item=request_item)
        try:
            logger.info(f"Sende manuellen Usernamen '{new_username}' fÃ¼r Antrag {request_id} an n8n URL: {n8n_webhook_url}"); logger.debug(f"Payload an n8n (manuell): {payload_to_n8n}")
            res = requests.post(n8n_webhook_url, json=payload_to_n8n, timeout=15); res.raise_for_status()
            flash(f"Neuer Benutzername '{new_username}' wurde zur Verarbeitung an n8n Ã¼bergeben.", "success")
            return redirect(url_for("view_request", request_id=request_id))
        except requests.exceptions.RequestException as e: logger.error(f"Fehler beim Senden des manuellen Usernamens an n8n: {e}", exc_info=True); flash(f"Fehler bei der Ãœbergabe des neuen Usernamens an die Verarbeitung: {e}", "danger")
        return render_template("manual_username.html", request_item=request_item)
    return render_template("manual_username.html", request_item=request_item)

@app.route("/webhook/n8n_update/ad_user_status/<int:request_id>", methods=["POST"])
def webhook_n8n_update_ad_user_status(request_id):
    # Token-Authentifizierung fÃ¼r den Webhook
    # ... (bestehender Code)
    if N8N_FLASK_TOKEN:
        received_token = request.headers.get("X-N8N-Token")
        if not received_token or received_token != N8N_FLASK_TOKEN: logger.warning(f"UngÃ¼ltiger/fehlender Token fÃ¼r n8n Update AD Status, Antrag {request_id}"); return jsonify({"status": "error", "message": "Invalid or missing token"}), 403
    raw_data_received = request.get_data(as_text=True); logger.info(f"ðŸ“¢ N8N Update fÃ¼r AD User Status (Antrag {request_id}) ROHDATEN EMPFANGEN: {raw_data_received}")
    data = request.json; logger.info(f"ðŸ“¢ N8N Update fÃ¼r AD User Status (Antrag {request_id}) GEPARST EMPFANGEN: {data}")
    if not data or not isinstance(data, dict): logger.warning(f"Keine validen JSON-Objekt-Daten im n8n Update fÃ¼r Antrag {request_id} empfangen. Empfangen: {type(data)}"); return jsonify({"status": "error", "message": "No valid JSON object data received"}), 400
    ad_creation_status = data.get("creation_status"); ad_username_created = data.get("ad_username"); ad_initial_password = data.get("initial_password")
    ad_status_message = data.get("status_message", f"AD Prozess fÃ¼r User '{ad_username_created or 'N/A'}' von n8n mit Status '{ad_creation_status or 'N/A'}' gemeldet.")
    conn_db = None
    try:
        conn_db = sqlite3.connect("db/onoffboarding.db"); c = conn_db.cursor()
        sql_update = """UPDATE requests SET n8n_ad_creation_status = ?, n8n_ad_username_created = ?, n8n_ad_initial_password = ?, n8n_ad_status_message = ? WHERE id = ?"""
        c.execute(sql_update, (ad_creation_status, ad_username_created, ad_initial_password, ad_status_message, request_id)); conn_db.commit()
        if c.rowcount > 0:
            logger.info(f"Antrag {request_id}: AD User Status von n8n aktualisiert. Status: {ad_creation_status}, User: {ad_username_created}")
            updated_request_item = get_request_item_as_dict(request_id)
            if updated_request_item: check_all_subprocesses_done(updated_request_item)
        else:
            logger.warning(f"Antrag {request_id} nicht gefunden fÃ¼r n8n AD Status Update.")
            return jsonify({"status": "error", "message": f"Request {request_id} not found"}), 404
        return jsonify({"status": "success", "message": "AD user status updated"}), 200
    except sqlite3.Error as e: logger.error(f"SQLite Fehler beim Aktualisieren des n8n AD Status fÃ¼r Antrag {request_id}: {e}", exc_info=True); return jsonify({"status": "error", "message": "Database error"}), 500
    except Exception as e: logger.error(f"Allgemeiner Fehler beim Aktualisieren des n8n AD Status fÃ¼r Antrag {request_id}: {e}", exc_info=True); return jsonify({"status": "error", "message": "Internal server error"}), 500
    finally:
        if conn_db: conn_db.close()


@app.route("/webhook/n8n/request_completed/<int:request_id>", methods=["POST"])
def n8n_request_completed(request_id):
    # Token-Authentifizierung
    # ... (bestehender Code)
    if N8N_FLASK_TOKEN:
        received_token = request.headers.get("X-N8N-Token")
        if not received_token or received_token != N8N_FLASK_TOKEN: logger.warning(f"UngÃ¼ltiger oder fehlender Token-Versuch fÃ¼r /webhook/n8n/request_completed/{request_id}"); return jsonify({"status": "error", "message": "Invalid or missing token"}), 403
    logger.info(f"ðŸ“¢ N8N meldet Abschluss fÃ¼r Antrag ID: {request_id}")
    if update_request_status(request_id, "abgeschlossen", expected_current_status="in_bearbeitung"):
        logger.info(f"âœ… Antrag {request_id} von n8n als 'abgeschlossen' markiert.")
        return jsonify({"status": "success", "message": f"Request {request_id} marked as completed"}), 200
    else:
        current_status_item = get_request_item_as_dict(request_id)
        current_status = current_status_item.get('status') if current_status_item else "unbekannt"
        logger.warning(f"Konnte Antrag {request_id} nicht von n8n als abgeschlossen markieren. Aktueller Status: '{current_status}', erwartet 'in_bearbeitung'.")
        if current_status not in ["abgeschlossen", "abgelehnt"]:
            if update_request_status(request_id, "abgeschlossen"):
                logger.info(f"Antrag {request_id} wurde nun doch als 'abgeschlossen' markiert (Force).")
                return jsonify({"status": "success", "message": f"Request {request_id} force-marked as completed"}), 200
        return jsonify({"status": "info", "message": f"Could not mark request {request_id} as completed by n8n. Current status: {current_status}"}), 200

@app.route("/update_hardware_status/<int:request_id>", methods=["GET", "POST"])
@require_ad_group("IT_GROUP")
def update_hardware_status(request_id):
    request_item = get_request_item_as_dict(request_id)
    if not request_item: flash(f"Antrag {request_id} nicht gefunden.", "danger"); return redirect(url_for("admin"))
    hardware_to_order_text = get_hardware_details_for_display(request_item)
    can_update_phone_on_hw_page = request_item.get('needs_fixed_phone')
    if request.method == "POST":
        action = request.form.get("action"); current_user = session.get("user", "System"); now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        update_fields = {}; success_message = None; action_processed = False
        if action == "ordered" and not request_item.get('hw_status_ordered_at'):
            update_fields['hw_status_ordered_at'] = now_str; update_fields['hw_status_ordered_by'] = current_user; success_message = "Hardware als 'bestellt' markiert."; action_processed = True
        elif action == "delivered" and request_item.get('hw_status_ordered_at') and not request_item.get('hw_status_delivered_at'):
            update_fields['hw_status_delivered_at'] = now_str; update_fields['hw_status_delivered_by'] = current_user; success_message = "Hardware als 'geliefert' markiert."; action_processed = True
        elif action == "installed" and request_item.get('hw_status_delivered_at') and not request_item.get('hw_status_installed_at'):
            update_fields['hw_status_installed_at'] = now_str; update_fields['hw_status_installed_by'] = current_user; success_message = "Hardware als 'installiert' markiert."; action_processed = True
        elif action == "setup_done" and request_item.get('hw_status_installed_at') and not request_item.get('hw_status_setup_done_at'):
            update_fields['hw_status_setup_done_at'] = now_str; update_fields['hw_status_setup_done_by'] = current_user; success_message = "Hardware als 'aufgebaut/fertig' markiert."; action_processed = True
        elif action == "phone_ordered" and can_update_phone_on_hw_page and not request_item.get('phone_status_ordered_at'):
            update_fields['phone_status_ordered_at'] = now_str; update_fields['phone_status_ordered_by'] = current_user; success_message = "Festarbeitsplatztelefon als 'bestellt' markiert."; action_processed = True
        elif action == "phone_setup_done" and can_update_phone_on_hw_page and request_item.get('phone_status_ordered_at') and not request_item.get('phone_status_setup_at'):
            action_processed = True
            assigned_phone_number = request.form.get("phone_number_assigned", "").strip()
            if not assigned_phone_number: flash("Bitte geben Sie die zugewiesene Rufnummer ein.", "danger")
            else:
                update_fields['phone_status_setup_at'] = now_str; update_fields['phone_status_setup_by'] = current_user
                update_fields['phone_number_assigned'] = assigned_phone_number
                success_message = f"Festarbeitsplatztelefon als 'aufgebaut/installiert' mit Nr. {assigned_phone_number} markiert."
        if not action_processed and action:
            flash("UngÃ¼ltige Aktion oder falsche Reihenfolge fÃ¼r Hardware/Telefon-Status.", "warning")
        elif update_fields and success_message:
            conn_db_hw = None
            try:
                conn_db_hw = sqlite3.connect("db/onoffboarding.db"); c = conn_db_hw.cursor()
                set_clause_parts = [f"{k} = ?" for k in update_fields.keys()]; values = list(update_fields.values())
                values.append(request_id); set_clause_str = ", ".join(set_clause_parts)
                c.execute(f"UPDATE requests SET {set_clause_str} WHERE id = ?", tuple(values)); conn_db_hw.commit()
                if c.rowcount > 0 :
                    flash(success_message, "success"); logger.info(f"Antrag {request_id}: Hardware/Telefon-Status '{action}' durch '{current_user}' gesetzt.")
                    updated_item = get_request_item_as_dict(request_id); check_all_subprocesses_done(updated_item)
                else: flash("Update nicht erfolgreich.", "warning")
            except sqlite3.Error as e: logger.error(f"DB-Fehler: {e}", exc_info=True); flash("DB Fehler.", "danger")
            finally:
                if conn_db_hw: conn_db_hw.close()
        return redirect(url_for('update_hardware_status', request_id=request_id))
    return render_template("update_hardware_status.html", request_item=request_item, hardware_to_order_text=hardware_to_order_text, can_update_phone_on_hw_page=can_update_phone_on_hw_page)

@app.route("/update_bauamt_status/<int:request_id>", methods=["GET", "POST"])
@require_ad_group("BAUAMT_GROUP")
def update_bauamt_status(request_id):
    if request.method == "GET" and request.args.get('reset_workplace_selection') == 'true':
        conn_db = None
        try:
            conn_db = sqlite3.connect("db/onoffboarding.db"); c = conn_db.cursor()
            c.execute("""UPDATE requests SET
                            workplace_needs_new_table = 0, workplace_needs_new_chair = 0,
                            workplace_needs_monitor_arms = 0, workplace_no_new_equipment = 0,
                            workplace_table_ordered_at = NULL, workplace_table_ordered_by = NULL,
                            workplace_table_setup_at = NULL, workplace_table_setup_by = NULL,
                            workplace_chair_ordered_at = NULL, workplace_chair_ordered_by = NULL,
                            workplace_chair_setup_at = NULL, workplace_chair_setup_by = NULL,
                            workplace_monitor_arms_ordered_at = NULL, workplace_monitor_arms_ordered_by = NULL,
                            workplace_monitor_arms_setup_at = NULL, workplace_monitor_arms_setup_by = NULL
                         WHERE id = ?""", (request_id,))
            conn_db.commit(); flash("Auswahl zurückgesetzt.", "info")
        except sqlite3.Error as e: logger.error(f"DB-Fehler: {e}"); flash("DB Fehler.", "danger")
        finally:
            if conn_db: conn_db.close()
        return redirect(url_for('update_bauamt_status', request_id=request_id))

    request_item = get_request_item_as_dict(request_id)
    if not request_item: flash(f"Antrag {request_id} nicht gefunden.", "danger"); return redirect(url_for("admin"))

    # show_key_button ist hier relevant für die Schlüsselvorbereitung
    show_key_prepared_button = request_item.get('key_required') and not request_item.get('key_status_prepared_at')

    if request.method == "POST":
        action = request.form.get("action"); current_user = session.get("user", "System"); now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        update_fields = {}; filename_to_save = None; success_message = None; action_processed = False

        # Schlüsselvorbereitung bleibt hier
        if action == "key_prepared" and request_item.get('key_required') and not request_item.get('key_status_prepared_at'):
            update_fields['key_status_prepared_at'] = now_str; update_fields['key_status_prepared_by'] = current_user; success_message = "Schlüssel als 'vorbereitet' markiert."; action_processed = True
        # Schlüsselausgabe wurde HIER ENTFERNT

        elif action == "update_room_number":
            action_processed = True; new_room_number = request.form.get("room_number", "").strip()
            if new_room_number != request_item.get("room_number"):
                update_fields['room_number'] = new_room_number; success_message = f"Zimmernummer zu '{new_room_number}' aktualisiert."
        elif action == "save_workplace_selection":
            action_processed = True
            needs_table = request.form.get("workplace_needs_new_table") == "true"
            needs_chair = request.form.get("workplace_needs_new_chair") == "true"
            needs_arms = request.form.get("workplace_needs_monitor_arms") == "true"
            no_equipment = request.form.get("workplace_no_new_equipment") == "true"
            if no_equipment: needs_table = False; needs_chair = False; needs_arms = False
            update_fields.update({'workplace_needs_new_table': needs_table, 'workplace_needs_new_chair': needs_chair, 'workplace_needs_monitor_arms': needs_arms, 'workplace_no_new_equipment': no_equipment})
            success_message = "Auswahl fÃ¼r Arbeitsplatzausstattung gespeichert."
            # Reset logic...
        elif action == "workplace_table_ordered" and request_item.get('workplace_needs_new_table') and not request_item.get('workplace_table_ordered_at'):
            update_fields['workplace_table_ordered_at'] = now_str; update_fields['workplace_table_ordered_by'] = current_user; success_message = "Tisch als bestellt markiert."; action_processed = True
        elif action == "workplace_table_setup" and request_item.get('workplace_needs_new_table') and request_item.get('workplace_table_ordered_at') and not request_item.get('workplace_table_setup_at'):
            update_fields['workplace_table_setup_at'] = now_str; update_fields['workplace_table_setup_by'] = current_user; success_message = "Tisch als aufgebaut markiert."; action_processed = True
        elif action == "workplace_chair_ordered" and request_item.get('workplace_needs_new_chair') and not request_item.get('workplace_chair_ordered_at'):
            update_fields['workplace_chair_ordered_at'] = now_str; update_fields['workplace_chair_ordered_by'] = current_user; success_message = "Stuhl als bestellt markiert."; action_processed = True
        elif action == "workplace_chair_setup" and request_item.get('workplace_needs_new_chair') and request_item.get('workplace_chair_ordered_at') and not request_item.get('workplace_chair_setup_at'):
            update_fields['workplace_chair_setup_at'] = now_str; update_fields['workplace_chair_setup_by'] = current_user; success_message = "Stuhl als aufgebaut markiert."; action_processed = True
        elif action == "workplace_monitor_arms_ordered" and request_item.get('workplace_needs_monitor_arms') and not request_item.get('workplace_monitor_arms_ordered_at'):
            update_fields['workplace_monitor_arms_ordered_at'] = now_str; update_fields['workplace_monitor_arms_ordered_by'] = current_user; success_message = "Monitorarme als bestellt markiert."; action_processed = True
        elif action == "workplace_monitor_arms_setup" and request_item.get('workplace_needs_monitor_arms') and request_item.get('workplace_monitor_arms_ordered_at') and not request_item.get('workplace_monitor_arms_setup_at'):
            update_fields['workplace_monitor_arms_setup_at'] = now_str; update_fields['workplace_monitor_arms_setup_by'] = current_user; success_message = "Monitorarme als montiert markiert."; action_processed = True

        if not action_processed and action : flash("UngÃ¼ltige Aktion oder falsche Reihenfolge fÃ¼r Bauamt-Status.", "warning")
        elif update_fields and success_message:
            conn_db_bauamt = None
            try:
                conn_db_bauamt = sqlite3.connect("db/onoffboarding.db"); c = conn_db_bauamt.cursor()
                set_clause_parts = [f"{k} = ?" for k in update_fields.keys()]; values = list(update_fields.values())
                values.append(request_id); set_clause_str = ", ".join(set_clause_parts)
                c.execute(f"UPDATE requests SET {set_clause_str} WHERE id = ?", tuple(values)); conn_db_bauamt.commit()
                if c.rowcount > 0:
                    flash(success_message, "success"); logger.info(f"Antrag {request_id}: Bauamt-Status '{action}' durch '{current_user}' gesetzt.")
                    updated_item = get_request_item_as_dict(request_id); check_all_subprocesses_done(updated_item)
                else: flash("Update nicht erfolgreich.", "warning")
            except sqlite3.Error as e: logger.error(f"DB-Fehler: {e}", exc_info=True); flash("DB Fehler.", "danger")
            finally:
                if conn_db_bauamt: conn_db_bauamt.close()
        return redirect(url_for('update_bauamt_status', request_id=request_id))
    return render_template("update_bauamt_status.html", request_item=request_item, show_key_prepared_button=show_key_prepared_button)


@app.route('/uploads/<path:filename>')
@require_ad_group("AD_GROUP") # Oder eine spezifischere Gruppe, falls nicht alle Admins alle Dateien sehen sollen
def uploaded_file(filename):
    directory = concrete_upload_folder
    try:
        logger.debug(f"Versuche Datei '{filename}' aus Verzeichnis '{directory}' zu senden.")
        return send_from_directory(directory, filename, as_attachment=True) # as_attachment=True fÃ¼r Download
    except FileNotFoundError:
        logger.error(f"Datei nicht gefunden fÃ¼r Download: {filename} in {directory}"); flash("Datei nicht gefunden.", "danger")
        return redirect(request.referrer or url_for("admin"))


@app.route("/update_email_status/<int:request_id>", methods=["GET", "POST"])
@require_ad_group("IT_GROUP")
def update_email_status(request_id):
    request_item = get_request_item_as_dict(request_id)
    if not request_item: flash(f"Antrag {request_id} nicht gefunden.", "danger"); return redirect(url_for("admin"))
    if not request_item.get('email_account_required'): flash(f"FÃ¼r Antrag {request_id} wurde kein E-Mail Account angefordert.", "info")
    if request.method == "POST":
        created_email_address = request.form.get("created_email_address", "").strip()
        current_user = session.get("user", "System"); now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if not created_email_address: flash("Bitte geben Sie die erstellte E-Mail-Adresse ein.", "danger")
        elif not is_valid_email(created_email_address): flash("Die eingegebene E-Mail-Adresse ist ungÃ¼ltig.", "danger")
        else:
            conn_db = None
            try:
                conn_db = sqlite3.connect("db/onoffboarding.db"); c = conn_db.cursor()
                sql_update = """UPDATE requests SET email_created_address = ?, email_creation_confirmed_at = ?, email_creation_confirmed_by = ?, n8n_email_status_message = ? WHERE id = ?"""
                status_msg = f"E-Mail Konto '{created_email_address}' erfasst."
                c.execute(sql_update, (created_email_address, now_str, current_user, status_msg, request_id)); conn_db.commit()
                if c.rowcount > 0:
                    flash(f"E-Mail-Adresse '{created_email_address}' fÃ¼r Antrag {request_id} erfolgreich gespeichert.", "success")
                    updated_item = get_request_item_as_dict(request_id); check_all_subprocesses_done(updated_item)
                else: flash("Update nicht erfolgreich.", "warning")
            except sqlite3.Error as e: logger.error(f"DB-Fehler: {e}", exc_info=True); flash("DB Fehler.", "danger")
            finally:
                if conn_db: conn_db.close()
            return redirect(url_for('update_email_status', request_id=request_id))
    return render_template("update_email_status.html", request_item=request_item)


@app.route("/update_software_status/<int:request_id>", methods=["GET", "POST"])
@require_ad_group("IT_GROUP")
def update_software_status(request_id):
    request_item = get_request_item_as_dict(request_id)
    if not request_item: flash(f"Antrag {request_id} nicht gefunden.", "danger"); return redirect(url_for("admin"))
    can_update_ris = request_item.get('needs_ris_access'); can_update_cipkom = request_item.get('needs_cipkom_access')
    if request.method == "POST":
        action = request.form.get("action"); current_user = session.get("user", "System"); now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        update_fields = {}; success_message = None; action_processed = False
        if action == "ris_granted" and can_update_ris and not request_item.get('ris_access_status_granted_at'):
            update_fields['ris_access_status_granted_at'] = now_str; update_fields['ris_access_status_granted_by'] = current_user; success_message = "RIS Zugang als 'erteilt' markiert."; action_processed = True
        elif action == "cipkom_granted" and can_update_cipkom and not request_item.get('cipkom_access_status_granted_at'):
            update_fields['cipkom_access_status_granted_at'] = now_str; update_fields['cipkom_access_status_granted_by'] = current_user; success_message = "CIPKOM Zugang als 'erteilt' markiert."; action_processed = True
        if not action_processed and action: flash("UngÃ¼ltige Software-Aktion oder Status bereits gesetzt/Anforderung nicht vorhanden.", "warning")
        elif update_fields and success_message:
            conn_db_sw = None
            try:
                conn_db_sw = sqlite3.connect("db/onoffboarding.db"); c = conn_db_sw.cursor()
                set_clause_parts = [f"{k} = ?" for k in update_fields.keys()]; values = list(update_fields.values())
                values.append(request_id); set_clause_str = ", ".join(set_clause_parts)
                c.execute(f"UPDATE requests SET {set_clause_str} WHERE id = ?", tuple(values)); conn_db_sw.commit()
                if c.rowcount > 0:
                    flash(success_message, "success");
                    updated_item = get_request_item_as_dict(request_id); check_all_subprocesses_done(updated_item)
                else: flash("Update nicht erfolgreich.", "warning")
            except sqlite3.Error as e: logger.error(f"DB-Fehler: {e}", exc_info=True); flash("DB Fehler.", "danger")
            finally:
                if conn_db_sw: conn_db_sw.close()
        return redirect(url_for('update_software_status', request_id=request_id))
    return render_template("update_software_status.html", request_item=request_item, can_update_ris=can_update_ris, can_update_cipkom=can_update_cipkom)


@app.route("/hr_update_status/<int:request_id>", methods=["GET", "POST"])
@require_ad_group("HR_GROUP")
def hr_update_status(request_id):
    request_item = get_request_item_as_dict(request_id)
    if not request_item: flash(f"Antrag {request_id} nicht gefunden.", "danger"); return redirect(url_for("admin"))

    # Logik für show_key_issue_button für HR (Schlüsselausgabe)
    show_key_issue_button = False
    if request_item.get('key_required') and request_item.get('key_status_prepared_at') and not request_item.get('key_status_issued_at'):
        if request_item.get('startdate'):
            try:
                start_date_obj = datetime.strptime(request_item.get('startdate'), '%Y-%m-%d').date()
                if start_date_obj <= date.today():
                    show_key_issue_button = True
            except ValueError:
                logger.warning(f"Ungültiges Startdatumformat für Antrag {request_id}: {request_item.get('startdate')}")
                show_key_issue_button = False # Bei Fehler auch nicht anzeigen

    if request.method == "POST":
        action = request.form.get("action"); current_user = session.get("user", "System"); now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        update_fields = {}; filename_to_save = None; success_message = None; action_processed = False
        
        # Bestehende Logik für HR Actions
        if action == "dienstvereinbarung_issued" and not request_item.get('hr_dienstvereinbarung_at'):
            update_fields['hr_dienstvereinbarung_at'] = now_str; update_fields['hr_dienstvereinbarung_by'] = current_user; success_message = "Dienstvereinbarung als herausgegeben markiert."; action_processed = True
        elif action == "datenschutz_issued" and not request_item.get('hr_datenschutz_at'):
            update_fields['hr_datenschutz_at'] = now_str; update_fields['hr_datenschutz_by'] = current_user; success_message = "Datenschutzblatt als herausgegeben markiert."; action_processed = True
        elif action == "dsgvo_informed" and not request_item.get('hr_dsgvo_informed_at'):
            update_fields['hr_dsgvo_informed_at'] = now_str; update_fields['hr_dsgvo_informed_by'] = current_user; success_message = "Über DSGVO als informiert markiert."; action_processed = True
        elif action == "it_directive_issued" and not request_item.get('hr_it_directive_at'):
            update_fields['hr_it_directive_at'] = now_str; update_fields['hr_it_directive_by'] = current_user; success_message = "Dienstanweisung IT als herausgegeben markiert."; action_processed = True
        elif action == "payroll_sheet_created" and not request_item.get('hr_payroll_sheet_at'):
            update_fields['hr_payroll_sheet_at'] = now_str; update_fields['hr_payroll_sheet_by'] = current_user; success_message = "Personaldatenblatt Abrechnung als erstellt markiert."; action_processed = True
        elif action == "security_guidelines_issued" and not request_item.get('hr_security_guidelines_at'):
            update_fields['hr_security_guidelines_at'] = now_str; update_fields['hr_security_guidelines_by'] = current_user; success_message = "Leitlinien Informationssicherheit als herausgegeben markiert."; action_processed = True
        elif action == "aida_access_created" and not request_item.get('aida_access_created_at'):
            update_fields['aida_access_created_at'] = now_str; update_fields['aida_access_created_by'] = current_user; success_message = "AIDA-Zugang als erstellt markiert."; action_processed = True
        elif action == "aida_key_registered" and not request_item.get('aida_key_registered_at'):
            update_fields['aida_key_registered_at'] = now_str; update_fields['aida_key_registered_by'] = current_user; success_message = "Schlüssel in AIDA als aufgenommen markiert."; action_processed = True

        # NEUE Schlüssel-Logik HIER EINFÜGEN (NUR key_issued)
        elif action == "key_issued" and request_item.get('key_required') and show_key_issue_button and not request_item.get('key_status_issued_at'):
            action_processed = True
            if 'protocol_pdf' not in request.files or request.files['protocol_pdf'].filename == '': flash('Keine Datei für das Protokoll ausgewählt!', 'warning')
            else:
                file = request.files['protocol_pdf']
                if file and allowed_file(file.filename):
                    original_filename = secure_filename(file.filename); timestamp_str = datetime.now().strftime("%Y%m%d%H%M%S")
                    base, ext = os.path.splitext(original_filename); filename_to_save = f"req{request_id}_keyprot_{timestamp_str}{ext}"[:250]
                    file.save(os.path.join(concrete_upload_folder, filename_to_save))
                    update_fields['key_status_issued_at'] = now_str; update_fields['key_status_issued_by'] = current_user
                    update_fields['key_issuance_protocol_filename'] = filename_to_save
                    success_message = f"Schlüssel als 'ausgegeben' markiert. Protokoll '{filename_to_save}' gespeichert."
                else: flash('Ungültiger Dateityp. Nur PDF erlaubt.', 'warning')

        if not action_processed and action: flash("Ungültige Aktion oder Status bereits gesetzt für HR/AIDA.", "warning")
        elif update_fields and success_message:
            conn_db_hr = None
            try:
                conn_db_hr = sqlite3.connect("db/onoffboarding.db"); c = conn_db_hr.cursor()
                set_clause_parts = [f"{k} = ?" for k in update_fields.keys()]; values = list(update_fields.values())
                values.append(request_id); set_clause_str = ", ".join(set_clause_parts)
                c.execute(f"UPDATE requests SET {set_clause_str} WHERE id = ?", tuple(values)); conn_db_hr.commit()
                if c.rowcount > 0:
                    flash(success_message, "success")
                    updated_item = get_request_item_as_dict(request_id); check_all_subprocesses_done(updated_item)
                else: flash("Update nicht erfolgreich.", "warning")
            except sqlite3.Error as e: logger.error(f"DB-Fehler: {e}", exc_info=True); flash("DB Fehler.", "danger")
            finally:
                if conn_db_hr: conn_db_hr.close()
        return redirect(url_for('hr_update_status', request_id=request_id))
    return render_template("hr_update_status.html", request_item=request_item, show_key_issue_button=show_key_issue_button)


@app.route("/update_office_status/<int:request_id>", methods=["GET", "POST"])
@require_ad_group(ENV_OFFICE_GROUP) # Stellt sicher, dass nur berechtigte User zugreifen
def update_office_status(request_id):
    request_item = get_request_item_as_dict(request_id)
    if not request_item:
        flash(f"Antrag {request_id} nicht gefunden.", "danger")
        return redirect(url_for("admin"))

    if request.method == "POST":
        action = request.form.get("action")
        current_user = session.get("user", "System")
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        update_fields = {}
        success_message = None
        action_processed = False # Wird auf True gesetzt, wenn eine gültige, offene Aufgabe gefunden wird

        logger.debug(f"POST-Request für /update_office_status/{request_id} mit Action: '{action}'")

        office_tasks_actions = {
            "office_outlook_contact_done": ('office_outlook_contact_at', 'office_outlook_contact_by', "Outlook-Kontakt als angelegt markiert."),
            "office_distribution_lists_done": ('office_distribution_lists_at', 'office_distribution_lists_by', "Verteilerlisten als aktualisiert markiert."),
            "office_phone_list_done": ('office_phone_list_at', 'office_phone_list_by', "Telefonliste als ergänzt markiert."),
            "office_birthday_calendar_done": ('office_birthday_calendar_at', 'office_birthday_calendar_by', "Geburtstagskalender als ergänzt markiert."),
            "office_welcome_gift_done": ('office_welcome_gift_at', 'office_welcome_gift_by', "Begrüßungsgeschenk als bereit markiert."),
            "office_business_cards_done": ('office_business_cards_at', 'office_business_cards_by', "Visitenkarten als bestellt markiert."),
            "office_organigram_done": ('office_organigram_at', 'office_organigram_by', "Organigramm als ergänzt markiert."),
            "office_homepage_update_done": ('office_homepage_updated_at', 'office_homepage_updated_by', "Änderungen Homepage als durchgeführt markiert.")
        }

        if action in office_tasks_actions:
            db_field_at = office_tasks_actions[action][0]
            task_already_done_value = request_item.get(db_field_at)

            logger.debug(f"Prüfe Aufgabe: Action='{action}', DB-Feld='{db_field_at}', Wert in DB='{task_already_done_value}' (Typ: {type(task_already_done_value)})")

            if not task_already_done_value: # Prüft auf None oder leeren String (beides "falsy")
                at_field, by_field, msg = office_tasks_actions[action]
                update_fields[at_field] = now_str
                update_fields[by_field] = current_user
                success_message = msg
                action_processed = True
                logger.info(f"Aktion '{action}' für Antrag {request_id} wird verarbeitet.")
            else:
                logger.warning(f"Aktion '{action}' für Antrag {request_id} nicht verarbeitet, da Aufgabe '{db_field_at}' bereits erledigt ist (Wert: '{task_already_done_value}').")

        elif action == "mayor_appointment_set":
            mayor_appt_date_str = request.form.get("mayor_appt_date")
            if not mayor_appt_date_str:
                flash("Bitte geben Sie Datum und Uhrzeit für den Bürgermeistertermin ein.", "danger")
                action_processed = False # Wichtig: action_processed explizit auf False setzen bei Fehler
            else:
                try:
                    dt_obj = datetime.strptime(mayor_appt_date_str, '%Y-%m-%dT%H:%M')
                    update_fields['office_mayor_appt_date'] = dt_obj.strftime('%Y-%m-%d %H:%M:%S')
                    update_fields['office_mayor_appt_confirmed_at'] = now_str
                    update_fields['office_mayor_appt_confirmed_by'] = current_user
                    success_message = f"Bürgermeistertermin für den {dt_obj.strftime('%d.%m.%Y um %H:%M Uhr')} bestätigt/aktualisiert."
                    action_processed = True # Hier auf True setzen, da Aktion gültig ist
                    logger.info(f"Aktion 'mayor_appointment_set' für Antrag {request_id} wird verarbeitet.")
                except ValueError:
                    flash("Ungültiges Datums-/Zeitformat für Bürgermeistertermin.", "danger")
                    action_processed = False # Wichtig: action_processed explizit auf False setzen bei Fehler

        if not action_processed and action:
            # Diese Meldung erscheint, wenn action_processed nicht auf True gesetzt wurde
            # UND eine Aktion vorhanden war (d.h. action war nicht None oder leer)
            logger.warning(f"Flash-Nachricht 'Ungültige Aktion oder Status bereits gesetzt' wird für Action '{action}' (Antrag {request_id}) angezeigt. action_processed={action_processed}")
            flash("Ungültige Aktion oder Status bereits gesetzt für Vorzimmer-Aufgabe.", "warning")
        elif update_fields and success_message:
            conn_db_office = None
            try:
                conn_db_office = sqlite3.connect("db/onoffboarding.db"); c = conn_db_office.cursor()
                set_clause_parts = [f"{k} = ?" for k in update_fields.keys()]
                values_office = list(update_fields.values())
                values_office.append(request_id)
                set_clause_str = ", ".join(set_clause_parts)

                sql_query = f"UPDATE requests SET {set_clause_str} WHERE id = ?"
                logger.debug(f"Führe DB-Update aus: {sql_query} mit Werten {tuple(values_office)}")
                c.execute(sql_query, tuple(values_office))
                conn_db_office.commit()

                if c.rowcount > 0:
                    flash(success_message, "success")
                    logger.info(f"Antrag {request_id}: Vorzimmer-Status '{action}' durch '{current_user}' erfolgreich gesetzt.")
                    updated_item = get_request_item_as_dict(request_id) # Erneut laden für check_all_subprocesses_done
                    if updated_item:
                        check_all_subprocesses_done(updated_item)
                else:
                    # Dieser Fall sollte selten eintreten, wenn die Logik oben stimmt
                    flash("Update nicht erfolgreich (keine Zeile geändert, möglicherweise war der Status bereits so).", "warning")
                    logger.warning(f"DB-Update für Antrag {request_id} (Action '{action}') hat keine Zeilen geändert.")
            except sqlite3.Error as e:
                logger.error(f"DB-Fehler beim Setzen des Vorzimmer-Status für Antrag {request_id}: {e}", exc_info=True)
                flash("DB Fehler beim Aktualisieren des Status.", "danger")
            finally:
                if conn_db_office: conn_db_office.close()

        return redirect(url_for('update_office_status', request_id=request_id))

    # Für GET Requests
    return render_template("update_office_status.html", request_item=request_item)


@app.route("/manual_complete/<int:request_id>", methods=["POST"])
@require_ad_group("AD_GROUP") # Nur generelle Admins dÃ¼rfen manuell abschlieÃŸen
def manual_complete_request(request_id):
    current_user = session.get("user", "System")
    logger.info(f"âž¡ï¸ Manueller Abschluss fÃ¼r Antrag ID: {request_id} durch User '{current_user}' angefordert.")
    request_item_for_check = get_request_item_as_dict(request_id)
    if not request_item_for_check:
        flash(f"Antrag {request_id} nicht gefunden.", "danger")
        return redirect(url_for("admin"))
    if request_item_for_check.get('status') != 'in_bearbeitung':
        flash(f"Antrag {request_id} kann nicht manuell abgeschlossen werden (Status: {request_item_for_check.get('status')}).", "warning")
        return redirect(url_for("view_request", request_id=request_id))
    is_fully_completed, pending_tasks = check_all_subprocesses_done(request_item_for_check)
    if is_fully_completed:
        if update_request_status(request_id, "abgeschlossen", expected_current_status="in_bearbeitung"):
            flash(f"Antrag {request_id} wurde erfolgreich manuell abgeschlossen.", "success")
            logger.info(f"Antrag {request_id} manuell auf 'abgeschlossen' gesetzt durch '{current_user}'.")
        else:
            flash(f"Fehler beim manuellen AbschlieÃŸen von Antrag {request_id}.", "danger")
    else:
        flash(f"Antrag {request_id} kann noch nicht abgeschlossen werden. Offene Punkte: {', '.join(pending_tasks) if pending_tasks else 'Keine identifiziert'}", "warning")
    return redirect(url_for("view_request", request_id=request_id))


@app.route("/print_request_combined/<int:request_id>")
#@require_ad_group("AD_GROUP") # Beispielhafter Zugriffsschutz
def print_request_combined(request_id):
    # Die PrÃ¼fung auf Bibliotheken kann hier entfallen, wenn sie in pdf_generator.py erfolgt
    # und eine Exception wirft oder None zurÃ¼ckgibt.

    request_item = get_request_item_as_dict(request_id)
    if not request_item:
        flash(f"Antrag {request_id} nicht gefunden.", "danger")
        return redirect(url_for("admin"))

    try:
        # Die Funktion aus pdf_generator aufrufen
        # request.url_root liefert die Basis-URL (z.B. http://localhost:5000/)
        merged_pdf_stream = pdf_generator.create_combined_pdf(
            request_item,
            concrete_upload_folder, # Aus app.py Ã¼bergeben
            request.url_root, # Basis-URL fÃ¼r relative Pfade in HTML/CSS
            pdf_template_name="view_for_pdf.html" # Name des PDF-Templates
        )

        if merged_pdf_stream is None: # Falls create_combined_pdf None bei Fehler zurÃ¼ckgibt
            flash("Fehler bei der PDF-Erstellung (intern).", "danger")
            return redirect(url_for('view_request', request_id=request_id))

        return send_file(
            merged_pdf_stream,
            as_attachment=False,
            download_name=f'Antrag_{request_id}_komplett.pdf',
            mimetype='application/pdf'
        )
    except ImportError: # FÃ¤ngt den Fehler ab, falls WeasyPrint/pypdf nicht da sind
         flash("PDF-Erstellungsbibliotheken sind nicht verfÃ¼gbar. Bitte Admin kontaktieren.", "danger")
         return redirect(url_for('view_request', request_id=request_id))
    except Exception as e:
        logger.error(f"Schwerwiegender Fehler bei der Erstellung des kombinierten PDFs fÃ¼r Antrag {request_id}: {e}", exc_info=True)
        flash(f"Schwerwiegender Fehler bei der PDF-Erstellung: {e}", "danger")
        return redirect(url_for('view_request', request_id=request_id))


# init_db() Funktion bleibt unverÃ¤ndert
def init_db():
    db_dir = 'db'; db_file = os.path.join(db_dir, 'onoffboarding.db'); conn_db = None
    try:
        if not os.path.exists(db_dir):
            os.makedirs(db_dir)
            logger.info(f"Verzeichnis '{db_dir}' erstellt fÃ¼r Datenbank.")
        conn_db = sqlite3.connect(db_file); c = conn_db.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS requests (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, lastname TEXT NOT NULL, firstname TEXT NOT NULL,
                        birthdate TEXT, job_title TEXT,
                        startdate TEXT, enddate TEXT, department TEXT, supervisor TEXT,
                        hardware_computer TEXT, hardware_monitor TEXT, hardware_accessories TEXT, hardware_mobile TEXT,
                        comments TEXT, referenceuser TEXT, process_type TEXT NOT NULL DEFAULT 'onboarding',
                        status TEXT NOT NULL DEFAULT 'offen', role TEXT, department_dn TEXT,
                        key_required BOOLEAN DEFAULT 0, required_windows BOOLEAN DEFAULT 0,
                        hardware_required BOOLEAN DEFAULT 0, email_account_required BOOLEAN DEFAULT 0,
                        needs_fixed_phone BOOLEAN DEFAULT 0, needs_ris_access BOOLEAN DEFAULT 0,
                        needs_cipkom_access BOOLEAN DEFAULT 0, other_software_notes TEXT,
                        cipkom_reference_user TEXT, room_number TEXT,
                        workplace_needs_new_table BOOLEAN DEFAULT 0, workplace_needs_new_chair BOOLEAN DEFAULT 0,
                        workplace_needs_monitor_arms BOOLEAN DEFAULT 0, workplace_no_new_equipment BOOLEAN DEFAULT 0,
                        needs_office_notification BOOLEAN DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        n8n_ad_creation_status TEXT, n8n_ad_username_created TEXT,
                        n8n_ad_initial_password TEXT, n8n_ad_status_message TEXT,
                        n8n_hardware_status_message TEXT,
                        hw_status_ordered_at TEXT, hw_status_ordered_by TEXT,
                        hw_status_delivered_at TEXT, hw_status_delivered_by TEXT,
                        hw_status_installed_at TEXT, hw_status_installed_by TEXT,
                        hw_status_setup_done_at TEXT, hw_status_setup_done_by TEXT,
                        n8n_key_status_message TEXT,
                        key_status_prepared_at TEXT, key_status_prepared_by TEXT,
                        key_status_issued_at TEXT, key_status_issued_by TEXT,
                        key_issuance_protocol_filename TEXT,
                        email_creation_notified_at TEXT, email_created_address TEXT,
                        email_creation_confirmed_at TEXT, email_creation_confirmed_by TEXT,
                        n8n_email_status_message TEXT,
                        hr_dienstvereinbarung_at TEXT, hr_dienstvereinbarung_by TEXT,
                        hr_datenschutz_at TEXT, hr_datenschutz_by TEXT,
                        hr_dsgvo_informed_at TEXT, hr_dsgvo_informed_by TEXT,
                        hr_it_directive_at TEXT, hr_it_directive_by TEXT,
                        hr_payroll_sheet_at TEXT, hr_payroll_sheet_by TEXT,
                        hr_security_guidelines_at TEXT, hr_security_guidelines_by TEXT,
                        phone_status_ordered_at TEXT, phone_status_ordered_by TEXT,
                        phone_status_setup_at TEXT, phone_status_setup_by TEXT,
                        phone_number_assigned TEXT,
                        ris_access_status_granted_at TEXT, ris_access_status_granted_by TEXT,
                        cipkom_access_status_granted_at TEXT, cipkom_access_status_granted_by TEXT,
                        n8n_software_status_message TEXT,
                        workplace_table_ordered_at TEXT, workplace_table_ordered_by TEXT,
                        workplace_table_setup_at TEXT, workplace_table_setup_by TEXT,
                        workplace_chair_ordered_at TEXT, workplace_chair_ordered_by TEXT,
                        workplace_chair_setup_at TEXT, workplace_chair_setup_by TEXT,
                        workplace_monitor_arms_ordered_at TEXT, workplace_monitor_arms_ordered_by TEXT,
                        workplace_monitor_arms_setup_at TEXT, workplace_monitor_arms_setup_by TEXT,
                        office_outlook_contact_at TEXT, office_outlook_contact_by TEXT,
                        office_distribution_lists_at TEXT, office_distribution_lists_by TEXT,
                        office_phone_list_at TEXT, office_phone_list_by TEXT,
                        office_birthday_calendar_at TEXT, office_birthday_calendar_by TEXT,
                        office_welcome_gift_at TEXT, office_welcome_gift_by TEXT,
                        office_mayor_appt_date TEXT, office_mayor_appt_confirmed_at TEXT, office_mayor_appt_confirmed_by TEXT,
                        office_business_cards_at TEXT, office_business_cards_by TEXT,
                        office_organigram_at TEXT, office_organigram_by TEXT,
                        office_homepage_updated_at TEXT, office_homepage_updated_by TEXT,
                        aida_access_created_at TEXT, aida_access_created_by TEXT,
                        aida_key_registered_at TEXT, aida_key_registered_by TEXT
                    )''')
        logger.info("Tabelle 'requests' geprÃ¼ft/erstellt.")
        c.execute("PRAGMA table_info(requests)"); columns = [info[1] for info in c.fetchall()]
        required_columns = {
            'birthdate': 'TEXT', 'job_title': 'TEXT',
            'startdate': 'TEXT', 'enddate': 'TEXT', 'department': 'TEXT', 'supervisor': 'TEXT',
            'hardware_computer': 'TEXT', 'hardware_monitor': 'TEXT', 'hardware_accessories': 'TEXT', 'hardware_mobile': 'TEXT',
            'comments': 'TEXT', 'referenceuser': 'TEXT', 'process_type': 'TEXT',
            'status': 'TEXT', 'role': 'TEXT', 'department_dn': 'TEXT',
            'key_required': 'BOOLEAN', 'required_windows': 'BOOLEAN',
            'hardware_required': 'BOOLEAN', 'email_account_required': 'BOOLEAN',
            'needs_fixed_phone': 'BOOLEAN', 'needs_ris_access': 'BOOLEAN',
            'needs_cipkom_access': 'BOOLEAN', 'other_software_notes': 'TEXT',
            'cipkom_reference_user': 'TEXT', 'room_number': 'TEXT',
            'workplace_needs_new_table': 'BOOLEAN', 'workplace_needs_new_chair': 'BOOLEAN',
            'workplace_needs_monitor_arms': 'BOOLEAN', 'workplace_no_new_equipment': 'BOOLEAN',
            'needs_office_notification': 'BOOLEAN',
            'created_at': 'TIMESTAMP',
            'n8n_ad_creation_status': 'TEXT', 'n8n_ad_username_created': 'TEXT',
            'n8n_ad_initial_password': 'TEXT', 'n8n_ad_status_message': 'TEXT',
            'n8n_hardware_status_message': 'TEXT',
            'hw_status_ordered_at': 'TEXT', 'hw_status_ordered_by': 'TEXT',
            'hw_status_delivered_at': 'TEXT', 'hw_status_delivered_by': 'TEXT',
            'hw_status_installed_at': 'TEXT', 'hw_status_installed_by': 'TEXT',
            'hw_status_setup_done_at': 'TEXT', 'hw_status_setup_done_by': 'TEXT',
            'n8n_key_status_message': 'TEXT',
            'key_status_prepared_at': 'TEXT', 'key_status_prepared_by': 'TEXT',
            'key_status_issued_at': 'TEXT', 'key_status_issued_by': 'TEXT',
            'key_issuance_protocol_filename': 'TEXT',
            'email_creation_notified_at': 'TEXT', 'email_created_address': 'TEXT',
            'email_creation_confirmed_at': 'TEXT', 'email_creation_confirmed_by': 'TEXT',
            'n8n_email_status_message': 'TEXT',
            'hr_dienstvereinbarung_at': 'TEXT', 'hr_dienstvereinbarung_by': 'TEXT',
            'hr_datenschutz_at': 'TEXT', 'hr_datenschutz_by': 'TEXT',
            'hr_dsgvo_informed_at': 'TEXT', 'hr_dsgvo_informed_by': 'TEXT',
            'hr_it_directive_at': 'TEXT', 'hr_it_directive_by': 'TEXT',
            'hr_payroll_sheet_at': 'TEXT', 'hr_payroll_sheet_by': 'TEXT',
            'hr_security_guidelines_at': 'TEXT', 'hr_security_guidelines_by': 'TEXT',
            'phone_status_ordered_at': 'TEXT', 'phone_status_ordered_by': 'TEXT',
            'phone_status_setup_at': 'TEXT', 'phone_status_setup_by': 'TEXT',
            'phone_number_assigned': 'TEXT',
            'ris_access_status_granted_at': 'TEXT', 'ris_access_status_granted_by': 'TEXT',
            'cipkom_access_status_granted_at': 'TEXT', 'cipkom_access_status_granted_by': 'TEXT',
            'n8n_software_status_message': 'TEXT',
            'workplace_table_ordered_at': 'TEXT', 'workplace_table_ordered_by': 'TEXT',
            'workplace_table_setup_at': 'TEXT', 'workplace_table_setup_by': 'TEXT',
            'workplace_chair_ordered_at': 'TEXT', 'workplace_chair_ordered_by': 'TEXT',
            'workplace_chair_setup_at': 'TEXT', 'workplace_chair_setup_by': 'TEXT',
            'workplace_monitor_arms_ordered_at': 'TEXT', 'workplace_monitor_arms_ordered_by': 'TEXT',
            'workplace_monitor_arms_setup_at': 'TEXT', 'workplace_monitor_arms_setup_by': 'TEXT',
            'office_outlook_contact_at': 'TEXT', 'office_outlook_contact_by': 'TEXT',
            'office_distribution_lists_at': 'TEXT', 'office_distribution_lists_by': 'TEXT',
            'office_phone_list_at': 'TEXT', 'office_phone_list_by': 'TEXT',
            'office_birthday_calendar_at': 'TEXT', 'office_birthday_calendar_by': 'TEXT',
            'office_welcome_gift_at': 'TEXT', 'office_welcome_gift_by': 'TEXT',
            'office_mayor_appt_date': 'TEXT', 'office_mayor_appt_confirmed_at': 'TEXT', 'office_mayor_appt_confirmed_by': 'TEXT',
            'office_business_cards_at': 'TEXT', 'office_business_cards_by': 'TEXT',
            'office_organigram_at': 'TEXT', 'office_organigram_by': 'TEXT',
            'office_homepage_updated_at': 'TEXT', 'office_homepage_updated_by': 'TEXT',
            'aida_access_created_at': 'TEXT', 'aida_access_created_by': 'TEXT',
            'aida_key_registered_at': 'TEXT', 'aida_key_registered_by': 'TEXT'
        }
        for col, col_type in required_columns.items():
            if col not in columns:
                try:
                    c.execute(f"ALTER TABLE requests ADD COLUMN {col} {col_type}")
                    logger.info(f"Spalte '{col}' zur Tabelle 'requests' hinzugefÃ¼gt.")
                except sqlite3.OperationalError as e:
                    logger.warning(f"Konnte Spalte '{col}' nicht hinzufÃ¼gen: {e}")
        conn_db.commit(); logger.info("âœ… Datenbank erfolgreich initialisiert/geprÃ¼ft.")
    except sqlite3.Error as e: logger.error(f"âŒ SQLite Fehler DB-Init ({db_file}): {e}", exc_info=True); sys.exit(f"Kritischer DB Fehler: {e}")
    except Exception as e: logger.error(f"âŒ Allgemeiner Fehler DB-Init: {e}", exc_info=True); sys.exit(f"Kritischer Initialisierungsfehler: {e}")
    finally:
        if conn_db: conn_db.close()

if __name__ == '__main__':
    logger.info("ðŸš€ Starte app.py (aus __main__)...")
    init_db()
    flask_debug = os.getenv("FLASK_DEBUG", "False").lower() in ("true", "1", "t")
    logger.info(f"Starte Flask App im Debug-Modus: {flask_debug}")
    app.run(host="0.0.0.0", port=5000, debug=flask_debug)

