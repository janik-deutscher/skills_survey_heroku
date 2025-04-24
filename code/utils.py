# utils.py
import streamlit as st
import time
import toml
import os
import json
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import config
import random # For GSheet throttle sleep

# --- NEW Firestore Imports ---
from google.cloud import firestore
from google.oauth2 import service_account as google_service_account # Alias to avoid name conflict
# --- END NEW Firestore Imports ---

secrets_path = "/etc/secrets/secrets.toml"

secrets = toml.load(secrets_path)

# --- Firestore Client Initialization ---
@st.cache_resource
def get_firestore_client():
    """Initializes and returns a Firestore client using credentials from Streamlit secrets."""
    try:
        creds_dict = st.secrets["firestore_credentials"]
        creds = google_service_account.Credentials.from_service_account_info(creds_dict)
        db = firestore.Client(credentials=creds)
        print("Firestore client initialized successfully.")
        return db
    except KeyError:
        st.error("Error: Firestore credentials ('firestore_credentials') not found in Streamlit secrets.")
        print("ERROR: Firestore credentials not found in secrets.")
        return None
    except Exception as e:
        st.error(f"Error initializing Firestore client: {e}")
        print(f"ERROR: Initializing Firestore client: {e}")
        return None
# --- END Firestore Client Initialization ---


# --- Firestore Utility Functions ---

def save_message_to_firestore(username, message_data):
    """Saves a single message to Firestore."""
    db = get_firestore_client()
    if not db or not username or not message_data:
        print("Error: Cannot save message, invalid input or DB client.")
        return False
    try:
        message_data_with_ts = message_data.copy()
        message_data_with_ts['timestamp'] = firestore.SERVER_TIMESTAMP
        doc_ref = db.collection("interviews").document(username).collection("messages").add(message_data_with_ts)
        return True
    except Exception as e:
        print(f"Error saving message to Firestore for user {username}: {e}")
        return False

def save_interview_state_to_firestore(username, state_data):
    """Saves key interview state variables to Firestore, removing obsolete keys."""
    db = get_firestore_client()
    if not db or not username or not state_data:
        print("Error: Cannot save state, invalid input or DB client.")
        return False
    try:
        state_data_cleaned = state_data.copy()
        obsolete_keys = ["manual_question_index", "manual_answers_storage", "manual_answers_formatted", "partial_ai_transcript_formatted", "manual_fallback_triggered"]
        for key in obsolete_keys:
            state_data_cleaned.pop(key, None)

        state_data_with_ts = state_data_cleaned
        state_data_with_ts['last_updated'] = firestore.SERVER_TIMESTAMP

        doc_ref = db.collection("interviews").document(username)
        doc_ref.set(state_data_with_ts, merge=True)
        return True
    except Exception as e:
        print(f"Error saving state to Firestore for user {username}: {e}")
        return False

def load_interview_state_from_firestore(username):
    """Loads interview state and messages from Firestore, ignoring obsolete keys."""
    db = get_firestore_client()
    if not db or not username:
        print("Error: Cannot load state, invalid input or DB client.")
        return {}, []
    loaded_state = {}
    loaded_messages = []
    try:
        state_doc_ref = db.collection("interviews").document(username)
        state_doc = state_doc_ref.get()
        if state_doc.exists:
            loaded_state_raw = state_doc.to_dict()
            obsolete_keys = ["manual_question_index", "manual_answers_storage", "manual_answers_formatted", "partial_ai_transcript_formatted", "manual_fallback_triggered", "last_updated"]
            loaded_state = {k: v for k, v in loaded_state_raw.items() if k not in obsolete_keys}
            if 'start_time_unix' in loaded_state_raw:
                loaded_state['start_time_unix'] = loaded_state_raw['start_time_unix']
            print(f"State loaded from Firestore for user {username}. Kept keys: {list(loaded_state.keys())}")
        else:
            print(f"No existing state found in Firestore for user {username}")

        messages_ref = state_doc_ref.collection("messages").order_by("timestamp", direction=firestore.Query.ASCENDING)
        docs = messages_ref.stream()
        for doc in docs:
            msg = doc.to_dict()
            msg.pop('timestamp', None)
            if 'role' in msg and 'content' in msg:
                 loaded_messages.append({'role': msg['role'], 'content': msg['content']})
        if loaded_messages:
             print(f"Loaded {len(loaded_messages)} messages from Firestore for user {username}")
        return loaded_state, loaded_messages
    except Exception as e:
        print(f"Error loading state/messages from Firestore for user {username}: {e}")
        return {}, []

# --- Interview Save (Formats Transcript for GSheet, Saves Timing Locally) ---
def save_interview_data(
    username,
    transcripts_directory,
    times_directory,
    file_name_addition_transcript="",
    file_name_addition_time="",
    is_final_save=False,
    messages_to_format=None
):
    """Formats AI transcript for GSheet if is_final_save=True. Saves timing data locally."""
    os.makedirs(transcripts_directory, exist_ok=True)
    os.makedirs(times_directory, exist_ok=True)
    if is_final_save:
        try:
            messages = messages_to_format if messages_to_format is not None else st.session_state.get("messages", [])
            if messages:
                lines = []
                for message in messages:
                    if message.get('role') == 'system': continue
                    if message.get('content', '') in config.CLOSING_MESSAGES.keys(): continue
                    is_closing_message_display = any(message.get('content', '') == display_text for code, display_text in config.CLOSING_MESSAGES.items())
                    if is_closing_message_display: continue
                    lines.append(f"{message.get('role', 'Unknown').capitalize()}: {message.get('content', '')}")
                formatted_transcript_string = "\n---\n".join(lines)
                st.session_state.current_formatted_transcript_for_gsheet = formatted_transcript_string
                print("Formatted AI transcript prepared for GSheet.")
            else:
                print(f"Warning: No messages provided or found for transcript formatting for user {username}.")
                st.session_state.current_formatted_transcript_for_gsheet = "ERROR: No messages found for formatting."
        except Exception as e:
            print(f"Error processing final transcript for {username}: {e}")
            st.session_state.current_formatted_transcript_for_gsheet = f"ERROR: Processing transcript failed - {e}"

    time_filename = f"{username}{file_name_addition_time}_time.csv"
    time_path = os.path.join(times_directory, time_filename)
    try:
        end_time = time.time()
        start_time = st.session_state.get("start_time", None)
        duration_seconds = round(end_time - start_time) if start_time else 0
        duration_minutes = duration_seconds / 60.0 if duration_seconds > 0 else 0
        start_time_utc_str = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(start_time)) if start_time else "N/A"
        time_df = pd.DataFrame({
            "username": [username], "start_time_unix": [start_time], "start_time_utc": [start_time_utc_str],
            "end_time_unix": [end_time], "duration_seconds": [duration_seconds], "duration_minutes": [duration_minutes]
        })
        time_df.to_csv(time_path, index=False, encoding='utf-8')
    except Exception as e:
        print(f"Error saving local time data to {time_path}: {e}")


# --- Survey Utility Functions ---
def create_survey_directory():
    """Creates the local survey directory."""
    os.makedirs(config.SURVEY_DIRECTORY, exist_ok=True)

def check_if_survey_completed(username):
    """Checks Firestore state for survey completion flag."""
    db = get_firestore_client()
    if db and username:
        try:
            state_doc_ref = db.collection("interviews").document(username)
            state_doc = state_doc_ref.get()
            if state_doc.exists:
                state_data = state_doc.to_dict()
                if state_data.get("survey_completed_flag", False) is True:
                    return True
        except Exception as e:
            print(f"Error checking survey completion in Firestore for {username}: {e}")
    return False

def save_survey_data_local(username, survey_responses):
    """Saves the survey responses locally as a JSON file (ephemeral backup)."""
    file_path = os.path.join(config.SURVEY_DIRECTORY, f"{username}_survey.json")
    consent_given = st.session_state.get("consent_given", False)
    data_to_save = {
        "username": username,
        "submission_timestamp_unix": time.time(),
        "submission_time_utc": time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()),
        "consent_given": consent_given,
        "responses": survey_responses # Includes new sliders now
    }
    try:
        with open(file_path, "w", encoding='utf-8') as f:
            json.dump(data_to_save, f, indent=4, ensure_ascii=False)
        print(f"Local survey backup saved for {username}.")
        return True
    except Exception as e:
        print(f"Error saving local survey backup for {username}: {e}")
        return False

def save_survey_data_to_firestore(username, survey_responses, consent_given, formatted_transcript, gsheet_save_status):
    """Saves survey responses (incl NIS, new sliders) and AI transcript to Firestore."""
    db = get_firestore_client()
    if not db or not username:
        print("Error: Cannot save survey to Firestore, invalid input or DB client.")
        return False
    try:
        survey_data_subdoc = {
            "username": username,
            "submission_timestamp_unix": time.time(),
            "submission_time_utc": time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()),
            "consent_given": consent_given,
            "survey_responses": survey_responses, # Includes new sliders now
            "formatted_transcript": formatted_transcript,
            "saved_to_gsheet_successfully": gsheet_save_status,
            "last_updated": firestore.SERVER_TIMESTAMP
        }
        data_to_merge = {
            "survey_data": survey_data_subdoc,
            "last_updated": firestore.SERVER_TIMESTAMP
        }
        interview_doc_ref = db.collection("interviews").document(username)
        interview_doc_ref.set(data_to_merge, merge=True)
        print(f"Survey data saved/merged into Firestore for user {username}")
        return True
    except Exception as e:
        print(f"Error saving survey data to Firestore for user {username}: {e}")
        return False

def save_survey_data_to_gsheet(username, survey_responses):
    """Saves survey responses (incl NIS, new sliders) and AI transcript to Google Sheets."""
    st.session_state["gsheet_save_successful"] = False
    try:
        scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        creds_dict = st.secrets["connections"]["gsheets"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)

        sheet_name = "pilot_survey_results"
        worksheet = gc.open(sheet_name).sheet1
        submission_time_utc = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        consent_given = st.session_state.get("consent_given", "ERROR: Consent status missing")

        ai_transcript_formatted = st.session_state.get("current_formatted_transcript_for_gsheet", "ERROR: AI Transcript not processed.")

        CHUNK_SIZE = 40000
        ai_transcript_chunks = [ai_transcript_formatted[i:i + CHUNK_SIZE] for i in range(0, len(ai_transcript_formatted), CHUNK_SIZE)]
        MAX_TRANSCRIPT_COLUMNS = 5
        ai_transcript_parts_for_sheet = ai_transcript_chunks[:MAX_TRANSCRIPT_COLUMNS] + [""] * (MAX_TRANSCRIPT_COLUMNS - len(ai_transcript_chunks))
        if len(ai_transcript_chunks) > MAX_TRANSCRIPT_COLUMNS:
            print(f"Warning: AI Transcript for {username} was longer than {MAX_TRANSCRIPT_COLUMNS} columns ({len(ai_transcript_chunks)} chunks) and has been truncated in GSheet.")

        # --- UPDATED row_to_append: Added learning_enjoyment and university_enjoyment ---
        # New columns are J and K. Subsequent columns shift right.
        row_to_append = [
            username,                                       # Col A: Username
            submission_time_utc,                            # Col B: Timestamp
            str(consent_given),                             # Col C: Consent Given
            survey_responses.get("age", ""),                # Col D: Age
            survey_responses.get("gender", ""),             # Col E: Gender
            survey_responses.get("major", ""),              # Col F: Major
            survey_responses.get("year", ""),               # Col G: Year of Study
            survey_responses.get("gpa", ""),                # Col H: GPA
            survey_responses.get("student_nis", ""),        # Col I: Student Number (NIS)
            str(survey_responses.get("learning_enjoyment", "")), # Col J: Learning Enjoyment (0-100) - NEW
            str(survey_responses.get("university_enjoyment", "")), # Col K: University Enjoyment (0-100) - NEW
            str(survey_responses.get("ai_usage_percentage", "")), # Col L: AI Usage % (Shifted from J)
            survey_responses.get("ai_model", ""),           # Col M: AI Model Name (Shifted from K)
            # AI Transcript Parts (Cols N-R - Shifted from L-P)
            *ai_transcript_parts_for_sheet
        ]
        # --- End Row Definition ---

        time.sleep(random.uniform(0.1, 1.5))

        worksheet.append_row(row_to_append, value_input_option='USER_ENTERED')
        print(f"Survey data & AI transcript for {username} appended to GSheet '{sheet_name}'.")
        st.session_state["gsheet_save_successful"] = True

        flag_file_path = os.path.join(config.SURVEY_DIRECTORY, f"{username}_survey_submitted_gsheet.flag")
        try:
            with open(flag_file_path, 'w') as f: f.write(f"Submitted at {submission_time_utc}")
        except Exception as flag_e:
            print(f"Warning: Failed to create local completion flag file for {username}: {flag_e}")

        return True

    except gspread.exceptions.APIError as api_e:
        st.error(f"Error saving to Google Sheets: API Error - {api_e}. Please try submitting again or contact the researcher.")
        print(f"GSheet API Error for {username}: {api_e}")
        return False
    except gspread.exceptions.SpreadsheetNotFound:
        st.error(f"Error: Google Spreadsheet '{sheet_name}' not found. Please contact the researcher.")
        print(f"Spreadsheet '{sheet_name}' not found.")
        return False
    except Exception as e:
        st.error(f"An unexpected error occurred saving to Google Sheets: {e}. Please contact the researcher.")
        print(f"Error saving survey data for {username} to GSheet: {e}")
        return False


def save_survey_data(username, survey_responses):
    """Main function to save survey data (incl NIS, new sliders). Tries GSheet first, then Firestore backup."""
    create_survey_directory()

    consent_given = st.session_state.get("consent_given", False)
    ai_transcript = st.session_state.get("current_formatted_transcript_for_gsheet", "ERROR: Transcript not available")

    # --- Attempt GSheet Save (now includes new sliders) ---
    gsheet_success = save_survey_data_to_gsheet(username, survey_responses)

    # --- Save info to Firestore (now includes new sliders) ---
    firestore_save_attempted = save_survey_data_to_firestore(
        username, survey_responses, consent_given,
        ai_transcript,
        gsheet_success
    )
    if not firestore_save_attempted:
         print(f"Warning: Failed to save survey data backup to Firestore for {username}.")

    # --- Local backup (now includes new sliders) ---
    save_survey_data_local(username, survey_responses)

    # --- Update Firestore completion flag ---
    if gsheet_success or firestore_save_attempted:
         final_state_update = {"survey_completed_flag": True}
         save_interview_state_to_firestore(username, final_state_update)
         print(f"Survey completion flag set to True in Firestore for {username}")
    else:
         print(f"Survey completion flag NOT set in Firestore for {username} due to saving failures.")

    return gsheet_success