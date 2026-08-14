import os
import re
import traceback
import calendar as pycalendar
from datetime import datetime, date
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm, OAuth2PasswordBearer
from sqlalchemy.orm import Session
from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, func, or_, text
from sqlalchemy.exc import IntegrityError
from jose import JWTError, jwt
from pydantic import BaseModel, Field

from database import Base, engine, get_db, SessionLocal
from models import Doctor, Clinic, Patient, Visit, VisitStatus, ClinicCalendar, ProcessedWebhookEvent
from schemas import (
    TokenSchema, DoctorOutSchema,
    DashboardSummaryOutSchema, VisitOutSchema, PatientOutSchema, ManualPatientAddSchema,
    PatientSummarySchema, StatusUpdateSchema
)
from security import verify_password, create_access_token, get_password_hash
from whatsapp import process_whatsapp_message, get_today_ist, generate_daily_token, trigger_reminders

# ==========================================
# 1. DATABASE MODELS (Safe Extension)
# ==========================================
class AdminSystem(Base):
    __tablename__ = "system_admins"
    id = Column(Integer, primary_key=True, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id"))
    username = Column(String, unique=True, index=True)
    password_hash = Column(String)

class ReminderSettings(Base):
    __tablename__ = "reminder_settings"
    clinic_id = Column(Integer, ForeignKey("clinics.id"), primary_key=True)
    enabled = Column(Boolean, default=True)
    near_turn_enabled = Column(Boolean, default=True)
    near_turn_patients = Column(Integer, default=2)
    your_turn_enabled = Column(Boolean, default=True)

if not hasattr(Patient, 'is_active'):
    Patient.is_active = Column(Boolean, default=True, server_default="true")

# Bind models without dropping tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title="WhatsApp Clinic Token System API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# 2. VALIDATION SCHEMAS
# ==========================================
class DoctorCreateSchema(BaseModel):
    name: str = Field(..., min_length=1)
    email: str = Field(..., min_length=5)
    password: str = Field(..., min_length=6)

class ReminderSettingsSchema(BaseModel):
    enabled: bool
    near_turn_enabled: bool
    near_turn_patients: int
    your_turn_enabled: bool

# ==========================================
# 3. AUTHENTICATION
# ==========================================
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")
SECRET_KEY = os.getenv("JWT_SECRET", "super-secret-key")
ALGORITHM = "HS256"

class AdminUser:
    def __init__(self, email: str, clinic: Clinic):
        self.id = 0
        self.name = "System Admin"
        self.email = email
        self.clinic_id = clinic.id if clinic else 1
        self.clinic = clinic
        self.is_online = True

def get_current_active_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if not username: raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    admin_email = os.getenv("ADMIN_USERNAME", "admin")
    if username == admin_email:
        return AdminUser(email=admin_email, clinic=db.query(Clinic).first())

    doc = db.query(Doctor).filter(Doctor.email == username, Doctor.is_active == True).first()
    if not doc: raise HTTPException(status_code=401, detail="User not found")
    return doc

def get_current_admin(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    user = get_current_active_user(token, db)
    if isinstance(user, Doctor): raise HTTPException(status_code=403, detail="Admins only.")
    return user

# ==========================================
# 4. STARTUP MIGRATION (Non-Destructive)
# ==========================================
@app.on_event("startup")
def setup_database_and_admin():
    db = SessionLocal()
    try:
        # Safe addition of duplicate-prevention state trackers
        try:
            db.execute(text("ALTER TABLE patients ADD COLUMN is_active BOOLEAN DEFAULT TRUE NOT NULL"))
            db.commit()
        except Exception: db.rollback()
        
        try:
            db.execute(text("ALTER TABLE visits ADD COLUMN near_turn_sent BOOLEAN DEFAULT FALSE NOT NULL"))
            db.execute(text("ALTER TABLE visits ADD COLUMN your_turn_sent BOOLEAN DEFAULT FALSE NOT NULL"))
            db.commit()
        except Exception: db.rollback()

        admin_email = os.getenv("ADMIN_USERNAME", "admin")
        admin_password = os.getenv("ADMIN_PASSWORD", "ChangeThisAdminPassword123!")
        
        clinic = db.query(Clinic).first()
        if not clinic:
            clinic = Clinic(name="System Default Clinic", whatsapp_phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID", "default_phone_id"), is_online=True)
            db.add(clinic)
            db.commit()
            db.refresh(clinic)
            
        admin = db.query(AdminSystem).filter(AdminSystem.username == admin_email).first()
        admin_hashed_pw = get_password_hash(admin_password)
        if not admin:
            admin = AdminSystem(clinic_id=clinic.id, username=admin_email, password_hash=admin_hashed_pw)
            db.add(admin)
            db.commit()
        else:
            if not admin.password_hash or not verify_password(admin_password, admin.password_hash):
                admin.password_hash = admin_hashed_pw
                db.commit()

        # Initialize Default Reminder Settings
        rem_set = db.query(ReminderSettings).filter_by(clinic_id=clinic.id).first()
        if not rem_set:
            db.add(ReminderSettings(clinic_id=clinic.id))
            db.commit()

        old_admin_doc = db.query(Doctor).filter(Doctor.email == admin_email).first()
        if old_admin_doc and old_admin_doc.is_active:
            old_admin_doc.is_active = False
            old_admin_doc.is_online = False
            db.commit()
                
    except Exception as e:
        print(f"Startup DB Error: {e}")
    finally:
        db.close()

# ==========================================
# 5. REMINDERS SETTINGS ENDPOINTS
# ==========================================
@app.get("/reminders/settings", response_model=ReminderSettingsSchema)
def get_reminder_settings(db: Session = Depends(get_db), current_admin: AdminSystem = Depends(get_current_admin)):
    settings = db.query(ReminderSettings).filter_by(clinic_id=current_admin.clinic_id).first()
    if not settings:
        settings = ReminderSettings(clinic_id=current_admin.clinic_id)
        db.add(settings)
        db.commit()
    return settings

@app.put("/reminders/settings", response_model=ReminderSettingsSchema)
def update_reminder_settings(payload: ReminderSettingsSchema, db: Session = Depends(get_db), current_admin: AdminSystem = Depends(get_current_admin)):
    settings = db.query(ReminderSettings).filter_by(clinic_id=current_admin.clinic_id).first()
    if not settings:
        settings = ReminderSettings(clinic_id=current_admin.clinic_id)
        db.add(settings)
    
    settings.enabled = payload.enabled
    settings.near_turn_enabled = payload.near_turn_enabled
    settings.near_turn_patients = payload.near_turn_patients
    settings.your_turn_enabled = payload.your_turn_enabled
    db.commit()
    return settings

# ==========================================
# 6. FRONTEND & AUTHENTICATION ENDPOINTS
# ==========================================
@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def serve_frontend():
    if os.path.exists("index.html"): return FileResponse("index.html")
    return HTMLResponse(content="<h1>Frontend UI missing</h1>", status_code=404)

@app.get("/health")
def health_check():
    return {"status": "healthy"}

@app.post("/auth/login", response_model=TokenSchema)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    admin = db.query(AdminSystem).filter(AdminSystem.username == form_data.username).first()
    if admin and verify_password(form_data.password, admin.password_hash):
        return {"access_token": create_access_token(data={"sub": admin.username, "role": "admin"}), "token_type": "bearer"}
    doc = db.query(Doctor).filter(Doctor.email == form_data.username, Doctor.is_active == True).first()
    if doc and verify_password(form_data.password, doc.password_hash):
        return {"access_token": create_access_token(data={"sub": doc.email, "role": "doctor"}), "token_type": "bearer"}
    raise HTTPException(status_code=400, detail="Invalid username or password")

@app.get("/auth/me")
def get_me(current_user = Depends(get_current_active_user)):
    return {
        "id": current_user.id,
        "clinic_id": current_user.clinic_id,
        "name": current_user.name,
        "email": current_user.email,
        "clinic_name": current_user.clinic.name if current_user.clinic else "Clinic",
        "is_online": True, 
        "role": "admin" if current_user.id == 0 else "doctor"
    }

# ==========================================
# 7. DOCTOR MANAGEMENT
# ==========================================
@app.post("/doctors/add", status_code=status.HTTP_201_CREATED)
def add_doctor(payload: DoctorCreateSchema, db: Session = Depends(get_db), current_admin: AdminSystem = Depends(get_current_admin)):
    email = payload.email.strip().lower()
    if not payload.name.strip(): raise HTTPException(status_code=400, detail="Doctor name is required.")
    if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email): raise HTTPException(status_code=400, detail="Invalid email format.")
    if len(payload.password) < 6: raise HTTPException(status_code=400, detail="Password must be at least 6 chars.")
    if db.query(Doctor).filter(func.lower(Doctor.email) == email).first() or db.query(AdminSystem).filter(func.lower(AdminSystem.username) == email).first():
        raise HTTPException(status_code=409, detail="Email already in use.")

    new_doc = Doctor(clinic_id=current_admin.clinic_id, name=payload.name.strip(), email=email, password_hash=get_password_hash(payload.password), is_active=True, is_online=True)
    db.add(new_doc)
    db.commit()
    db.refresh(new_doc)
    return {"message": "Doctor added", "doctor": {"id": new_doc.id, "name": new_doc.name, "email": new_doc.email}}

@app.get("/doctors", response_model=List[DoctorOutSchema])
def get_all_doctors(db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    admin_email = os.getenv("ADMIN_USERNAME", "admin")
    doctors = db.query(Doctor).filter(Doctor.clinic_id == current_user.clinic_id, Doctor.is_active == True, Doctor.email != admin_email).all()
    return [DoctorOutSchema(id=d.id, clinic_id=d.clinic_id, name=d.name, email=d.email, clinic_name=d.clinic.name, is_online=d.is_online) for d in doctors]

@app.put("/doctors/{doctor_id}/status", response_model=StatusUpdateSchema)
def update_doctor_status(doctor_id: int, payload: StatusUpdateSchema, db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    doc = db.query(Doctor).filter(Doctor.id == doctor_id, Doctor.clinic_id == current_user.clinic_id).first()
    if not doc: raise HTTPException(404, "Doctor not found")
    doc.is_online = payload.is_online
    db.commit()
    return {"is_online": doc.is_online}

@app.delete("/doctors/{doctor_id}")
def delete_doctor(doctor_id: int, db: Session = Depends(get_db), current_admin: AdminSystem = Depends(get_current_admin)):
    doc = db.query(Doctor).filter(Doctor.id == doctor_id, Doctor.clinic_id == current_admin.clinic_id).first()
    if not doc: raise HTTPException(404, "Doctor not found")
    doc.is_active = False
    doc.is_online = False
    db.commit()
    return {"message": "Doctor deleted"}

# ==========================================
# 8. QUEUE MANAGEMENT WITH REMINDER TRIGGER
# ==========================================
@app.get("/doctor/{doctor_id}/queue")
def get_specific_doctor_queue(doctor_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    doc = db.query(Doctor).filter(Doctor.id == doctor_id, Doctor.clinic_id == current_user.clinic_id).first()
    if not doc: raise HTTPException(404, "Doctor not found")
    today = get_today_ist()
    visits = db.query(Visit).filter(Visit.doctor_id == doctor_id, Visit.visit_date == today).order_by(Visit.token_number.asc()).all()
    
    current_visit = next((v for v in visits if v.status == VisitStatus.CURRENT), None)
    waiting_visits = [v for v in visits if v.status == VisitStatus.WAITING]
    skipped_visits = [v for v in visits if v.status == VisitStatus.SKIPPED]

    def fmt(v):
        if not v: return None
        return {"id": v.id, "token_number": v.token_number, "patient_name": v.patient.name, "patient_phone": v.patient.phone_number or v.patient.whatsapp_number, "visit_reason": v.visit_reason, "doctor_name": f"Dr. {doc.name}"}

    return {"current_token": fmt(current_visit), "next_patient": fmt(waiting_visits[0]) if waiting_visits else None, "skipped_patients": [fmt(v) for v in skipped_visits], "waiting_count": len(waiting_visits), "completed_count": sum(1 for v in visits if v.status == VisitStatus.COMPLETED), "today_total": len(visits)}

@app.post("/doctor/{doctor_id}/next-patient")
def next_patient(doctor_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    today = get_today_ist()
    curr_visit = db.query(Visit).filter(Visit.doctor_id == doctor_id, Visit.visit_date == today, Visit.status == VisitStatus.CURRENT).first()
    if curr_visit:
        curr_visit.status = VisitStatus.COMPLETED
        curr_visit.completed_at = datetime.utcnow()

    next_visit = db.query(Visit).filter(Visit.doctor_id == doctor_id, Visit.visit_date == today, Visit.status == VisitStatus.WAITING).order_by(Visit.token_number.asc()).first()
    if next_visit:
        next_visit.status = VisitStatus.CURRENT
        db.commit()
        trigger_reminders(db, current_user.clinic_id, doctor_id) # <--- TRIGGER
        return {"message": f"Token #{next_visit.token_number} is now CURRENT"}
    
    db.commit()
    trigger_reminders(db, current_user.clinic_id, doctor_id) # <--- TRIGGER
    return {"message": "No waiting patients remaining"}

@app.post("/visit/{visit_id}/{action}")
def manage_visit_status(visit_id: int, action: str, db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    visit = db.query(Visit).filter(Visit.id == visit_id).first()
    if not visit: raise HTTPException(404, "Visit not found")

    if action in ["accept", "recall"]:
        today = get_today_ist()
        curr_visit = db.query(Visit).filter(Visit.doctor_id == visit.doctor_id, Visit.visit_date == today, Visit.status == VisitStatus.CURRENT).first()
        if curr_visit:
            curr_visit.status = VisitStatus.COMPLETED
            curr_visit.completed_at = datetime.utcnow()
        visit.status = VisitStatus.CURRENT
    elif action == "skip":
        visit.status = VisitStatus.SKIPPED
    elif action == "cancel":
        visit.status = VisitStatus.CANCELLED
        visit.cancelled_at = datetime.utcnow()
    else: raise HTTPException(400, "Invalid action")
    
    db.commit()
    trigger_reminders(db, current_user.clinic_id, visit.doctor_id) # <--- TRIGGER
    return {"message": f"Token #{visit.token_number} status updated to {visit.status}"}

@app.post("/doctor/add-walkin")
def add_walkin_patient(payload: ManualPatientAddSchema, db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    patient = db.query(Patient).filter(Patient.clinic_id == current_user.clinic_id, Patient.whatsapp_number == payload.whatsapp_number).first()
    if not patient:
        patient = Patient(clinic_id=current_user.clinic_id, name=payload.name, whatsapp_number=payload.whatsapp_number, phone_number=payload.phone_number, age=payload.age, gender=payload.gender)
        db.add(patient)
    elif not patient.is_active:
        patient.is_active = True
        patient.name = payload.name
    db.commit()
    db.refresh(patient)
    
    try:
        visit = generate_daily_token(db, current_user.clinic_id, payload.doctor_id, patient.id, payload.visit_reason)
        return {"message": "Patient added", "token_number": visit.token_number}
    except Exception: raise HTTPException(500, "Token generation error.")

# ==========================================
# 9. PATIENTS, DASHBOARD & WEBHOOK (Unchanged)
# ==========================================
@app.get("/doctor/today", response_model=DashboardSummaryOutSchema)
def get_today_summary(db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    today = get_today_ist()
    visits = db.query(Visit).filter(Visit.clinic_id == current_user.clinic_id, Visit.visit_date == today).order_by(Visit.token_number.asc()).all()
    return DashboardSummaryOutSchema(clinic_name=current_user.clinic.name, today_date=today.strftime("%d %B %Y"), waiting_count=sum(1 for v in visits if v.status == VisitStatus.WAITING), completed_count=sum(1 for v in visits if v.status == VisitStatus.COMPLETED), cancelled_count=sum(1 for v in visits if v.status == VisitStatus.CANCELLED), total_count=len(visits))

@app.get("/clinic/status", response_model=StatusUpdateSchema)
def get_clinic_status(db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    return {"is_online": db.query(Clinic).filter(Clinic.id == current_user.clinic_id).first().is_online}

@app.put("/clinic/status", response_model=StatusUpdateSchema)
def update_clinic_status(payload: StatusUpdateSchema, db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    clinic = db.query(Clinic).filter(Clinic.id == current_user.clinic_id).first()
    clinic.is_online = payload.is_online
    db.commit()
    return {"is_online": clinic.is_online}

@app.get("/doctor/patients-summary", response_model=PatientSummarySchema)
def get_patients_summary(query: Optional[str] = None, start_date: Optional[str] = None, end_date: Optional[str] = None, db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    pq = db.query(Patient).filter(Patient.clinic_id == current_user.clinic_id, Patient.is_active == True)
    if query: pq = pq.filter(or_(Patient.name.ilike(f"%{query}%"), Patient.whatsapp_number.ilike(f"%{query}%")))
    patient_ids = {p.id for p in pq.all()}
    if not patient_ids: return PatientSummarySchema(total_patients=0, new_patients=0, returning_patients=0, total_visits=0, completed_visits=0, cancelled_visits=0, waiting_visits=0)
    vq = db.query(Visit).filter(Visit.clinic_id == current_user.clinic_id, Visit.patient_id.in_(patient_ids))
    if start_date: vq = vq.filter(Visit.visit_date >= datetime.strptime(start_date, "%Y-%m-%d").date())
    if end_date: vq = vq.filter(Visit.visit_date <= datetime.strptime(end_date, "%Y-%m-%d").date())
    visits = vq.all()
    return PatientSummarySchema(total_patients=len({v.patient_id for v in visits}), new_patients=0, returning_patients=0, total_visits=len(visits), completed_visits=sum(1 for v in visits if v.status == VisitStatus.COMPLETED), cancelled_visits=sum(1 for v in visits if v.status == VisitStatus.CANCELLED), waiting_visits=sum(1 for v in visits if v.status in [VisitStatus.WAITING, VisitStatus.CURRENT]))

@app.get("/doctor/patients", response_model=List[PatientOutSchema])
def search_patients(query: Optional[str] = None, start_date: Optional[str] = None, end_date: Optional[str] = None, db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    q = db.query(Patient).filter(Patient.clinic_id == current_user.clinic_id, Patient.is_active == True)
    if query: q = q.filter(or_(Patient.name.ilike(f"%{query}%"), Patient.whatsapp_number.ilike(f"%{query}%"), Patient.phone_number.ilike(f"%{query}%")))
    patients = q.order_by(Patient.id.desc()).all()
    s_date = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
    e_date = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None
    res = []
    for p in patients:
        vq = db.query(Visit).filter(Visit.patient_id == p.id)
        if s_date: vq = vq.filter(Visit.visit_date >= s_date)
        if e_date: vq = vq.filter(Visit.visit_date <= e_date)
        visits = vq.order_by(Visit.id.asc()).all()
        if not visits and (s_date or e_date): continue
        res.append(PatientOutSchema(id=p.id, name=p.name, whatsapp_number=p.whatsapp_number, phone_number=p.phone_number, age=p.age, gender=p.gender, created_at=p.created_at, visit_count=len(visits), first_visit_date=visits[0].visit_date if visits else None, last_visit_date=visits[-1].visit_date if visits else None, last_token_number=visits[-1].token_number if visits else None, last_visit_reason=visits[-1].visit_reason if visits else None, total_completed=sum(1 for v in visits if v.status == VisitStatus.COMPLETED), total_cancelled=sum(1 for v in visits if v.status == VisitStatus.CANCELLED), total_waiting=sum(1 for v in visits if v.status in [VisitStatus.WAITING, VisitStatus.CURRENT])))
    return res

@app.delete("/patients/{patient_id}")
def delete_patient(patient_id: int, db: Session = Depends(get_db), current_admin: AdminSystem = Depends(get_current_admin)):
    patient = db.query(Patient).filter(Patient.id == patient_id, Patient.clinic_id == current_admin.clinic_id).first()
    if not patient: raise HTTPException(404, "Patient not found")
    patient.is_active = False
    db.commit()
    return {"message": "Patient deleted"}

@app.get("/doctor/patients/{patient_id}/history", response_model=List[VisitOutSchema])
def get_patient_history(patient_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    patient = db.query(Patient).filter(Patient.id == patient_id, Patient.clinic_id == current_user.clinic_id).first()
    if not patient: raise HTTPException(404, "Patient not found")
    visits = db.query(Visit).filter(Visit.patient_id == patient_id, Visit.clinic_id == current_user.clinic_id).order_by(Visit.id.desc()).all()
    return [VisitOutSchema(id=v.id, token_number=v.token_number, visit_date=v.visit_date, visit_reason=v.visit_reason, status=v.status, created_at=v.created_at, completed_at=v.completed_at, cancelled_at=v.cancelled_at, patient_id=patient.id, doctor_id=v.doctor_id, doctor_name=v.doctor.name if v.doctor else "Unknown", patient_name=patient.name, patient_age=patient.age, patient_gender=patient.gender, patient_phone=patient.phone_number or patient.whatsapp_number) for v in visits]

@app.get("/doctor/tokens", response_model=List[VisitOutSchema])
def get_tokens_by_date(date_str: Optional[str] = None, status_filter: Optional[str] = "ALL", db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    req_date = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else get_today_ist()
    q = db.query(Visit).filter(Visit.clinic_id == current_user.clinic_id, Visit.visit_date == req_date)
    if status_filter and status_filter != "ALL": q = q.filter(Visit.status == status_filter)
    visits = q.order_by(Visit.token_number.asc()).all()
    return [VisitOutSchema(id=v.id, token_number=v.token_number, visit_date=v.visit_date, visit_reason=v.visit_reason, status=v.status, created_at=v.created_at, completed_at=v.completed_at, cancelled_at=v.cancelled_at, patient_id=v.patient_id, doctor_id=v.doctor_id, doctor_name=v.doctor.name if v.doctor else "Unknown", patient_name=v.patient.name, patient_age=v.patient.age, patient_gender=v.patient.gender, patient_phone=v.patient.phone_number or v.patient.whatsapp_number) for v in visits]

@app.get("/webhook")
def verify_webhook(request: Request):
    verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "verify_token")
    if request.query_params.get("hub.mode") == "subscribe" and request.query_params.get("hub.verify_token") == verify_token:
        return HTMLResponse(content=request.query_params.get("hub.challenge"), status_code=200)
    raise HTTPException(403, "Verification failed")

@app.post("/webhook")
async def receive_webhook(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                val = change.get("value", {})
                if "statuses" in val: continue
                for msg in val.get("messages", []):
                    if msg.get("type") == "text":
                        try:
                            rec = ProcessedWebhookEvent(message_id=msg.get("id"), phone_number_id=val.get("metadata", {}).get("phone_number_id"), sender_number=msg.get("from"), event_type="text")
                            db.add(rec)
                            db.commit()
                        except IntegrityError: db.rollback(); continue
                        process_whatsapp_message(db, val.get("metadata", {}).get("phone_number_id"), msg.get("from"), msg.get("text", {}).get("body", ""), msg.get("id"))
    except Exception as e: print(e)
    return JSONResponse(content={"status": "received"}, status_code=200)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
