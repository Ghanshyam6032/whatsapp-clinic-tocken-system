import os
import traceback
import calendar as pycalendar
from datetime import datetime, date
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from sqlalchemy import func, or_, text
from sqlalchemy.exc import IntegrityError

from database import Base, engine, get_db, SessionLocal
from models import Doctor, Clinic, Patient, Visit, VisitStatus, ClinicCalendar, ProcessedWebhookEvent
from schemas import (
    TokenSchema, DoctorOutSchema,
    DashboardSummaryOutSchema, VisitOutSchema, PatientOutSchema, ManualPatientAddSchema,
    PatientSummarySchema, StatusUpdateSchema
)
from security import verify_password, create_access_token, get_current_doctor, get_password_hash
from whatsapp import process_whatsapp_message, get_today_ist, generate_daily_token

Base.metadata.create_all(bind=engine)

app = FastAPI(title="WhatsApp Clinic Token System API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def setup_database_and_admin():
    db = SessionLocal()
    try:
        # Safe schema migration for preserving existing data 
        try:
            db.execute(text("ALTER TABLE patients ADD COLUMN phone_number VARCHAR(20)"))
            db.commit()
        except Exception:
            db.rollback() 
            
        try:
            db.execute(text("ALTER TABLE visits ADD COLUMN doctor_id INTEGER REFERENCES doctors(id)"))
            db.commit()
        except Exception:
            db.rollback() 

        try:
            db.execute(text("ALTER TABLE clinics ADD COLUMN is_online BOOLEAN DEFAULT TRUE NOT NULL"))
            db.commit()
        except Exception:
            db.rollback()

        try:
            db.execute(text("ALTER TABLE doctors ADD COLUMN is_online BOOLEAN DEFAULT TRUE NOT NULL"))
            db.commit()
        except Exception:
            db.rollback()

        # Load environment variables safely
        admin_email = os.getenv("ADMIN_USERNAME", "admin")
        admin_password = os.getenv("ADMIN_PASSWORD", "ChangeThisAdminPassword123!")
        
        # Safe Debugging
        print("\n--- AUTHENTICATION CONFIGURATION DEBUG ---")
        print(f"ADMIN_USERNAME loaded: {admin_email}")
        print(f"ADMIN_PASSWORD configured: {'YES' if admin_password else 'NO'}")
        print("------------------------------------------\n")
        
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
            
        admin = db.query(Doctor).filter(Doctor.email == admin_email).first()
        if not admin:
            # Create new admin if they don't exist
            admin = Doctor(
                clinic_id=clinic.id,
                name="Admin Doctor",
                email=admin_email,
                password_hash=get_password_hash(admin_password),
                is_active=True,
                is_online=True
            )
            db.add(admin)
            db.commit()
            db.refresh(admin)
        else:
            # CRITICAL FIX: If admin exists but password in .env changed, update the database hash
            if not verify_password(admin_password, admin.password_hash):
                admin.password_hash = get_password_hash(admin_password)
                db.commit()
                db.refresh(admin)
                print(f"[INFO] Updated database password hash for {admin_email} to match current .env")
            
        # Migrate existing backward-compatible visits to the default admin doctor
        db.execute(text(f"UPDATE visits SET doctor_id = {admin.id} WHERE doctor_id IS NULL"))
        db.commit()
        
    except Exception as e:
        print(f"Error initializing app/database: {e}")
    finally:
        db.close()

@app.get("/health")
def health_check():
    return {"status": "healthy"}

@app.get("/")
def root():
    return {"status": "Online"}

# Frontend Serve Karne Ka Naya Route
@app.get("/dashboard")
def serve_dashboard():
    return FileResponse("index.html")

@app.post("/auth/login", response_model=TokenSchema)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    doctor = db.query(Doctor).filter(Doctor.email == form_data.username).first()
    if not doctor or not verify_password(form_data.password, doctor.password_hash):
        raise HTTPException(status_code=400, detail="Invalid username or password")

    access_token = create_access_token(data={"sub": str(doctor.id)})
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/auth/me", response_model=DoctorOutSchema)
def get_me(current_doctor: Doctor = Depends(get_current_doctor)):
    return DoctorOutSchema(
        id=current_doctor.id,
        clinic_id=current_doctor.clinic_id,
        name=current_doctor.name,
        email=current_doctor.email,
        clinic_name=current_doctor.clinic.name,
        is_online=current_doctor.is_online
    )

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
                
                if "statuses" in value:
                    continue
                    
                phone_number_id = value.get("metadata", {}).get("phone_number_id")
                messages = value.get("messages", [])

                for msg in messages:
                    msg_id = msg.get("id")
                    sender_phone = msg.get("from")
                    msg_type = msg.get("type")
                    
                    if msg_type == "text" and msg_id and sender_phone and phone_number_id:
                        msg_body = msg.get("text", {}).get("body", "")
                        
                        try:
                            processed_record = ProcessedWebhookEvent(
                                message_id=msg_id,
                                phone_number_id=phone_number_id,
                                sender_number=sender_phone,
                                event_type="text"
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

@app.get("/clinic/status", response_model=StatusUpdateSchema)
def get_clinic_status(db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    clinic = db.query(Clinic).filter(Clinic.id == current_doctor.clinic_id).first()
    return {"is_online": clinic.is_online}

@app.put("/clinic/status", response_model=StatusUpdateSchema)
def update_clinic_status(payload: StatusUpdateSchema, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    clinic = db.query(Clinic).filter(Clinic.id == current_doctor.clinic_id).first()
    clinic.is_online = payload.is_online
    db.commit()
    return {"is_online": clinic.is_online}

@app.get("/doctors", response_model=List[DoctorOutSchema])
def get_all_doctors(db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    doctors = db.query(Doctor).filter(Doctor.clinic_id == current_doctor.clinic_id, Doctor.is_active == True).all()
    # Explicitly map the properties to the Pydantic schema
    return [
        DoctorOutSchema(
            id=d.id,
            clinic_id=d.clinic_id,
            name=d.name,
            email=d.email,
            clinic_name=d.clinic.name,
            is_online=d.is_online
        ) for d in doctors
    ]

@app.put("/doctors/{doctor_id}/status", response_model=StatusUpdateSchema)
def update_doctor_status(doctor_id: int, payload: StatusUpdateSchema, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    doc = db.query(Doctor).filter(Doctor.id == doctor_id, Doctor.clinic_id == current_doctor.clinic_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Doctor not found")
    doc.is_online = payload.is_online
    db.commit()
    return {"is_online": doc.is_online}

@app.get("/doctor/{doctor_id}/queue")
def get_specific_doctor_queue(doctor_id: int, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    # 1. Verify the requested doctor belongs to the admin's clinic
    target_doctor = db.query(Doctor).filter(Doctor.id == doctor_id, Doctor.clinic_id == current_doctor.clinic_id).first()
    if not target_doctor:
        raise HTTPException(status_code=404, detail="Doctor not found")

    # 2. Fetch today's visits for THIS specific doctor
    today = get_today_ist()
    visits = db.query(Visit).filter(Visit.doctor_id == doctor_id, Visit.visit_date == today).order_by(Visit.token_number.asc()).all()
    
    # 3. Categorize visits
    current_visit = next((v for v in visits if v.status == VisitStatus.CURRENT), None)
    waiting_visits = [v for v in visits if v.status == VisitStatus.WAITING]
    skipped_visits = [v for v in visits if v.status == VisitStatus.SKIPPED]
    next_patient = waiting_visits[0] if waiting_visits else None

    # 4. Helper function to format the response exactly as the frontend expects
    def format_visit(v):
        if not v:
            return None
        return {
            "id": v.id,
            "token_number": v.token_number,
            "patient_name": v.patient.name,
            "patient_phone": v.patient.phone_number or v.patient.whatsapp_number,
            "visit_reason": v.visit_reason,
            "doctor_name": f"Dr. {target_doctor.name}"
        }

    # 5. Return the structured dictionary
    return {
        "current_token": format_visit(current_visit),
        "next_patient": format_visit(next_patient),
        "skipped_patients": [format_visit(v) for v in skipped_visits],
        "waiting_count": len(waiting_visits),
        "completed_count": sum(1 for v in visits if v.status == VisitStatus.COMPLETED),
        "today_total": len(visits)
    }

@app.post("/visit/{visit_id}/accept")
def accept_visit(visit_id: int, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    visit = db.query(Visit).filter(Visit.id == visit_id).first()
    if not visit: raise HTTPException(404, detail="Visit not found")
    if visit.status not in [VisitStatus.WAITING, VisitStatus.SKIPPED]:
        raise HTTPException(400, detail="Can only accept WAITING or SKIPPED tokens.")
    
    # Complete any existing current visit for this specific doctor
    today = get_today_ist()
    curr_visit = db.query(Visit).filter(Visit.doctor_id == visit.doctor_id, Visit.visit_date == today, Visit.status == VisitStatus.CURRENT).first()
    if curr_visit:
        curr_visit.status = VisitStatus.COMPLETED
        curr_visit.completed_at = datetime.utcnow()

    visit.status = VisitStatus.CURRENT
    db.commit()
    return {"message": f"Token #{visit.token_number} accepted and is now CURRENT."}

@app.post("/visit/{visit_id}/skip")
def skip_visit(visit_id: int, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    visit = db.query(Visit).filter(Visit.id == visit_id).first()
    if not visit: raise HTTPException(404, detail="Visit not found")
    
    if visit.status != VisitStatus.WAITING:
        raise HTTPException(400, detail="Can only skip WAITING tokens.")
        
    visit.status = VisitStatus.SKIPPED
    db.commit()
    return {"message": f"Token #{visit.token_number} skipped."}

@app.post("/visit/{visit_id}/recall")
def recall_visit(visit_id: int, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    return accept_visit(visit_id, db, current_doctor)

@app.post("/visit/{visit_id}/cancel")
def cancel_visit(visit_id: int, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    visit = db.query(Visit).filter(Visit.id == visit_id).first()
    if not visit: raise HTTPException(404, detail="Visit not found")
    
    visit.status = VisitStatus.CANCELLED
    visit.cancelled_at = datetime.utcnow()
    db.commit()
    return {"message": f"Token #{visit.token_number} cancelled."}

@app.get("/doctor/today", response_model=DashboardSummaryOutSchema)
def get_today_summary(db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    today = get_today_ist()
    # We fetch ALL visits for the clinic today to show in the main dashboard summary
    visits = db.query(Visit).filter(Visit.clinic_id == current_doctor.clinic_id, Visit.visit_date == today).order_by(Visit.token_number.asc()).all()

    waiting_cnt = sum(1 for v in visits if v.status == VisitStatus.WAITING)
    completed_cnt = sum(1 for v in visits if v.status == VisitStatus.COMPLETED)
    cancelled_cnt = sum(1 for v in visits if v.status == VisitStatus.CANCELLED)

    return DashboardSummaryOutSchema(
        clinic_name=current_doctor.clinic.name, 
        today_date=today.strftime("%d %B %Y"), 
        waiting_count=waiting_cnt, 
        completed_count=completed_cnt,
        cancelled_count=cancelled_cnt, 
        total_count=len(visits)
    )

@app.post("/doctor/next-patient")
def next_patient(db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    today = get_today_ist()

    curr_visit = db.query(Visit).filter(Visit.doctor_id == current_doctor.id, Visit.visit_date == today, Visit.status == VisitStatus.CURRENT).first()
    if curr_visit:
        curr_visit.status = VisitStatus.COMPLETED
        curr_visit.completed_at = datetime.utcnow()

    next_visit = db.query(Visit).filter(Visit.doctor_id == current_doctor.id, Visit.visit_date == today, Visit.status == VisitStatus.WAITING).order_by(Visit.token_number.asc()).first()
    if next_visit:
        next_visit.status = VisitStatus.CURRENT
        db.commit()
        return {"message": f"Token #{next_visit.token_number} is now CURRENT"}
    
    db.commit()
    return {"message": "No waiting patients remaining"}

@app.post("/doctor/add-walkin")
def add_walkin_patient(payload: ManualPatientAddSchema, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    clinic_id = current_doctor.clinic_id
    patient = db.query(Patient).filter(Patient.clinic_id == clinic_id, Patient.whatsapp_number == payload.whatsapp_number).first()

    if not patient:
        patient = Patient(clinic_id=clinic_id, name=payload.name, whatsapp_number=payload.whatsapp_number, phone_number=payload.phone_number, age=payload.age, gender=payload.gender)
        db.add(patient)
        db.commit()
        db.refresh(patient)
    
    try:
        # Use the specific doctor_id passed from the UI
        visit = generate_daily_token(db, clinic_id, payload.doctor_id, patient.id, payload.visit_reason)
        return {"message": "Patient added successfully", "token_number": visit.token_number}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Concurrency issue during token generation. Please retry.")

@app.get("/doctor/calendar/{year}/{month}")
def get_month_calendar(year: int, month: int, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    clinic_id = current_doctor.clinic_id
    num_days = pycalendar.monthrange(year, month)[1]
    closed_records = db.query(ClinicCalendar).filter(ClinicCalendar.clinic_id == clinic_id, func.extract('year', ClinicCalendar.date) == year, func.extract('month', ClinicCalendar.date) == month, ClinicCalendar.status == "CLOSED").all()
    closed_days = {r.date.day for r in closed_records}
    
    calendar_days = []
    for day in range(1, num_days + 1):
        status = "CLOSED" if day in closed_days else "OPEN"
        calendar_days.append({"day": day, "date": f"{year}-{month:02d}-{day:02d}", "status": status})
    return {"year": year, "month": month, "days": calendar_days}

@app.post("/doctor/calendar/{date_str}/close")
def close_clinic_date(date_str: str, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    clinic_id = current_doctor.clinic_id
    rec = db.query(ClinicCalendar).filter(ClinicCalendar.clinic_id == clinic_id, ClinicCalendar.date == d).first()
    if not rec:
        rec = ClinicCalendar(clinic_id=clinic_id, date=d, status="CLOSED")
        db.add(rec)
    else:
        rec.status = "CLOSED"
    db.commit()
    return {"message": f"Date {date_str} is now CLOSED"}

@app.post("/doctor/calendar/{date_str}/open")
def open_clinic_date(date_str: str, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    rec = db.query(ClinicCalendar).filter(ClinicCalendar.clinic_id == current_doctor.clinic_id, ClinicCalendar.date == d).first()
    if rec:
        db.delete(rec)
        db.commit()
    return {"message": f"Date {date_str} is now OPEN"}

@app.get("/doctor/patients-summary", response_model=PatientSummarySchema)
def get_patients_summary(query: Optional[str] = None, start_date: Optional[str] = None, end_date: Optional[str] = None, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    s_date = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
    e_date = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None

    pq = db.query(Patient).filter(Patient.clinic_id == current_doctor.clinic_id)
    if query:
        pq = pq.filter(or_(Patient.name.ilike(f"%{query}%"), Patient.whatsapp_number.ilike(f"%{query}%"), Patient.phone_number.ilike(f"%{query}%")))
    
    patient_ids = {p.id for p in pq.all()}

    if not patient_ids:
        return PatientSummarySchema(total_patients=0, new_patients=0, returning_patients=0, total_visits=0, completed_visits=0, cancelled_visits=0, waiting_visits=0)

    # Note: Summary spans all doctors in the clinic now, not just the single admin.
    vq = db.query(Visit).filter(Visit.clinic_id == current_doctor.clinic_id, Visit.patient_id.in_(patient_ids))
    if s_date: vq = vq.filter(Visit.visit_date >= s_date)
    if e_date: vq = vq.filter(Visit.visit_date <= e_date)
    visits = vq.all()

    total_visits = len(visits)
    completed_visits = sum(1 for v in visits if v.status == VisitStatus.COMPLETED)
    cancelled_visits = sum(1 for v in visits if v.status == VisitStatus.CANCELLED)
    waiting_visits = sum(1 for v in visits if v.status in [VisitStatus.WAITING, VisitStatus.CURRENT])
    patient_ids_in_range = {v.patient_id for v in visits}
    
    total_patients = len(patient_ids_in_range)
    new_patients = sum(1 for pid in patient_ids_in_range if s_date and e_date and s_date <= db.query(Patient).get(pid).created_at.date() <= e_date) if s_date and e_date else len(patient_ids)
    returning_patients = total_patients - new_patients

    return PatientSummarySchema(total_patients=total_patients, new_patients=new_patients, returning_patients=returning_patients, total_visits=total_visits, completed_visits=completed_visits, cancelled_visits=cancelled_visits, waiting_visits=waiting_visits)

@app.get("/doctor/patients", response_model=List[PatientOutSchema])
def search_patients(query: Optional[str] = None, start_date: Optional[str] = None, end_date: Optional[str] = None, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    q = db.query(Patient).filter(Patient.clinic_id == current_doctor.clinic_id)
    if query:
        q = q.filter(or_(Patient.name.ilike(f"%{query}%"), Patient.whatsapp_number.ilike(f"%{query}%"), Patient.phone_number.ilike(f"%{query}%")))

    patients = q.order_by(Patient.id.desc()).all()
    s_date = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
    e_date = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None

    res = []
    for p in patients:
        vq = db.query(Visit).filter(Visit.patient_id == p.id, Visit.clinic_id == current_doctor.clinic_id)
        if s_date: vq = vq.filter(Visit.visit_date >= s_date)
        if e_date: vq = vq.filter(Visit.visit_date <= e_date)
        
        visits = vq.order_by(Visit.id.asc()).all()
        if not visits and (s_date or e_date):
            continue
            
        res.append(PatientOutSchema(
            id=p.id, name=p.name, whatsapp_number=p.whatsapp_number, phone_number=p.phone_number, age=p.age, gender=p.gender, created_at=p.created_at, visit_count=len(visits),
            first_visit_date=visits[0].visit_date if visits else None, last_visit_date=visits[-1].visit_date if visits else None,
            last_token_number=visits[-1].token_number if visits else None, last_visit_reason=visits[-1].visit_reason if visits else None,
            total_completed=sum(1 for v in visits if v.status == VisitStatus.COMPLETED), 
            total_cancelled=sum(1 for v in visits if v.status == VisitStatus.CANCELLED), 
            total_waiting=sum(1 for v in visits if v.status in [VisitStatus.WAITING, VisitStatus.CURRENT])
        ))
    return res

@app.get("/doctor/patients/{patient_id}/history", response_model=List[VisitOutSchema])
def get_patient_history(patient_id: int, db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    patient = db.query(Patient).filter(Patient.id == patient_id, Patient.clinic_id == current_doctor.clinic_id).first()
    if not patient: raise HTTPException(status_code=404, detail="Patient not found")
    visits = db.query(Visit).filter(Visit.patient_id == patient_id, Visit.clinic_id == current_doctor.clinic_id).order_by(Visit.id.desc()).all()
    return [
        VisitOutSchema(
            id=v.id, token_number=v.token_number, visit_date=v.visit_date, visit_reason=v.visit_reason, status=v.status,
            created_at=v.created_at, completed_at=v.completed_at, cancelled_at=v.cancelled_at, patient_id=patient.id,
            doctor_id=v.doctor_id, doctor_name=v.doctor.name if v.doctor else "Unknown", patient_name=patient.name, patient_age=patient.age, 
            patient_gender=patient.gender, patient_phone=patient.phone_number or patient.whatsapp_number
        ) for v in visits
    ]

@app.get("/doctor/tokens", response_model=List[VisitOutSchema])
def get_tokens_by_date(date_str: Optional[str] = None, status_filter: Optional[str] = "ALL", db: Session = Depends(get_db), current_doctor: Doctor = Depends(get_current_doctor)):
    req_date = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else get_today_ist()
    q = db.query(Visit).filter(Visit.clinic_id == current_doctor.clinic_id, Visit.visit_date == req_date)
    if status_filter and status_filter != "ALL":
        q = q.filter(Visit.status == status_filter)
    visits = q.order_by(Visit.token_number.asc()).all()
    return [
        VisitOutSchema(
            id=v.id, token_number=v.token_number, visit_date=v.visit_date, visit_reason=v.visit_reason, status=v.status,
            created_at=v.created_at, completed_at=v.completed_at, cancelled_at=v.cancelled_at, patient_id=v.patient_id,
            doctor_id=v.doctor_id, doctor_name=v.doctor.name if v.doctor else "Unknown", patient_name=v.patient.name, patient_age=v.patient.age, 
            patient_gender=v.patient.gender, patient_phone=v.patient.phone_number or v.patient.whatsapp_number
        ) for v in visits
    ]
