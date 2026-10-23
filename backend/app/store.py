"""Persistence. Firestore when credentials are configured, otherwise in memory.

Collections:
  orders/{order_id}                 order document (mandate, offers, payment state)
  orders/{order_id}/messages/{id}   negotiation + event feed, ordered by `seq`
  sellers/{seller_id}               reliability stats (kept by the buyer agent)
  seller_sessions/{seller}_{order}  each seller agent's PRIVATE negotiation state
"""
import copy
import json
import threading
import time
from typing import Any

from . import config


class MemoryStore:
    def __init__(self) -> None:
        self._orders: dict[str, dict] = {}
        self._messages: dict[str, list[dict]] = {}
        self._docs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def save_order(self, order: dict) -> None:
        with self._lock:
            self._orders[order["id"]] = copy.deepcopy(order)

    def get_order(self, order_id: str) -> dict | None:
        with self._lock:
            o = self._orders.get(order_id)
            return copy.deepcopy(o) if o else None

    def list_orders(self) -> list[dict]:
        with self._lock:
            return sorted((copy.deepcopy(o) for o in self._orders.values()),
                          key=lambda o: o.get("created_at", 0), reverse=True)

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


class FirestoreStore:
    def __init__(self) -> None:
        import firebase_admin
        from firebase_admin import credentials, firestore

        if not firebase_admin._apps:
            if config.FIREBASE_CREDENTIALS_JSON:
                cred = credentials.Certificate(json.loads(config.FIREBASE_CREDENTIALS_JSON))
            else:
                cred = credentials.ApplicationDefault()
            firebase_admin.initialize_app(cred)
        self.db = firestore.client()
        self._seq_lock = threading.Lock()
        self._seq: dict[str, int] = {}

    def save_order(self, order: dict) -> None:
        self.db.collection("orders").document(order["id"]).set(order)

    def get_order(self, order_id: str) -> dict | None:
        snap = self.db.collection("orders").document(order_id).get()
        return snap.to_dict() if snap.exists else None

    def list_orders(self) -> list[dict]:
        q = self.db.collection("orders").order_by("created_at", direction="DESCENDING").limit(50)
        return [d.to_dict() for d in q.stream()]

    def _next_seq(self, order_id: str) -> int:
        with self._seq_lock:
            if order_id not in self._seq:
                existing = self.db.collection("orders").document(order_id).collection("messages").count().get()
                self._seq[order_id] = int(existing[0][0].value)
            n = self._seq[order_id]
            self._seq[order_id] = n + 1
            return n

    def add_message(self, order_id: str, msg: dict) -> dict:
        msg = {**msg, "seq": self._next_seq(order_id), "ts": time.time()}
        self.db.collection("orders").document(order_id).collection("messages").document(f"{msg['seq']:05d}").set(msg)
        return msg

    def list_messages(self, order_id: str, after: int = -1) -> list[dict]:
        q = (self.db.collection("orders").document(order_id).collection("messages")
             .where("seq", ">", after).order_by("seq"))
        return [d.to_dict() for d in q.stream()]

    def get_doc(self, collection: str, doc_id: str) -> dict:
        snap = self.db.collection(collection).document(doc_id).get()
        return snap.to_dict() if snap.exists else {}

    def set_doc(self, collection: str, doc_id: str, data: dict) -> None:
        self.db.collection(collection).document(doc_id).set(data)


def _make_store() -> Any:
    if config.FIREBASE_CREDENTIALS_JSON or config.GOOGLE_APPLICATION_CREDENTIALS:
        return FirestoreStore()
    return MemoryStore()


store = _make_store()
