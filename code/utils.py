# utils.py
import streamlit as st
import time
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


# --- Firestore Client Initialization ---
@st.cache_resource # Cache the client across reruns for the same session
def get_firestore_client():
    """Initializes and returns a Firestore client using credentials from Streamlit secrets."""
    try:
        # Get credentials from the dedicated section in secrets.toml
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


# --- NEW Firestore Utility Functions ---

def save_message_to_firestore(username, message_data):
    """Saves a single message to Firestore."""
    db = get_firestore_client()
    if not db or not username or not message_data:
        print("Error: Cannot save message, invalid input or DB client.")
        return False

    try:
        # Add a server timestamp for ordering
        message_data_with_ts = message_data.copy()
        message_data_with_ts['timestamp'] = firestore.SERVER_TIMESTAMP

        # Path: interviews/{username}/messages/{auto-id}
        # Using .add() automatically generates a unique doc ID for each message
        doc_ref = db.collection("interviews").document(username).collection("messages").add(message_data_with_ts)
        # print(f"Message saved to Firestore for user {username}") # Can be verbose
        return True
    except Exception as e:
        print(f"Error saving message to Firestore for user {username}: {e}")
        # Optional: st.warning("Could not save message progress.") # Avoid cluttering UI?
        return False

def save_interview_state_to_firestore(username, state_data):
    """Saves key interview state variables to Firestore."""
    db = get_firestore_client()
    if not db or not username or not state_data:
        print("Error: Cannot save state, invalid input or DB client.")
        return False

    try:
        # Add a server timestamp for last update
        state_data_with_ts = state_data.copy()
        state_data_with_ts['last_updated'] = firestore.SERVER_TIMESTAMP

        # Path: interviews/{username}
        doc_ref = db.collection("interviews").document(username)
        # Use merge=True to update fields without overwriting the whole document
        # (especially important to avoid deleting the messages subcollection)
        doc_ref.set(state_data_with_ts, merge=True)
        # print(f"State saved to Firestore for user {username}: {list(state_data.keys())}")
        return True
    except Exception as e:
        print(f"Error saving state to Firestore for user {username}: {e}")
        # Optional: st.warning("Could not save session state.")
        return False

def load_interview_state_from_firestore(username):
    """Loads interview state and messages from Firestore."""
    db = get_firestore_client()
    if not db or not username:
        print("Error: Cannot load state, invalid input or DB client.")
        return None, [] # Return empty state and messages

    loaded_state = {}
    loaded_messages = []

    try:
        # Load main state document: interviews/{username}
        state_doc_ref = db.collection("interviews").document(username)
        state_doc = state_doc_ref.get()
        if state_doc.exists:
            loaded_state = state_doc.to_dict()
            # Remove Firestore timestamp objects if they cause issues with session state
            loaded_state.pop('last_updated', None)
            # Timestamps need careful handling if stored directly - loading Unix timestamp is safer
            # loaded_state.pop('start_time', None)
            print(f"State loaded from Firestore for user {username}")
        else:
            print(f"No existing state found in Firestore for user {username}")

        # Load messages subcollection: interviews/{username}/messages
        # Order messages by the timestamp they were saved
        messages_ref = state_doc_ref.collection("messages").order_by("timestamp", direction=firestore.Query.ASCENDING)
        docs = messages_ref.stream() # Use stream() for potentially large collections

        for doc in docs:
            msg = doc.to_dict()
            # Remove Firestore timestamp before adding to session state list
            msg.pop('timestamp', None)
            # Ensure keys match st.session_state.messages structure
            if 'role' in msg and 'content' in msg:
                 loaded_messages.append({'role': msg['role'], 'content': msg['content']})

        if loaded_messages:
             print(f"Loaded {len(loaded_messages)} messages from Firestore for user {username}")

        return loaded_state, loaded_messages

    except Exception as e:
        print(f"Error loading state/messages from Firestore for user {username}: {e}")
        # Return empty/default state on error to allow app to start fresh
        return {}, []


# --- Password Check (Keep signature if config.LOGINS might be True, otherwise remove) ---
# def check_password():
#     """ Example: Returns 'True' if the user has entered a correct password."""
#     # Implement your login logic here if config.LOGINS is True
#     pass


# --- Interview Check ---
def check_if_interview_completed(username):
    """Checks if the final interview transcript JSON file exists for the user.
       NOTE: This local check is less reliable than Firestore state in deployment."""
    # --- NOTE: This checks local files which are ephemeral in deployment. ---
    file_path = os.path.join(config.TRANSCRIPTS_DIRECTORY, f"{username}_transcript.json")
    return os.path.exists(file_path)


# --- Interview Save (Local Files - Primarily for Timing Data / GSheet Formatting) ---
def save_interview_data(
    username,
    transcripts_directory,
    times_directory,
    file_name_addition_transcript="",
    file_name_addition_time="",
    is_final_save=False,
    # --- NEW: Optional messages list override ---
    messages_to_format=None
):
    """Formats transcript for GSheet if is_final_save=True (using provided messages
       or session state). Saves timing data locally."""
    os.makedirs(transcripts_directory, exist_ok=True)
    os.makedirs(times_directory, exist_ok=True)

    if is_final_save:
        try:
            # Use provided messages if available, otherwise use session state
            messages = messages_to_format if messages_to_format is not None else st.session_state.get("messages", [])

            if messages:
                lines = []
                for message in messages:
                    if message.get('role') == 'system': continue
                    if message.get('content', '') in config.CLOSING_MESSAGES.keys(): continue
                    lines.append(f"{message.get('role', 'Unknown').capitalize()}: {message.get('content', '')}")
                formatted_transcript_string = "\n---\n".join(lines)
                # Store in a specific key used by the GSheet function
                st.session_state.current_formatted_transcript_for_gsheet = formatted_transcript_string
                print("Formatted transcript prepared for GSheet.")
            else:
                print(f"Warning: No messages provided or found for transcript formatting for user {username}.")
                st.session_state.current_formatted_transcript_for_gsheet = "ERROR: No messages found for formatting."
        except Exception as e:
            print(f"Error processing final transcript for {username}: {e}")
            st.error(f"Error processing transcript: {e}")
            st.session_state.current_formatted_transcript_for_gsheet = f"ERROR: Processing transcript failed - {e}"

    # --- Save timing data (Keep as is) ---
    time_filename = f"{username}{file_name_addition_time}_time.csv"
    time_path = os.path.join(times_directory, time_filename)
    try:
        end_time = time.time()
        start_time = st.session_state.get("start_time", None)
        duration_seconds = round(end_time - start_time) if start_time else 0
        duration_minutes = duration_seconds / 60.0
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
    """Creates the survey directory defined in config if it doesn't exist."""
    # --- NOTE: Ephemeral in deployment ---
    os.makedirs(config.SURVEY_DIRECTORY, exist_ok=True)


def check_if_survey_completed(username):
    """Checks if survey completed flag is set in Firestore OR if flag file exists."""
    # --- MODIFIED: Prioritize checking Firestore ---

    # 1. Check Firestore state first
    db = get_firestore_client()
    if db and username:
        try:
            state_doc_ref = db.collection("interviews").document(username)
            state_doc = state_doc_ref.get()
            if state_doc.exists:
                state_data = state_doc.to_dict()
                if state_data.get("survey_completed_flag", False) is True:
                    print(f"Survey completion flag TRUE in Firestore for {username}")
                    return True
        except Exception as e:
            print(f"Error checking survey completion in Firestore for {username}: {e}")
            # Proceed to check local file as fallback

    # 2. Check for local flag file (mainly for local dev or if Firestore fails)
    # --- NOTE: Flag file is ephemeral in deployment ---
    gsheet_check_path = os.path.join(config.SURVEY_DIRECTORY, f"{username}_survey_submitted_gsheet.flag")
    if os.path.exists(gsheet_check_path):
        print(f"Survey completion flag file found locally for {username}")
        return True

    return False # Not completed


def save_survey_data_local(username, survey_responses):
    """Saves the survey responses locally as a JSON file (optional backup)."""
    # --- NOTE: Ephemeral in deployment ---
    file_path = os.path.join(config.SURVEY_DIRECTORY, f"{username}_survey.json")
    consent_given = st.session_state.get("consent_given", False)
    data_to_save = {
        "username": username,
        "submission_timestamp_unix": time.time(),
        "submission_time_utc": time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()),
        "consent_given": consent_given,
        "responses": survey_responses
    }
    try:
        with open(file_path, "w", encoding='utf-8') as f:
            json.dump(data_to_save, f, indent=4, ensure_ascii=False)
        print(f"Local survey backup saved for {username}.") # Added print
        return True
    except Exception as e:
        # st.error(f"Error saving local survey backup: {e}") # Avoid UI error
        print(f"Error saving local survey backup for {username}: {e}")
        return False

def save_survey_data_to_firestore(username, survey_responses, consent_given, formatted_transcript, gsheet_save_status):
    """Saves survey responses and transcript to Firestore."""
    db = get_firestore_client()
    if not db or not username:
        print("Error: Cannot save survey to Firestore, invalid input or DB client.")
        return False

    try:
        data_to_save = {
            "username": username,
            "submission_timestamp_unix": time.time(),
            "submission_time_utc": time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()),
            "consent_given": consent_given,
            "survey_responses": survey_responses,
            "formatted_transcript": formatted_transcript, # Save transcript here too
            "saved_to_gsheet_successfully": gsheet_save_status, # Track if GSheet save worked
            "last_updated": firestore.SERVER_TIMESTAMP
        }

        # Save to the main interview document
        survey_doc_ref = db.collection("interviews").document(username)

        # Use merge=True to add survey data without overwriting messages subcollection etc.
        survey_doc_ref.set({"survey_data": data_to_save}, merge=True)
        print(f"Survey data saved to Firestore for user {username}")
        return True
    except Exception as e:
        print(f"Error saving survey data to Firestore for user {username}: {e}")
        return False


def save_survey_data_to_gsheet(username, survey_responses):
    """Saves the survey responses, consent status, PARTIAL AI transcript,
       AND MANUAL answers to Google Sheets."""
    st.session_state["gsheet_save_successful"] = False # Reset flag
    try:
        # --- GSheet Client Setup (Keep as is) ---
        scopes = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
        creds_dict = st.secrets["connections"]["gsheets"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)

        sheet_name = "pilot_survey_results" # Ensure this is correct
        worksheet = gc.open(sheet_name).sheet1
        submission_time_utc = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        consent_given = st.session_state.get("consent_given", "ERROR: Consent status missing")

        # --- Get Formatted Transcripts (AI and Manual) ---
        # This key is now populated by save_interview_data or the manual fallback logic
        ai_transcript_formatted = st.session_state.get("current_formatted_transcript_for_gsheet", "ERROR: AI Transcript not processed.")
        manual_answers_formatted = st.session_state.get("manual_answers_formatted", "") # Get manual answers if they exist
        # --- End Get Formatted Transcripts ---

        # --- AI Transcript Splitting Logic ---
        CHUNK_SIZE = 40000
        ai_transcript_chunks = [ai_transcript_formatted[i:i + CHUNK_SIZE] for i in range(0, len(ai_transcript_formatted), CHUNK_SIZE)]
        MAX_TRANSCRIPT_COLUMNS = 5 # Keep consistent
        ai_transcript_parts_for_sheet = ai_transcript_chunks[:MAX_TRANSCRIPT_COLUMNS] + [""] * (MAX_TRANSCRIPT_COLUMNS - len(ai_transcript_chunks))
        if len(ai_transcript_chunks) > MAX_TRANSCRIPT_COLUMNS:
            print(f"Warning: AI Transcript for {username} was longer than {MAX_TRANSCRIPT_COLUMNS} columns and has been truncated in GSheet.")
        # --- End AI Transcript Splitting ---

        # --- Define row - INCLUDING Manual Answers Column ---
        row_to_append = [
            username,                           # Col A: Username
            submission_time_utc,                # Col B: Timestamp
            str(consent_given),                 # Col C: Consent Given
            survey_responses.get("age", ""),            # Col D: Age
            survey_responses.get("gender", ""),         # Col E: Gender
            survey_responses.get("major", ""),          # Col F: Major
            survey_responses.get("year", ""),           # Col G: Year of Study
            survey_responses.get("gpa", ""),            # Col H: GPA
            survey_responses.get("ai_frequency", ""),   # Col I: AI Use Frequency
            survey_responses.get("ai_model", ""),       # Col J: AI Model Name
            # AI Transcript Parts (Cols K-O assuming 5 parts)
            *ai_transcript_parts_for_sheet,
            # Manual Answers (Col P - assuming one column)
            manual_answers_formatted
        ]
        # --- End Row Definition ---

        # --- Throttling (Keep as is) ---
        time.sleep(random.uniform(0.1, 1.5))

        worksheet.append_row(row_to_append, value_input_option='USER_ENTERED')
        print(f"Survey data, AI transcript & Manual answers (if any) for {username} appended to GSheet '{sheet_name}'.")
        st.session_state["gsheet_save_successful"] = True

        # --- Local flag file write (Keep as is) ---
        flag_file_path = os.path.join(config.SURVEY_DIRECTORY, f"{username}_survey_submitted_gsheet.flag")
        try:
            with open(flag_file_path, 'w') as f: f.write(f"Submitted at {submission_time_utc}")
        except Exception as flag_e:
            print(f"Warning: Failed to create local completion flag file for {username}: {flag_e}")

        return True

    # --- Error Handling (Keep as is) ---
    except gspread.exceptions.APIError as api_e:
        st.error(f"Error saving to Google Sheets: API Error - {api_e}. Please try submitting again.")
        print(f"GSheet API Error for {username}: {api_e}")
        return False
    except gspread.exceptions.SpreadsheetNotFound:
        st.error(f"Error: Spreadsheet '{sheet_name}' not found.")
        print(f"Spreadsheet '{sheet_name}' not found.")
        return False
    except Exception as e:
        st.error(f"An error occurred saving to Google Sheets: {e}.")
        print(f"Error saving survey data for {username} to GSheet: {e}")
        return False


def save_survey_data(username, survey_responses):
    """Main function to save survey data. Tries GSheet first, then Firestore backup."""
    create_survey_directory() # Creates local ephemeral dir

    # Transcript formatting should have happened either at normal completion
    # or just before entering the survey stage from manual fallback.
    # Retrieve the potentially partial AI transcript and manual answers from session state.
    consent_given = st.session_state.get("consent_given", False)
    ai_transcript = st.session_state.get("current_formatted_transcript_for_gsheet", "ERROR: Transcript not available")
    manual_answers = st.session_state.get("manual_answers_formatted", "") # Will be empty if no fallback

    # --- Attempt GSheet Save ---
    gsheet_success = save_survey_data_to_gsheet(username, survey_responses)

    # --- Save combined info to Firestore ---
    # Pass GSheet status along for the record
    firestore_save_attempted = save_survey_data_to_firestore(
        username, survey_responses, consent_given,
        # Save *both* transcripts to Firestore if they exist
        f"AI Transcript:\n{ai_transcript}\n\nManual Answers:\n{manual_answers}".strip(),
        gsheet_success
    )
    if not firestore_save_attempted:
         st.warning("Failed to save survey data backup to Firestore.")

    # --- Local backup (optional, ephemeral) ---
    save_survey_data_local(username, survey_responses)

    # --- Update Firestore completion flag ---
    if firestore_save_attempted: # Mark completed in Firestore state if backup worked
         save_interview_state_to_firestore(username, {"survey_completed_flag": True})

    return gsheet_success # Return success based on GSheet save