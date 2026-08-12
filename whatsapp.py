import os
import requests
import zoneinfo
from datetime import datetime, date
from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified
from models import Clinic, Patient, Visit, VisitStatus, ClinicCalendar, ConversationState, Doctor

IST = zoneinfo.ZoneInfo("Asia/Kolkata")

def get_today_ist() -> date:
    return datetime.now(IST).date()

def send_whatsapp_message(phone_number_id: str, to: str, text_msg: str, reason_for_send: str, message_id: str, state: str):
    access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    if not access_token:
        print("WHATSAPP_ACCESS_TOKEN missing. Message suppressed.")
        return

    url = f"https://graph.facebook.com/v26.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"body": text_msg}
    }
    try:
        requests.post(url, headers=headers, json=payload, timeout=10)
    except Exception as e:
        print(f"→ Error sending WhatsApp message to {to}: {str(e)}")

def get_or_create_state(db: Session, clinic_id: int, whatsapp_number: str) -> ConversationState:
    state_rec = db.query(ConversationState).filter(
        ConversationState.clinic_id == clinic_id,
        ConversationState.whatsapp_number == whatsapp_number
    ).first()
    if not state_rec:
        state_rec = ConversationState(
            clinic_id=clinic_id,
            whatsapp_number=whatsapp_number,
            state="MAIN_MENU",
            temporary_data={}
        )
        db.add(state_rec)
        db.commit()
        db.refresh(state_rec)
    return state_rec

def reset_state(db: Session, clinic_id: int, whatsapp_number: str):
    state_rec = get_or_create_state(db, clinic_id, whatsapp_number)
    state_rec.state = "MAIN_MENU"
    state_rec.temporary_data = {}
    flag_modified(state_rec, "temporary_data")
    db.commit()

def generate_daily_token(db: Session, clinic_id: int, doctor_id: int, patient_id: int, reason: str) -> Visit:
    clinic = db.query(Clinic).get(clinic_id)
    if not clinic or not clinic.is_online:
        raise ValueError("OFFLINE_CLINIC")
        
    doctor = db.query(Doctor).get(doctor_id)
    if not doctor or not doctor.is_online:
        raise ValueError("OFFLINE_DOCTOR")

    today = get_today_ist()
    closed_rec = db.query(ClinicCalendar).filter(
        ClinicCalendar.clinic_id == clinic_id,
        ClinicCalendar.date == today,
        ClinicCalendar.status == "CLOSED"
    ).first()
    if closed_rec:
        raise ValueError("CLOSED_CLINIC")

    active_visit = db.query(Visit).filter(
        Visit.clinic_id == clinic_id,
        Visit.doctor_id == doctor_id,
        Visit.patient_id == patient_id,
        Visit.visit_date == today,
        Visit.status.in_([VisitStatus.WAITING, VisitStatus.CURRENT])
    ).first()

    if active_visit:
        raise ValueError(f"ACTIVE_TOKEN:{active_visit.token_number}")

    for _ in range(5):
        try:
            max_token = db.query(func.max(Visit.token_number)).filter(
                Visit.clinic_id == clinic_id,
                Visit.doctor_id == doctor_id,
                Visit.visit_date == today
            ).scalar() or 0

            next_token = max_token + 1
            new_visit = Visit(
                clinic_id=clinic_id,
                doctor_id=doctor_id,
                patient_id=patient_id,
                token_number=next_token,
                visit_date=today,
                visit_reason=reason,
                status=VisitStatus.WAITING
            )
            db.add(new_visit)
            db.commit()
            db.refresh(new_visit)
            return new_visit
        except IntegrityError:
            db.rollback()
            continue
            
    raise Exception("Concurrent generation failed. Please try again.")

def process_whatsapp_message(db: Session, phone_number_id: str, sender_phone: str, msg_body: str, message_id: str):
    clinic = db.query(Clinic).filter(Clinic.whatsapp_phone_number_id == phone_number_id).first()
    if not clinic: return

    msg_text = msg_body.strip()
    lower_text = msg_text.lower()
    state_rec = get_or_create_state(db, clinic.id, sender_phone)
    state = state_rec.state

    def reply(text_msg: str, reason: str, updated_state: str):
        send_whatsapp_message(phone_number_id, sender_phone, text_msg, reason, message_id, updated_state)

    if lower_text in ["menu", "hi", "hello", "start", "cancel", "reset"]:
        reset_state(db, clinic.id, sender_phone)
        msg = f"👋 Welcome to {clinic.name}\n\n🎫 1️⃣ Get Token\n📊 2️⃣ Check Token Status\n❌ 3️⃣ Cancel Token"
        reply(msg, "Reset", "MAIN_MENU")
        return

    temp_data = dict(state_rec.temporary_data) if isinstance(state_rec.temporary_data, dict) else {}

    if state == "MAIN_MENU":
        if msg_text == "1":
            if not clinic.is_online:
                reply("🔴 Clinic is currently offline.\n\nSorry, the clinic is not accepting new tokens right now.\n\nPlease try again later.", "Offline", "MAIN_MENU")
                return

            doctors = db.query(Doctor).filter(Doctor.clinic_id == clinic.id, Doctor.is_active == True).order_by(Doctor.id.asc()).all()
            if not doctors:
                reply("Sorry, no doctors are currently available.", "No Doctors", "MAIN_MENU")
                return

            today = get_today_ist()
            msg = "👨‍⚕️ Choose a Doctor\n\n"
            doc_map = {}
            for idx, doc in enumerate(doctors, 1):
                if doc.is_online:
                    wait_count = db.query(Visit).filter(Visit.doctor_id == doc.id, Visit.visit_date == today, Visit.status == VisitStatus.WAITING).count()
                    msg += f"{idx}️⃣ Dr. {doc.name}\n🟢 Online\n👥 Waiting: {wait_count}\n\n"
                    doc_map[str(idx)] = doc.id
                else:
                    msg += f"{idx}️⃣ Dr. {doc.name}\n🔴 Offline\n\n"

            state_rec.temporary_data = {"doc_map": doc_map} 
            flag_modified(state_rec, "temporary_data")
            state_rec.state = "DOCTOR_SELECTION"
            db.commit()
            reply(msg.strip(), "Prompting Doctor", "DOCTOR_SELECTION")

        elif msg_text == "2":
            today = get_today_ist()
            patient = db.query(Patient).filter(Patient.clinic_id == clinic.id, Patient.whatsapp_number == sender_phone).first()
            if not patient:
                reply("You have no registered tokens today. Option 1 to Get Token.", "Check status - No patient", "MAIN_MENU")
                return

            visits = db.query(Visit).filter(Visit.patient_id == patient.id, Visit.visit_date == today).all()
            if not visits:
                reply("You do not have a token registered for today.", "Check status - No visit", "MAIN_MENU")
                return

            out_msg = ""
            for visit in visits:
                doc = db.query(Doctor).get(visit.doctor_id)
                curr_visit = db.query(Visit).filter(Visit.doctor_id == doc.id, Visit.visit_date == today, Visit.status == VisitStatus.CURRENT).first()
                c_num = f"#{curr_visit.token_number}" if curr_visit else "Not Started"
                ahead = db.query(Visit).filter(Visit.doctor_id == doc.id, Visit.visit_date == today, Visit.status == VisitStatus.WAITING, Visit.token_number < visit.token_number).count()
                
                out_msg += f"🎫 Your Token: #{visit.token_number}\n\n👨‍⚕️ Doctor: Dr. {doc.name}\n\n👨‍⚕️ Current Token: {c_num}\n\n"
                
                if visit.status == VisitStatus.CANCELLED:
                    out_msg += "❌ Your token has been cancelled.\n\n"
                elif visit.status == VisitStatus.COMPLETED:
                    out_msg += "✅ Your visit has been completed.\n\n"
                elif visit.status == VisitStatus.CURRENT:
                    out_msg += f"🟢 IT'S YOUR TURN!\n\nPlease proceed to the doctor.\n\n"
                elif visit.status == VisitStatus.SKIPPED:
                    out_msg += "⏭️ Your token has been skipped temporarily.\n\n"
                else:
                    out_msg += f"👥 Patients Before You: {ahead}\n\n🟢 Status: WAITING\n\n"
            reply(out_msg.strip(), "Status Check", "MAIN_MENU")

        elif msg_text == "3":
            today = get_today_ist()
            patient = db.query(Patient).filter(Patient.clinic_id == clinic.id, Patient.whatsapp_number == sender_phone).first()
            if not patient:
                reply("You do not have an active token today.", "Cancel - No patient", "MAIN_MENU")
                return
            
            visits = db.query(Visit).filter(Visit.patient_id == patient.id, Visit.visit_date == today, Visit.status.in_([VisitStatus.WAITING, VisitStatus.CURRENT])).all()
            if not visits:
                reply("You do not have an active token today.", "Cancel - No active", "MAIN_MENU")
                return

            if len(visits) == 1:
                visit = visits[0]
                doc = db.query(Doctor).get(visit.doctor_id)
                state_rec.temporary_data = {"cancel_visit_id": visit.id}
                flag_modified(state_rec, "temporary_data")
                state_rec.state = "CONFIRM_CANCEL"
                db.commit()
                reply(f"🎫 Your Token: #{visit.token_number}\n👨‍⚕️ Doctor: Dr. {doc.name}\n\nAre you sure you want to cancel?\n\n✅ 1️⃣ Yes, Cancel Token\n↩️ 2️⃣ No, Keep Token", "Confirm Cancel", "CONFIRM_CANCEL")
            else:
                msg = "You have multiple active tokens. Please select which one to cancel:\n\n"
                v_map = {}
                for idx, v in enumerate(visits, 1):
                    doc = db.query(Doctor).get(v.doctor_id)
                    msg += f"{idx}️⃣ Dr. {doc.name} (Token #{v.token_number})\n"
                    v_map[str(idx)] = v.id
                state_rec.temporary_data = {"cancel_map": v_map}
                flag_modified(state_rec, "temporary_data")
                state_rec.state = "SELECT_CANCEL"
                db.commit()
                reply(msg, "Select Cancel", "SELECT_CANCEL")
        else:
            reply(f"⚠️ Please select a valid option.\n\n👋 Welcome to {clinic.name}\n\n🎫 1️⃣ Get Token\n📊 2️⃣ Check Token Status\n❌ 3️⃣ Cancel Token", "Invalid menu", "MAIN_MENU")

    elif state == "DOCTOR_SELECTION":
        doc_map = temp_data.get("doc_map", {})
        if msg_text not in doc_map:
            reply("⚠️ Please select a valid online doctor number from the list.", "Invalid Doc", state)
            return

        doctor_id = doc_map[msg_text]
        patient = db.query(Patient).filter(Patient.clinic_id == clinic.id, Patient.whatsapp_number == sender_phone).first()

        new_data = {"doctor_id": doctor_id}
        if patient:
            visits = db.query(Visit).filter(Visit.patient_id == patient.id).order_by(Visit.visit_date.asc()).all()
            
            today = get_today_ist()
            active_visit = next((v for v in visits if v.visit_date == today and v.doctor_id == doctor_id and v.status in [VisitStatus.WAITING, VisitStatus.CURRENT]), None)
            if active_visit:
                reply(f"⚠️ You already have an active token for this doctor today.\n\n🎫 Your Token: #{active_visit.token_number}", "Active Block", "MAIN_MENU")
                reset_state(db, clinic.id, sender_phone)
                return

            total_visits = len(visits)
            first_v = visits[0].visit_date.strftime('%Y-%m-%d') if total_visits > 0 else "N/A"
            last_v = visits[-1].visit_date.strftime('%Y-%m-%d') if total_visits > 0 else "N/A"
            
            new_data["patient_id"] = patient.id
            state_rec.temporary_data = new_data
            flag_modified(state_rec, "temporary_data")
            state_rec.state = "RETURNING_PATIENT_PROFILE"
            db.commit()
            
            msg = f"👋 Welcome back, {patient.name}!\n\n📋 Your Patient Profile\n\n👤 Name: {patient.name}\n📱 Mobile: {patient.phone_number or patient.whatsapp_number}\n🎂 Age: {patient.age}\n⚧ Gender: {patient.gender}\n\n📅 First Visit: {first_v}\n📅 Last Visit: {last_v}\n🔢 Total Visits: {total_visits}\n\nWould you like to continue?\n\n✅ 1️⃣ Continue\n✏️ 2️⃣ Update Details"
            reply(msg, "Returning Profile", "RETURNING_PATIENT_PROFILE")
        else:
            state_rec.temporary_data = new_data
            flag_modified(state_rec, "temporary_data")
            state_rec.state = "WAITING_FOR_NAME"
            db.commit()
            reply("👤 Please enter your full name.", "Ask Name", "WAITING_FOR_NAME")

    elif state == "RETURNING_PATIENT_PROFILE":
        if msg_text == "1":
            state_rec.state = "WAITING_FOR_REASON"
            db.commit()
            reply("🩺 What is the reason for today's visit?", "Ask Reason", "WAITING_FOR_REASON")
        elif msg_text == "2":
            state_rec.state = "UPDATE_DETAILS_MENU"
            db.commit()
            reply("1️⃣ Name\n2️⃣ Mobile Number\n3️⃣ Age\n4️⃣ Gender\n5️⃣ Keep Existing Details", "Update Menu", "UPDATE_DETAILS_MENU")
        else:
            reply("⚠️ Please select:\n✅ 1️⃣ Continue\n✏️ 2️⃣ Update Details", "Invalid Choice", state)

    elif state == "UPDATE_DETAILS_MENU":
        update_map = {"1": "name", "2": "phone", "3": "age", "4": "gender"}
        if msg_text == "5":
            state_rec.state = "WAITING_FOR_REASON"
            db.commit()
            reply("🩺 What is the reason for today's visit?", "Ask Reason", "WAITING_FOR_REASON")
        elif msg_text in update_map:
            field = update_map[msg_text]
            temp_data["update_field"] = field
            state_rec.temporary_data = temp_data 
            flag_modified(state_rec, "temporary_data")
            state_rec.state = "WAITING_FOR_UPDATE_VALUE"
            db.commit()
            if field == "name": reply("👤 Please enter your new full name.", "Update Name", "WAITING_FOR_UPDATE_VALUE")
            elif field == "phone": reply("📱 Please enter your new mobile number.", "Update Phone", "WAITING_FOR_UPDATE_VALUE")
            elif field == "age": reply("🎂 Please enter your new age.", "Update Age", "WAITING_FOR_UPDATE_VALUE")
            elif field == "gender": reply("👨 1️⃣ Male\n👩 2️⃣ Female\n🧑 3️⃣ Other", "Update Gender", "WAITING_FOR_UPDATE_VALUE")
        else:
            reply("⚠️ Invalid option. Select 1 to 5.", "Invalid Option", state)

    elif state == "WAITING_FOR_UPDATE_VALUE":
        field = temp_data.get("update_field")
        patient = db.query(Patient).get(temp_data.get("patient_id"))
        
        if field == "age" and (not msg_text.isdigit() or not 0 <= int(msg_text) <= 120):
            reply("⚠️ Please enter a valid age between 0 and 120.", "Invalid Age", state)
            return
        elif field == "gender":
            g_map = {"1": "Male", "2": "Female", "3": "Other"}
            if msg_text not in g_map:
                reply("⚠️ Please select:\n👨 1️⃣ Male\n👩 2️⃣ Female\n🧑 3️⃣ Other", "Invalid Gender", state)
                return
            msg_text = g_map[msg_text]

        if field == "name": patient.name = msg_text
        elif field == "phone": patient.phone_number = msg_text
        elif field == "age": patient.age = int(msg_text)
        elif field == "gender": patient.gender = msg_text
        
        state_rec.state = "WAITING_FOR_REASON"
        db.commit()
        reply("🩺 What is the reason for today's visit?", "Ask Reason", "WAITING_FOR_REASON")

    elif state == "WAITING_FOR_NAME":
        if len(msg_text) < 2 or len(msg_text) > 100 or msg_text.isdigit():
            reply("⚠️ Please enter a valid full name (2 to 100 characters).", "Invalid name", state)
            return
        temp_data["name"] = msg_text
        state_rec.temporary_data = temp_data 
        flag_modified(state_rec, "temporary_data") 
        state_rec.state = "WAITING_FOR_PHONE"
        db.commit()
        reply("📱 Please enter your mobile number.", "Ask Phone", "WAITING_FOR_PHONE")

    elif state == "WAITING_FOR_PHONE":
        if len(msg_text) < 7 or len(msg_text) > 15:
            reply("⚠️ Please enter a valid mobile number.", "Invalid phone", state)
            return
        temp_data["phone_number"] = msg_text
        state_rec.temporary_data = temp_data 
        flag_modified(state_rec, "temporary_data") 
        state_rec.state = "WAITING_FOR_AGE"
        db.commit()
        reply("🎂 Please enter your age.", "Ask Age", "WAITING_FOR_AGE")

    elif state == "WAITING_FOR_AGE":
        if not msg_text.isdigit() or not (0 <= int(msg_text) <= 120):
            reply("⚠️ Please enter a valid age between 0 and 120.", "Invalid Age", state)
            return
        temp_data["age"] = int(msg_text)
        state_rec.temporary_data = temp_data 
        flag_modified(state_rec, "temporary_data") 
        state_rec.state = "WAITING_FOR_GENDER"
        db.commit()
        reply("👨 1️⃣ Male\n👩 2️⃣ Female\n🧑 3️⃣ Other", "Ask Gender", "WAITING_FOR_GENDER")

    elif state == "WAITING_FOR_GENDER":
        g_map = {"1": "Male", "2": "Female", "3": "Other"}
        if msg_text not in g_map:
            reply("⚠️ Please select:\n👨 1️⃣ Male\n👩 2️⃣ Female\n🧑 3️⃣ Other", "Invalid Gender", state)
            return
        temp_data["gender"] = g_map[msg_text]
        state_rec.temporary_data = temp_data 
        flag_modified(state_rec, "temporary_data") 
        state_rec.state = "WAITING_FOR_REASON" # <-- FIX: Successfully transition state to WAITING_FOR_REASON
        db.commit()                             # <-- FIX: Commit state change and json data
        reply("🩺 What is the reason for today's visit?", "Ask Reason", "WAITING_FOR_REASON")

    elif state == "WAITING_FOR_REASON":
        # Accept any visit reason as requested
        if not temp_data.get("patient_id") and not temp_data.get("name"):
            reply("⚠️ Your session expired or data was lost. Please type 'hi' to start again.", "Data Lost", "MAIN_MENU")
            reset_state(db, clinic.id, sender_phone)
            return
        
        doctor_id = temp_data.get("doctor_id")
        doc = db.query(Doctor).get(doctor_id)
        patient_id = temp_data.get("patient_id")
        
        if not patient_id:
            try:
                patient = Patient(
                    clinic_id=clinic.id,
                    name=temp_data.get("name"),
                    whatsapp_number=sender_phone,
                    phone_number=temp_data.get("phone_number"),
                    age=temp_data.get("age"),
                    gender=temp_data.get("gender")
                )
                db.add(patient)
                db.commit()
                db.refresh(patient)
                patient_id = patient.id
            except IntegrityError:
                db.rollback()
                patient = db.query(Patient).filter(Patient.clinic_id == clinic.id, Patient.whatsapp_number == sender_phone).first()
                if patient:
                    patient_id = patient.id
                else:
                    reply("⚠️ Could not create patient profile. Please type 'hi' to restart.", "DB Error", "MAIN_MENU")
                    reset_state(db, clinic.id, sender_phone)
                    return
        else:
            patient = db.query(Patient).get(patient_id)
            
        try:
            visit = generate_daily_token(db, clinic.id, doctor_id, patient_id, msg_text)
            today = get_today_ist()
            current_visit = db.query(Visit).filter(Visit.doctor_id == doctor_id, Visit.visit_date == today, Visit.status == VisitStatus.CURRENT).first()
            c_num = f"#{current_visit.token_number}" if current_visit else "Not Started"
            ahead = db.query(Visit).filter(Visit.doctor_id == doctor_id, Visit.visit_date == today, Visit.status == VisitStatus.WAITING, Visit.token_number < visit.token_number).count()
            
            msg = f"✅ TOKEN GENERATED SUCCESSFULLY\n\n👨‍⚕️ Doctor: Dr. {doc.name}\n\n🎫 Your Token: #{visit.token_number}\n\n👤 Patient: {patient.name}\n🎂 Age: {patient.age}\n🩺 Reason: {visit.visit_reason}\n\n👨‍⚕️ Current Token: {c_num}\n👥 Patients Before You: {ahead}\n\n🙏 Thank you for using {clinic.name}."
            reply(msg, "Token Generated", "MAIN_MENU")
            reset_state(db, clinic.id, sender_phone)
            
        except ValueError as e:
            err = str(e)
            if err == "OFFLINE_CLINIC":
                reply("🔴 Clinic is currently offline.\n\nSorry, the clinic is not accepting new tokens right now.\n\nPlease try again later.", "Closed", "MAIN_MENU")
            elif err == "OFFLINE_DOCTOR":
                reply("🔴 Doctor is currently offline and not accepting new tokens.", "Doctor Offline", "MAIN_MENU")
            elif err == "CLOSED_CLINIC":
                reply(f"🔴 Clinic Closed\n\n{clinic.name} is closed today.", "Closed", "MAIN_MENU")
            elif err.startswith("ACTIVE_TOKEN"):
                tok = err.split(":")[1]
                reply(f"⚠️ You already have an active token for this doctor today.\n\n🎫 Your Token: #{tok}", "Active Block", "MAIN_MENU")
            reset_state(db, clinic.id, sender_phone)
        except Exception:
            reply("⚠️ Failed to generate token due to high traffic. Please type 1 to try again.", "Token Fail", "MAIN_MENU")
            reset_state(db, clinic.id, sender_phone)

    elif state == "SELECT_CANCEL":
        cancel_map = temp_data.get("cancel_map", {})
        if msg_text not in cancel_map:
            reply("⚠️ Please select a valid number from the list.", "Invalid Cancel Map", state)
            return
            
        visit_id = cancel_map[msg_text]
        visit = db.query(Visit).get(visit_id)
        doc = db.query(Doctor).get(visit.doctor_id)
        
        state_rec.temporary_data = {"cancel_visit_id": visit.id}
        flag_modified(state_rec, "temporary_data")
        state_rec.state = "CONFIRM_CANCEL"
        db.commit()
        reply(f"🎫 Your Token: #{visit.token_number}\n👨‍⚕️ Doctor: Dr. {doc.name}\n\nAre you sure you want to cancel?\n\n✅ 1️⃣ Yes, Cancel Token\n↩️ 2️⃣ No, Keep Token", "Confirm Cancel", "CONFIRM_CANCEL")

    elif state == "CONFIRM_CANCEL":
        if msg_text == "1":
            visit_id = temp_data.get("cancel_visit_id")
            visit = db.query(Visit).get(visit_id)
            if visit and visit.status in [VisitStatus.WAITING, VisitStatus.CURRENT]:
                visit.status = VisitStatus.CANCELLED
                visit.cancelled_at = datetime.utcnow()
                db.commit()
                reply(f"❌ Your token has been cancelled.", "Cancelled", "MAIN_MENU")
            else:
                reply("Token could not be cancelled.", "Cancel Logic Fail", "MAIN_MENU")
        else:
            reply("Token cancellation aborted.", "Cancel Abort", "MAIN_MENU")
        reset_state(db, clinic.id, sender_phone)