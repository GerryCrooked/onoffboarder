import os
import jwt
import datetime

SECRET = os.getenv("TOKEN_SECRET", "supersecrettoken")

def generate_token(user, expires_minutes=30):
    payload = {
        "user": user,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(minutes=expires_minutes)
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")

def decode_token(token):
    try:
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
        return payload["user"]
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None
