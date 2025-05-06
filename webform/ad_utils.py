import os
from ldap3 import Server, Connection, ALL, NTLM, SUBTREE

def get_ad_connection():
    server = Server(os.getenv("AD_SERVER"), get_info=ALL)
    conn = Connection(
        server,
        user=os.getenv("AD_USER"),
        password=os.getenv("AD_PASSWORD"),
        authentication=NTLM,
        auto_bind=True
    )
    return conn

def get_ous(base_dn):
    conn = get_ad_connection()
    conn.search(search_base=base_dn,
                search_filter='(objectClass=organizationalUnit)',
                search_scope=SUBTREE,
                attributes=['name'])
    return [entry['attributes']['name'] for entry in conn.response if 'attributes' in entry]

def get_users_from_ou(ou_dn):
    conn = get_ad_connection()
    conn.search(search_base=ou_dn,
                search_filter='(objectClass=user)',
                search_scope=SUBTREE,
                attributes=['sAMAccountName', 'displayName'])
    return [(entry['attributes']['sAMAccountName'], entry['attributes']['displayName'])
            for entry in conn.response if 'attributes' in entry]
