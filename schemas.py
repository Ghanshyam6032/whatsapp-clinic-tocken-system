from pydantic import BaseModel
from typing import Optional, List
from datetime import date, datetime
from models import VisitStatus

class TokenSchema(BaseModel):
    access_token: str
    token_type: str = "bearer"

class DoctorLoginSchema(BaseModel):
    email: str
    password: str

class DoctorOutSchema(BaseModel):
    id: int
    clinic_id: int
    name: str
    email: str
    clinic_name: str
    is_online: bool
    class Config: from_attributes = True

class PatientOutSchema(BaseModel):
    id: int
    name: str
    whatsapp_number: str
    phone_number: Optional[str] = None
    age: int
    gender: str
    created_at: datetime
    visit_count: Optional[int] = 0
    first_visit_date: Optional[date] = None
    last_visit_date: Optional[date] = None
    last_token_number: Optional[int] = None
    last_visit_reason: Optional[str] = None
    total_completed: Optional[int] = 0
    total_cancelled: Optional[int] = 0
    total_waiting: Optional[int] = 0
    class Config: from_attributes = True

class VisitOutSchema(BaseModel):
    id: int
    token_number: int
    visit_date: date
    visit_reason: str
    status: VisitStatus
    created_at: datetime
    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    patient_id: int
    doctor_id: Optional[int] = None
    doctor_name: Optional[str] = None
    patient_name: Optional[str] = None
    patient_age: Optional[int] = None
    patient_gender: Optional[str] = None
    patient_phone: Optional[str] = None
    class Config: from_attributes = True

class DashboardSummaryOutSchema(BaseModel):
    clinic_name: str
    today_date: str
    current_visit: Optional[VisitOutSchema] = None
    next_waiting_visit: Optional[VisitOutSchema] = None
    waiting_count: int
    completed_count: int
    cancelled_count: int
    total_count: int

class PatientSummarySchema(BaseModel):
    total_patients: int
    new_patients: int
    returning_patients: int
    total_visits: int
    completed_visits: int
    cancelled_visits: int
    waiting_visits: int

class ManualPatientAddSchema(BaseModel):
    name: str
    whatsapp_number: str
    phone_number: str
    age: int
    gender: str
    visit_reason: str

class StatusUpdateSchema(BaseModel):
    is_online: bool