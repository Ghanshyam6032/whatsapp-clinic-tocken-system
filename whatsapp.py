import os
import requests
from datetime import datetime
import pytz
from sqlalchemy.orm import Session
from models import Clinic, Doctor, Patient, Visit, VisitStatus

# ==========================================
# TIMEZONE & HELPER FUNCTIONS
# ==========================================
def get_today_ist():
    ist = pytz.timezone('Asia/Kolkata')
    return datetime.now(ist).date()

def generate_daily_token(db: Session, clinic_id: int, doctor_id: int, patient_id: int, visit_reason: str):
    today = get_today_ist()
    # Find the maximum token number assigned for this specific doctor today
    last_visit = db.query(Visit).filter(
        Visit.doctor_id == doctor_id, 
        Visit.visit_date == today
    ).order_by(Visit.token_number.desc()).first()
    
    new_token_number = (last_visit.token_number + 1) if last_visit else 1
    
    new_visit = Visit(
        clinic_id=clinic_id,
        doctor_id=doctor_id,
        patient_id=patient_id,
        token_number=new_token_number,
        visit_date=today,
        visit_reason=visit_reason,
        status=VisitStatus.WAITING
    )
    db.add(new_visit)
    db.commit()
    db.refresh(new_visit)
    return new_visit

# ==========================================
# WHATSAPP API SENDER HELPER
# ==========================================
def send_whatsapp_message(phone_number_id: str, to_number: str, message_body: str):
    access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    if not access_token:
        print("ERROR: WHATSAPP_ACCESS_TOKEN is missing")
        return

    url = f"https://graph.facebook.com/v17.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": message_body}
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Failed to send WhatsApp message: {e}")

# ==========================================
# IN-MEMORY STATE MANAGEMENT (For Bot Flow)
# ==========================================
USER_STATES = {}

# ==========================================
# MAIN WHATSAPP MESSAGE PROCESSOR
# ==========================================
def process_whatsapp_message(db: Session, phone_number_id: str, sender_phone: str, msg_body: str, msg_id: str):
    clinic = db.query(Clinic).filter(Clinic.whatsapp_phone_number_id == phone_number_id).first()
    if not clinic:
        # Fallback to default clinic if phone_number_id mismatch
        clinic = db.query(Clinic).first()
        if not clinic: return
        
    text = msg_body.strip().lower()
    state_data = USER_STATES.get(sender_phone, {"step": "INIT"})
    step = state_data.get("step")

    # 1. Start / Greeting Menu
    if text in ['hi', 'hello', 'hey', 'start', 'menu', '1']:
        if not clinic.is_online:
            send_whatsapp_message(phone_number_id, sender_phone, f"Sorry, {clinic.name} is currently closed. Please check back later.")
            return

        welcome_msg = (
            f"Welcome to {clinic.name}!\n\n"
            "Please select an option:\n"
            "1️⃣ Book an Appointment (Get Token)\n"
            "2️⃣ Check My Token Status\n"
            "3️⃣ Cancel My Token\n\n"
            "Reply with 1, 2, or 3."
        )
        USER_STATES[sender_phone] = {"step": "AWAITING_MENU_CHOICE"}
        send_whatsapp_message(phone_number_id, sender_phone, welcome_msg)
        return

    # 2. Handle Menu Choice
    if step == "AWAITING_MENU_CHOICE":
        if text == '1':
            # Fetch Real Active Doctors ONLY (Strictly exclude Admin via email and is_active flag)
            admin_email = os.getenv("ADMIN_USERNAME", "admin")
            doctors = db.query(Doctor).filter(
                Doctor.clinic_id == clinic.id,
                Doctor.is_online == True,
                Doctor.is_active == True,
                Doctor.email != admin_email
            ).all()

            if not doctors:
                send_whatsapp_message(phone_number_id, sender_phone, "Currently, no doctors are available. Please try again later.")
                USER_STATES.pop(sender_phone, None)
                return

            doc_list_msg = "Please select a Doctor by replying with their number:\n\n"
            for index, doc in enumerate(doctors, 1):
                doc_list_msg += f"{index}. Dr. {doc.name}\n"
            
            USER_STATES[sender_phone] = {
                "step": "AWAITING_DOCTOR_SELECTION", 
                "doctors": [d.id for d in doctors]
            }
            send_whatsapp_message(phone_number_id, sender_phone, doc_list_msg)
            
        elif text == '2':
            # Check Status logic
            today = get_today_ist()
            patient = db.query(Patient).filter(Patient.whatsapp_number == sender_phone).first()
            if patient:
                # Get the most recent active token
                visit = db.query(Visit).filter(Visit.patient_id == patient.id, Visit.visit_date == today, Visit.status == VisitStatus.WAITING).first()
                if visit:
                    send_whatsapp_message(phone_number_id, sender_phone, f"Your Token is #{visit.token_number} for Dr. {visit.doctor.name}.\nPlease wait for your turn.")
                else:
                    send_whatsapp_message(phone_number_id, sender_phone, "You don't have any active waiting tokens for today.")
            else:
                send_whatsapp_message(phone_number_id, sender_phone, "No records found for this number.")
            USER_STATES.pop(sender_phone, None)
            
        elif text == '3':
            # Cancel Token logic
            today = get_today_ist()
            patient = db.query(Patient).filter(Patient.whatsapp_number == sender_phone).first()
            if patient:
                visit = db.query(Visit).filter(Visit.patient_id == patient.id, Visit.visit_date == today, Visit.status == VisitStatus.WAITING).first()
                if visit:
                    visit.status = VisitStatus.CANCELLED
                    db.commit()
                    send_whatsapp_message(phone_number_id, sender_phone, f"Your token #{visit.token_number} for Dr. {visit.doctor.name} has been cancelled successfully.")
                else:
                    send_whatsapp_message(phone_number_id, sender_phone, "You don't have any active tokens to cancel.")
            else:
                send_whatsapp_message(phone_number_id, sender_phone, "No records found.")
            USER_STATES.pop(sender_phone, None)
        else:
            send_whatsapp_message(phone_number_id, sender_phone, "Invalid option. Please reply with 1, 2, or 3.")
        return

    # 3. Handle Doctor Selection
    if step == "AWAITING_DOCTOR_SELECTION":
        try:
            choice_idx = int(text) - 1
            doctors_list = state_data.get("doctors", [])
            if 0 <= choice_idx < len(doctors_list):
                selected_doc_id = doctors_list[choice_idx]
                state_data["selected_doc_id"] = selected_doc_id
                state_data["step"] = "AWAITING_NAME"
                USER_STATES[sender_phone] = state_data
                send_whatsapp_message(phone_number_id, sender_phone, "Please reply with the Patient's Full Name:")
            else:
                send_whatsapp_message(phone_number_id, sender_phone, "Invalid doctor selection. Please enter a valid number from the list.")
        except ValueError:
            send_whatsapp_message(phone_number_id, sender_phone, "Please reply with a valid number.")
        return

    # 4. Handle Patient Name
    if step == "AWAITING_NAME":
        state_data["patient_name"] = msg_body.strip()
        state_data["step"] = "AWAITING_REASON"
        USER_STATES[sender_phone] = state_data
        send_whatsapp_message(phone_number_id, sender_phone, "Briefly type the reason for your visit (e.g., Fever, Checkup, Stomach pain):")
        return

    # 5. Handle Reason & Generate Token
    if step == "AWAITING_REASON":
        reason = msg_body.strip()
        patient_name = state_data.get("patient_name")
        selected_doc_id = state_data.get("selected_doc_id")
        
        # Save or Fetch Patient
        patient = db.query(Patient).filter(Patient.clinic_id == clinic.id, Patient.whatsapp_number == sender_phone).first()
        if not patient:
            patient = Patient(
                clinic_id=clinic.id,
                name=patient_name,
                whatsapp_number=sender_phone,
                phone_number=sender_phone,
                age=0,
                gender="Not Specified"
            )
            db.add(patient)
            db.commit()
            db.refresh(patient)
        else:
            # Update name if changed
            patient.name = patient_name
            db.commit()

        # Check if patient already has a waiting token for this specific doctor today
        today = get_today_ist()
        existing_visit = db.query(Visit).filter(
            Visit.patient_id == patient.id,
            Visit.doctor_id == selected_doc_id,
            Visit.visit_date == today,
            Visit.status == VisitStatus.WAITING
        ).first()

        if existing_visit:
            send_whatsapp_message(phone_number_id, sender_phone, f"You already have a waiting token (#{existing_visit.token_number}) for this doctor today.")
        else:
            # Generate Token
            try:
                new_visit = generate_daily_token(db, clinic.id, selected_doc_id, patient.id, reason)
                doc = db.query(Doctor).filter(Doctor.id == selected_doc_id).first()
                success_msg = (
                    f"✅ *Appointment Confirmed!*\n\n"
                    f"👤 Patient: {patient.name}\n"
                    f"👨‍⚕️ Doctor: Dr. {doc.name}\n"
                    f"🎫 *Token Number: {new_visit.token_number}*\n\n"
                    f"We will notify you when your turn approaches. Reply 'Menu' anytime."
                )
                send_whatsapp_message(phone_number_id, sender_phone, success_msg)
            except Exception as e:
                send_whatsapp_message(phone_number_id, sender_phone, "Sorry, an error occurred while generating your token. Please try again later.")
        
        # Clear state
        USER_STATES.pop(sender_phone, None)
        return

    # Fallback if state gets lost
    send_whatsapp_message(phone_number_id, sender_phone, "Type 'Hi' or 'Menu' to start.")
