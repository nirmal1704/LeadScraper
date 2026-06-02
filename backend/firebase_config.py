"""
firebase_config.py — Firebase Admin SDK initialisation.
"""
import os
import json
import firebase_admin
from firebase_admin import credentials, firestore, storage, auth

_app = None

def get_firebase_app():
    global _app
    if _app:
        return _app

    # On Render: set FIREBASE_SERVICE_ACCOUNT env var as the JSON string
    sa_json = os.getenv("FIREBASE_SERVICE_ACCOUNT")
    if sa_json:
        cred = credentials.Certificate(json.loads(sa_json))
    else:
        # Local dev: put serviceAccountKey.json in backend/
        cred = credentials.Certificate("serviceAccountKey.json")

    _app = firebase_admin.initialize_app(cred)
    return _app


def get_db():
    get_firebase_app()
    return firestore.client()


def get_bucket():
    get_firebase_app()
    return storage.bucket()


def verify_token(id_token: str) -> dict:
    """Verify Firebase ID token and return decoded claims."""
    get_firebase_app()
    return auth.verify_id_token(id_token)
