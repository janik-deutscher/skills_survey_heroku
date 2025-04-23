# utils.py (Heroku Secrets Version - CORRECTED Credential Handling)
import streamlit as st
import time
import os
import json
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials as ServiceAccountCredentials # Use explicit alias
import config
import random
import uuid

# --- Firestore Imports ---
from google.cloud import firestore
# Note: Firestore library often handles auth implicitly if GOOGLE_APPLICATION_CREDENTIALS env var is set
# OR if default service account credentials on the platform (like Cloud Run, GAE) are available.
# However, explicitly creating credentials gives more control.

# --- HEROKU CHANGE: Function to get Google Credentials DICT from Env Var ---
@st.cache_resource
def get_google_creds_dict_from_env(env_var_name="GOOGLE_CREDENTIALS_JSON"):
    """Gets the Google Credentials dictionary by parsing JSON from an environment variable."""
    print(f"Attempting to read environment variable: {env_var_name}")
    creds_json_str = os.environ.get(env_var_name)
    if creds_json_str:
        print(f"Found content in {env_var_name}. Attempting to parse JSON.")
        try:
            creds_dict = json.loads(creds_json_str)
            print(f"Successfully parsed JSON credentials for project: {creds_dict.get('project_id')}")
            return creds_dict
        except json.JSONDecodeError as e:
            st.error(f"CRITICAL: Failed to parse JSON from env var '{env_var_name}'. Error: {e}"); st.stop()
            return None
        except Exception as e:
            st.error(f"CRITICAL: Unexpected error processing credentials from env var '{env_var_name}': {e}"); st.stop()
            return None
    else:
        st.error(f"CRITICAL: Environment variable '{env_var_name}' not found."); st.stop()
        return None

# --- Firestore Client Initialization (Using Specific Creds) ---
@st.cache_resource
def get_firestore_client():
    """Initializes and returns a Firestore client using credentials from Env Var."""
    print("Attempting to get Firestore client...")
    creds_dict = get_google_creds_dict_from_env()
    if creds_dict:
        try:
            # Create Firestore-specific credentials from the dictionary
            # Firestore typically doesn't require specific scopes if using service account key directly
            creds_firestore = ServiceAccountCredentials.from_service_account_info(creds_dict)
            db = firestore.Client(credentials=creds_firestore, project=creds_dict.get('project_id'))
            print("INFO: Firestore client initialized successfully using environment variable.")
            # Test connection
            try:
                 db.collection('_test_connection').limit(1).get()
                 print("INFO: Firestore connection test successful.")
            except Exception as conn_err:
                 st.warning(f"Warning: Firestore connection test failed: {conn_err}")
            return db
        except Exception as e:
            st.error(f"Error initializing Firestore client: {e}")
            print(f"ERROR: Initializing Firestore client: {e}")
            return None
    else:
        print("ERROR: Cannot initialize Firestore client, credentials dictionary is missing.")
        return None

# --- GSpread Client Initialization (Using Specific Creds with Scopes) ---
@st.cache_resource
def get_gsheet_client():
    """Authorizes and returns a gspread client instance using Env Var credentials."""
    print("Attempting to get GSpread client...")
    creds_dict = get_google_creds_dict_from_env()
    if not creds_dict:
        st.error("Cannot initialize GSpread client: Credentials dictionary not available.")
        return None
    try:
        # Create GSheet-specific credentials with necessary scopes
        scopes_gsheets = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        creds_gsheets = ServiceAccountCredentials.from_service_account_info(creds_dict, scopes=scopes_gsheets)
        gc = gspread.authorize(creds_gsheets)
        print("INFO: GSpread client authorized successfully.")
        return gc
    except Exception as e:
        st.error(f"Failed to authorize GSpread client: {e}")
        print(f"ERROR: Failed to authorize GSpread client: {e}")
        return None

# --- Firestore Utility Functions (Unchanged - rely on get_firestore_client) ---
def save_message_to_firestore(username, message_data):
    db = get_firestore_client()
    if not db: return False # Add check
    # ... rest of function ...
    try:
        message_data_with_ts = message_data.copy(); message_data_with_ts['timestamp'] = firestore.SERVER_TIMESTAMP
        db.collection("interviews").document(username).collection("messages").add(message_data_with_ts)
        return True
    except Exception as e: print(f"Error saving message: {e}"); return False

def save_interview_state_to_firestore(username, state_data):
    db = get_firestore_client()
    if not db: return False # Add check
    # ... rest of function ...
    try:
        state_data_with_ts = state_data.copy(); state_data_with_ts['last_updated'] = firestore.SERVER_TIMESTAMP
        db.collection("interviews").document(username).set(state_data_with_ts, merge=True)
        return True
    except Exception as e: print(f"Error saving state: {e}"); return False

def load_interview_state_from_firestore(username):
    db = get_firestore_client()
    if not db: return {}, [] # Add check
    # ... rest of function ...
    loaded_state = {}; loaded_messages = []
    try:
        state_doc_ref = db.collection("interviews").document(username); state_doc = state_doc_ref.get()
        if state_doc.exists: loaded_state = state_doc.to_dict(); loaded_state.pop('last_updated', None)
        else: print(f"No state found for {username}")
        messages_ref = state_doc_ref.collection("messages").order_by("timestamp", direction=firestore.Query.ASCENDING); docs = messages_ref.stream()
        for doc in docs:
            msg = doc.to_dict(); msg.pop('timestamp', None)
            if 'role' in msg and 'content' in msg: loaded_messages.append({'role': msg['role'], 'content': msg['content']})
        print(f"Loaded {len(loaded_messages)} messages for {username}")
        return loaded_state, loaded_messages
    except Exception as e: print(f"Error loading state/messages: {e}"); return {}, []

# --- GSpread Save Function (Uses get_gsheet_client) ---
def save_survey_data_to_gsheet(username, survey_responses):
    """Saves survey data to Google Sheets."""
    st.session_state["gsheet_save_successful"] = False
    gc = get_gsheet_client() # Get authorized client
    if not gc: return False # Check if client init failed
    try:
        sheet_name = "pilot_survey_results"
        worksheet = gc.open(sheet_name).sheet1
        # ... (rest of GSheet saving logic unchanged) ...
        submission_time_utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        consent_given = st.session_state.get("consent_given", "ERROR")
        ai_transcript_formatted = st.session_state.get("current_formatted_transcript_for_gsheet", "ERROR")
        manual_answers_formatted = st.session_state.get("manual_answers_formatted", "")
        # Split transcript
        CHUNK_SIZE=40000; MAX_TRANSCRIPT_COLUMNS=5
        ai_transcript_chunks = [ai_transcript_formatted[i:i+CHUNK_SIZE] for i in range(0, len(ai_transcript_formatted), CHUNK_SIZE)]
        ai_transcript_parts_for_sheet = ai_transcript_chunks[:MAX_TRANSCRIPT_COLUMNS] + [""] * (MAX_TRANSCRIPT_COLUMNS - len(ai_transcript_chunks))
        # Build row
        row_to_append = [ username, submission_time_utc, str(consent_given), survey_responses.get("age", ""), survey_responses.get("gender", ""), survey_responses.get("major", ""), survey_responses.get("year", ""), survey_responses.get("gpa", ""), survey_responses.get("ai_frequency", ""), survey_responses.get("ai_model", ""), *ai_transcript_parts_for_sheet, manual_answers_formatted ]
        time.sleep(random.uniform(0.1, 1.5))
        worksheet.append_row(row_to_append, value_input_option='USER_ENTERED')
        st.session_state["gsheet_save_successful"] = True; return True
    # ... (Keep existing GSheet error handling) ...
    except Exception as e: print(f"Error saving survey data to GSheet: {e}"); st.error(f"GSheet Save Error: {e}"); return False


# --- Other Util Functions (Unchanged logic, ensure they call correct save/load functions) ---
def format_transcript_for_gsheet(messages_to_format=None):
    # ... (Keep original logic) ...
    try:
        messages = messages_to_format if messages_to_format is not None else st.session_state.get("messages", [])
        if messages: lines = []; # ... build lines ...
            for message in messages:
                role = message.get('role', 'Unknown'); content = message.get('content', '')
                if role == 'system': continue
                closing_vals = list(config.CLOSING_MESSAGES.values()); closing_keys = list(config.CLOSING_MESSAGES.keys())
                if content in closing_vals or content in closing_keys: continue
                lines.append(f"{role.capitalize()}: {content}")
            return "\n---\n".join(lines)
        else: return "ERROR: No messages for formatting."
    except Exception as e: print(f"Error formatting transcript: {e}"); return f"ERROR: {e}"

def save_timing_to_state(username):
     # ... (Keep original logic using save_interview_state_to_firestore) ...
    try:
        end_time_unix = time.time(); start_time_unix = st.session_state.get("start_time_unix", None)
        if start_time_unix: # ... calculate timing ...
             duration_seconds = round(end_time_unix - start_time_unix); duration_minutes = duration_seconds / 60.0
             start_time_utc_str = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(start_time_unix)); end_time_utc_str = time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(end_time_unix))
             timing_data = { "start_time_unix": start_time_unix, "start_time_utc": start_time_utc_str, "end_time_unix": end_time_unix, "end_time_utc": end_time_utc_str, "duration_seconds": duration_seconds, "duration_minutes": round(duration_minutes, 2) }
             save_success = save_interview_state_to_firestore(username, {"timing_data": timing_data}) # Calls Firestore state save
             if save_success: st.session_state["timing_data"] = timing_data; return True
             else: print(f"ERROR: Failed saving timing to Firestore"); return False
        else: print("Warning: start_time_unix not found."); return False
    except Exception as e: print(f"Error saving timing: {e}"); return False

def check_if_survey_completed(username):
     # ... (Keep original logic using get_firestore_client) ...
    db = get_firestore_client()
    if db and username:
        try: state_doc_ref = db.collection("interviews").document(username); state_doc = state_doc_ref.get()
            if state_doc.exists: state_data = state_doc.to_dict(); return state_data.get("survey_completed_flag", False) is True
        except Exception as e: print(f"Error checking survey completion: {e}")
    return False

def save_survey_data(username, survey_responses):
     # ... (Keep original logic calling save_survey_data_to_gsheet and backup/state functions) ...
    consent_given = st.session_state.get("consent_given", False)
    ai_transcript = st.session_state.get("current_formatted_transcript_for_gsheet", "ERROR: AI transcript missing")
    manual_answers = st.session_state.get("manual_answers_formatted", "")
    combined_transcript = f"AI Transcript:\n{ai_transcript}\n\nManual Answers:\n{manual_answers}".strip()
    gsheet_success = save_survey_data_to_gsheet(username, survey_responses)
    st.session_state.saved_to_gsheet_successfully = gsheet_success
    firestore_save_attempted = save_survey_data_to_firestore_backup(username, survey_responses, consent_given, combined_transcript, gsheet_success)
    if not firestore_save_attempted: st.warning("Failed to save survey data backup to Firestore.")
    state_update_success = save_interview_state_to_firestore(username, {"survey_completed_flag": True, "current_stage": config.COMPLETED_STAGE })
    if not state_update_success: print("ERROR: Failed to update final state flags in Firestore.")
    return gsheet_success # Return GSheet status for UI

def save_survey_data_to_firestore_backup(username, survey_responses, consent_given, combined_transcript, gsheet_save_status):
     # ... (Keep original logic using get_firestore_client) ...
    db = get_firestore_client()
    if not db: return False
    try: # ... build data_to_save dict ...
        submission_time_unix = time.time()
        data_to_save = { "username": username, "submission_timestamp_unix": submission_time_unix, "submission_time_utc": time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(submission_time_unix)), "consent_given": consent_given, "survey_responses": survey_responses, "combined_transcript": combined_transcript, "saved_to_gsheet_successfully": gsheet_save_status, "last_updated": firestore.SERVER_TIMESTAMP }
        survey_doc_ref = db.collection("interviews").document(username)
        survey_doc_ref.set({"survey_backup_data": data_to_save}, merge=True)
        print(f"INFO: Survey backup data saved to Firestore for user {username}")
        return True
    except Exception as e: print(f"Error saving survey backup: {e}"); return False

# --- Function Renaming for Clarity ---
# This function IS the one loading state from Firestore using env vars
initialize_session_state_from_env = load_interview_state_from_firestore