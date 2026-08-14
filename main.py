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
from pydantic import BaseModel, EmailStr, Field

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
# 1. NEW: DEDICATED ADMIN SYSTEM MODEL
# ==========================================
class AdminSystem(Base):
    __tablename__ = "system_admins"
    id = Column(Integer, primary_key=True, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id"))
    username = Column(String, unique=True, index=True)
    password_hash = Column(String)

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
# 4. SAFE STARTUP MIGRATION
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
            
        # Migrate Admin to system_admins safely
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

        # REMOVE ADMIN FROM DOCTORS TABLE (Preserve history, hide from UI)
        old_admin_doc = db.query(Doctor).filter(Doctor.email == admin_email).first()
        if old_admin_doc:
            old_admin_doc.is_active = False
            old_admin_doc.is_online = False
            old_admin_doc.name = "Legacy System Admin (Hidden)"
            db.commit()

        # NOTE: Removed the destructive "UPDATE visits SET doctor_id = admin.id" logic forever.
                
    except Exception as e:
        print(f"Error initializing app/database: {e}")
    finally:
        db.close()

# ==========================================
# 5. AUTH LOGIN ENDPOINT
# ==========================================
@app.post("/auth/login", response_model=TokenSchema)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    # 1. Check Admin
    admin = db.query(AdminSystem).filter(AdminSystem.username == form_data.username).first()
    if admin and verify_password(form_data.password, admin.password_hash):
        access_token = create_access_token(data={"sub": admin.username, "role": "admin"})
        return {"access_token": access_token, "token_type": "bearer"}

    # 2. Check Doctor (Allows future doctor login features)
    doctor = db.query(Doctor).filter(Doctor.email == form_data.username, Doctor.is_active == True).first()
    if doctor and verify_password(form_data.password, doctor.password_hash):
        access_token = create_access_token(data={"sub": doctor.email, "role": "doctor"})
        return {"access_token": access_token, "token_type": "bearer"}

    raise HTTPException(status_code=400, detail="Invalid username or password")

# ==========================================
# 6. ADD NEW DOCTOR ENDPOINT
# ==========================================
@app.post("/doctors/add", status_code=status.HTTP_201_CREATED)
def add_doctor(payload: DoctorCreateSchema, db: Session = Depends(get_db), current_admin: AdminSystem = Depends(get_current_admin)):
    name = payload.name.strip()
    email = payload.email.strip().lower()
    password = payload.password
    
    # Validations
    if not name:
        raise HTTPException(status_code=400, detail="Doctor name is required.")
    
    email_regex = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
    if not re.match(email_regex, email):
        raise HTTPException(status_code=400, detail="Invalid email format.")

    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")

    # Check Email Duplication
    existing_doc = db.query(Doctor).filter(func.lower(Doctor.email) == email).first()
    if existing_doc:
        raise HTTPException(status_code=409, detail="A doctor with this email already exists.")
        
    existing_admin = db.query(AdminSystem).filter(func.lower(AdminSystem.username) == email).first()
    if existing_admin:
        raise HTTPException(status_code=409, detail="This email is reserved for system administrators.")

    # Create Real Doctor Record
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
        "message": "Doctor added successfully!",
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
# 7. FETCH DOCTORS (SAFEGUARDED)
# ==========================================
@app.get("/doctors", response_model=List[DoctorOutSchema])
def get_all_doctors(db: Session = Depends(get_db), current_admin: AdminSystem = Depends(get_current_admin)):
    admin_email = os.getenv("ADMIN_USERNAME", "admin")
    
    # EXCLUDE admin & deactivated doctors completely
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

# Rest of your existing endpoints (status, queue, walkin, webhook etc)
# Remain the same, just ensure they require current_admin: AdminSystem = Depends(get_current_admin)
