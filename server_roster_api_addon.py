
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
import os, datetime

try:
    from main import db  # reuse firestore client if available
except Exception:
    from firebase_admin import firestore, initialize_app, _apps  # type: ignore
    if not _apps:
        initialize_app()
    db = firestore.client()

router = APIRouter()

def utcnow_iso() -> str:
    return datetime.datetime.utcnow().isoformat() + "Z"

def require_admin(token_header: Optional[str], token_query: Optional[str]) -> None:
    expected = os.getenv("ADMIN_TOKEN", "").strip()
    if not expected:
        return
    provided = (token_header or token_query or "").strip()
    if provided != expected:
        raise HTTPException(401, "Unauthorized")

class RosterUpdate(BaseModel):
    emails: List[str]

class BindToOrg(BaseModel):
    org_id: Optional[str] = None

class InlineRoster(BaseModel):
    emails: List[str]

@router.get("/rosters/{org_id}")
def get_org_roster(org_id: str, x_organizer_token: Optional[str] = Header(default=None), token: Optional[str] = None) -> Dict[str, Any]:
    require_admin(x_organizer_token, token)
    doc = db.collection("org_rosters").document(org_id).get()
    if not doc.exists:
        return {"org_id": org_id, "emails": []}
    data = doc.to_dict() or {}
    return {"org_id": org_id, "emails": data.get("emails", [])}

@router.put("/rosters/{org_id}")
def put_org_roster(org_id: str, payload: RosterUpdate, x_organizer_token: Optional[str] = Header(default=None), token: Optional[str] = None) -> Dict[str, Any]:
    require_admin(x_organizer_token, token)
    emails = sorted({(e or "").strip().lower() for e in payload.emails if (e or "").strip()})
    db.collection("org_rosters").document(org_id).set({"emails": emails, "updatedAt": utcnow_iso()})
    return {"ok": True, "org_id": org_id, "count": len(emails)}

@router.get("/pots/{pot_id}/roster-resolved")
def get_resolved_roster(pot_id: str, x_organizer_token: Optional[str] = Header(default=None), token: Optional[str] = None) -> Dict[str, Any]:
    require_admin(x_organizer_token, token)
    ib = db.collection("pot_roster_inline").document(pot_id).get()
    if ib.exists:
        r = ib.to_dict() or {}
        return {"source": "inline", "emails": r.get("emails", []), "pot_id": pot_id}
    b = db.collection("pot_roster_binding").document(pot_id).get()
    if b.exists:
        org_id = (b.to_dict() or {}).get("org_id")
        if org_id:
            doc = db.collection("org_rosters").document(org_id).get()
            emails = (doc.to_dict() or {}).get("emails", []) if doc.exists else []
            return {"source": "org", "org_id": org_id, "emails": emails, "pot_id": pot_id}
    return {"source": "none", "emails": [], "pot_id": pot_id}

@router.put("/pots/{pot_id}/roster-binding")
def set_roster_binding(pot_id: str, payload: BindToOrg, x_organizer_token: Optional[str] = Header(default=None), token: Optional[str] = None) -> Dict[str, Any]:
    require_admin(x_organizer_token, token)
    if payload.org_id:
        db.collection("pot_roster_binding").document(pot_id).set({"org_id": payload.org_id, "updatedAt": utcnow_iso()})
    else:
        db.collection("pot_roster_binding").document(pot_id).delete()
    return {"ok": True, "pot_id": pot_id, "org_id": payload.org_id}

@router.put("/pots/{pot_id}/roster-inline")
def set_inline_roster(pot_id: str, payload: InlineRoster, x_organizer_token: Optional[str] = Header(default=None), token: Optional[str] = None) -> Dict[str, Any]:
    require_admin(x_organizer_token, token)
    emails = sorted({(e or "").strip().lower() for e in payload.emails if (e or "").strip()})
    if emails:
        db.collection("pot_roster_inline").document(pot_id).set({"emails": emails, "updatedAt": utcnow_iso()})
    else:
        db.collection("pot_roster_inline").document(pot_id).delete()
    return {"ok": True, "pot_id": pot_id, "count": len(emails)}
