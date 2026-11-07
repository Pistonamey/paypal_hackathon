"""This agent's own memory: negotiation sessions (with its PRIVATE cost and floor) and a short activity log.

In memory by default. With FIREBASE_CREDENTIALS_JSON (or FIREBASE_CREDENTIALS_PATH) set it uses Firestore, under its own collection
(STORE_COLLECTION), so a restart doesn't forget a hold that still has to be captured.
"""
import collections
import copy
import json
import threading
import time

from . import config

LOG_SIZE = 200


class MemoryStore:
    kind = "memory"

    def __init__(self):
        self._docs: dict[str, dict] = {}
        self._log: collections.deque = collections.deque(maxlen=LOG_SIZE)
        self._lock = threading.Lock()

    def get(self, key: str) -> dict:
        with self._lock:
            return copy.deepcopy(self._docs.get(key, {}))

    def put(self, key: str, doc: dict) -> None:
        with self._lock:
            self._docs[key] = copy.deepcopy(doc)

    def log(self, entry: dict) -> None:
        with self._lock:
            self._log.appendleft({"at": time.time(), **entry})

    def recent(self, n: int = 60) -> list[dict]:
        with self._lock:
            return list(self._log)[:n]


class FirestoreStore(MemoryStore):
    kind = "firestore"

    def __init__(self, credentials_json: str):
        super().__init__()
        import firebase_admin
        from firebase_admin import credentials, firestore
        info = json.loads(credentials_json)
        name = f"{config.AGENT_ID}-agent"
        try:
            app = firebase_admin.get_app(name)
        except ValueError:
            app = firebase_admin.initialize_app(credentials.Certificate(info), name=name)
        self._db = firestore.client(app)
        self._col = config.STORE_COLLECTION

    def get(self, key: str) -> dict:
        snap = self._db.collection(self._col).document(f"session_{key}").get()
        return snap.to_dict() or {} if snap.exists else {}

    def put(self, key: str, doc: dict) -> None:
        self._db.collection(self._col).document(f"session_{key}").set(doc)

    def log(self, entry: dict) -> None:
        super().log(entry)
        try:
            self._db.collection(self._col).document("log").collection("entries").add({"at": time.time(), **entry})
        except Exception:  # noqa: BLE001 - the log is a convenience; never fail a deal over it
            pass

    def recent(self, n: int = 60) -> list[dict]:
        try:
            from firebase_admin import firestore
            q = (self._db.collection(self._col).document("log").collection("entries")
                 .order_by("at", direction=firestore.Query.DESCENDING).limit(n))
            return [d.to_dict() for d in q.stream()]
        except Exception:  # noqa: BLE001
            return super().recent(n)


store = FirestoreStore(config.FIREBASE_CREDENTIALS_JSON) if config.FIREBASE_CREDENTIALS_JSON else MemoryStore()