from sqlalchemy import Column, Integer, String, Date, DateTime, ForeignKey, Enum, UniqueConstraint, JSON, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from database import Base

class VisitStatus(str, enum.Enum):
    WAITING = "WAITING"
    CURRENT = "CURRENT"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"

class Clinic(Base):
    __tablename__ = "clinics"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    phone = Column(String(20), nullable=True)
    whatsapp_phone_number_id = Column(String(50), unique=True, nullable=False, index=True)
    address = Column(String(250), nullable=True)
    is_online = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Doctor(Base):
    __tablename__ = "doctors"
    id = Column(Integer, primary_key=True, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    email = Column(String(100), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)
    is_online = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    clinic = relationship("Clinic")

class Patient(Base):
    __tablename__ = "patients"
    id = Column(Integer, primary_key=True, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    whatsapp_number = Column(String(20), nullable=False, index=True)
    phone_number = Column(String(20), nullable=True)
    age = Column(Integer, nullable=False)
    gender = Column(String(20), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint('clinic_id', 'whatsapp_number', name='_clinic_patient_uc'),)

class Visit(Base):
    __tablename__ = "visits"
    id = Column(Integer, primary_key=True, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id"), nullable=False, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=True, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False, index=True)
    token_number = Column(Integer, nullable=False)
    visit_date = Column(Date, nullable=False, index=True)
    visit_reason = Column(String(250), nullable=False)
    status = Column(Enum(VisitStatus), default=VisitStatus.WAITING, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    patient = relationship("Patient")
    doctor = relationship("Doctor")
    __table_args__ = (UniqueConstraint('clinic_id', 'doctor_id', 'visit_date', 'token_number', name='_clinic_doctor_date_token_uc'),)

class ClinicCalendar(Base):
    __tablename__ = "clinic_calendar"
    id = Column(Integer, primary_key=True, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id"), nullable=False, index=True)
    date = Column(Date, nullable=False, index=True)
    status = Column(String(20), default="CLOSED", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint('clinic_id', 'date', name='_clinic_date_uc'),)

class ConversationState(Base):
    __tablename__ = "conversation_states"
    id = Column(Integer, primary_key=True, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id"), nullable=False, index=True)
    whatsapp_number = Column(String(20), nullable=False, index=True)
    state = Column(String(50), nullable=False, default="MAIN_MENU")
    temporary_data = Column(JSON, default={})
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint('clinic_id', 'whatsapp_number', name='_clinic_conv_uc'),)

class ProcessedWebhookEvent(Base):
    __tablename__ = "processed_webhook_events"
    id = Column(Integer, primary_key=True, index=True)
    message_id = Column(String(255), unique=True, nullable=False, index=True)
    phone_number_id = Column(String(50))
    sender_number = Column(String(50))
    event_type = Column(String(50))
    processed_at = Column(DateTime, default=datetime.utcnow)