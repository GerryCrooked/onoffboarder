import os
from ldap3 import Server, Connection, ALL, LEVEL

def get_ad_connection():
    server = Server(
        os.getenv("AD_SERVER"),
        get_info=ALL,
        use_ssl=os.getenv("AD_USE_SSL", "false").lower() == "true"
    )

    conn = Connection(
        server,
        user=os.getenv("AD_USER"),
        password=os.getenv("AD_PASSWORD"),
        authentication="SIMPLE",
        auto_bind=True
    )
    return conn

def should_exclude(path, excludes):
    for ex in excludes:
        if path[:len(ex)] == ex:
            return True
    return False

def build_ou_tree(base_dn, current_path=None, exclude_paths=None, depth=0, max_depth=5):
    if exclude_paths is None:
        exclude_paths = []
    if current_path is None:
        current_path = []

    if depth > max_depth:
        return []

    conn = get_ad_connection()
    conn.search(
        search_base=base_dn,
        search_filter='(objectClass=organizationalUnit)',
        search_scope=LEVEL,
        attributes=['distinguishedName', 'name']
    )

    result = []

    for entry in conn.entries:
        dn = entry.distinguishedName.value
        name = entry.name.value
        path = current_path + [name]
        if should_exclude(path, exclude_paths):
            continue

        subtree_dn = dn
        children = build_ou_tree(subtree_dn, path, exclude_paths, depth=depth + 1, max_depth=max_depth)
        node = {
            "text": name,
            "data": {"dn": dn},
            "children": children,
            "state": {"opened": False}
        }
        result.append(node)

    return result

def get_users_in_ou(ou_dn):
    conn = get_ad_connection()
    conn.search(
        search_base=ou_dn,
        search_filter='(&(objectClass=user)(!(objectClass=computer)))',
        search_scope=LEVEL,
        attributes=['sAMAccountName', 'displayName']
    )
    users = []
    for entry in conn.entries:
        display = entry.displayName.value if hasattr(entry.displayName, "value") else "Unbekannt"
        sam = entry.sAMAccountName.value if hasattr(entry.sAMAccountName, "value") else "N/A"
        users.append({
            "displayName": display,
            "username": sam
        })
    return users

def parse_exclude_ous():
    val = os.getenv("AD_OU_EXCLUDE", "")
    paths = [p.strip().split(",") for p in val.split(";") if p.strip()]
    return paths

def get_supervisors_in_ou(ou_dn):
    groupname = os.getenv("SUPERVISOR_GROUP", "vorgesetzter")
    conn = get_ad_connection()

    # Finde DN der Gruppe „vorgesetzter“
    conn.search(
        search_base=os.getenv("AD_SEARCH_BASE"),
        search_filter=f"(&(objectClass=group)(cn={groupname}))",
        attributes=["member"]
    )

    if not conn.entries or "member" not in conn.entries[0]:
        return []

    group_members_dns = set(str(dn) for dn in conn.entries[0]["member"])

    # Suche alle Benutzer in der angegebenen OU
    conn.search(
        search_base=ou_dn,
        search_filter='(&(objectClass=user)(!(objectClass=computer)))',
        search_scope=LEVEL,
        attributes=['sAMAccountName', 'displayName', 'distinguishedName', 'mail']
    )

    supervisors = []
    for entry in conn.entries:
        user_dn = str(entry.distinguishedName.value)
        if user_dn in group_members_dns:
            supervisors.append({
                "displayName": entry.displayName.value if hasattr(entry.displayName, "value") else "Unbekannt",
                "username": entry.sAMAccountName.value if hasattr(entry.sAMAccountName, "value") else "N/A",
                "email": entry.mail.value if hasattr(entry.mail, "value") else ""
            })

    return supervisors


def get_all_supervisors():
    groupname = os.getenv("SUPERVISOR_GROUP", "vorgesetzter")
    conn = get_ad_connection()

    conn.search(
        search_base=os.getenv("AD_SEARCH_BASE"),
        search_filter=f"(&(objectClass=group)(cn={groupname}))",
        attributes=["member"]
    )

    if not conn.entries or "member" not in conn.entries[0]:
        return []

    member_dns = conn.entries[0]["member"]
    supervisors = []

    for dn in member_dns:
        conn.search(
            search_base=str(dn),
            search_filter="(objectClass=user)",
            search_scope="BASE",  # wichtig: BASE statt LEVEL!
            attributes=["sAMAccountName", "displayName", "mail", "distinguishedName"]
        )
        if conn.entries:
            user = conn.entries[0]
            supervisors.append({
                "displayName": getattr(user.displayName, "value", "Unbekannt"),
                "username": getattr(user.sAMAccountName, "value", "N/A"),
                "email": getattr(user.mail, "value", ""),
                "dn": getattr(user.distinguishedName, "value", "")
            })

    return supervisors
