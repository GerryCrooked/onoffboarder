import os
from ldap3 import Server, Connection, ALL, LEVEL, SUBTREE, BASE 
from ldap3.core.exceptions import LDAPException
import logging

logger = logging.getLogger(__name__)

def get_ad_connection():
    server_uri = os.getenv("AD_SERVER")
    ad_user = os.getenv("AD_USER")
    ad_password = os.getenv("AD_PASSWORD")
    use_ssl_str = os.getenv("AD_USE_SSL", "false").lower()

    if not server_uri:
        logger.error("AD_SERVER nicht in Umgebungsvariablen gesetzt!")
        raise ValueError("AD_SERVER fehlt in der Konfiguration.")
    if not ad_user or not ad_password:
        logger.error("AD_USER oder AD_PASSWORD nicht in Umgebungsvariablen gesetzt! Diese werden für allgemeine AD-Abfragen benötigt.")
        raise ValueError("AD-Anmeldeinformationen für den Service-Account fehlen in der Konfiguration.")

    use_ssl = use_ssl_str == "true"
    prefix = "ldaps://" if use_ssl else "ldap://"
    if not server_uri.lower().startswith(("ldap://", "ldaps://")):
        server_uri = prefix + server_uri
    
    logger.debug(f"Verbinde mit AD-Server: {server_uri}, SSL: {use_ssl} mit Benutzer: {ad_user}")

    server = Server(
        server_uri,
        get_info=ALL,
        use_ssl=use_ssl
    )
    
    try:
        conn = Connection(
            server,
            user=ad_user,
            password=ad_password,
            authentication="SIMPLE",
            auto_bind=True,
            raise_exceptions=True
        )
        logger.debug("AD-Verbindung (Service-Account) erfolgreich hergestellt und gebunden.")
        return conn
    except Exception as e:
        logger.error(f"Fehler beim Herstellen der AD-Verbindung oder Bind mit Service-Benutzer {ad_user}: {e}", exc_info=True)
        raise 

def parse_exclude_ous():
    val = os.getenv("AD_OU_EXCLUDE", "")
    paths = [p.strip().split(",") for p in val.split(";") if p.strip()]
    logger.debug(f"Geparste Ausschluss-Pfade für OUs: {paths}")
    return paths

def should_exclude(path, excludes):
    for ex_path_list in excludes:
        if len(ex_path_list) <= len(path) and path[:len(ex_path_list)] == ex_path_list:
            logger.debug(f"Pfad {path} wird durch Ausschlussregel {ex_path_list} ausgeschlossen.")
            return True
    return False

def build_ou_tree(base_dn, current_path=None, exclude_paths=None, depth=0, max_depth=5):
    if exclude_paths is None: exclude_paths = parse_exclude_ous()
    if current_path is None: current_path = []
    if depth > max_depth:
        logger.debug(f"Maximale Tiefe {max_depth} für OU-Baum bei Pfad {current_path} erreicht.")
        return []

    conn = None
    try:
        conn = get_ad_connection()
        conn.search(
            search_base=base_dn,
            search_filter='(objectClass=organizationalUnit)',
            search_scope=LEVEL,
            attributes=['distinguishedName', 'name']
        )
        result = []
        for entry in conn.entries:
            ou_name = getattr(entry, 'name', [''])[0] if getattr(entry, 'name', []) else str(entry.entry_dn).split(',')[0].split('=')[1]
            dn = entry.distinguishedName.value
            path = current_path + [ou_name]
            if should_exclude(path, exclude_paths): continue
            children = build_ou_tree(dn, path, exclude_paths, depth=depth + 1, max_depth=max_depth)
            node = {"text": ou_name, "data": {"dn": dn}, "children": children, "state": {"opened": False}}
            result.append(node)
        return result
    except Exception as e:
        logger.error(f"Fehler beim Abrufen des OU-Baums für Basis {base_dn}: {e}", exc_info=True)
        return []
    finally:
        if conn and conn.bound: conn.unbind()

def get_users_in_ou(ou_dn):
    logger.debug(f"Suche Benutzer in OU: {ou_dn}")
    conn = None
    users = []
    try:
        conn = get_ad_connection()
        conn.search(
            search_base=ou_dn,
            search_filter='(&(objectClass=user)(!(objectClass=computer))(!(userAccountControl:1.2.840.113556.1.4.803:=2)))',
            search_scope=LEVEL, 
            attributes=['sAMAccountName', 'displayName', 'mail', 'distinguishedName']
        )
        for entry in conn.entries:
            users.append({
                "displayName": getattr(entry.displayName, "value", str(entry.entry_dn).split(',')[0].split('=')[1]),
                "username": getattr(entry.sAMAccountName, "value", ""),
                "mail": getattr(entry.mail, "value", ""),
                "dn": getattr(entry.distinguishedName, "value", str(entry.entry_dn))
            })
        logger.debug(f"{len(users)} Benutzer in OU {ou_dn} gefunden.")
    except Exception as e:
        logger.error(f"Fehler beim Abrufen von Benutzern aus OU {ou_dn}: {e}", exc_info=True)
    finally:
        if conn and conn.bound: conn.unbind()
    return users

def _get_group_dn(conn, group_cn):
    search_base = os.getenv("AD_SEARCH_BASE")
    if not search_base:
        logger.error("AD_SEARCH_BASE nicht für Gruppensuche gesetzt.")
        return None
    try:
        conn.search(
            search_base=search_base,
            search_filter=f"(&(objectClass=group)(cn={group_cn}))",
            search_scope=SUBTREE,
            attributes=['distinguishedName']
        )
        if conn.entries:
            group_dn = conn.entries[0].distinguishedName.value
            logger.debug(f"DN für Gruppe '{group_cn}' gefunden: {group_dn}")
            return group_dn
        logger.warning(f"Gruppe '{group_cn}' nicht im AD unter {search_base} gefunden.")
    except Exception as e:
        logger.error(f"Fehler bei der Suche nach Gruppen-DN für '{group_cn}': {e}", exc_info=True)
    return None

def get_user_dn_by_samaccountname(conn, user_sam_account_name):
    search_base = os.getenv("AD_SEARCH_BASE")
    if not search_base:
        logger.error("AD_SEARCH_BASE nicht für Benutzersuche (get_user_dn_by_samaccountname) gesetzt.")
        return None
    try:
        if conn.search(search_base=search_base,
                       search_filter=f"(sAMAccountName={user_sam_account_name})",
                       search_scope=SUBTREE,
                       attributes=['distinguishedName']):
            if conn.entries:
                user_dn = conn.entries[0].distinguishedName.value
                logger.debug(f"DN für Benutzer '{user_sam_account_name}' gefunden: {user_dn}")
                return user_dn
        logger.warning(f"Benutzer '{user_sam_account_name}' nicht im AD unter {search_base} für DN-Abfrage gefunden.")
    except Exception as e:
        logger.error(f"Fehler bei der Suche nach Benutzer-DN für '{user_sam_account_name}': {e}", exc_info=True)
    return None

def get_user_details_by_samaccountname(conn, user_sam_account_name, attributes=None):
    """Ruft Benutzerdetails für einen sAMAccountName ab."""
    if attributes is None:
        attributes = ['displayName', 'mail', 'distinguishedName', 'sAMAccountName']
    
    search_base = os.getenv("AD_SEARCH_BASE")
    if not search_base:
        logger.error("AD_SEARCH_BASE nicht für Benutzersuche (get_user_details_by_samaccountname) gesetzt.")
        return None
    try:
        if conn.search(search_base=search_base,
                       search_filter=f"(sAMAccountName={user_sam_account_name})",
                       search_scope=SUBTREE,
                       attributes=attributes):
            if conn.entries:
                entry = conn.entries[0]
                user_details = {attr: getattr(entry, attr).value if getattr(entry, attr) else None for attr in attributes}
                # Speziell für mehrwertige Attribute (obwohl hier nicht direkt verwendet, aber als Beispiel):
                # if 'memberOf' in attributes:
                #     user_details['memberOf'] = [m.value for m in entry.memberOf] if entry.memberOf else []
                logger.debug(f"Details für Benutzer '{user_sam_account_name}' gefunden: {user_details}")
                return user_details
        logger.warning(f"Benutzer '{user_sam_account_name}' nicht im AD unter {search_base} für Detailabfrage gefunden.")
    except Exception as e:
        logger.error(f"Fehler bei der Suche nach Benutzerdetails für '{user_sam_account_name}': {e}", exc_info=True)
    return None


def is_user_member_of_group_by_env_var(user_sam_account_name, group_name_env_var_key):
    target_group_cn = os.getenv(group_name_env_var_key)
    if not target_group_cn:
        logger.error(f"Umgebungsvariable '{group_name_env_var_key}' für Gruppenname nicht gesetzt.")
        # Im Decorator wird dieser Fall abgefangen, hier könnten wir auch eine Exception werfen
        raise ValueError(f"Umgebungsvariable '{group_name_env_var_key}' für Gruppenname nicht gesetzt.")

    if not user_sam_account_name:
        logger.error("Kein Benutzername (user_sam_account_name) für Gruppenprüfung übergeben.")
        return False # Oder ValueError
        
    conn = None
    try:
        conn = get_ad_connection() 
        user_dn = get_user_dn_by_samaccountname(conn, user_sam_account_name)
        if not user_dn: return False 

        target_group_dn = _get_group_dn(conn, target_group_cn)
        if not target_group_dn: return False 

        is_member = conn.search(search_base=user_dn,
                                search_filter=f"(memberOf:1.2.840.113556.1.4.1941:={target_group_dn})",
                                search_scope=BASE,
                                attributes=['cn'])
        logger.debug(f"Gruppenmitgliedschaftsprüfung für User '{user_sam_account_name}' in Gruppe '{target_group_cn}': {'Ja' if is_member else 'Nein'}")
        return bool(is_member)
    except ValueError as ve: # Fängt ValueErrors von get_ad_connection (fehlende Konfig) oder target_group_cn
        logger.error(f"Konfigurationsfehler (ValueError) bei Gruppenmitgliedschaftsprüfung: {ve}", exc_info=False) # exc_info=False, da der Fehler klar ist
        raise # Erneut auslösen, damit der Decorator es fangen kann
    except LDAPException as e: 
        logger.error(f"LDAP-Fehler: User '{user_sam_account_name}', Gruppe via ENV '{group_name_env_var_key}' (CN '{target_group_cn}'): {e}", exc_info=True)
        return False # Bei LDAP-Fehlern während der Prüfung eher False zurückgeben als Exception
    except Exception as e: 
        logger.error(f"Allg. Fehler: User '{user_sam_account_name}', Gruppe via ENV '{group_name_env_var_key}' (CN '{target_group_cn}'): {e}", exc_info=True)
        return False # Bei unerwarteten Fehlern False
    finally:
        if conn and conn.bound: conn.unbind()

def get_supervisors_in_ou(ou_dn, search_scope=SUBTREE):
    group_cn = os.getenv("SUPERVISOR_GROUP", "vorgesetzter") # Nutzt die ENV_SUPERVISOR_GROUP
    logger.debug(f"Suche Vorgesetzte (Gruppe: '{group_cn}') in OU: {ou_dn}, Scope: {search_scope}")
    supervisors = []
    conn = None
    try:
        conn = get_ad_connection()
        supervisor_group_dn = _get_group_dn(conn, group_cn)
        if not supervisor_group_dn: return []
        conn.search(
            search_base=ou_dn,
            search_filter=f"(&(objectClass=user)(!(objectClass=computer))(!(userAccountControl:1.2.840.113556.1.4.803:=2))(memberOf:1.2.840.113556.1.4.1941:={supervisor_group_dn}))",
            search_scope=search_scope,
            attributes=['sAMAccountName', 'displayName', 'mail', 'distinguishedName']
        )
        for entry in conn.entries:
            supervisors.append({
                "displayName": getattr(entry.displayName, "value", str(entry.entry_dn).split(',')[0].split('=')[1]),
                "username": getattr(entry.sAMAccountName, "value", ""),
                "mail": getattr(entry.mail, "value", ""),
                "dn": getattr(entry.distinguishedName, "value", str(entry.entry_dn))
            })
        logger.debug(f"{len(supervisors)} Vorgesetzte in OU {ou_dn} (Gruppe '{group_cn}') gefunden.")
    except Exception as e:
        logger.error(f"Fehler beim Abrufen von Vorgesetzten aus OU {ou_dn}: {e}", exc_info=True)
    finally:
        if conn and conn.bound: conn.unbind()
    return supervisors

def get_all_supervisors():
    group_cn = os.getenv("SUPERVISOR_GROUP", "vorgesetzter") # Nutzt die ENV_SUPERVISOR_GROUP
    search_base = os.getenv("AD_SEARCH_BASE")
    if not search_base:
        logger.error("AD_SEARCH_BASE ist nicht für get_all_supervisors gesetzt.")
        return []
    logger.debug(f"Suche alle Vorgesetzte (Gruppe: '{group_cn}') in Basis: {search_base}")
    supervisors = []
    conn = None
    try:
        conn = get_ad_connection()
        supervisor_group_dn = _get_group_dn(conn, group_cn)
        if not supervisor_group_dn: return []
        conn.search(
            search_base=search_base,
            search_filter=f"(&(objectClass=user)(!(objectClass=computer))(!(userAccountControl:1.2.840.113556.1.4.803:=2))(memberOf:1.2.840.113556.1.4.1941:={supervisor_group_dn}))",
            search_scope=SUBTREE,
            attributes=['sAMAccountName', 'displayName', 'mail', 'distinguishedName']
        )
        for entry in conn.entries:
             supervisors.append({
                "displayName": getattr(entry.displayName, "value", str(entry.entry_dn).split(',')[0].split('=')[1]),
                "username": getattr(entry.sAMAccountName, "value", ""), # sAMAccountName ist der Loginname
                "mail": getattr(entry.mail, "value", ""),
                "dn": getattr(entry.distinguishedName, "value", str(entry.entry_dn))
            })
        logger.debug(f"{len(supervisors)} globale Vorgesetzte (Gruppe '{group_cn}') gefunden.")
    except Exception as e:
        logger.error(f"Fehler beim Abrufen aller Vorgesetzten: {e}", exc_info=True)
    finally:
        if conn and conn.bound: conn.unbind()
    return supervisors
