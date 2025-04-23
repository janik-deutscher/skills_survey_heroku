# app.py (Heroku Secrets Version)
import streamlit as st
import os # For environment variables
# import requests # Keep only if actually used directly in app.py
import time
# import pandas as pd # Remove if not used in app.py
import utils # Import your utils module (Heroku version)
import config
import json # Keep if used directly in app.py
import numpy as np
import uuid
import random # For GSheet throttle sleep
import re # For parsing outline

# --- Tenacity Imports ---
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# --- Constants ---
WELCOME_STAGE = "welcome"
INTERVIEW_STAGE = "interview"
MANUAL_INTERVIEW_STAGE = "manual_interview"
SURVEY_STAGE = "survey"
COMPLETED_STAGE = "completed" # Ensure this matches config if used there

# --- API Setup & Retry Configuration ---
openai_client = None
anthropic_client = None
api = None
RETRYABLE_ERRORS = ()

# --- HEROKU CHANGE: Fetch API key from environment variables ---
openai_api_key_from_env = os.environ.get('API_KEY_OPENAI')
# anthropic_api_key_from_env = os.environ.get('API_KEY_ANTHROPIC')

if "gpt" in config.MODEL.lower():
    api = "openai"
    from openai import OpenAI, RateLimitError, APITimeoutError, APIConnectionError, InternalServerError as OpenAIInternalServerError
    if openai_api_key_from_env:
        try:
            openai_client = OpenAI(api_key=openai_api_key_from_env, timeout=60.0)
            print("INFO: OpenAI client initialized successfully using environment variable.")
        except Exception as e:
            st.error(f"CRITICAL Error initializing OpenAI client from environment variable: {e}"); st.stop()
    else:
        st.error("CRITICAL: Environment variable 'API_KEY_OPENAI' not found.");
        st.info("Hint: If running locally, set the environment variable. If deploying, ensure it's set as a Heroku Config Var.")
        st.stop()
    RETRYABLE_ERRORS = (RateLimitError, APITimeoutError, APIConnectionError, OpenAIInternalServerError)

elif "claude" in config.MODEL.lower():
    # Add Anthropic initialization using os.environ.get('API_KEY_ANTHROPIC') if needed
    st.error("Anthropic client initialization needs implementation if used.")
    st.stop()
else:
    st.error("Model name must contain 'gpt' or 'claude'."); st.stop()

api_retry_decorator = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(RETRYABLE_ERRORS),
    reraise=True
)
# --- End API Setup & Retry ---

# --- Manual Interview Questions Setup (Keep original logic) ---
outline_parts = config.INTERVIEW_OUTLINE.split("**Part ")
manual_questions_map = {}
part_keys = ["Intro", "I", "II", "III", "IV"]
current_part_index = 0
intro_match = re.search(r"\*\*Begin the interview with:\*\*\s*'(.*?)'", config.INTERVIEW_OUTLINE, re.DOTALL)
if intro_match: manual_questions_map["Intro"] = [{"key": "intro_q", "text": intro_match.group(1).strip()}]
else: manual_questions_map["Intro"] = []
framing_match = re.search(r"\*\*Ask Next \(Framing Q\):\*\*\s*'(.*?)'", config.INTERVIEW_OUTLINE, re.DOTALL)
if framing_match: manual_questions_map["Framing"] = [{"key": "framing_q", "text": framing_match.group(1).strip()}]
else: manual_questions_map["Framing"] = []
for i, part_text in enumerate(outline_parts[1:]):
    part_key = f"Part{part_keys[i+1]}"; manual_questions_map[part_key] = []
    ask_matches = re.findall(r"\*\*Ask\s*(?:\(.*?Q\))?\s*:\*\*\s*'(.*?)'", part_text, re.DOTALL)
    for q_idx, q_text in enumerate(ask_matches):
        manual_questions_map[part_key].append({"key": f"{part_key}_q{q_idx+1}", "text": q_text.strip()})
manual_questions_map["Summary"] = [{"key": "summary_prompt", "text": "Based on our discussion (including any AI parts and your manual answers), could you briefly summarize your key perspectives on skills and AI's impact on them?"}]
part_key_sequence = ["Intro", "Framing", "PartI", "PartII", "PartIII", "PartIV", "Summary"]

def find_last_ai_part_completed(messages):
    # (Keep original logic)
    last_completed_part_index = -1
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if manual_questions_map.get("Framing") and manual_questions_map["Framing"][0]["text"] in content: return 0
            for idx, part_key in enumerate(part_keys[1:]):
                part_index_in_list = idx + 1
                if part_key in manual_questions_map:
                    for question_data in manual_questions_map[part_key]:
                         if question_data["text"][:50] in content[:70]:
                             last_completed_part_index = max(last_completed_part_index, part_index_in_list); break
            if last_completed_part_index > 0: break
    if last_completed_part_index == -1 and manual_questions_map.get("Intro"):
         if any(manual_questions_map["Intro"][0]["text"] in msg.get("content","") for msg in reversed(messages) if msg.get("role")=="assistant"): return -1
    return last_completed_part_index

# --- Page Config (Heroku compatible) ---
st.set_page_config(page_title="Skills & AI Interview") # No icon needed here

# --- User Identification & Session State Initialization ---
if "session_initialized" not in st.session_state: st.session_state.session_initialized = False
if "username" not in st.session_state: st.session_state.username = None

# Generate UUID if none exists
if st.session_state.username is None:
    st.session_state.username = f"user_{uuid.uuid4()}"
    print(f"INFO: Generated potential new user UUID: {st.session_state.username}.")

username = st.session_state.username

# --- Initialize Session State Function (using Firestore backend via Env Vars) ---
def initialize_session_state_from_env(user_id): # Use this name consistently
    if st.session_state.get("session_initialized", False): return
    print(f"Attempting to initialize session from Firestore (via Env) for user: {user_id}")

    # Default values
    default_values = {
        "messages": [], "current_stage": WELCOME_STAGE, "consent_given": False,
        "start_time_unix": None, "interview_active": False, "interview_completed_flag": False,
        "survey_completed_flag": False, "welcome_shown": False, "partial_ai_transcript_formatted": "",
        "manual_answers_formatted": "", "current_formatted_transcript_for_gsheet": "",
        "timing_data": None, "saved_to_gsheet_successfully": None
    }
    for key, default_value in default_values.items():
        if key not in st.session_state: st.session_state[key] = default_value
    print("Initialized session state with default values.")

    # Attempt to load existing state from Firestore
    loaded_state, loaded_messages = utils.load_interview_state_from_firestore(user_id) # Call Firestore loader

    # Always ensure messages list exists
    if 'messages' not in st.session_state or not isinstance(st.session_state.messages, list):
        st.session_state.messages = []
    st.session_state.messages = loaded_messages

    # Inject system prompt if needed
    if api == "openai":
        if not st.session_state.messages or st.session_state.messages[0].get("role") != "system":
            print("INFO: System prompt missing. Re-injecting.")
            sys_prompt_dict = {"role": "system", "content": config.SYSTEM_PROMPT}
            if isinstance(st.session_state.messages, list):
                 st.session_state.messages.insert(0, sys_prompt_dict)
            else:
                 st.session_state.messages = [sys_prompt_dict]

    # Overwrite defaults with loaded state
    if loaded_state:
        print(f"INFO: Overwriting defaults with state loaded from Firestore for user: {user_id}")
        for key in default_values:
            if key in loaded_state:
                 st.session_state[key] = loaded_state[key]
    elif not loaded_messages:
        print(f"INFO: No previous state/messages found for {user_id} in Firestore. Initializing fresh.")

    st.session_state.session_initialized = True
    print(f"INFO: Session initialized. Stage: {st.session_state.get('current_stage')}, Msgs: {len(st.session_state.get('messages', []))}, StartTimeUnix: {st.session_state.get('start_time_unix')}")

# --- Function to Determine Current Stage (Keep original logic) ---
def determine_current_stage(user_id):
    current_stage_in_state = st.session_state.get("current_stage"); new_stage = current_stage_in_state
    # Use the Firestore check function
    survey_done = utils.check_if_survey_completed(user_id) # Call util check
    # survey_done = st.session_state.get("survey_completed_flag", False) # Less reliable? Check DB.
    interview_done = st.session_state.get("interview_completed_flag", False)
    welcome_done = st.session_state.get("welcome_shown", False)
    manual_fallback = (current_stage_in_state == MANUAL_INTERVIEW_STAGE)

    if survey_done: new_stage = COMPLETED_STAGE
    elif manual_fallback: new_stage = MANUAL_INTERVIEW_STAGE
    elif interview_done: new_stage = SURVEY_STAGE
    elif welcome_done: new_stage = INTERVIEW_STAGE
    else: new_stage = WELCOME_STAGE

    if new_stage != current_stage_in_state:
         print(f"INFO: Stage re-determined: {current_stage_in_state} -> {new_stage}")
         st.session_state.current_stage = new_stage


# --- Initialize Session State ---
if username is None:
    st.error("CRITICAL: Username could not be determined."); st.stop()
if not st.session_state.get("session_initialized", False):
    initialize_session_state_from_env(username) # Use the correct init function name
    determine_current_stage(username)
    st.rerun()

# --- === Main Application Logic === ---
if not st.session_state.get("session_initialized", False):
    st.spinner("Initializing session...")
    st.stop()

# --- Section 0: Welcome Stage ---
if st.session_state.get("current_stage") == WELCOME_STAGE:
    st.title("Welcome")
    # Display welcome markdown (ensure username is displayed)
    st.markdown(f"""
    Hi there, [...]
    *(Your User ID for this session is: `{st.session_state.username}`)*
    """)
    st.markdown("---")
    st.subheader("Information Sheet & Consent Form")
    # Display consent form markdown (ensure username interpolation works & text is updated)
    st.markdown(f"""
**Study Title:** [...] \n
**Researcher:** [...]
[...]
**3. Privacy, Anonymity, API Usage, and Logging:**
*   [...] User ID: `{st.session_state.username}`.
[...]
*   **Research Data:** [...] linked to your **anonymized User ID** (`{st.session_state.username}`).
[...]
*   **Persistent Logging (Firestore):** To prevent data loss [...], your anonymized chat messages and session state are saved to a secure cloud database (Google Cloud Firestore) [...]. Data is linked only to your anonymized User ID (`{st.session_state.username}`). Final results are also sent to Google Sheets.
[...]
**5. Voluntary Participation and Withdrawal:**
[...]
*   If you have concerns [...], contact Janik Deutscher (janik.deutscher@upf.edu) with your User ID (`{st.session_state.username}`). [...]
[...]
*(Rest of consent form markdown)*
    """)

    # Consent Checkbox Logic (Calls Firestore save)
    consent = st.checkbox("I confirm that I have read...", key="consent_checkbox", value=st.session_state.get("consent_given", False))
    if consent != st.session_state.get("consent_given", False):
        st.session_state.consent_given = consent
        utils.save_interview_state_to_firestore(username, {'consent_given': consent}) # Calls Firestore save

    # Start Button Logic (Calls Firestore save)
    if st.button("Start Interview", key="start_interview_btn", disabled=not st.session_state.get("consent_given", False)):
        if st.session_state.get("consent_given", False):
            st.session_state.welcome_shown = True
            st.session_state.current_stage = INTERVIEW_STAGE
            utils.save_interview_state_to_firestore(username, {'welcome_shown': True, 'current_stage': INTERVIEW_STAGE}) # Calls Firestore save
            print("INFO: Moving to Interview Stage from Welcome"); st.rerun()


# --- Section 1: Interview Stage ---
elif st.session_state.get("current_stage") == INTERVIEW_STAGE:
    st.title("Part 1: Interview")

    # Start time handling (Calls Firestore save)
    if st.session_state.get("start_time_unix") is None:
        current_time = time.time()
        st.session_state.start_time_unix = current_time
        utils.save_interview_state_to_firestore(username, {"start_time_unix": current_time}) # Calls Firestore save
        print(f"INFO: Start time initialized ({current_time}).")

    # Mark interview active (Calls Firestore save)
    if not st.session_state.get("interview_active", False):
         st.session_state.interview_active = True
         utils.save_interview_state_to_firestore(username, {"interview_active": True}) # Calls Firestore save
         print("INFO: Interview marked as active.")

    st.info("Please answer the interviewer's questions.")

    # Quit Button Logic (Calls Firestore saves)
    if st.button("Quit Interview Early", key="quit_interview"):
        st.session_state.interview_active = False
        st.session_state.interview_completed_flag = True
        quit_message = "You have chosen to end the interview early..."
        quit_msg_dict = {"role": "assistant", "content": quit_message}
        st.session_state.messages.append(quit_msg_dict)
        utils.save_message_to_firestore(username, quit_msg_dict) # Calls Firestore save

        utils.save_timing_to_state(username) # Calls Firestore state save internally
        formatted_transcript = utils.format_transcript_for_gsheet(st.session_state.messages)
        st.session_state.current_formatted_transcript_for_gsheet = formatted_transcript
        state_update = {
            "interview_active": False, "interview_completed_flag": True,
            "current_stage": SURVEY_STAGE, "partial_ai_transcript_formatted": formatted_transcript
        }
        utils.save_interview_state_to_firestore(username, state_update) # Calls Firestore save

        st.warning(quit_message)
        st.session_state.current_stage = SURVEY_STAGE
        print("INFO: Moving to Survey Stage after Quit."); time.sleep(1); st.rerun()

    # Display chat messages (Logic Unchanged)
    for message in st.session_state.get("messages", []):
        # ... (message filtering logic unchanged) ...
        if message.get('role') == "system": continue
        content = message.get('content', '')
        closing_vals = list(config.CLOSING_MESSAGES.values())
        closing_keys = list(config.CLOSING_MESSAGES.keys())
        if content in closing_vals or content in closing_keys: continue
        avatar = config.AVATAR_INTERVIEWER if message.get('role') == "assistant" else config.AVATAR_RESPONDENT
        with st.chat_message(message.get('role', 'unknown'), avatar=avatar): st.markdown(content)

    # Initial message generation (Calls Firestore save)
    if not st.session_state.get("messages", []) or \
       (api == "openai" and len(st.session_state.get("messages", [])) == 1 and st.session_state.get("messages", [])[0].get("role") == "system"):
        # ... (API call logic unchanged) ...
        try:
            # ... (placeholder setup) ...
                try:
                    # ... (API call execution) ...
                    message_placeholder.markdown(message_interviewer)
                except RETRYABLE_ERRORS as e_retry:
                     # ... (Error handling - calls Firestore save) ...
                     print(f"ERROR: Initial API call failed: {e_retry}")
                     message_placeholder.error(f"Error connecting... Switching fallback.")
                     partial_transcript = utils.format_transcript_for_gsheet(st.session_state.messages)
                     st.session_state.partial_ai_transcript_formatted = partial_transcript
                     state_update = {"current_stage": MANUAL_INTERVIEW_STAGE,"interview_active": False,"manual_fallback_triggered": True,"partial_ai_transcript_formatted": partial_transcript}
                     utils.save_interview_state_to_firestore(username, state_update) # Calls Firestore save
                     st.session_state.current_stage = MANUAL_INTERVIEW_STAGE; st.rerun()
                except Exception as e_fatal:
                     # ... (Error handling - calls Firestore save) ...
                     print(f"ERROR: Non-retryable initial API error: {e_fatal}")
                     message_placeholder.error(f"Unexpected error... Switching fallback.")
                     partial_transcript = utils.format_transcript_for_gsheet(st.session_state.messages)
                     st.session_state.partial_ai_transcript_formatted = partial_transcript
                     state_update = {"current_stage": MANUAL_INTERVIEW_STAGE,"interview_active": False,"manual_fallback_triggered": True,"partial_ai_transcript_formatted": partial_transcript}
                     utils.save_interview_state_to_firestore(username, state_update) # Calls Firestore save
                     st.session_state.current_stage = MANUAL_INTERVIEW_STAGE; st.rerun()
            # ... (Save assistant message - calls Firestore save) ...
            assistant_msg_dict = {"role": "assistant", "content": message_interviewer.strip()}
            st.session_state.messages.append(assistant_msg_dict)
            utils.save_message_to_firestore(username, assistant_msg_dict) # Calls Firestore save
            print("INFO: Initial message obtained and saved."); time.sleep(0.1); st.rerun()
        except Exception as e:
            # ... (Outer error handling) ...
            st.error(f"Failed initial message setup: {e}"); st.stop()

    # Handle user input (Calls Firestore saves)
    if prompt := st.chat_input("Your response..."):
        user_msg_dict = {"role": "user", "content": prompt}
        st.session_state.messages.append(user_msg_dict)
        utils.save_message_to_firestore(username, user_msg_dict) # Calls Firestore save
        with st.chat_message("user", avatar=config.AVATAR_RESPONDENT): st.markdown(prompt)
        try:
            with st.chat_message("assistant", avatar=config.AVATAR_INTERVIEWER):
                 # ... (placeholder setup) ...
                 try:
                    # ... (API streaming logic unchanged) ...
                    # ... (Save assistant message - calls Firestore save) ...
                    assistant_msg_content = full_response_content.strip()
                    assistant_msg_dict = {"role": "assistant", "content": assistant_msg_content}
                    if not st.session_state.messages or st.session_state.messages[-1] != assistant_msg_dict:
                        st.session_state.messages.append(assistant_msg_dict)
                        utils.save_message_to_firestore(username, assistant_msg_dict) # Calls Firestore save
                    # ... (Handle code detection - calls Firestore saves) ...
                    if detected_code:
                        # ... (set flags) ...
                        utils.save_timing_to_state(username) # Calls Firestore save internally
                        formatted_transcript = utils.format_transcript_for_gsheet(st.session_state.messages)
                        st.session_state.current_formatted_transcript_for_gsheet = formatted_transcript
                        state_update = {"interview_active": False,"interview_completed_flag": True,"current_stage": SURVEY_STAGE,"partial_ai_transcript_formatted": formatted_transcript}
                        utils.save_interview_state_to_firestore(username, state_update) # Calls Firestore save
                        # ... (display message, change stage, rerun) ...

                 except RETRYABLE_ERRORS as e_retry:
                     # ... (Error handling - calls Firestore save) ...
                     partial_transcript = utils.format_transcript_for_gsheet(st.session_state.messages)
                     st.session_state.partial_ai_transcript_formatted = partial_transcript
                     state_update = {"current_stage": MANUAL_INTERVIEW_STAGE,"interview_active": False,"manual_fallback_triggered": True,"partial_ai_transcript_formatted": partial_transcript}
                     utils.save_interview_state_to_firestore(username, state_update) # Calls Firestore save
                     st.session_state.current_stage = MANUAL_INTERVIEW_STAGE; st.rerun()
                 except Exception as e_fatal:
                     # ... (Error handling - calls Firestore save) ...
                     partial_transcript = utils.format_transcript_for_gsheet(st.session_state.messages)
                     st.session_state.partial_ai_transcript_formatted = partial_transcript
                     state_update = {"current_stage": MANUAL_INTERVIEW_STAGE,"interview_active": False,"manual_fallback_triggered": True,"partial_ai_transcript_formatted": partial_transcript}
                     utils.save_interview_state_to_firestore(username, state_update) # Calls Firestore save
                     st.session_state.current_stage = MANUAL_INTERVIEW_STAGE; st.rerun()
        except Exception as e:
             # ... (Outer error handling - calls Firestore save) ...
            partial_transcript = utils.format_transcript_for_gsheet(st.session_state.messages)
            st.session_state.partial_ai_transcript_formatted = partial_transcript
            state_update = {"current_stage": MANUAL_INTERVIEW_STAGE,"interview_active": False,"manual_fallback_triggered": True,"partial_ai_transcript_formatted": partial_transcript}
            utils.save_interview_state_to_firestore(username, state_update) # Calls Firestore save
            st.session_state.current_stage = MANUAL_INTERVIEW_STAGE; st.rerun()

# --- Section 1.5: Manual Interview Fallback Stage ---
elif st.session_state.get("current_stage") == MANUAL_INTERVIEW_STAGE:
    # ... (Fallback logic unchanged, ensure state save calls Firestore) ...
    if not questions_to_ask:
        # ... (Error handling - calls Firestore save) ...
        state_update = {"interview_active": False,"interview_completed_flag": True,"current_stage": SURVEY_STAGE,"manual_fallback_triggered": True,"manual_answers_formatted": st.session_state.manual_answers_formatted,"partial_ai_transcript_formatted": st.session_state.current_formatted_transcript_for_gsheet}
        utils.save_interview_state_to_firestore(username, state_update) # Calls Firestore save
        st.session_state.current_stage = SURVEY_STAGE; st.rerun(); st.stop()
    # ... (Form display unchanged) ...
    if manual_submitted:
        # ... (Format answers) ...
        state_update = {"interview_active": False,"interview_completed_flag": True,"current_stage": SURVEY_STAGE,"manual_fallback_triggered": True,"manual_answers_formatted": manual_formatted_answers,"partial_ai_transcript_formatted": st.session_state.current_formatted_transcript_for_gsheet}
        utils.save_interview_state_to_firestore(username, state_update) # Calls Firestore save
        st.session_state.current_stage = SURVEY_STAGE; st.rerun()

# --- Section 2: Survey Stage ---
elif st.session_state.get("current_stage") == SURVEY_STAGE:
    # ... (Title, info, transcript check unchanged) ...
    # ... (Form definition unchanged) ...
    if submitted:
        # ... (Validation unchanged) ...
        if not (age == "Select..." # ... rest of validation
               ):
            survey_responses = {# ... survey data ...
            }
            # --- Calls utils.save_survey_data which handles GSheet and Firestore backup/state update ---
            save_successful_gsheet = utils.save_survey_data(username, survey_responses)
            if save_successful_gsheet:
                st.session_state.survey_completed_flag = True
                st.session_state.current_stage = COMPLETED_STAGE
                # Stage update now happens inside utils.save_survey_data
                st.success("Survey submitted! Thank you."); st.balloons(); time.sleep(3); st.rerun()
            else:
                st.warning("Could not save to Google Sheets (backup should be saved). Try again or contact researcher.")
                # Check Firestore state to see if marked complete there
                if utils.check_if_survey_completed(username): # Checks Firestore now
                     st.info("Backup system indicates completion. Moving forward.")
                     st.session_state.survey_completed_flag = True
                     st.session_state.current_stage = COMPLETED_STAGE
                     # utils.save_interview_state_to_firestore(username, {"current_stage": COMPLETED_STAGE}) # Ensure stage updated if GSheet failed? Already done in save_survey_data
                     time.sleep(2); st.rerun()


# --- Section 3: Completed Stage (Unchanged) ---
elif st.session_state.get("current_stage") == COMPLETED_STAGE:
    # ... (Completion message logic unchanged) ...
    st.title("Thank You!")
    if st.session_state.get("survey_completed_flag", False):
        st.success("You have completed the interview and the survey. Your contribution is greatly appreciated!")
        st.markdown("You may now close this window.")
    else:
        st.warning("Completion status not confirmed.")
        st.markdown("If error, contact researcher.")


# --- Fallback / Initializing ---
else:
    st.spinner("Loading application state...")
    print(f"INFO: Fallback/Loading state. User: {username}, Stage: {st.session_state.get('current_stage')}, Initialized: {st.session_state.get('session_initialized')}")
    time.sleep(1.0)
    if username and st.session_state.get("session_initialized"):
        initialize_session_state_from_env(username) # Call correct init function
        determine_current_stage(username)
    st.rerun()