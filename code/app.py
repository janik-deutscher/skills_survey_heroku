# app.py
import streamlit as st
import time
import pandas as pd
import utils # Import your utils module
import os
import config
import json
import numpy as np
import uuid
import random # For GSheet throttle sleep
# Removed 're' import as it was only for manual questions map

# --- NEW Firestore Import ---
from google.cloud import firestore
# --- END NEW Firestore Import ---

# --- NEW Tenacity Imports ---
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
# --- END NEW Tenacity Imports ---

# --- <<< NEW Local Storage Import >>> ---
from streamlit_local_storage import LocalStorage
# --- <<< END NEW Local Storage Import >>> ---

# --- Constants ---
WELCOME_STAGE = "welcome"
INTERVIEW_STAGE = "interview"
# MANUAL_INTERVIEW_STAGE = "manual_interview" # REMOVED
SURVEY_STAGE = "survey"
COMPLETED_STAGE = "completed"

# --- API Setup & Retry Configuration ---
openai_client = None
anthropic_client = None
api = None
RETRYABLE_ERRORS = () # Default empty

if "gpt" in config.MODEL.lower():
    api = "openai"; from openai import OpenAI, RateLimitError, APITimeoutError, APIConnectionError, InternalServerError as OpenAIInternalServerError
    try: openai_client = OpenAI(api_key=st.secrets["API_KEY_OPENAI"], timeout=60.0)
    except KeyError: st.error("Error: OpenAI API key ('API_KEY_OPENAI') not found."); st.stop()
    except Exception as e: st.error(f"Error initializing OpenAI client: {e}"); st.stop()
    RETRYABLE_ERRORS = (RateLimitError, APITimeoutError, APIConnectionError, OpenAIInternalServerError)

elif "claude" in config.MODEL.lower():
    api = "anthropic"; import anthropic
    try: anthropic_client = anthropic.Anthropic(api_key=st.secrets["API_KEY_ANTHROPIC"], timeout=60.0)
    except KeyError: st.error("Error: Anthropic API key ('API_KEY_ANTHROPIC') not found."); st.stop()
    except Exception as e: st.error(f"Error initializing Anthropic client: {e}"); st.stop()
    RETRYABLE_ERRORS = (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError, anthropic.APITimeoutError)
else:
    st.error("Model name must contain 'gpt' or 'claude'."); st.stop()

api_retry_decorator = retry(
    stop=stop_after_attempt(3), # Retry up to 3 times (initial call + 2 retries)
    wait=wait_exponential(multiplier=1, min=2, max=10), # Wait 2s, 4s, 8s... up to 10s between retries
    retry=retry_if_exception_type(RETRYABLE_ERRORS),
    reraise=True # Reraise the exception if all retries fail
)
# --- End API Setup & Retry ---

# --- Manual Interview Questions Setup ---
# REMOVED - Manual question map parsing and related functions are no longer needed
# --- End Manual Interview Questions Setup ---


# --- Page Config ---
st.set_page_config(page_title="Skills & AI Interview", page_icon=config.AVATAR_INTERVIEWER)

# --- <<< Initialize Local Storage >>> ---
localS = LocalStorage()
# --- <<< End Initialize Local Storage >>> ---


# --- User Identification & Session State Initialization ---

# Initialize core states immediately if they don't exist
if "session_initialized" not in st.session_state: st.session_state.session_initialized = False
if "username" not in st.session_state: st.session_state.username = None

# --- <<< START REVISED USERNAME LOGIC (v3) >>> ---
# Try to get username from local storage FIRST
if st.session_state.username is None:
    storage_key = "skills_survey_username_uuid"
    username_from_storage = localS.getItem(storage_key)
    print(f"Raw value retrieved from local storage for key '{storage_key}': {username_from_storage}") # DEBUG Print raw value

    retrieved_username = None
    if isinstance(username_from_storage, dict) and 'value' in username_from_storage:
        retrieved_username = username_from_storage['value']
    elif isinstance(username_from_storage, str):
        retrieved_username = username_from_storage

    if retrieved_username:
        print(f"Found username string in local storage: {retrieved_username}")
        st.session_state.username = retrieved_username
    else:
        if username_from_storage is not None:
             print(f"Value found in local storage ({username_from_storage}) is not a valid username string or is null/empty. Generating new.")
        else:
             print("No username key/value found in local storage. Generating new one.")

        new_username = f"user_{uuid.uuid4()}"
        st.session_state.username = new_username
        localS.setItem(storage_key, new_username)
        print(f"INFO: Generated new user UUID and saved to local storage: {new_username}")
        st.rerun()

username = st.session_state.username
# --- <<< END REVISED USERNAME LOGIC >>> ---


# --- Directory Creation (for local backups - wrapped) ---
if username:
    try:
        os.makedirs(config.TRANSCRIPTS_DIRECTORY, exist_ok=True); os.makedirs(config.TIMES_DIRECTORY, exist_ok=True)
        os.makedirs(config.BACKUPS_DIRECTORY, exist_ok=True); os.makedirs(config.SURVEY_DIRECTORY, exist_ok=True)
    except OSError as e: print(f"Warning: Failed to create local data directories: {e}.")

# --- Initialize Session State Function (Corrected Version - No Manual Fallback State) ---
def initialize_session_state_with_firestore(user_id):
    if st.session_state.get("session_initialized", False): return
    print(f"Attempting to initialize session for user: {user_id}")
    default_values = {
        "messages": [], "current_stage": WELCOME_STAGE, "consent_given": False,
        "start_time": None, "start_time_file_names": None, "interview_active": False,
        "interview_completed_flag": False, "survey_completed_flag": False, "welcome_shown": False
    }
    for key, default_value in default_values.items():
        if key not in st.session_state: st.session_state[key] = default_value
    print("Initialized session state with default values.")

    loaded_state, loaded_messages = utils.load_interview_state_from_firestore(user_id)
    st.session_state.messages = loaded_messages

    if api == "openai":
        if not st.session_state.messages or st.session_state.messages[0].get("role") != "system":
            print("System prompt missing after loading messages for OpenAI. Re-injecting.")
            sys_prompt_dict = {"role": "system", "content": config.SYSTEM_PROMPT}
            st.session_state.messages.insert(0, sys_prompt_dict)

    if loaded_state:
        print(f"Overwriting defaults with state loaded from Firestore for user: {user_id}")
        st.session_state.current_stage = loaded_state.get("current_stage", st.session_state.current_stage)
        st.session_state.consent_given = loaded_state.get("consent_given", st.session_state.consent_given)
        st.session_state.interview_active = loaded_state.get("interview_active", st.session_state.interview_active)
        st.session_state.interview_completed_flag = loaded_state.get("interview_completed_flag", st.session_state.interview_completed_flag)
        st.session_state.survey_completed_flag = loaded_state.get("survey_completed_flag", st.session_state.survey_completed_flag)
        st.session_state.welcome_shown = loaded_state.get("welcome_shown", st.session_state.welcome_shown)

        start_time_unix = loaded_state.get("start_time_unix", None)
        if start_time_unix:
             try:
                 loaded_start_time = float(start_time_unix); st.session_state.start_time = loaded_start_time
                 st.session_state.start_time_file_names = time.strftime("%Y%m%d_%H%M%S", time.localtime(loaded_start_time))
             except (TypeError, ValueError): print(f"Warning: Could not parse loaded start_time_unix: {start_time_unix}. Keeping default.")

    elif not loaded_messages:
        print(f"No previous state or messages found for {user_id}. Initializing fresh session.")
        if 'messages' not in st.session_state or not isinstance(st.session_state.messages, list): st.session_state.messages = []

    st.session_state.session_initialized = True
    print(f"Session initialized. Stage: {st.session_state.get('current_stage')}, Msgs: {len(st.session_state.get('messages', []))}, StartTime: {st.session_state.get('start_time')}")

# --- Function to Determine Current Stage (No Manual Fallback) ---
def determine_current_stage(user_id):
    current_stage_in_state = st.session_state.get("current_stage")
    new_stage = current_stage_in_state

    survey_done = st.session_state.get("survey_completed_flag", False)
    interview_done = st.session_state.get("interview_completed_flag", False)
    welcome_done = st.session_state.get("welcome_shown", False)

    if survey_done:
        new_stage = COMPLETED_STAGE
    elif interview_done:
        new_stage = SURVEY_STAGE
    elif welcome_done:
        new_stage = INTERVIEW_STAGE
    else:
        new_stage = WELCOME_STAGE

    if new_stage != current_stage_in_state:
         print(f"Stage re-determined: {current_stage_in_state} -> {new_stage}")
         st.session_state.current_stage = new_stage
         utils.save_interview_state_to_firestore(username, {"current_stage": new_stage})


# --- Initialize Session State ---
if username is None:
    st.error("Username could not be determined. Please refresh.")
    st.stop()

if not st.session_state.get("session_initialized", False):
    initialize_session_state_with_firestore(username)
    determine_current_stage(username)
    st.rerun() # Rerun once after initialization and stage determination


# --- === Main Application Logic === ---

if not st.session_state.get("session_initialized", False):
    st.spinner("Initializing session...")
    st.stop() # Prevent rendering further until initialized


# --- Section 0: Welcome Stage ---
if st.session_state.get("current_stage") == WELCOME_STAGE:
    st.title("Welcome")
    st.markdown(f"""
    Hi there, thanks for your interest in this research project!

    My name is Janik, and I'm a PhD Candidate at UPF. For my research, I'm exploring how university students like you think about valuable skills for the future, the role of Artificial Intelligence (AI), and how these views connect to educational choices.

    To understand your perspective, this study involves an **interview conducted by an AI assistant** followed by a short survey.

    Before we begin, please carefully read the **Information Sheet & Consent Form** below.
    """)
    st.markdown("---")
    st.subheader("Information Sheet & Consent Form")
    # --- Consent Form Content (UPDATED NIS explanation) ---
    st.markdown(f"""
**Study Title:** Student Perspectives on Skills, Careers, and Artificial Intelligence \n
**Researcher:** Janik Deutscher (janik.deutscher@upf.edu), PhD Candidate, Universitat Pompeu Fabra

**Please read the following information carefully before deciding to participate:**

**1. Purpose of the Research:**
*   This study is part of a PhD research project aiming to understand how university students perceive valuable skills for their future careers, how the rise of Artificial Intelligence (AI) might influence these views, and how this connects to educational choices, interests, and overall university experience.

**2. What Participation Involves:**
*   If you agree to participate, you will engage in:
    *   An interview conducted via text chat with an **AI assistant**. The AI will ask you open-ended questions about your career aspirations, skill perceptions, educational choices, and views on AI. Should the AI encounter persistent technical issues, the interview may not be able to be completed within this application.
    *   A **short survey** following the interview with some additional questions, including demographics, university experience, and AI usage.
*   The estimated total time commitment is approximately **30-40 minutes**.

**3. Privacy, Anonymity, API Usage, and Logging:**
*   Your privacy is protected. No directly identifiable information (like your name, email, or address) will be collected. Your session is identified only by the anonymized User ID: `{username}`. The optional Student Number (NIS) collected in the final survey will be stored securely and handled according to strict UPF data protection regulations. It will not be shared outside the research context defined by UPF protocols.
*   **AI Interview Data Handling:** To enable the AI assistant to converse with you, your typed responses during the interview will be sent via a secure Application Programming Interface (API) to the AI service provider (OpenAI or Anthropic, depending on the model used). This is done solely to generate the AI's replies in real-time.
*   **Data Use by AI Provider:** Based on the current policies of major AI providers like OpenAI and Anthropic for API usage, data submitted through the API is **not used to train their AI models**.
*   **Research Data:** The research team receives the final survey answers (including the optional NIS and other survey responses) and interview transcript via secure methods (e.g., Google Sheets with restricted access). During data analysis, this data will be linked to your **anonymized User ID** (`{username}`), not to any other identifier.
*   **Anonymization:** Any potentially identifying details mentioned during the interview (e.g., specific names, unique places) will be **removed or pseudonymized** in the final transcripts used for analysis or publication. The NIS, if provided, will be handled according to strict data protection protocols.
*   **Persistent Logging (Firestore):** To prevent data loss due to technical issues (e.g., browser crash, network disconnect), your anonymized chat messages are saved to a secure cloud database (Google Cloud Firestore) hosted by Google after each turn. This data is linked only to your anonymized User ID (`{username}`) and is used primarily for data recovery and secondarily for analysis if final submission fails. Key application state (like consent status and current progress stage) is also saved here to allow resuming sessions.

**4. Data Storage and Use:**
*   Anonymized research data (final GSheet entries, potentially anonymized Firestore logs, and NIS handled separately under strict protocols) will be stored securely on UPF servers or secure cloud platforms (GCP).
*   Data will be kept for the duration of the PhD project and up to two years after its finalization for scientific validation, according to UPF regulations.
*   Anonymized data may be reused for other related research projects or archived/published in a public repository in the future.

**5. Voluntary Participation and Withdrawal:**
*   Your participation is entirely **voluntary**. You may choose not to provide your Student Number (NIS).
*   You can **stop the interview at any time** without penalty by using the "Quit" button or simply closing the window. Due to the persistent logging, data up to the point you stopped may still be retained linked to your User ID.
*   You may choose **not to answer any specific question** in the survey.
*   If you have concerns after participation, you can contact Janik Deutscher (janik.deutscher@upf.edu) with your User ID (`{username}`). Data removal might be complex once anonymized and aggregated.

**6. Risks and Benefits:**
*   Participating in this study involves risks **no greater than those encountered in everyday life** (e.g., reflecting on your opinions). Providing your NIS carries the standard risks associated with sharing such identifiers, mitigated by strict data security protocols.
*   There are **no direct benefits** guaranteed to you from participating, although your responses will contribute valuable insights to research on education and career preparation.

**7. Contact Information:**
*   If you have questions about this study, please contact the researcher, **Janik Deutscher (janik.deutscher@upf.edu)**.
*   If you have concerns about this study or your rights as a participant, you may contact **UPF’s Institutional Committee for the Ethical Review of Projects (CIREP)** by phone (+34 935 422 186) or email (secretaria.cirep@upf.edu). CIREP is independent of the research team and treats inquiries confidentially.

**8. GDPR Information (Data Protection):**
*   In accordance with the General Data Protection Regulation (GDPR) 2016/679 (EU), we provide the following:
    *   **Data Controller:** Universitat Pompeu Fabra. Pl. de la Mercè, 10-12. 08002 Barcelona. Tel. +34 935 422 000.
    *   **Data Protection Officer (DPO):** Contact via email at dpd@upf.edu.
    *   **Purposes of Processing:** Carrying out the research project described above. Anonymized research data will be kept as described in section 4. The optional NIS will be processed according to UPF data protection protocols. The temporary processing of interview data by the AI provider via API is described in section 3. Persistent logging to Firestore for data integrity and session resumption is described in section 3.
    *   **Legal Basis:** Your explicit consent. You can withdraw consent at any time (though data withdrawal post-submission may be limited as explained above). The processing of NIS is based on your explicit consent to provide it.
    *   **Your Rights:** You have the right to access your data; request rectification, deletion, or portability (in certain cases); object to processing; or request limitation. Procedures are described at www.upf.edu/web/proteccio-dades/drets. Contact the DPO (dpd@upf.edu) for queries. If unsatisfied, you may contact the Catalan Data Protection Authority (apdcat.gencat.cat).
    """)
    # --- End Consent Form Content ---
    consent = st.checkbox("I confirm that I have read and understood the information sheet above, including the information about how the AI interview works, data logging, and the optional collection of the Student Number (NIS). I am 18 years or older, and I voluntarily consent to participate in this study.", key="consent_checkbox", value=st.session_state.get("consent_given", False))
    if consent != st.session_state.get("consent_given", False):
        st.session_state.consent_given = consent
        utils.save_interview_state_to_firestore(username, {'consent_given': consent})
    if st.button("Start Interview", key="start_interview_btn", disabled=not st.session_state.get("consent_given", False)):
        if st.session_state.get("consent_given", False):
            st.session_state.welcome_shown = True; st.session_state.current_stage = INTERVIEW_STAGE
            utils.save_interview_state_to_firestore(username, {'welcome_shown': True, 'current_stage': INTERVIEW_STAGE})
            print("Moving to Interview Stage from Welcome"); st.rerun()

# --- Section 1: Interview Stage ---
elif st.session_state.get("current_stage") == INTERVIEW_STAGE:
    st.title("Part 1: Interview")
    # --- Start Time & Active Logic (No Changes) ---
    if st.session_state.start_time is None and "start_time_unix" not in st.session_state.get("loaded_state", {}):
        st.session_state.start_time = time.time(); st.session_state.start_time_file_names = time.strftime("%Y%m%d_%H%M%S", time.localtime(st.session_state.start_time))
        utils.save_interview_state_to_firestore(username, {"start_time_unix": st.session_state.start_time}); print("Start time initialized and saved.")
    if not st.session_state.get("interview_active", False):
         st.session_state.interview_active = True; utils.save_interview_state_to_firestore(username, {"interview_active": True}); print("Interview marked as active.")
    # --- Quit Button Logic (No Changes) ---
    st.info("Please answer the interviewer's questions.")
    if st.button("Quit Interview Early", key="quit_interview"):
        st.session_state.interview_active = False; st.session_state.interview_completed_flag = True
        quit_message = "You have chosen to end the interview early. Proceeding to the final questions."; quit_msg_dict = {"role": "assistant", "content": quit_message}
        st.session_state.messages.append(quit_msg_dict); utils.save_message_to_firestore(username, quit_msg_dict)
        utils.save_interview_data(username=username, transcripts_directory=config.TRANSCRIPTS_DIRECTORY, times_directory=config.TIMES_DIRECTORY, is_final_save=True, messages_to_format=st.session_state.messages)
        utils.save_interview_state_to_firestore(username, {"interview_active": False, "interview_completed_flag": True, "current_stage": SURVEY_STAGE})
        st.warning(quit_message); st.session_state.current_stage = SURVEY_STAGE; print("Moving to Survey Stage after Quit."); time.sleep(1); st.rerun()

    # --- Display Chat History (No Changes) ---
    for message in st.session_state.get("messages", []):
        if message.get('role') == "system": continue;
        if message.get('content', '') in config.CLOSING_MESSAGES.keys(): continue
        is_closing_message_display = any(message.get('content', '') == display_text for code, display_text in config.CLOSING_MESSAGES.items())
        if is_closing_message_display: continue
        avatar = config.AVATAR_INTERVIEWER if message.get('role') == "assistant" else config.AVATAR_RESPONDENT
        with st.chat_message(message.get('role', 'unknown'), avatar=avatar): st.markdown(message.get('content', ''))

    # --- Initial Assistant Message Logic (No Manual Fallback) ---
    if not st.session_state.get("messages", []) or \
       (api == "openai" and len(st.session_state.get("messages", [])) == 1 and st.session_state.get("messages", [])[0].get("role") == "system"):
        print("No previous assistant/user messages found, attempting to get initial message.")
        try:
            if api == "openai":
                 if not st.session_state.messages or st.session_state.messages[0].get("role") != "system":
                     sys_prompt_dict = {"role": "system", "content": config.SYSTEM_PROMPT}
                     st.session_state.messages.insert(0, sys_prompt_dict)
                     utils.save_interview_state_to_firestore(username, {})

            with st.chat_message("assistant", avatar=config.AVATAR_INTERVIEWER):
                message_placeholder = st.empty(); message_placeholder.markdown("Thinking...")
                api_messages = []; message_interviewer = ""
                if api == "openai":
                    if st.session_state.messages and st.session_state.messages[0].get("role") == 'system':
                         api_messages = [st.session_state.messages[0]]
                elif api == "anthropic":
                    api_messages = [{"role": "user", "content": "Please begin the interview."}]

                api_kwargs = { "model": config.MODEL, "messages": api_messages, "max_tokens": config.MAX_OUTPUT_TOKENS, "stream": False }
                if api == "anthropic": api_kwargs["system"] = config.SYSTEM_PROMPT
                if config.TEMPERATURE is not None: api_kwargs["temperature"] = config.TEMPERATURE

                try:
                    print("Attempting initial API call with retry...")
                    @api_retry_decorator
                    def get_initial_completion():
                        if api == "openai": return openai_client.chat.completions.create(**api_kwargs)
                        elif api == "anthropic": return anthropic_client.messages.create(**api_kwargs)
                        return None
                    response = get_initial_completion()
                    if api == "openai": message_interviewer = response.choices[0].message.content
                    elif api == "anthropic": message_interviewer = response.content[0].text
                    print("Initial API call success after retry logic.")
                    message_placeholder.markdown(message_interviewer)

                except RETRYABLE_ERRORS as e_retry:
                     print(f"Initial API call failed after retries: {e_retry}")
                     message_placeholder.error(f"Error connecting to the AI assistant after multiple attempts: {e_retry}. Your progress is saved. Please try refreshing the page in a few moments. If the problem persists, contact the researcher.")
                     utils.save_interview_data(username=username, transcripts_directory=config.TRANSCRIPTS_DIRECTORY, times_directory=config.TIMES_DIRECTORY, is_final_save=False, messages_to_format=st.session_state.messages)
                     st.stop()
                except Exception as e_fatal:
                     print(f"Non-retryable initial API error: {e_fatal}")
                     message_placeholder.error(f"An unexpected error occurred connecting to the AI assistant: {e_fatal}. Your progress is saved. Please try refreshing the page. If the problem persists, contact the researcher.")
                     utils.save_interview_data(username=username, transcripts_directory=config.TRANSCRIPTS_DIRECTORY, times_directory=config.TIMES_DIRECTORY, is_final_save=False, messages_to_format=st.session_state.messages)
                     st.stop()

            assistant_msg_dict = {"role": "assistant", "content": message_interviewer.strip()}
            st.session_state.messages.append(assistant_msg_dict)
            utils.save_message_to_firestore(username, assistant_msg_dict)
            print("Initial message obtained and saved."); time.sleep(0.1); st.rerun()

        except Exception as e:
            if 'message_placeholder' in locals(): message_placeholder.empty()
            st.error(f"Failed during initial message setup: {e}. Please refresh and try again.");
            st.stop()

    # --- Chat Input & Response Logic (No Manual Fallback) ---
    if prompt := st.chat_input("Your response..."):
        user_msg_dict = {"role": "user", "content": prompt}
        st.session_state.messages.append(user_msg_dict); utils.save_message_to_firestore(username, user_msg_dict)
        with st.chat_message("user", avatar=config.AVATAR_RESPONDENT): st.markdown(prompt)

        try:
            with st.chat_message("assistant", avatar=config.AVATAR_INTERVIEWER):
                 message_placeholder = st.empty(); message_placeholder.markdown("Thinking...")
                 message_interviewer = ""; full_response_content = ""; stream_closed = False; detected_code = None

                 if api == "openai": api_messages_for_call = st.session_state.messages
                 elif api == "anthropic": api_messages_for_call = [m for m in st.session_state.messages if m.get("role") != "system"]

                 api_kwargs = { "model": config.MODEL, "messages": api_messages_for_call, "max_tokens": config.MAX_OUTPUT_TOKENS, "stream": True }
                 if api == "anthropic": api_kwargs["system"] = config.SYSTEM_PROMPT
                 if config.TEMPERATURE is not None: api_kwargs["temperature"] = config.TEMPERATURE

                 try:
                    if api == "openai":
                        stream = openai_client.chat.completions.create(**api_kwargs)
                        for chunk in stream:
                             if chunk.choices and len(chunk.choices) > 0:
                                 delta = chunk.choices[0].delta
                                 if delta and delta.content:
                                     text_delta = delta.content; full_response_content += text_delta; current_content_stripped = full_response_content.strip()
                                     for code in config.CLOSING_MESSAGES.keys():
                                         if code == current_content_stripped:
                                             detected_code = code
                                             message_interviewer = full_response_content.replace(code, "").strip(); stream_closed = True; break
                                     if stream_closed: break
                                     message_interviewer = full_response_content; message_placeholder.markdown(message_interviewer + "▌")
                        if not stream_closed: message_placeholder.markdown(message_interviewer)

                    elif api == "anthropic":
                         with anthropic_client.messages.stream(**api_kwargs) as stream:
                            for text_delta in stream.text_stream:
                                 if text_delta is not None:
                                     full_response_content += text_delta; current_content_stripped = full_response_content.strip()
                                     for code in config.CLOSING_MESSAGES.keys():
                                         if code == current_content_stripped:
                                             detected_code = code
                                             message_interviewer = full_response_content.replace(code,"").strip(); stream_closed = True; break
                                     if stream_closed: break
                                     message_interviewer = full_response_content; message_placeholder.markdown(message_interviewer + "▌")
                         if not stream_closed: message_placeholder.markdown(message_interviewer)

                    assistant_msg_content = full_response_content.strip()
                    assistant_msg_dict = {"role": "assistant", "content": assistant_msg_content}

                    if not detected_code or message_interviewer:
                        if not st.session_state.messages or st.session_state.messages[-1] != assistant_msg_dict:
                           st.session_state.messages.append(assistant_msg_dict)
                           utils.save_message_to_firestore(username, assistant_msg_dict)

                    if detected_code:
                        st.session_state.interview_active = False; st.session_state.interview_completed_flag = True
                        closing_message_display = config.CLOSING_MESSAGES[detected_code]

                        if message_interviewer: message_placeholder.markdown(message_interviewer)
                        else: message_placeholder.empty()

                        utils.save_interview_data(username=username, transcripts_directory=config.TRANSCRIPTS_DIRECTORY, times_directory=config.TIMES_DIRECTORY, is_final_save=True, messages_to_format=st.session_state.messages)
                        utils.save_interview_state_to_firestore(username, {"interview_active": False, "interview_completed_flag": True, "current_stage": SURVEY_STAGE})
                        if closing_message_display: st.success(closing_message_display)
                        st.session_state.current_stage = SURVEY_STAGE
                        print("Moving to Survey Stage after code detection."); time.sleep(2); st.rerun()

                 except RETRYABLE_ERRORS as e_retry:
                     print(f"API call failed during chat stream after retries: {e_retry}")
                     message_placeholder.error(f"Connection to the AI assistant failed: {e_retry}. Your progress is saved. Please try refreshing the page in a few moments. If the problem persists, contact the researcher.")
                     utils.save_interview_data(username=username, transcripts_directory=config.TRANSCRIPTS_DIRECTORY, times_directory=config.TIMES_DIRECTORY, is_final_save=False, messages_to_format=st.session_state.messages)
                     st.stop()
                 except Exception as e_fatal:
                     print(f"Unhandled API error during chat stream: {e_fatal}")
                     message_placeholder.error(f"An unexpected error occurred: {e_fatal}. Your progress is saved. Please try refreshing the page. If the problem persists, contact the researcher.")
                     utils.save_interview_data(username=username, transcripts_directory=config.TRANSCRIPTS_DIRECTORY, times_directory=config.TIMES_DIRECTORY, is_final_save=False, messages_to_format=st.session_state.messages)
                     st.stop()

        except Exception as e:
            if 'message_placeholder' in locals(): message_placeholder.empty()
            st.error(f"An error occurred processing the chat response: {e}. Your progress is saved. Please try refreshing the page. If the problem persists, contact the researcher.")
            utils.save_interview_data(username=username, transcripts_directory=config.TRANSCRIPTS_DIRECTORY, times_directory=config.TIMES_DIRECTORY, is_final_save=False, messages_to_format=st.session_state.messages)
            st.stop()


# --- Section 1.5: Manual Interview Fallback Stage ---
# REMOVED ENTIRELY


# --- Section 2: Survey Stage ---
elif st.session_state.get("current_stage") == SURVEY_STAGE:
    st.title("Part 2: Survey")
    st.info(f"Thank you, please answer a few final questions.")

    # --- Transcript Check Logic ---
    if "current_formatted_transcript_for_gsheet" not in st.session_state:
         print("WARNING: Formatted transcript key missing at survey stage entry. Attempting generation.")
         utils.save_interview_data(username=username, transcripts_directory=config.TRANSCRIPTS_DIRECTORY, times_directory=config.TIMES_DIRECTORY, is_final_save=True, messages_to_format=st.session_state.get("messages", []))
         if "current_formatted_transcript_for_gsheet" not in st.session_state:
              st.error("Error: Could not generate the interview transcript for saving.")
              st.session_state.current_formatted_transcript_for_gsheet = "ERROR: Transcript generation failed before survey."
              print("CRITICAL ERROR: Transcript generation failed before survey.")

    # --- Survey Options ---
    age_options = ["Select...", "Under 18"] + [str(i) for i in range(18, 36)] + ["Older than 35"]
    gender_options = ["Select...", "Male", "Female", "Non-binary", "Prefer not to say"]
    major_options = [
        "Select...",
        "Business Management and Administration",
        "Economics",
        "Business Sciences - Management",
        "International Business Economics",
        "Double Degree in Law-ECO/ADE",
        "Industrial Technologies and Economic Analysis",
        "Other"
    ]
    year_options = [
        "Select...",
        "First Year",
        "Second Year",
        "Third Year",
        "Fourth Year",
        "Fifth Year",
        "Other/Not Applicable"
    ]
    gpa_values = np.round(np.arange(5.0, 10.01, 0.1), 1)
    gpa_options = ["Select...", "Below 5.0"] + [f"{gpa:.1f}" for gpa in gpa_values] + ["Prefer not to say / Not applicable"]

    with st.form("survey_form"):
        st.subheader("Demographic Information")
        age = st.selectbox("What is your age?", age_options, key="age")
        gender = st.selectbox("What is your gender?", gender_options, key="gender")
        major = st.selectbox("What is your main field of study (or double degree)?", major_options, key="major")
        year_of_study = st.selectbox("What year of study are you currently in?", year_options, key="year")
        gpa = st.selectbox("What is your approximate GPA or academic average (on a scale of 10)?", gpa_options, key="gpa")

        # --- UPDATED NIS Field Help Text ---
        student_nis_input = st.text_input("Student number (NIS)", key="student_nis", help="Providing your NIS is optional.")

        st.subheader("University Experience")
        learning_enjoyment_value = st.slider(
            "From 0 to 100, how much do you enjoy learning just for the sake of it?",
            min_value=0,
            max_value=100,
            value=50,
            key="learning_enjoyment_slider",
            help="0 = Not at all, 100 = Very much"
        )
        university_enjoyment_value = st.slider(
            "From 0 to 100, how much are you enjoying your experience at university?",
            min_value=0,
            max_value=100,
            value=50,
            key="university_enjoyment_slider",
            help="Considering everything (academics, social life, etc.). 0 = Not at all, 100 = Very much"
        )

        st.subheader("AI Usage")
        ai_usage_percentage_value = st.slider(
            "How much are you using AI for your university work?",
            min_value=0,
            max_value=100,
            value=50,
            key="ai_usage_slider",
            help="Estimate the percentage of your university tasks where you utilize AI tools. 0 = Not at all, 100 = For almost all tasks."
        )
        ai_model = st.text_input("Which AI model are you mostly using?", key="ai_model")

        submitted = st.form_submit_button("Submit Survey Responses")

    if submitted:
        # Validation
        if (age == "Select..." or gender == "Select..." or major == "Select..." or year_of_study == "Select..." or gpa == "Select..."):
            st.warning("Please answer all dropdown questions.")
        else:
            # --- Capture all responses ---
            survey_responses = {
                "age": age,
                "gender": gender,
                "major": major,
                "year": year_of_study,
                "gpa": gpa,
                "student_nis": student_nis_input.strip(),
                "learning_enjoyment": learning_enjoyment_value,
                "university_enjoyment": university_enjoyment_value,
                "ai_usage_percentage": ai_usage_percentage_value,
                "ai_model": ai_model
            }
            # --- Pass to saving functions ---
            save_successful = utils.save_survey_data(username, survey_responses)

            if save_successful:
                st.session_state.survey_completed_flag = True; st.session_state.current_stage = COMPLETED_STAGE
                utils.save_interview_state_to_firestore(username, {"current_stage": COMPLETED_STAGE, "survey_completed_flag": True})
                st.success("Survey submitted! Thank you."); st.balloons(); time.sleep(3); st.rerun()
            else:
                st.warning("Could not save survey results to primary storage (Google Sheets). Your responses may have been saved to our backup system. Please contact the researcher.")


# --- Section 3: Completed Stage ---
elif st.session_state.get("current_stage") == COMPLETED_STAGE:
    st.title("Thank You!")
    if st.session_state.get("survey_completed_flag", False):
        st.success("You have completed the interview and the survey. Your contribution is greatly appreciated!")
        st.markdown("You may now close this window.")
    else:
        st.warning("Navigated to completion page, but survey completion status is not confirmed in the session.")
        st.markdown("If you believe this is an error, please contact the researcher.")


# --- Fallback / Initializing ---
else:
    st.spinner("Loading application state...")
    print(f"Info: Fallback/Loading state. User: {username}, Stage: {st.session_state.get('current_stage')}, Initialized: {st.session_state.get('session_initialized')}")
    time.sleep(1.0)
    if username and st.session_state.get("session_initialized"):
        determine_current_stage(username)
    st.rerun()