"""MoCDTI Staff Register - Flask backend; accounts are created by HR/Admin."""
import os, json, secrets
from datetime import datetime, timezone, timedelta
from markupsafe import escape
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, session, redirect, request, jsonify, url_for, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__, static_folder="static")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config.update(
    SQLALCHEMY_DATABASE_URI=os.environ["DATABASE_URL"].replace("postgres://", "postgresql://", 1),
    SECRET_KEY=os.environ["SECRET_KEY"],
    SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=8 * 3600,
)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024   # attachments (base64)
db = SQLAlchemy(app)

# Field names match your HTML file's record keys, so the front end maps 1:1.
STAFF_FIELDS = ("firstName middleName lastName sex maritalStatus dob staffNo contact email "
    "qualifications grade position unit status category profCategory firstAppointment "
    "currentAppointment datePosted supName supGrade supId emergName emergRelationship "
    "emergContact remarks quarantineType quarantineDate quarantineReason").split()
# Visible/editable by HR-Admin only; everyone else never receives these.
HR_ONLY = {"dob", "maritalStatus", "staffNo", "qualifications",
           "emergName", "emergRelationship", "emergContact", "remarks"}
TRAINING_FIELDS = "title provider startDate endDate certificate remarks".split()
ROLES = ("admin", "officer", "general")   # HR/Admin, Authorised Officer, General Officer

Staff = type("Staff", (db.Model,), {
    "__tablename__": "staff",
    "id": db.Column(db.Integer, primary_key=True),
    "quarantined": db.Column(db.Boolean, default=False, nullable=False),
    "attachmentsMeta": db.Column(db.Text, default="{}"),
    **{f: db.Column(db.Text, default="") for f in STAFF_FIELDS}})
Training = type("Training", (db.Model,), {
    "__tablename__": "training",
    "id": db.Column(db.Integer, primary_key=True),
    "staff_id": db.Column(db.Integer, db.ForeignKey("staff.id", ondelete="CASCADE"), index=True),
    **{f: db.Column(db.Text, default="") for f in TRAINING_FIELDS}})

class Attachment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    staff_id = db.Column(db.Integer, index=True)
    slot = db.Column(db.String(40))
    data = db.Column(db.Text)          # base64 data URL; move to object storage if volume grows
SLOTS = ("passportPhoto", "appointmentLetter", "promotionLetter", "releaseLetter")

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    name = db.Column(db.String(255), default="")
    role = db.Column(db.String(20), default="general", nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    password_hash = db.Column(db.String(255))
    must_change = db.Column(db.Boolean, default=True, nullable=False)   # temp password must be replaced
    failed_attempts = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime(timezone=True))

class Audit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    user_email = db.Column(db.String(255))
    action = db.Column(db.String(40))
    entity = db.Column(db.String(40))
    entity_id = db.Column(db.Integer)
    detail = db.Column(db.Text)

with app.app_context():
    db.create_all()
    # First HR/Admin account is created from env vars only when no users exist yet.
    if not User.query.first() and os.environ.get("ADMIN_EMAIL") and os.environ.get("ADMIN_PASSWORD"):
        db.session.add(User(email=os.environ["ADMIN_EMAIL"].strip().lower(), name="HR Administrator", role="admin",
                            password_hash=generate_password_hash(os.environ["ADMIN_PASSWORD"]), must_change=True))
        db.session.commit()

# ---------- helpers ----------
def me():
    email = session.get("email")
    u = User.query.filter_by(email=email).first() if email else None
    return u if u and u.active else None

def log(action, entity, entity_id=None, detail=None):
    db.session.add(Audit(user_email=session.get("email"), action=action, entity=entity,
                         entity_id=entity_id, detail=json.dumps(detail, default=str) if detail else None))

def trainings_by_staff():
    m = {}
    for t in Training.query.order_by(Training.id):
        m.setdefault(t.staff_id, []).append({"id": str(t.id), **{f: getattr(t, f) or "" for f in TRAINING_FIELDS}})
    return m

def out(s, role, tr=None):
    d = {f: getattr(s, f) or "" for f in STAFF_FIELDS if role == "admin" or f not in HR_ONLY}
    if tr is None: tr = trainings_by_staff().get(s.id, [])
    d.update(id=str(s.id), quarantined=bool(s.quarantined), trainings=tr,
             attachments=json.loads(s.attachmentsMeta or "{}"),
             name=" ".join(x for x in (s.firstName, s.middleName, s.lastName) if x))
    return d

def apply(s, data, role):
    changes = {}
    for f in STAFF_FIELDS:
        if f in data and (role == "admin" or f not in HR_ONLY):
            new, old = (data[f] or "").strip(), getattr(s, f) or ""
            if new != old:
                changes[f] = [old, new]; setattr(s, f, new)
    if "quarantined" in data and bool(data["quarantined"]) != bool(s.quarantined):
        changes["quarantined"] = [bool(s.quarantined), bool(data["quarantined"])]; s.quarantined = bool(data["quarantined"])
    if isinstance(data.get("attachments"), dict):
        s.attachmentsMeta = json.dumps(data["attachments"])
    return changes

def sync_training(s, items):
    if items is None: return
    new = [{f: (i.get(f) or "").strip() for f in TRAINING_FIELDS} for i in items if (i.get("title") or "").strip()]
    old = [{f: getattr(t, f) or "" for f in TRAINING_FIELDS} for t in Training.query.filter_by(staff_id=s.id).order_by(Training.id)]
    if new != old:
        Training.query.filter_by(staff_id=s.id).delete()
        for n in new: db.session.add(Training(staff_id=s.id, **n))
        log("training_update", "staff", s.id, {"before": old, "after": new})

def need(*roles):
    u = me()
    if not u: return None, (jsonify(error="Not signed in"), 401)
    if u.role not in roles: return None, (jsonify(error="Not permitted"), 403)
    return u, None

@app.before_request
def guard():
    u = me()
    if request.path.startswith("/api") and u and u.must_change and request.path != "/api/me":
        return jsonify(error="Change your temporary password first"), 403
    if request.path.startswith("/api") and request.method != "GET" \
            and request.headers.get("X-Requested-With") != "fetch":
        return jsonify(error="Bad request"), 400          # basic CSRF protection

# ---------- accounts created by HR/Admin: email + password ----------
MAX_FAILS, LOCK_MIN, MIN_PW = 5, 15, 10
PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:linear-gradient(#0f2c4c,#0a1f35);font-family:Segoe UI,Arial,sans-serif}}
form{{background:#fff;border-top:4px solid #c9a961;padding:32px 34px;width:100%;max-width:380px;box-sizing:border-box;margin:16px}}
small{{color:#c9a961;letter-spacing:.1em;text-transform:uppercase;font-weight:600;font-size:11px}}h1{{font-size:19px;color:#0a1f35;margin:6px 0 4px}}
p{{font-size:12.5px;color:#5a6472}}label{{display:block;font-size:12px;color:#5a6472;margin:12px 0 4px}}
input{{width:100%;padding:10px;border:1px solid #e2dfd4;box-sizing:border-box;font-size:14px}}
button{{margin-top:18px;width:100%;padding:11px;background:#0f2c4c;color:#fff;border:0;font-weight:600;cursor:pointer}}.e{{color:#a6402f;font-size:12.5px;margin-top:10px}}</style>
<form method=post><small>MoCDTI Staff Register</small><h1>{title}</h1>{note}{fields}<button>{btn}</button><div class=e>{err}</div></form>"""
def field(label, name, typ="password"):
    return f'<label>{label}</label><input name="{name}" type="{typ}" required autocomplete="off">'

@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        u = User.query.filter_by(email=email).first()
        now = datetime.now(timezone.utc)
        if u and u.locked_until and u.locked_until > now:
            err = "Too many failed attempts. Try again in a few minutes."
        elif u and u.active and u.password_hash and check_password_hash(u.password_hash, request.form.get("password", "")):
            u.failed_attempts, u.locked_until = 0, None
            session.clear(); session.permanent = True; session["email"] = email
            log("login", "user", u.id); db.session.commit()
            return redirect("/change-password" if u.must_change else "/")
        else:
            if u:
                u.failed_attempts = (u.failed_attempts or 0) + 1
                if u.failed_attempts >= MAX_FAILS:
                    u.locked_until, u.failed_attempts = now + timedelta(minutes=LOCK_MIN), 0
                db.session.commit()
            err = "Incorrect email or password, or the account is disabled."
    return PAGE.format(title="Sign in", note="", btn="Sign in", err=escape(err),
                       fields=field("Email", "email", "email") + field("Password", "password"))

@app.route("/change-password", methods=["GET", "POST"])
def change_password():
    u = me()
    if not u: return redirect("/login")
    err = ""
    if request.method == "POST":
        cur, new, conf = (request.form.get(k, "") for k in ("current", "new", "confirm"))
        if not check_password_hash(u.password_hash, cur): err = "Current password is incorrect."
        elif len(new) < MIN_PW: err = f"New password must be at least {MIN_PW} characters."
        elif new != conf: err = "New passwords do not match."
        elif new == cur: err = "Choose a password different from the current one."
        else:
            u.password_hash, u.must_change = generate_password_hash(new), False
            log("password_change", "user", u.id); db.session.commit()
            return redirect("/")
    note = f"<p>Use at least {MIN_PW} characters. {'You must set your own password before continuing.' if u.must_change else ''}</p>"
    return PAGE.format(title="Change password", note=note, btn="Save password", err=escape(err),
        fields=field("Current (or temporary) password", "current") + field("New password", "new") + field("Confirm new password", "confirm"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# ---------- pages ----------
@app.route("/")
def index():
    u = me()
    if not u: return redirect("/login")
    if u.must_change: return redirect("/change-password")
    return send_from_directory("static", "index.html")

@app.route("/healthz")
def healthz(): return "ok"

# ---------- staff API ----------
@app.get("/api/me")
def api_me():
    u, err = need(*ROLES)
    return err or jsonify(email=u.email, name=u.name, role=u.role)

@app.get("/api/staff")
def staff_list():
    u, err = need(*ROLES)
    if err: return err
    tr = trainings_by_staff()
    return jsonify([out(s, u.role, tr.get(s.id, [])) for s in Staff.query.order_by(Staff.lastName, Staff.firstName)])

@app.post("/api/staff")
def staff_add():
    u, err = need("admin", "officer")
    if err: return err
    s = Staff(); changes = apply(s, request.get_json() or {}, u.role)
    if not (s.firstName and s.lastName): return jsonify(error="First and last name required"), 400
    db.session.add(s); db.session.flush(); log("create", "staff", s.id, changes)
    sync_training(s, (request.get_json() or {}).get("trainings")); db.session.commit()
    return jsonify(out(s, u.role)), 201

@app.put("/api/staff/<int:sid>")
def staff_edit(sid):
    u, err = need("admin", "officer")
    if err: return err
    s = db.get_or_404(Staff, sid); changes = apply(s, request.get_json() or {}, u.role)
    if changes: log("update", "staff", sid, changes)
    sync_training(s, (request.get_json() or {}).get("trainings"))
    db.session.commit(); return jsonify(out(s, u.role))

@app.post("/api/staff/<int:sid>/quarantine")
def quarantine(sid):
    u, err = need("admin", "officer")
    if err: return err
    d, s = request.get_json() or {}, db.get_or_404(Staff, sid)
    s.quarantined = True
    s.quarantineType = d.get("type", "Other"); s.quarantineDate = d.get("date", ""); s.quarantineReason = d.get("reason", "")
    log("quarantine", "staff", sid, d); db.session.commit(); return jsonify(out(s, u.role))

@app.post("/api/staff/<int:sid>/restore")
def restore(sid):
    u, err = need("admin", "officer")
    if err: return err
    s = db.get_or_404(Staff, sid)
    s.quarantined = False; s.quarantineType = s.quarantineDate = s.quarantineReason = ""
    log("restore", "staff", sid); db.session.commit(); return jsonify(out(s, u.role))

@app.delete("/api/staff/<int:sid>")
def staff_delete(sid):
    u, err = need("admin")
    if err: return err
    s = db.get_or_404(Staff, sid)
    log("delete", "staff", sid, {"name": f"{s.firstName} {s.lastName}"})
    Training.query.filter_by(staff_id=sid).delete(); Attachment.query.filter_by(staff_id=sid).delete(); db.session.delete(s); db.session.commit()
    return "", 204

@app.post("/api/import")                      # one-time load of your existing records
def bulk_import():
    u, err = need("admin")
    if err: return err
    n = 0
    for rec in request.get_json() or []:
        s = Staff(); apply(s, rec, "admin"); db.session.add(s); db.session.flush(); sync_training(s, rec.get("trainings")); n += 1
    log("import", "staff", detail={"count": n}); db.session.commit()
    return jsonify(imported=n)

# ---------- training ----------
@app.get("/api/training")
def training_list():
    u, err = need(*ROLES)
    return err or jsonify([{"id": t.id, "staff_id": t.staff_id, **{f: getattr(t, f) for f in TRAINING_FIELDS}}
                           for t in Training.query.all()])

@app.post("/api/staff/<int:sid>/training")
def training_add(sid):
    u, err = need("admin", "officer")
    if err: return err
    d = request.get_json() or {}
    t = Training(staff_id=sid, **{f: d.get(f, "") for f in TRAINING_FIELDS})
    db.session.add(t); db.session.flush(); log("create", "training", t.id, d); db.session.commit()
    return jsonify(id=t.id), 201

@app.delete("/api/training/<int:tid>")
def training_delete(tid):
    u, err = need("admin", "officer")
    if err: return err
    t = db.get_or_404(Training, tid); log("delete", "training", tid); db.session.delete(t); db.session.commit()
    return "", 204

# ---------- admin: users & audit ----------
@app.get("/api/users")
def users_list():
    u, err = need("admin")
    return err or jsonify([dict(id=x.id, email=x.email, name=x.name, role=x.role, active=x.active, must_change=x.must_change) for x in User.query])

@app.put("/api/users/<int:uid>")
def users_edit(uid):
    u, err = need("admin")
    if err: return err
    x, d = db.get_or_404(User, uid), request.get_json() or {}
    if x.id == u.id and (d.get("active") is False or d.get("role", "admin") != "admin"):
        return jsonify(error="You cannot disable or demote your own account"), 400
    if d.get("role") in ROLES: x.role = d["role"]
    if "active" in d: x.active = bool(d["active"])
    log("user_update", "user", uid, d); db.session.commit(); return jsonify(ok=True)

@app.get("/api/audit")
def audit_list():
    u, err = need("admin")
    return err or jsonify([dict(at=a.at.isoformat(), user=a.user_email, action=a.action, entity=a.entity,
        entity_id=a.entity_id, detail=a.detail) for a in Audit.query.order_by(Audit.id.desc()).limit(500)])

# ---------- attachments (photo + letters) ----------
@app.route("/api/staff/<int:sid>/attachment/<slot>", methods=["GET", "PUT", "DELETE"])
def attachment(sid, slot):
    if slot not in SLOTS: return jsonify(error="Unknown attachment"), 404
    if request.method == "GET" and slot == "passportPhoto": u, err = need(*ROLES)   # photo: all roles
    elif request.method == "GET": u, err = need("admin", "officer")                  # letters: restricted
    else: u, err = need("admin", "officer")
    if err: return err
    a = Attachment.query.filter_by(staff_id=sid, slot=slot).first()
    if request.method == "GET":
        return jsonify(data=a.data) if a else (jsonify(error="none"), 404)
    if request.method == "PUT":
        data = (request.get_json() or {}).get("data", "")
        if not data: return jsonify(error="Empty file"), 400
        if not a: a = Attachment(staff_id=sid, slot=slot); db.session.add(a)
        a.data = data; log("attachment_upload", "staff", sid, {"slot": slot})
    elif a:
        db.session.delete(a); log("attachment_delete", "staff", sid, {"slot": slot})
    db.session.commit(); return jsonify(ok=True)

def new_temp_password(): return secrets.token_urlsafe(9)

@app.post("/api/users")
def users_create():
    u, err = need("admin")
    if err: return err
    d = request.get_json() or {}
    email = (d.get("email") or "").strip().lower()
    if "@" not in email: return jsonify(error="Enter a valid email address"), 400
    if User.query.filter_by(email=email).first(): return jsonify(error="That email already has an account"), 409
    pw = new_temp_password()
    x = User(email=email, name=(d.get("name") or "").strip(), role=d.get("role") if d.get("role") in ROLES else "general",
             password_hash=generate_password_hash(pw), must_change=True)
    db.session.add(x); db.session.flush(); log("user_create", "user", x.id, {"email": email, "role": x.role}); db.session.commit()
    return jsonify(id=x.id, email=email, temp_password=pw), 201

@app.post("/api/users/<int:uid>/reset")
def users_reset(uid):
    u, err = need("admin")
    if err: return err
    x, pw = db.get_or_404(User, uid), new_temp_password()
    x.password_hash, x.must_change, x.failed_attempts, x.locked_until = generate_password_hash(pw), True, 0, None
    log("password_reset", "user", uid); db.session.commit()
    return jsonify(email=x.email, temp_password=pw)
