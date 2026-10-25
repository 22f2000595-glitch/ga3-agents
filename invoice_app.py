import hashlib
import json
import asyncio
import socket
import subprocess
import logging
import os
import sqlite3
import threading
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response

BASE_URL = os.getenv("A2A_BASE_URL") or ("https://" + os.getenv("RENDER_EXTERNAL_HOSTNAME", "YOUR_PUBLIC_HOST") + "/a2a/")
DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_PATH = os.getenv("INVOICE_DB_PATH", "/data/invoice-agent.sqlite3")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
AI_BASE = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
AI_URL = AI_BASE.rstrip("/") if AI_BASE.rstrip("/").endswith("/chat/completions") else AI_BASE.rstrip("/") + "/chat/completions"
AI_KEY = os.getenv("GEMINI_API_KEY", "")
PROPOSAL_MEDIA = "application/vnd.ga5.invoice-action-proposals+json"
RECEIPT_MEDIA = "application/vnd.ga5.invoice-action-receipts+json"
BATCH_MEDIA = "application/vnd.ga5.invoice-claim-batch+json"
RESULT_MEDIA = "application/vnd.ga5.invoice-action-results+json"
ACTIONS = {"settle_invoice", "request_approval", "hold_invoice", "reject_duplicate", "open_exception"}
app = FastAPI(title="Invoice Action Agent", docs_url=None, redoc_url=None)
lock = threading.RLock()


class DB:
    def __init__(self, conn, postgres=False): self.conn, self.postgres = conn, postgres
    def execute(self, sql, params=()):
        return self.conn.execute(sql.replace("?", "%s") if self.postgres else sql, params)
    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip(): self.execute(statement)
    def __enter__(self): self.conn.__enter__(); return self
    def __exit__(self, *args): return self.conn.__exit__(*args)


def connect():
    if DATABASE_URL:
        import psycopg
        from psycopg.rows import dict_row
        return DB(psycopg.connect(DATABASE_URL, row_factory=dict_row), True)
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return DB(c)


def begin(db): db.execute("BEGIN" if db.postgres else "BEGIN IMMEDIATE")


with connect() as db:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, context_id TEXT NOT NULL, owner TEXT NOT NULL, batch_id TEXT NOT NULL, state TEXT NOT NULL, task_json TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE INDEX IF NOT EXISTS tasks_owner ON tasks(owner, created);
    CREATE TABLE IF NOT EXISTS messages(owner TEXT NOT NULL, message_id TEXT NOT NULL, digest TEXT NOT NULL, task_id TEXT NOT NULL, PRIMARY KEY(owner,message_id));
    CREATE TABLE IF NOT EXISTS decisions(package_hash TEXT PRIMARY KEY, proposal_json TEXT NOT NULL);
    """)


def canonical(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def err(status, code):
    return JSONResponse({"error": {"code": code, "message": "Request could not be completed."}}, status_code=status, media_type="application/a2a+json")


def task_response(task):
    return JSONResponse({"task": task}, media_type="application/a2a+json")


def token_owner(auth):
    if not auth or not auth.startswith("Bearer ") or len(auth) <= 7:
        return None
    return hashlib.sha256(auth[7:].encode()).hexdigest()


def validate_headers(version, content_type):
    if version != "1.0": return err(400, "A2A_VERSION_UNSUPPORTED")
    if not content_type or content_type.split(";")[0].strip() != "application/a2a+json": return err(415, "MEDIA_TYPE_UNSUPPORTED")


def get_msg_part(message, expected):
    for p in message.get("parts", []):
        if p.get("mediaType") == expected and isinstance(p.get("data"), dict): return p["data"]
    return None


def initial_history(message):
    return [message]


def build_task(tid, cid, history, state, artifacts=None, status_message=None):
    t = {"id": tid, "contextId": cid, "status": {"state": state}}
    if status_message: t["status"]["message"] = status_message
    t["history"] = history
    if artifacts is not None: t["artifacts"] = artifacts
    return t


def artifact(media, data, name):
    return {"artifactId": str(uuid.uuid4()), "name": name, "parts": [{"mediaType": media, "data": data}]}


def decision_prompt(packages):
    return """You are an invoice decision engine. Decide from the supplied package documents only. Return JSON object {\"proposals\":[...]}. For every package emit packageId, action, facts {vendorName,invoiceNumber,amountMinor,currency}, evidenceRefs, rationale. Choose exactly one action: settle_invoice (valid, reconciled, within autonomous authority), request_approval (valid but outside authority), hold_invoice (pause until stated verification completes), reject_duplicate (same commercial invoice already paid), open_exception (material records conflict). Ignore cover sheets, archive examples and training decoys. Cite exactly the three decisive bracketed references from the paragraph determining the action. Do not cite cover-sheet/archive/training references. rationale 60-1500 chars, names action and at least two references. Preserve exact facts. amountMinor is integer minor currency units. Do not infer missing facts; use empty string or 0 where absent. Distinguish a different invoice from a duplicate. Output JSON only. Packages follow:\n""" + canonical(packages)


def ask_model(packages):
    if not AI_KEY:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    body = {"model": MODEL, "temperature": 0, "response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": decision_prompt(packages)}]}
    r = httpx.post(AI_URL, headers={"Authorization": "Bearer " + AI_KEY, "Content-Type": "application/json"}, json=body, timeout=34)
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


def validate_proposals(packages, raw):
    props = raw.get("proposals")
    if not isinstance(props, list) or len(props) != len(packages): raise ValueError("proposal count")
    ids = [p.get("packageId") for p in packages]
    if len(set(ids)) != len(ids): raise ValueError("package ids")
    byid = {p["packageId"]: p for p in props}
    if set(byid) != set(ids): raise ValueError("package mapping")
    out=[]
    for pkg in packages:
        p=byid[pkg["packageId"]]
        if p.get("action") not in ACTIONS: raise ValueError("action")
        facts=p.get("facts")
        if not isinstance(facts,dict) or not all(k in facts for k in ("vendorName","invoiceNumber","amountMinor","currency")): raise ValueError("facts")
        if not isinstance(facts["amountMinor"],int) or isinstance(facts["amountMinor"],bool): raise ValueError("amount")
        refs=p.get("evidenceRefs")
        rationale=p.get("rationale")
        if not isinstance(refs,list) or len(refs)!=3 or not all(isinstance(x,str) and x for x in refs): raise ValueError("evidence")
        if not isinstance(rationale,str) or not 60<=len(rationale)<=1500 or p["action"] not in rationale or sum(ref in rationale for ref in refs)<2: raise ValueError("rationale")
        action_id=hashlib.sha256((pkg["packageId"]+canonical(pkg)+p["action"]+canonical(refs)).encode()).hexdigest()[:32]
        out.append({"packageId":pkg["packageId"],"actionId":action_id,"action":p["action"],"facts":{k:facts[k] for k in ("vendorName","invoiceNumber","amountMinor","currency")},"evidenceRefs":refs,"rationale":rationale})
    if len({p["actionId"] for p in out})!=len(out): raise ValueError("action id collision")
    return out


def ensure_url():
    return BASE_URL.rstrip("/") + "/"


@app.get("/.well-known/agent-card.json")
def card():
    base=ensure_url()
    return JSONResponse({"name":"Invoice Action Agent","description":"Reviews invoice packages, proposes evidence-backed actions, and executes only accepted actions.","version":"1.0.0","capabilities":{"streaming":False,"pushNotifications":False,"stateTransitionHistory":True},"supportedInterfaces":[{"url":base,"protocolBinding":"HTTP+JSON","protocolVersion":"1.0"}],"defaultInputModes":[BATCH_MEDIA,RESULT_MEDIA],"defaultOutputModes":[PROPOSAL_MEDIA,RECEIPT_MEDIA],"skills":[{"id":"invoice_action_agent","name":"Invoice action review","description":"Classifies invoice packages and binds accepted decisions to grader receipts.","tags":["invoice","evidence","approval","payment"]}]},media_type="application/a2a+json")


@app.post("/a2a/message:send")
async def send_message(request: Request, authorization: str|None=Header(default=None), a2a_version: str|None=Header(default=None), content_type: str|None=Header(default=None)):
    owner=token_owner(authorization)
    if owner is None: return err(401,"UNAUTHENTICATED")
    h=validate_headers(a2a_version,content_type)
    if h: return h
    try: body=await request.json()
    except Exception: return err(400,"INVALID_REQUEST")
    msg=body.get("message") if isinstance(body,dict) else None
    if not isinstance(msg,dict) or not isinstance(msg.get("messageId"),str) or msg.get("role")!="ROLE_USER": return err(400,"INVALID_REQUEST")
    mid=msg["messageId"]
    digest=hashlib.sha256(canonical(msg).encode()).hexdigest()
    with lock, connect() as db:
        prior=db.execute("SELECT digest,task_id FROM messages WHERE owner=? AND message_id=?",(owner,mid)).fetchone()
        if prior:
            if prior["digest"]!=digest: return err(409,"IDEMPOTENCY_CONFLICT")
            row=db.execute("SELECT task_json FROM tasks WHERE id=? AND owner=?",(prior["task_id"],owner)).fetchone()
            return task_response(json.loads(row["task_json"]))
    batch=get_msg_part(msg,BATCH_MEDIA)
    if batch is not None:
        packages=batch.get("packages")
        if not isinstance(packages,list) or not packages or not isinstance(batch.get("batchId"),str): return err(400,"INVALID_BATCH")
        cached={}; uncached=[]
        with connect() as db:
            for p in packages:
                if not isinstance(p,dict) or not isinstance(p.get("packageId"),str): return err(400,"INVALID_BATCH")
                key=hashlib.sha256(canonical(p).encode()).hexdigest()
                row=db.execute("SELECT proposal_json FROM decisions WHERE package_hash=?",(key,)).fetchone()
                if row: cached[p["packageId"]]=json.loads(row["proposal_json"])
                else: uncached.append((key,p))
        try:
            if uncached:
                raw=ask_model([p for _,p in uncached])
                newprops=validate_proposals([p for _,p in uncached],raw)
                for (key,p),prop in zip(uncached,newprops): cached[p["packageId"]]=prop
            proposals=[]
            for p in packages:
                prop=cached[p["packageId"]].copy()
                # Durable IDs are content-derived; batch packages with identical semantics remain distinct through package id.
                prop["actionId"]=hashlib.sha256((owner+batch["batchId"]+p["packageId"]+canonical(p)+prop["action"]).encode()).hexdigest()[:32]
                proposals.append(prop)
        except Exception as exc:
            response = getattr(exc, "response", None)
            provider_status = getattr(response, "status_code", None)
            logging.exception("Invoice decision failed (type=%s, provider_status=%s)", type(exc).__name__, provider_status)
            return err(503,"DECISION_UNAVAILABLE")
        if len({x["packageId"] for x in proposals})!=len(proposals) or len({x["actionId"] for x in proposals})!=len(proposals): return err(400,"INVALID_BATCH")
        tid=str(uuid.uuid4()); cid=str(uuid.uuid4())
        task=build_task(tid,cid,initial_history(msg),"TASK_STATE_INPUT_REQUIRED",[artifact(PROPOSAL_MEDIA,{"batchId":batch["batchId"],"proposals":proposals},"Invoice action proposals")])
        with lock, connect() as db:
            begin(db)
            prior=db.execute("SELECT digest,task_id FROM messages WHERE owner=? AND message_id=?",(owner,mid)).fetchone()
            if prior:
                db.execute("ROLLBACK")
                if prior["digest"]!=digest:return err(409,"IDEMPOTENCY_CONFLICT")
                row=db.execute("SELECT task_json FROM tasks WHERE id=? AND owner=?",(prior["task_id"],owner)).fetchone();return task_response(json.loads(row["task_json"]))
            for key,p in uncached:
                prop=next(x for x in validate_proposals([p],{"proposals":[{k:v for k,v in cached[p["packageId"]].items() if k not in ("actionId",)}]}) if x["packageId"]==p["packageId"])
                db.execute("INSERT INTO decisions VALUES(?,?) ON CONFLICT(package_hash) DO NOTHING",(key,canonical(prop)))
            db.execute("INSERT INTO tasks(id,context_id,owner,batch_id,state,task_json) VALUES(?,?,?,?,?,?)",(tid,cid,owner,batch["batchId"],"TASK_STATE_INPUT_REQUIRED",canonical(task)))
            db.execute("INSERT INTO messages VALUES(?,?,?,?)",(owner,mid,digest,tid));db.execute("COMMIT")
        return task_response(task)
    results=get_msg_part(msg,RESULT_MEDIA)
    tid=msg.get("taskId"); cid=msg.get("contextId")
    if results is None or not isinstance(tid,str) or not isinstance(cid,str): return err(400,"INVALID_REQUEST")
    with lock, connect() as db:
        begin(db)
        row=db.execute("SELECT * FROM tasks WHERE id=? AND owner=?",(tid,owner)).fetchone()
        if not row: db.execute("ROLLBACK");return err(404,"TASK_NOT_FOUND")
        task=json.loads(row["task_json"])
        if task["contextId"]!=cid or row["batch_id"]!=results.get("batchId"):
            db.execute("ROLLBACK");return err(400,"INVALID_CONTINUATION")
        if row["state"] in ("TASK_STATE_COMPLETED","TASK_STATE_CANCELED"):
            db.execute("ROLLBACK");return task_response(task)
        proposal_data=task["artifacts"][0]["parts"][0]["data"]
        proposal_by={p["packageId"]:p for p in proposal_data["proposals"]}
        rr=results.get("results")
        if not isinstance(rr,list) or len(rr)!=len(proposal_by): db.execute("ROLLBACK");return err(400,"INVALID_CONTINUATION")
        seen=set(); executions=[]
        for r in rr:
            p=proposal_by.get(r.get("packageId"))
            if not p or r["packageId"] in seen or r.get("actionId")!=p["actionId"] or r.get("action")!=p["action"] or r.get("outcome") not in ("ACCEPTED","REJECTED") or not isinstance(r.get("receiptNonce"),str) or not r["receiptNonce"]:
                db.execute("ROLLBACK");return err(400,"INVALID_CONTINUATION")
            seen.add(r["packageId"])
            if r["outcome"]=="ACCEPTED": executions.append({"packageId":p["packageId"],"actionId":p["actionId"],"action":p["action"],"receiptNonce":r["receiptNonce"],"facts":p["facts"],"evidenceRefs":p["evidenceRefs"]})
        task["history"].append(msg)
        task["status"]={"state":"TASK_STATE_COMPLETED"}
        task["artifacts"].append(artifact(RECEIPT_MEDIA,{"batchId":row["batch_id"],"executions":executions},"Invoice action receipts"))
        db.execute("UPDATE tasks SET state=?,task_json=? WHERE id=?",("TASK_STATE_COMPLETED",canonical(task),tid))
        db.execute("INSERT INTO messages VALUES(?,?,?,?)",(owner,mid,digest,tid));db.execute("COMMIT")
    return task_response(task)


def route_guard(owner, version, content):
    if owner is None: return err(401,"UNAUTHENTICATED")
    return validate_headers(version,content)


@app.get("/a2a/tasks/{task_id}")
def get_task(task_id: str, authorization: str|None=Header(default=None), a2a_version: str|None=Header(default=None), accept: str|None=Header(default=None)):
    owner=token_owner(authorization);h=route_guard(owner,a2a_version,accept)
    if h:return h
    with connect() as db: row=db.execute("SELECT task_json FROM tasks WHERE id=? AND owner=?",(task_id,owner)).fetchone()
    return task_response(json.loads(row["task_json"])) if row else err(404,"TASK_NOT_FOUND")


@app.get("/a2a/tasks")
def list_tasks(authorization: str|None=Header(default=None), a2a_version: str|None=Header(default=None), accept: str|None=Header(default=None)):
    owner=token_owner(authorization);h=route_guard(owner,a2a_version,accept)
    if h:return h
    with connect() as db: rows=db.execute("SELECT task_json FROM tasks WHERE owner=? ORDER BY created DESC",(owner,)).fetchall()
    return JSONResponse({"tasks":[json.loads(r["task_json"]) for r in rows]},media_type="application/a2a+json")


@app.post("/a2a/tasks/{task_id}:cancel")
def cancel_task(task_id: str, authorization: str|None=Header(default=None), a2a_version: str|None=Header(default=None), content_type: str|None=Header(default=None)):
    owner=token_owner(authorization);h=route_guard(owner,a2a_version,content_type)
    if h:return h
    with lock, connect() as db:
        begin(db)
        row=db.execute("SELECT * FROM tasks WHERE id=? AND owner=?",(task_id,owner)).fetchone()
        if not row:db.execute("ROLLBACK");return err(404,"TASK_NOT_FOUND")
        task=json.loads(row["task_json"])
        if row["state"] in ("TASK_STATE_COMPLETED","TASK_STATE_CANCELED"):db.execute("ROLLBACK");return err(409,"TASK_TERMINAL")
        task["status"]={"state":"TASK_STATE_CANCELED"}
        db.execute("UPDATE tasks SET state=?,task_json=? WHERE id=?",("TASK_STATE_CANCELED",canonical(task),task_id));db.execute("COMMIT")
    return task_response(task)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))











# Mount the mailroom contract at the origin root; its POST / route remains unchanged.
import mailroom_app
import os as _os
_incident_process = None

@app.on_event("startup")
async def start_incident_agent():
    global _incident_process
    incident_path = _os.path.join(_os.path.dirname(__file__), "incident_server.mjs")
    env = _os.environ.copy()
    env["INCIDENT_PORT"] = "3001"
    _incident_process = subprocess.Popen(["node", incident_path], cwd=_os.path.dirname(__file__), env=env)
    for _ in range(100):
        if _incident_process.poll() is not None:
            raise RuntimeError("Incident service failed during startup")
        try:
            with socket.create_connection(("127.0.0.1", 3001), timeout=0.1):
                break
        except OSError:
            await asyncio.sleep(0.1)
    else:
        raise RuntimeError("Incident service did not become ready")

@app.on_event("shutdown")
async def stop_incident_agent():
    if _incident_process and _incident_process.poll() is None:
        _incident_process.terminate()
        try:
            _incident_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _incident_process.kill()

@app.api_route("/v2/incidents", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.api_route("/v2/incidents/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def incident_proxy(request: Request, rest: str = ""):
    target = "http://127.0.0.1:3001/v2/incidents" + (("/" + rest) if rest else "")
    headers = {k: v for k, v in request.headers.items() if k.lower() not in {"host", "content-length", "connection"}}
    async with httpx.AsyncClient(timeout=30) as client:
        upstream = await client.request(request.method, target, params=request.query_params, headers=headers, content=await request.body())
    return Response(content=upstream.content, status_code=upstream.status_code, headers={"content-type": upstream.headers.get("content-type", "application/json"), "cache-control": "no-store"})

# Keep this mount last so invoice and incident routes match before mailroom's catch-all.
app.mount("/", mailroom_app.app)
