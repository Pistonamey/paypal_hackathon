"""Persistence: Firestore (and optionally Cloud Storage) when Firebase is configured, memory otherwise.

Firestore layout:
  orders/{order_id}                   the order: mandate, offers, signed mandates, PayPal state
  orders/{order_id}/messages/{seq}    the agent conversation and payment events, ordered by `seq`
  counters/{order_id}                 next message number, incremented in a transaction
  sellers/{seller_id}                 reliability stats, kept by the buyer agent
  seller_sessions/{seller}_{order}    each seller agent's PRIVATE negotiation state (floor price etc.)
  evidence/{evidence_id}              a damage photo: hash and metadata, plus either the compressed
                                      image bytes or the path of the original in Cloud Storage

Only this backend reads or writes these (through the Admin SDK). Firestore security rules should
deny all client access; the browser never talks to Firebase directly.
"""
import base64
import copy
import json
import logging
import threading
import time
from typing import Any

from . import config

log = logging.getLogger("procuro.store")

MAX_DOC_BYTES = 1_000_000  # Firestore's limit is 1 MiB per document, including field names


def firestore_safe(value: Any) -> Any:
    """Make a value storable in Firestore.

    Firestore rejects arrays directly inside arrays and non-string map keys. Model output
    (damage assessments, explanations) is free-form, so normalise it before writing.
    """
    if isinstance(value, dict):
        return {str(k) if str(k) else "_": firestore_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [{"items": firestore_safe(list(v))} if isinstance(v, (list, tuple)) else firestore_safe(v)
                for v in value]
    if isinstance(value, float) and value != value:  # NaN
        return None
    return value


# --------------------------------------------------------------------------- memory

class MemoryStore:
    kind = "memory"
    photo_storage = "memory"

    def __init__(self) -> None:
        self._orders: dict[str, dict] = {}
        self._messages: dict[str, list[dict]] = {}
        self._docs: dict[str, dict] = {}
        self._blobs: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def save_order(self, order: dict) -> None:
        with self._lock:
            self._orders[order["id"]] = copy.deepcopy(order)

    def get_order(self, order_id: str) -> dict | None:
        with self._lock:
            o = self._orders.get(order_id)
            return copy.deepcopy(o) if o else None

    def list_orders(self, limit: int = 1000) -> list[dict]:
        with self._lock:
            return sorted((copy.deepcopy(o) for o in self._orders.values()),
                          key=lambda o: o.get("created_at", 0), reverse=True)[:limit]

    def list_orders_by_status(self, status: str) -> list[dict]:
        return [o for o in self.list_orders() if o.get("status") == status]

    def add_message(self, order_id: str, msg: dict) -> dict:
        with self._lock:
            lst = self._messages.setdefault(order_id, [])
            msg = {**msg, "seq": len(lst), "ts": time.time()}
            lst.append(msg)
            return copy.deepcopy(msg)

    def list_messages(self, order_id: str, after: int = -1) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(m) for m in self._messages.get(order_id, []) if m["seq"] > after]

    def get_doc(self, collection: str, doc_id: str) -> dict:
        with self._lock:
            return copy.deepcopy(self._docs.get(f"{collection}/{doc_id}", {}))

    def set_doc(self, collection: str, doc_id: str, data: dict) -> None:
        with self._lock:
            self._docs[f"{collection}/{doc_id}"] = copy.deepcopy(data)

    def save_evidence(self, record: dict, original: bytes, compressed: bytes) -> dict:
        with self._lock:
            self._blobs[record["id"]] = compressed
            self._docs[f"evidence/{record['id']}"] = {**record, "stored": "memory"}
            return copy.deepcopy(self._docs[f"evidence/{record['id']}"])

    def load_evidence(self, evidence_id: str) -> tuple[dict, bytes] | None:
        with self._lock:
            rec = self._docs.get(f"evidence/{evidence_id}")
            data = self._blobs.get(evidence_id)
            return (copy.deepcopy(rec), data) if rec and data is not None else None


# --------------------------------------------------------------------------- firestore

def parse_service_account(raw: str) -> dict:
    """Read the service-account key from FIREBASE_CREDENTIALS_JSON, however it was pasted.

    Accepts the key file's JSON on one line (quoted or not), in double quotes (where .env turns
    the key's \\n into real line breaks), split over several lines inside quotes, or base64-encoded.
    """
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1].strip()
    if text == "{":
        raise RuntimeError("FIREBASE_CREDENTIALS_JSON only has its first line ('{'). The key was pasted over "
                           "several lines without quotes. Put it on one line, or wrap the whole value in single quotes.")
    if not text.startswith("{"):
        try:
            text = base64.b64decode(text, validate=True).decode()
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError("FIREBASE_CREDENTIALS_JSON doesn't look like the key file's JSON. It should start "
                               "with {\"type\": \"service_account\", ...") from None
    try:
        info = json.loads(text, strict=False)  # strict=False allows real line breaks inside the private key
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"FIREBASE_CREDENTIALS_JSON isn't valid JSON ({exc.msg} at character {exc.pos}). "
                           "Paste the whole key file, or use FIREBASE_CREDENTIALS_PATH.") from exc
    missing = [k for k in ("type", "project_id", "private_key", "client_email") if not info.get(k)]
    if missing or info.get("type") != "service_account":
        raise RuntimeError("FIREBASE_CREDENTIALS_JSON isn't a service-account key (missing "
                           f"{', '.join(missing) or 'type: service_account'}). Download it from Project settings "
                           "→ Service accounts → Generate new private key.")
    # A key copied through some tools ends up with "\\n" written out literally: restore the line breaks.
    if "\\n" in info["private_key"]:
        info["private_key"] = info["private_key"].replace("\\n", "\n")
    return info


def _load_credentials():
    from firebase_admin import credentials

    if config.FIREBASE_CREDENTIALS_JSON:
        return credentials.Certificate(parse_service_account(config.FIREBASE_CREDENTIALS_JSON))
    if config.FIREBASE_CREDENTIALS_PATH:
        return credentials.Certificate(config.FIREBASE_CREDENTIALS_PATH)
    return credentials.ApplicationDefault()


class FirestoreStore:
    kind = "firestore"

    def __init__(self, client=None, bucket=None) -> None:
        """`client` / `bucket` can be injected (tests); otherwise built from the configured key."""
        if client is None:
            import firebase_admin
            from firebase_admin import firestore, storage

            if not firebase_admin._apps:
                firebase_admin.initialize_app(_load_credentials())
            client = firestore.client()
            if config.FIREBASE_STORAGE_BUCKET:
                bucket = storage.bucket(config.FIREBASE_STORAGE_BUCKET)
        self.db = client
        self.bucket = bucket
        self.photo_storage = "cloud-storage" if bucket is not None else "firestore"

    # --- startup check
    def ping(self) -> None:
        """Write and read one document, so a missing database or wrong key fails at startup."""
        ref = self.db.collection("meta").document("health")
        ref.set({"checked_at": time.time()})
        if not ref.get().exists:
            raise RuntimeError("Firestore write could not be read back")
        if self.bucket is not None and not self.bucket.exists():
            raise RuntimeError(f"Storage bucket {self.bucket.name} does not exist")

    # --- orders
    def save_order(self, order: dict) -> None:
        self.db.collection("orders").document(order["id"]).set(firestore_safe(order))

    def get_order(self, order_id: str) -> dict | None:
        snap = self.db.collection("orders").document(order_id).get()
        return snap.to_dict() if snap.exists else None

    def list_orders(self, limit: int = 1000) -> list[dict]:
        from google.cloud.firestore import Query

        q = self.db.collection("orders").order_by("created_at", direction=Query.DESCENDING).limit(limit)
        return [d.to_dict() for d in q.stream()]

    def list_orders_by_status(self, status: str) -> list[dict]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        q = self.db.collection("orders").where(filter=FieldFilter("status", "==", status))
        return [d.to_dict() for d in q.stream()]

    # --- messages
    def _next_seq(self, order_id: str) -> int:
        """Next message number for an order, safe across several server processes."""
        from google.cloud import firestore

        ref = self.db.collection("counters").document(order_id)

        @firestore.transactional
        def bump(transaction) -> int:
            snap = ref.get(transaction=transaction)
            n = int((snap.to_dict() or {}).get("next", 0)) if snap.exists else 0
            transaction.set(ref, {"next": n + 1})
            return n

        return bump(self.db.transaction())

    def add_message(self, order_id: str, msg: dict) -> dict:
        msg = firestore_safe({**msg, "seq": self._next_seq(order_id), "ts": time.time()})
        (self.db.collection("orders").document(order_id).collection("messages")
         .document(f"{msg['seq']:06d}").set(msg))
        return msg

    def list_messages(self, order_id: str, after: int = -1) -> list[dict]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        q = (self.db.collection("orders").document(order_id).collection("messages")
             .where(filter=FieldFilter("seq", ">", after)).order_by("seq"))
        return [d.to_dict() for d in q.stream()]

    # --- generic docs
    def get_doc(self, collection: str, doc_id: str) -> dict:
        snap = self.db.collection(collection).document(doc_id).get()
        return snap.to_dict() if snap.exists else {}

    def set_doc(self, collection: str, doc_id: str, data: dict) -> None:
        self.db.collection(collection).document(doc_id).set(firestore_safe(data))

    # --- evidence photos
    def save_evidence(self, record: dict, original: bytes, compressed: bytes) -> dict:
        doc = dict(record)
        if self.bucket is not None:
            path = f"evidence/{record['order_id']}/{record['id']}{record['ext']}"
            self.bucket.blob(path).upload_from_string(original, content_type=record["media_type"])
            doc.update({"stored": "cloud-storage", "path": path})
        else:
            if len(compressed) > MAX_DOC_BYTES - 20_000:
                raise ValueError("photo is too large to store even after compression")
            doc.update({"stored": "firestore", "data": compressed})
        self.db.collection("evidence").document(record["id"]).set(doc)
        return {k: v for k, v in doc.items() if k != "data"}

    def load_evidence(self, evidence_id: str) -> tuple[dict, bytes] | None:
        snap = self.db.collection("evidence").document(evidence_id).get()
        if not snap.exists:
            return None
        doc = snap.to_dict()
        if doc.get("stored") == "cloud-storage" and self.bucket is not None:
            data = self.bucket.blob(doc["path"]).download_as_bytes()
            return {**doc, "media_type": doc["media_type"]}, data
        data = doc.pop("data", None)
        if data is None:
            return None
        return {**doc, "media_type": "image/jpeg"}, bytes(data)


# --------------------------------------------------------------------------- choose

STORE_ERROR: str | None = None


def _make_store() -> Any:
    global STORE_ERROR
    configured = (config.FIREBASE_CREDENTIALS_PATH or config.FIREBASE_CREDENTIALS_JSON
                  or config.GOOGLE_APPLICATION_CREDENTIALS)
    if config.STORE_BACKEND == "memory" or not configured:
        if not configured:
            log.warning("Firebase is not configured: orders are kept in memory and lost on restart.")
        return MemoryStore()
    try:
        s = FirestoreStore()
        s.ping()
        log.info("Using Firestore (photos in %s).", s.photo_storage)
        return s
    except Exception as exc:  # noqa: BLE001 - keep the app usable, but say so loudly
        STORE_ERROR = _explain(exc)
        log.error("FIREBASE NOT WORKING, falling back to memory (data will be lost on restart): %s", STORE_ERROR)
        return MemoryStore()


def _explain(exc: Exception) -> str:
    text = str(exc)
    if "does not exist" in text and "database" in text.lower():
        return "The Firestore database hasn't been created yet. In the Firebase console open Firestore Database and click Create database."
    if "PERMISSION_DENIED" in text or "403" in text:
        return f"The service account isn't allowed to use Firestore (check it's from the right project). Details: {text[:300]}"
    if "No such file" in text or "FileNotFoundError" in type(exc).__name__:
        return f"The credentials file wasn't found: {config.FIREBASE_CREDENTIALS_PATH or config.GOOGLE_APPLICATION_CREDENTIALS}"
    return f"{type(exc).__name__}: {text[:400]}"


store = _make_store()