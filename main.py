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
from sqlalchemy import Column, Integer, String, ForeignKey, func, or_, text
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
from whatsapp import process_whatsapp_message, get_today_ist, generate_daily_token

# ==========================================
# 1. ADMIN SYSTEM MODEL
# ==========================================
class AdminSystem(Base):
    __tablename__ = "system_admins"
    id = Column(Integer, primary_key=True, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id"))
    username = Column(String, unique=True, index=True)
    password_hash = Column(String)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="WhatsApp Clinic Token System API", version="1.0.0")

# CORS config to allow same-origin requests seamlessly
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
    name: str = Field(..., min_length=1, description="Doctor's full name")
    email: str = Field(..., min_length=5, description="Doctor's email/login ID")
    password: str = Field(..., min_length=6, description="Password (min 6 chars)")

# ==========================================
# 3. AUTHENTICATION (ADMIN & DOCTOR)
# ==========================================
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")
SECRET_KEY = os.getenv("JWT_SECRET", "super-secret-key")
ALGORITHM = "HS256"

def get_current_admin(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        role: str = payload.get("role")
        if username is None or role != "admin":
            raise HTTPException(status_code=403, detail="Only Admins can perform this action.")
    except JWTError:
        raise credentials_exception
        
    admin = db.query(AdminSystem).filter(AdminSystem.username == username).first()
    if admin is None:
        raise credentials_exception
    return admin

# ==========================================
# 4. SAFE STARTUP MIGRATION (No Data Loss)
# ==========================================
@app.on_event("startup")
def setup_database_and_admin():
    db = SessionLocal()
    try:
        admin_email = os.getenv("ADMIN_USERNAME", "admin")
        admin_password = os.getenv("ADMIN_PASSWORD", "ChangeThisAdminPassword123!")
        
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
            db.refresh(clinic)
            
        # Migrate Admin securely to system_admins
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

        # SAFEGUARD: Remove Admin from Doctors Table (Preserves historical visits, hides from UI)
        old_admin_doc = db.query(Doctor).filter(Doctor.email == admin_email).first()
        if old_admin_doc and old_admin_doc.is_active:
            old_admin_doc.is_active = False
            old_admin_doc.is_online = False
            old_admin_doc.name = "Legacy System Admin (Hidden)"
            db.commit()
                
    except Exception as e:
        print(f"Error initializing app/database: {e}")
    finally:
        db.close()

# ==========================================
# 5. FRONTEND & HEALTH ENDPOINTS (Fixed Root)
# ==========================================
@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def serve_frontend():
    """Serves the index.html directly from the root URL"""
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return HTMLResponse(content="<h1>Frontend UI missing (index.html not found)</h1>", status_code=404)

@app.get("/health")
def health_check():
    """System health check endpoint"""
    return {"status": "healthy"}

# ==========================================
# 6. AUTHENTICATION ENDPOINTS
# ==========================================
@app.post("/auth/login", response_model=TokenSchema)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    admin = db.query(AdminSystem).filter(AdminSystem.username == form_data.username).first()
    if admin and verify_password(form_data.password, admin.password_hash):
        access_token = create_access_token(data={"sub": admin.username, "role": "admin"})
        return {"access_token": access_token, "token_type": "bearer"}

    doctor = db.query(Doctor).filter(Doctor.email == form_data.username, Doctor.is_active == True).first()
    if doctor and verify_password(form_data.password, doctor.password_hash):
        access_token = create_access_token(data={"sub": doctor.email, "role": "doctor"})
        return {"access_token": access_token, "token_type": "bearer"}

    raise HTTPException(status_code=400, detail="Invalid username or password")

@app.get("/auth/me")
def get_me(current_admin: AdminSystem = Depends(get_current_admin)):
    return {
        "id": current_admin.id,
        "clinic_id": current_admin.clinic_id,
        "name": "System Administrator",
        "email": current_admin.username,
        "clinic_name": "Clinic Management",
        "is_online": True, 
        "role": "admin"
    }

# ==========================================
# 7. ADD DOCTOR ENDPOINT
# ==========================================
@app.post("/doctors/add", status_code=status.HTTP_201_CREATED)
def add_doctor(payload: DoctorCreateSchema, db: Session = Depends(get_db), current_admin: AdminSystem = Depends(get_current_admin)):
    name = payload.name.strip()
    email = payload.email.strip().lower()
    password = payload.password
    
    if not name:
        raise HTTPException(status_code=400, detail="Doctor name is required.")
    
    email_regex = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
    if not re.match(email_regex, email):
        raise HTTPException(status_code=400, detail="Invalid email format.")

    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")

    existing_doc = db.query(Doctor).filter(func.lower(Doctor.email) == email).first()
    if existing_doc:
        raise HTTPException(status_code=409, detail="A doctor with this email already exists.")
        
    existing_admin = db.query(AdminSystem).filter(func.lower(AdminSystem.username) == email).first()
    if existing_admin:
        raise HTTPException(status_code=409, detail="This email is reserved for system administrators.")

    new_doc = Doctor(
        clinic_id=current_admin.clinic_id,
        name=name,
        email=email,
        password_hash=get_password_hash(password),
        is_active=True,
        is_online=True
    )
    
    try:
        db.add(new_doc)
        db.commit()
        db.refresh(new_doc)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="Database error occurred while saving the doctor.")

    return {
        "message": "Doctor added successfully",
        "doctor": {
            "id": new_doc.id,
            "name": new_doc.name,
            "email": new_doc.email,
            "clinic_id": new_doc.clinic_id,
            "is_active": new_doc.is_active,
            "is_online": new_doc.is_online
        }
    }

# ==========================================
# 8. FETCH REAL DOCTORS ONLY
# ==========================================
@app.get("/doctors", response_model=List[DoctorOutSchema])
def get_all_doctors(db: Session = Depends(get_db), current_admin: AdminSystem = Depends(get_current_admin)):
    admin_email = os.getenv("ADMIN_USERNAME", "admin")
    
    doctors = db.query(Doctor).filter(
        Doctor.clinic_id == current_admin.clinic_id, 
        Doctor.is_active == True,
        Doctor.email != admin_email 
    ).all()
    
    return [
        DoctorOutSchema(
            id=d.id, clinic_id=d.clinic_id, name=d.name, email=d.email,
            clinic_name=d.clinic.name, is_online=d.is_online
        ) for d in doctors
    ]

@app.put("/doctors/{doctor_id}/status", response_model=StatusUpdateSchema)
def update_doctor_status(doctor_id: int, payload: StatusUpdateSchema, db: Session = Depends(get_db), current_admin: AdminSystem = Depends(get_current_admin)):
    doc = db.query(Doctor).filter(Doctor.id == doctor_id, Doctor.clinic_id == current_admin.clinic_id).first()
    if not doc: raise HTTPException(status_code=404, detail="Doctor not found")
    doc.is_online = payload.is_online
    db.commit()
    return {"is_online": doc.is_online}

# ==========================================
# 9. QUEUE & CLINIC MANAGEMENT
# ==========================================
@app.get("/doctor/{doctor_id}/queue")
def get_specific_doctor_queue(doctor_id: int, db: Session = Depends(get_db), current_admin: AdminSystem = Depends(get_current_admin)):
    target_doctor = db.query(Doctor).filter(Doctor.id == doctor_id, Doctor.clinic_id == current_admin.clinic_id).first()
    if not target_doctor: raise HTTPException(status_code=404, detail="Doctor not found")

    today = get_today_ist()
    visits = db.query(Visit).filter(Visit.doctor_id == doctor_id, Visit.visit_date == today).order_by(Visit.token_number.asc()).all()
    
    current_visit = next((v for v in visits if v.status == VisitStatus.CURRENT), None)
    waiting_visits = [v for v in visits if v.status == VisitStatus.WAITING]
    skipped_visits = [v for v in visits if v.status == VisitStatus.SKIPPED]

    def format_visit(v):
        if not v: return None
        return {
            "id": v.id, "token_number": v.token_number, "patient_name": v.patient.name,
            "patient_phone": v.patient.phone_number or v.patient.whatsapp_number,
            "visit_reason": v.visit_reason, "doctor_name": f"Dr. {target_doctor.name}"
        }

    return {
        "current_token": format_visit(current_visit),
        "next_patient": format_visit(waiting_visits[0]) if waiting_visits else None,
        "skipped_patients": [format_visit(v) for v in skipped_visits],
        "waiting_count": len(waiting_visits),
        "completed_count": sum(1 for v in visits if v.status == VisitStatus.COMPLETED),
        "today_total": len(visits)
    }

@app.post("/doctor/{doctor_id}/next-patient")
def next_patient(doctor_id: int, db: Session = Depends(get_db), current_admin: AdminSystem = Depends(get_current_admin)):
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

@app.post("/doctor/next-patient")
def next_patient_fallback(payload: dict = None, db: Session = Depends(get_db), current_admin: AdminSystem = Depends(get_current_admin)):
    doctor_id = payload.get("doctor_id") if payload else None
    if not doctor_id:
        raise HTTPException(status_code=400, detail="doctor_id is required in the payload.")
    return next_patient(doctor_id, db, current_admin)

@app.get("/clinic/status", response_model=StatusUpdateSchema)
def get_clinic_status(db: Session = Depends(get_db), current_admin: AdminSystem = Depends(get_current_admin)):
    clinic = db.query(Clinic).filter(Clinic.id == current_admin.clinic_id).first()
    return {"is_online": clinic.is_online}

@app.put("/clinic/status", response_model=StatusUpdateSchema)
def update_clinic_status(payload: StatusUpdateSchema, db: Session = Depends(get_db), current_admin: AdminSystem = Depends(get_current_admin)):
    clinic = db.query(Clinic).filter(Clinic.id == current_admin.clinic_id).first()
    clinic.is_online = payload.is_online
    db.commit()
    return {"is_online": clinic.is_online}

@app.post("/doctor/add-walkin")
def add_walkin_patient(payload: ManualPatientAddSchema, db: Session = Depends(get_db), current_admin: AdminSystem = Depends(get_current_admin)):
    patient = db.query(Patient).filter(Patient.clinic_id == current_admin.clinic_id, Patient.whatsapp_number == payload.whatsapp_number).first()
    if not patient:
        patient = Patient(clinic_id=current_admin.clinic_id, name=payload.name, whatsapp_number=payload.whatsapp_number, phone_number=payload.phone_number, age=payload.age, gender=payload.gender)
        db.add(patient)
        db.commit()
        db.refresh(patient)
    try:
        visit = generate_daily_token(db, current_admin.clinic_id, payload.doctor_id, patient.id, payload.visit_reason)
        return {"message": "Patient added successfully", "token_number": visit.token_number}
    except Exception:
        raise HTTPException(status_code=500, detail="Token generation error.")

@app.get("/doctor/today", response_model=DashboardSummaryOutSchema)
def get_today_summary(db: Session = Depends(get_db), current_admin: AdminSystem = Depends(get_current_admin)):
    today = get_today_ist()
    visits = db.query(Visit).filter(Visit.clinic_id == current_admin.clinic_id, Visit.visit_date == today).order_by(Visit.token_number.asc()).all()
    clinic = db.query(Clinic).filter(Clinic.id == current_admin.clinic_id).first()
    return DashboardSummaryOutSchema(
        clinic_name=clinic.name, today_date=today.strftime("%d %B %Y"), 
        waiting_count=sum(1 for v in visits if v.status == VisitStatus.WAITING), 
        completed_count=sum(1 for v in visits if v.status == VisitStatus.COMPLETED),
        cancelled_count=sum(1 for v in visits if v.status == VisitStatus.CANCELLED), 
        total_count=len(visits)
    )

# ==========================================
# 10. WHATSAPP WEBHOOK (Unchanged/Safe)
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
