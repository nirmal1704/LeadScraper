"""firebase_config.py — Firebase Admin SDK initialisation."""
import os
import json
import firebase_admin
from firebase_admin import credentials, firestore, auth

_app = None


def get_firebase_app():
    global _app
    if _app:
        return _app
    sa_json = os.getenv("FIREBASE_SERVICE_ACCOUNT")
    cred = credentials.Certificate(json.loads(sa_json)) if sa_json else credentials.Certificate("serviceAccountKey.json")
    _app = firebase_admin.initialize_app(cred)
    return _app


def get_db():
    get_firebase_app()
    return firestore.client()


def verify_token(id_token: str) -> dict:
    get_firebase_app()
    return auth.verify_id_token(id_token)
