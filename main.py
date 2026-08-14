import os
import re
import traceback
from datetime import datetime
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm, OAuth2PasswordBearer
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from jose import JWTError, jwt
from pydantic import BaseModel, Field

from database import Base, engine, get_db, SessionLocal
from models import Doctor, Clinic, Patient, Visit, VisitStatus, ClinicCalendar, ProcessedWebhookEvent
from schemas import (
    TokenSchema, DoctorOutSchema, DashboardSummaryOutSchema, VisitOutSchema,
    PatientOutSchema, ManualPatientAddSchema, PatientSummarySchema, StatusUpdateSchema
)
from security import verify_password, create_access_token, get_password_hash
from whatsapp import process_whatsapp_message, get_today_ist, generate_daily_token

app = FastAPI(title="WhatsApp Clinic Token System API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class DoctorCreateSchema(BaseModel):
    name: str = Field(..., min_length=1)
    email: str = Field(..., min_length=5)
    password: str = Field(..., min_length=6)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")
SECRET_KEY = os.getenv("JWT_SECRET", "super-secret-key")
ALGORITHM = "HS256"

# ==========================================
# 1. ADMIN != DOCTOR AUTHENTICATION FIX
# ==========================================
class AdminUser:
    """Mock Admin Object so endpoints can safely extract .clinic_id without being a Doctor"""
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
        if not username:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    admin_email = os.getenv("ADMIN_USERNAME", "admin")
    
    # Check if this is the Admin
    if username == admin_email:
        clinic = db.query(Clinic).first()
        return AdminUser(email=admin_email, clinic=clinic)

    # Check if this is a real Doctor
    doc = db.query(Doctor).filter(Doctor.email == username, Doctor.is_active == True).first()
    if not doc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return doc

# ==========================================
# 2. STARTUP (Safe Migration)
# ==========================================
@app.on_event("startup")
def setup_database_and_admin():
    db = SessionLocal()
    try:
        # Ensure Clinic Exists
        clinic = db.query(Clinic).first()
        if not clinic:
            clinic = Clinic(
                name="System Default Clinic",
                whatsapp_phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID", "default_phone_id"),
                is_online=True
            )
            db.add(clinic)
            db.commit()

        # SAFEGUARD: Mark old Admin as inactive in Doctor table so they vanish from UI and WhatsApp
        admin_email = os.getenv("ADMIN_USERNAME", "admin")
        old_admin = db.query(Doctor).filter(Doctor.email == admin_email).first()
        if old_admin and old_admin.is_active:
            old_admin.is_active = False
            old_admin.is_online = False
            db.commit()
    finally:
        db.close()

# ==========================================
# 3. ROOT & HEALTH ENDPOINTS
# ==========================================
@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return HTMLResponse(content="<h1>Frontend UI missing (index.html not found)</h1>", status_code=404)

@app.get("/health")
def health_check():
    return {"status": "healthy"}

# ==========================================
# 4. LOGIN & AUTHENTICATION ENDPOINTS
# ==========================================
@app.post("/auth/login", response_model=TokenSchema)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    admin_email = os.getenv("ADMIN_USERNAME", "admin")
    admin_password = os.getenv("ADMIN_PASSWORD", "ChangeThisAdminPassword123!")

    # 1. Admin Login (100% Reliable via ENV)
    if form_data.username == admin_email and form_data.password == admin_password:
        access_token = create_access_token(data={"sub": admin_email, "role": "admin"})
        return {"access_token": access_token, "token_type": "bearer"}

    # 2. Doctor Login (For Future Proofing)
    doc = db.query(Doctor).filter(Doctor.email == form_data.username, Doctor.is_active == True).first()
    if doc and verify_password(form_data.password, doc.password_hash):
        access_token = create_access_token(data={"sub": doc.email, "role": "doctor"})
        return {"access_token": access_token, "token_type": "bearer"}

    raise HTTPException(status_code=400, detail="Invalid username or password")

@app.get("/auth/me")
def get_me(current_user = Depends(get_current_active_user)):
    """This will now securely return HTTP 200 for both Admin and Doctors."""
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
# 5. DOCTORS ENDPOINTS
# ==========================================
@app.post("/doctors/add", status_code=status.HTTP_201_CREATED)
def add_doctor(payload: DoctorCreateSchema, db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    if current_user.id != 0:
        raise HTTPException(status_code=403, detail="Only the system administrator can add doctors.")

    name = payload.name.strip()
    email = payload.email.strip().lower()
    
    if not name: raise HTTPException(status_code=400, detail="Doctor name is required.")
    if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email):
        raise HTTPException(status_code=400, detail="Invalid email format.")

    existing_doc = db.query(Doctor).filter(func.lower(Doctor.email) == email).first()
    if existing_doc or email == os.getenv("ADMIN_USERNAME", "admin"):
        raise HTTPException(status_code=409, detail="A doctor or admin with this email already exists.")

    new_doc = Doctor(
        clinic_id=current_user.clinic_id,
        name=name,
        email=email,
        password_hash=get_password_hash(payload.password),
        is_active=True,
        is_online=True
    )
    
    db.add(new_doc)
    db.commit()
    db.refresh(new_doc)

    return {"message": "Doctor added successfully", "doctor": {"id": new_doc.id, "name": new_doc.name, "email": new_doc.email}}

@app.get("/doctors", response_model=List[DoctorOutSchema])
def get_all_doctors(db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    admin_email = os.getenv("ADMIN_USERNAME", "admin")
    doctors = db.query(Doctor).filter(Doctor.clinic_id == current_user.clinic_id, Doctor.is_active == True, Doctor.email != admin_email).all()
    return [DoctorOutSchema(id=d.id, clinic_id=d.clinic_id, name=d.name, email=d.email, clinic_name=d.clinic.name, is_online=d.is_online) for d in doctors]

@app.put("/doctors/{doctor_id}/status", response_model=StatusUpdateSchema)
def update_doctor_status(doctor_id: int, payload: StatusUpdateSchema, db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    doc = db.query(Doctor).filter(Doctor.id == doctor_id, Doctor.clinic_id == current_user.clinic_id).first()
    if not doc: raise HTTPException(status_code=404, detail="Doctor not found")
    doc.is_online = payload.is_online
    db.commit()
    return {"is_online": doc.is_online}

# ==========================================
# 6. QUEUE & CLINIC MANAGEMENT
# ==========================================
@app.get("/doctor/{doctor_id}/queue")
def get_specific_doctor_queue(doctor_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    target_doctor = db.query(Doctor).filter(Doctor.id == doctor_id, Doctor.clinic_id == current_user.clinic_id).first()
    if not target_doctor: raise HTTPException(status_code=404, detail="Doctor not found")

    today = get_today_ist()
    visits = db.query(Visit).filter(Visit.doctor_id == doctor_id, Visit.visit_date == today).order_by(Visit.token_number.asc()).all()
    
    current_visit = next((v for v in visits if v.status == VisitStatus.CURRENT), None)
    waiting_visits = [v for v in visits if v.status == VisitStatus.WAITING]
    skipped_visits = [v for v in visits if v.status == VisitStatus.SKIPPED]

    def format_visit(v):
        if not v: return None
        return {"id": v.id, "token_number": v.token_number, "patient_name": v.patient.name, "patient_phone": v.patient.phone_number or v.patient.whatsapp_number, "visit_reason": v.visit_reason, "doctor_name": f"Dr. {target_doctor.name}"}

    return {
        "current_token": format_visit(current_visit),
        "next_patient": format_visit(waiting_visits[0]) if waiting_visits else None,
        "skipped_patients": [format_visit(v) for v in skipped_visits],
        "waiting_count": len(waiting_visits),
        "completed_count": sum(1 for v in visits if v.status == VisitStatus.COMPLETED),
        "today_total": len(visits)
    }

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
        return {"message": f"Token #{next_visit.token_number} is now CURRENT"}
    db.commit()
    return {"message": "No waiting patients remaining"}

@app.post("/visit/{visit_id}/{action}")
def manage_visit_status(visit_id: int, action: str, db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    visit = db.query(Visit).filter(Visit.id == visit_id).first()
    if not visit: raise HTTPException(404, detail="Visit not found")

    if action == "accept" or action == "recall":
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
    else:
        raise HTTPException(400, detail="Invalid action")
    
    db.commit()
    return {"message": f"Token #{visit.token_number} status updated to {visit.status}"}

@app.post("/doctor/add-walkin")
def add_walkin_patient(payload: ManualPatientAddSchema, db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    patient = db.query(Patient).filter(Patient.clinic_id == current_user.clinic_id, Patient.whatsapp_number == payload.whatsapp_number).first()
    if not patient:
        patient = Patient(clinic_id=current_user.clinic_id, name=payload.name, whatsapp_number=payload.whatsapp_number, phone_number=payload.phone_number, age=payload.age, gender=payload.gender)
        db.add(patient)
        db.commit()
        db.refresh(patient)
    try:
        visit = generate_daily_token(db, current_user.clinic_id, payload.doctor_id, patient.id, payload.visit_reason)
        return {"message": "Patient added successfully", "token_number": visit.token_number}
    except Exception:
        raise HTTPException(status_code=500, detail="Token generation error.")

@app.get("/doctor/today", response_model=DashboardSummaryOutSchema)
def get_today_summary(db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    today = get_today_ist()
    visits = db.query(Visit).filter(Visit.clinic_id == current_user.clinic_id, Visit.visit_date == today).order_by(Visit.token_number.asc()).all()
    clinic = db.query(Clinic).filter(Clinic.id == current_user.clinic_id).first()
    return DashboardSummaryOutSchema(
        clinic_name=clinic.name, today_date=today.strftime("%d %B %Y"), 
        waiting_count=sum(1 for v in visits if v.status == VisitStatus.WAITING), 
        completed_count=sum(1 for v in visits if v.status == VisitStatus.COMPLETED),
        cancelled_count=sum(1 for v in visits if v.status == VisitStatus.CANCELLED), 
        total_count=len(visits)
    )

@app.get("/clinic/status", response_model=StatusUpdateSchema)
def get_clinic_status(db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    clinic = db.query(Clinic).filter(Clinic.id == current_user.clinic_id).first()
    return {"is_online": clinic.is_online}

@app.put("/clinic/status", response_model=StatusUpdateSchema)
def update_clinic_status(payload: StatusUpdateSchema, db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    clinic = db.query(Clinic).filter(Clinic.id == current_user.clinic_id).first()
    clinic.is_online = payload.is_online
    db.commit()
    return {"is_online": clinic.is_online}

@app.get("/doctor/patients-summary", response_model=PatientSummarySchema)
def get_patients_summary(query: Optional[str] = None, start_date: Optional[str] = None, end_date: Optional[str] = None, db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    pq = db.query(Patient).filter(Patient.clinic_id == current_user.clinic_id)
    if query: pq = pq.filter(or_(Patient.name.ilike(f"%{query}%"), Patient.whatsapp_number.ilike(f"%{query}%")))
    patient_ids = {p.id for p in pq.all()}
    if not patient_ids: return PatientSummarySchema(total_patients=0, new_patients=0, returning_patients=0, total_visits=0, completed_visits=0, cancelled_visits=0, waiting_visits=0)

    vq = db.query(Visit).filter(Visit.clinic_id == current_user.clinic_id, Visit.patient_id.in_(patient_ids))
    if start_date: vq = vq.filter(Visit.visit_date >= datetime.strptime(start_date, "%Y-%m-%d").date())
    if end_date: vq = vq.filter(Visit.visit_date <= datetime.strptime(end_date, "%Y-%m-%d").date())
    visits = vq.all()

    return PatientSummarySchema(
        total_patients=len({v.patient_id for v in visits}), new_patients=0, returning_patients=0,
        total_visits=len(visits), completed_visits=sum(1 for v in visits if v.status == VisitStatus.COMPLETED),
        cancelled_visits=sum(1 for v in visits if v.status == VisitStatus.CANCELLED),
        waiting_visits=sum(1 for v in visits if v.status in [VisitStatus.WAITING, VisitStatus.CURRENT])
    )

@app.get("/doctor/patients", response_model=List[PatientOutSchema])
def search_patients(query: Optional[str] = None, start_date: Optional[str] = None, end_date: Optional[str] = None, db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    q = db.query(Patient).filter(Patient.clinic_id == current_user.clinic_id)
    if query: q = q.filter(or_(Patient.name.ilike(f"%{query}%"), Patient.whatsapp_number.ilike(f"%{query}%"), Patient.phone_number.ilike(f"%{query}%")))
    patients = q.order_by(Patient.id.desc()).all()
    res = []
    
    s_date = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
    e_date = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None
    
    for p in patients:
        vq = db.query(Visit).filter(Visit.patient_id == p.id)
        if s_date: vq = vq.filter(Visit.visit_date >= s_date)
        if e_date: vq = vq.filter(Visit.visit_date <= e_date)
        visits = vq.order_by(Visit.id.asc()).all()
        if not visits and (s_date or e_date): continue
        
        res.append(PatientOutSchema(
            id=p.id, name=p.name, whatsapp_number=p.whatsapp_number, phone_number=p.phone_number, age=p.age, gender=p.gender, created_at=p.created_at, visit_count=len(visits),
            first_visit_date=visits[0].visit_date if visits else None, last_visit_date=visits[-1].visit_date if visits else None,
            last_token_number=visits[-1].token_number if visits else None, last_visit_reason=visits[-1].visit_reason if visits else None,
            total_completed=sum(1 for v in visits if v.status == VisitStatus.COMPLETED), total_cancelled=sum(1 for v in visits if v.status == VisitStatus.CANCELLED), 
            total_waiting=sum(1 for v in visits if v.status in [VisitStatus.WAITING, VisitStatus.CURRENT])
        ))
    return res

@app.get("/doctor/patients/{patient_id}/history", response_model=List[VisitOutSchema])
def get_patient_history(patient_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    patient = db.query(Patient).filter(Patient.id == patient_id, Patient.clinic_id == current_user.clinic_id).first()
    if not patient: raise HTTPException(status_code=404, detail="Patient not found")
    visits = db.query(Visit).filter(Visit.patient_id == patient_id, Visit.clinic_id == current_user.clinic_id).order_by(Visit.id.desc()).all()
    return [
        VisitOutSchema(
            id=v.id, token_number=v.token_number, visit_date=v.visit_date, visit_reason=v.visit_reason, status=v.status, created_at=v.created_at, completed_at=v.completed_at, cancelled_at=v.cancelled_at, patient_id=patient.id, doctor_id=v.doctor_id, doctor_name=v.doctor.name if v.doctor else "Unknown", patient_name=patient.name, patient_age=patient.age, patient_gender=patient.gender, patient_phone=patient.phone_number or patient.whatsapp_number
        ) for v in visits
    ]

@app.get("/doctor/tokens", response_model=List[VisitOutSchema])
def get_tokens_by_date(date_str: Optional[str] = None, status_filter: Optional[str] = "ALL", db: Session = Depends(get_db), current_user = Depends(get_current_active_user)):
    req_date = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else get_today_ist()
    q = db.query(Visit).filter(Visit.clinic_id == current_user.clinic_id, Visit.visit_date == req_date)
    if status_filter and status_filter != "ALL": q = q.filter(Visit.status == status_filter)
    visits = q.order_by(Visit.token_number.asc()).all()
    return [
        VisitOutSchema(
            id=v.id, token_number=v.token_number, visit_date=v.visit_date, visit_reason=v.visit_reason, status=v.status, created_at=v.created_at, completed_at=v.completed_at, cancelled_at=v.cancelled_at, patient_id=v.patient_id, doctor_id=v.doctor_id, doctor_name=v.doctor.name if v.doctor else "Unknown", patient_name=v.patient.name, patient_age=v.patient.age, patient_gender=v.patient.gender, patient_phone=v.patient.phone_number or v.patient.whatsapp_number
        ) for v in visits
    ]

# ==========================================
# 7. WHATSAPP WEBHOOK (Unchanged)
# ==========================================
@app.get("/webhook")
def verify_webhook(request: Request):
    verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "verify_token")
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == verify_token:
            return HTMLResponse(content=challenge, status_code=200)
        raise HTTPException(status_code=403, detail="Verification failed")
    return {"status": "webhook endpoint ready"}

@app.post("/webhook")
async def receive_webhook(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    try:
        entries = data.get("entry", [])
        for entry in entries:
            changes = entry.get("changes", [])
            for change in changes:
                value = change.get("value", {})
                if "statuses" in value: continue
                    
                phone_number_id = value.get("metadata", {}).get("phone_number_id")
                messages = value.get("messages", [])

                for msg in messages:
                    msg_id, sender_phone, msg_type = msg.get("id"), msg.get("from"), msg.get("type")
                    if msg_type == "text" and msg_id and sender_phone and phone_number_id:
                        msg_body = msg.get("text", {}).get("body", "")
                        try:
                            processed_record = ProcessedWebhookEvent(
                                message_id=msg_id, phone_number_id=phone_number_id,
                                sender_number=sender_phone, event_type="text"
                            )
                            db.add(processed_record)
                            db.commit()
                        except IntegrityError:
                            db.rollback()
                            continue
                        
                        process_whatsapp_message(db, phone_number_id, sender_phone, msg_body, msg_id)
    except Exception as e:
        print(f"WEBHOOK ERROR: {str(e)}")
        traceback.print_exc()
    return JSONResponse(content={"status": "received"}, status_code=200)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
