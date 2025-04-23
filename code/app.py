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
import re # For parsing outline

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
MANUAL_INTERVIEW_STAGE = "manual_interview" # New stage for fallback
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
# Extract main questions from the outline
outline_parts = config.INTERVIEW_OUTLINE.split("**Part ")
manual_questions_map = {}
part_keys = ["Intro", "I", "II", "III", "IV"] # Match split parts
current_part_index = 0
intro_match = re.search(r"\*\*Begin the interview with:\*\*\s*'(.*?)'", config.INTERVIEW_OUTLINE, re.DOTALL)
if intro_match: manual_questions_map["Intro"] = [{"key": "intro_q", "text": intro_match.group(1).strip()}]
else: manual_questions_map["Intro"] = []
framing_match = re.search(r"\*\*Ask Next \(Framing Q\):\*\*\s*'(.*?)'", config.INTERVIEW_OUTLINE, re.DOTALL)
if framing_match: manual_questions_map["Framing"] = [{"key": "framing_q", "text": framing_match.group(1).strip()}]
else: manual_questions_map["Framing"] = []
for i, part_text in enumerate(outline_parts[1:]): # Skip text before Part I
    part_key = f"Part{part_keys[i+1]}"; manual_questions_map[part_key] = []
    ask_matches = re.findall(r"\*\*Ask\s*(?:\(.*?Q\))?\s*:\*\*\s*'(.*?)'", part_text, re.DOTALL)
    for q_idx, q_text in enumerate(ask_matches):
        manual_questions_map[part_key].append({"key": f"{part_key}_q{q_idx+1}", "text": q_text.strip()})
manual_questions_map["Summary"] = [{"key": "summary_prompt", "text": "Based on our discussion (including any AI parts and your manual answers), could you briefly summarize your key perspectives on skills and AI's impact on them?"}]

# Function to find the last completed part based on AI messages
def find_last_ai_part_completed(messages):
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
part_key_sequence = ["Intro", "Framing", "PartI", "PartII", "PartIII", "PartIV", "Summary"]


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
    # The component might need a dummy UI element to trigger JS on first load
    # Add a hidden button or small text element if retrieval seems unreliable
    # st.markdown("<span id='local_storage_trigger'></span>", unsafe_allow_html=True) # Example trigger

    username_from_storage = localS.getItem(storage_key)
    print(f"Raw value retrieved from local storage for key '{storage_key}': {username_from_storage}") # DEBUG Print raw value

    # Check if retrieved value is directly the username string (and not None or empty)
    retrieved_username = None
    if isinstance(username_from_storage, dict) and 'value' in username_from_storage:
        retrieved_username = username_from_storage['value']
    elif isinstance(username_from_storage, str): # Handle case where it might store directly as string
        retrieved_username = username_from_storage

    if retrieved_username: # Check if we got a non-empty string
        print(f"Found username string in local storage: {retrieved_username}")
        st.session_state.username = retrieved_username
    else:
        # Handle cases where it might be None, empty string, or maybe {}
        if username_from_storage is not None:
             print(f"Value found in local storage ({username_from_storage}) is not a valid username string or is null/empty. Generating new.")
        else:
             print("No username key/value found in local storage. Generating new one.")

        # Generate a new UUID
        new_username = f"user_{uuid.uuid4()}"
        st.session_state.username = new_username
        # Store the *new* username DIRECTLY as a string
        localS.setItem(storage_key, new_username)
        print(f"INFO: Generated new user UUID and saved to local storage: {new_username}")
        # Force a rerun immediately after setting the username for the first time
        st.rerun()

username = st.session_state.username # Assign the established username
# --- <<< END REVISED USERNAME LOGIC >>> ---


# --- Directory Creation (for local backups - wrapped) ---
if username:
    try:
        os.makedirs(config.TRANSCRIPTS_DIRECTORY, exist_ok=True); os.makedirs(config.TIMES_DIRECTORY, exist_ok=True)
        os.makedirs(config.BACKUPS_DIRECTORY, exist_ok=True); os.makedirs(config.SURVEY_DIRECTORY, exist_ok=True)
    except OSError as e: print(f"Warning: Failed to create local data directories: {e}.")

# --- Initialize Session State Function (Corrected Version) ---
def initialize_session_state_with_firestore(user_id):
    if st.session_state.get("session_initialized", False): return
    print(f"Attempting to initialize session for user: {user_id}")
    default_values = { "messages": [], "current_stage": WELCOME_STAGE, "consent_given": False, "start_time": None, "start_time_file_names": None, "interview_active": False, "interview_completed_flag": False, "survey_completed_flag": False, "welcome_shown": False, "partial_ai_transcript_formatted": "", "manual_answers_formatted": "" }
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
        st.session_state.partial_ai_transcript_formatted = loaded_state.get("partial_ai_transcript_formatted", "")
        st.session_state.manual_answers_formatted = loaded_state.get("manual_answers_formatted", "")
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

# --- Function to Determine Current Stage ---
def determine_current_stage(user_id):
    current_stage_in_state = st.session_state.get("current_stage"); new_stage = current_stage_in_state
    survey_done = st.session_state.get("survey_completed_flag", False); interview_done = st.session_state.get("interview_completed_flag", False)
    welcome_done = st.session_state.get("welcome_shown", False); manual_fallback = (current_stage_in_state == MANUAL_INTERVIEW_STAGE)
    if survey_done: new_stage = COMPLETED_STAGE
    elif manual_fallback: new_stage = MANUAL_INTERVIEW_STAGE
    elif interview_done: new_stage = SURVEY_STAGE
    elif welcome_done: new_stage = INTERVIEW_STAGE
    else: new_stage = WELCOME_STAGE
    if new_stage != current_stage_in_state:
         print(f"Stage re-determined: {current_stage_in_state} -> {new_stage}")
         st.session_state.current_stage = new_stage

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
    st.markdown(f"""
**Study Title:** Student Perspectives on Skills, Careers, and Artificial Intelligence \n
**Researcher:** Janik Deutscher (janik.deutscher@upf.edu), PhD Candidate, Universitat Pompeu Fabra

**Please read the following information carefully before deciding to participate:**

**1. Purpose of the Research:**
*   This study is part of a PhD research project aiming to understand how university students perceive valuable skills for their future careers, how the rise of Artificial Intelligence (AI) might influence these views, and how this connects to educational choices.

**2. What Participation Involves:**
*   If you agree to participate, you will engage in:
    *   An interview conducted via text chat with an **AI assistant**. The AI will ask you open-ended questions about your career aspirations, skill perceptions, educational choices, and views on AI. Should the AI encounter a persistent technical issue, you may be asked to complete the remaining questions manually.
    *   A **short survey** following the interview with some additional questions.
*   The estimated total time commitment is approximately **30-40 minutes**.

**3. Privacy, Anonymity, API Usage, and Logging:**
*   Your privacy is protected. No directly identifiable information (like your name, email, or address) will be collected. Your session is identified only by the anonymized User ID: `{username}`.
*   **AI Interview Data Handling:** To enable the AI assistant to converse with you, your typed responses during the interview will be sent via a secure Application Programming Interface (API) to the AI service provider (OpenAI, for the GPT model used in this study). This is done solely to generate the AI's replies in real-time.
*   **Data Use by AI Provider:** Based on the current policies of major AI providers like OpenAI for API usage, data submitted through the API is **not used to train their AI models**.
*   **Research Data:** The research team receives the final survey answers and interview transcript (potentially including both AI and manually answered portions) via secure methods (e.g., Google Sheets with restricted access). During data analysis, this data will be linked to your **anonymized User ID** (`{username}`), not to any other identifier.
*   **Anonymization:** Any potentially identifying details mentioned during the interview (e.g., specific names, unique places) will be **removed or pseudonymized** in the final transcripts used for analysis or publication.
*   **Persistent Logging (Firestore):** To prevent data loss due to technical issues (e.g., browser crash, network disconnect), your anonymized chat messages are saved to a secure cloud database (Google Cloud Firestore) hosted by Google after each turn. This data is linked only to your anonymized User ID (`{username}`) and is used primarily for data recovery and secondarily for analysis if final submission fails. Key application state (like consent status and current progress stage) is also saved here to allow resuming sessions.

**4. Data Storage and Use:**
*   Anonymized research data (final GSheet entries, potentially anonymized Firestore logs) will be stored securely on UPF servers or secure cloud platforms (GCP).
*   Data will be kept for the duration of the PhD project and up to two years after its finalization for scientific validation, according to UPF regulations.
*   Anonymized data may be reused for other related research projects or archived/published in a public repository in the future.

**5. Voluntary Participation and Withdrawal:**
*   Your participation is entirely **voluntary**.
*   You can **stop the interview at any time** without penalty by using the "Quit" button or simply closing the window. Due to the persistent logging, data up to the point you stopped may still be retained linked to your User ID.
*   You may choose **not to answer any specific question** in the survey.
*   If you have concerns after participation, you can contact Janik Deutscher (janik.deutscher@upf.edu) with your User ID (`{username}`). Data removal might be complex once anonymized and aggregated.

**6. Risks and Benefits:**
*   Participating in this study involves risks **no greater than those encountered in everyday life** (e.g., reflecting on your opinions).
*   There are **no direct benefits** guaranteed to you from participating, although your responses will contribute valuable insights to research on education and career preparation.

**7. Contact Information:**
*   If you have questions about this study, please contact the researcher, **Janik Deutscher (janik.deutscher@upf.edu)**.
*   If you have concerns about this study or your rights as a participant, you may contact **UPF’s Institutional Committee for the Ethical Review of Projects (CIREP)** by phone (+34 935 422 186) or email (secretaria.cirep@upf.edu). CIREP is independent of the research team and treats inquiries confidentially.

**8. GDPR Information (Data Protection):**
*   In accordance with the General Data Protection Regulation (GDPR) 2016/679 (EU), we provide the following:
    *   **Data Controller:** Universitat Pompeu Fabra. Pl. de la Mercè, 10-12. 08002 Barcelona. Tel. +34 935 422 000.
    *   **Data Protection Officer (DPO):** Contact via email at dpd@upf.edu.
    *   **Purposes of Processing:** Carrying out the research project described above. Anonymized research data will be kept as described in section 4. The temporary processing of interview data by the AI provider via API is described in section 3. Persistent logging to Firestore for data integrity and session resumption is described in section 3.
    *   **Legal Basis:** Your explicit consent. You can withdraw consent at any time (though data withdrawal post-submission may be limited as explained above).
    *   **Your Rights:** You have the right to access your data; request rectification, deletion, or portability (in certain cases); object to processing; or request limitation. Procedures are described at www.upf.edu/web/proteccio-dades/drets. Contact the DPO (dpd@upf.edu) for queries. If unsatisfied, you may contact the Catalan Data Protection Authority (apdcat.gencat.cat).
    """)
    consent = st.checkbox("I confirm that I have read and understood the information sheet above, including the information about how the AI interview works and data logging. I am 18 years or older, and I voluntarily consent to participate in this study.", key="consent_checkbox", value=st.session_state.get("consent_given", False))
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
    if st.session_state.start_time is None and "start_time_unix" not in st.session_state.get("loaded_state", {}):
        st.session_state.start_time = time.time(); st.session_state.start_time_file_names = time.strftime("%Y%m%d_%H%M%S", time.localtime(st.session_state.start_time))
        utils.save_interview_state_to_firestore(username, {"start_time_unix": st.session_state.start_time}); print("Start time initialized and saved.")
    if not st.session_state.get("interview_active", False):
         st.session_state.interview_active = True; utils.save_interview_state_to_firestore(username, {"interview_active": True}); print("Interview marked as active.")
    st.info("Please answer the interviewer's questions.")
    if st.button("Quit Interview Early", key="quit_interview"):
        st.session_state.interview_active = False; st.session_state.interview_completed_flag = True
        quit_message = "You have chosen to end the interview early. Proceeding to the final questions."; quit_msg_dict = {"role": "assistant", "content": quit_message}
        st.session_state.messages.append(quit_msg_dict); utils.save_message_to_firestore(username, quit_msg_dict)
        utils.save_interview_data(username=username, transcripts_directory=config.TRANSCRIPTS_DIRECTORY, times_directory=config.TIMES_DIRECTORY, is_final_save=True, messages_to_format=st.session_state.messages)
        utils.save_interview_state_to_firestore(username, {"interview_active": False, "interview_completed_flag": True, "current_stage": SURVEY_STAGE})
        st.warning(quit_message); st.session_state.current_stage = SURVEY_STAGE; print("Moving to Survey Stage after Quit."); time.sleep(1); st.rerun()
    for message in st.session_state.get("messages", []):
        if message.get('role') == "system": continue;
        if message.get('content', '') in config.CLOSING_MESSAGES.keys(): continue
        avatar = config.AVATAR_INTERVIEWER if message.get('role') == "assistant" else config.AVATAR_RESPONDENT
        with st.chat_message(message.get('role', 'unknown'), avatar=avatar): st.markdown(message.get('content', ''))
    if not st.session_state.get("messages", []) or \
       (api == "openai" and len(st.session_state.get("messages", [])) == 1 and st.session_state.get("messages", [])[0].get("role") == "system"):
        print("No previous assistant/user messages found, attempting to get initial message.")
        try:
            if api == "openai":
                 if not st.session_state.messages or st.session_state.messages[0].get("role") != "system":
                     sys_prompt_dict = {"role": "system", "content": config.SYSTEM_PROMPT}; st.session_state.messages.insert(0, sys_prompt_dict)
            with st.chat_message("assistant", avatar=config.AVATAR_INTERVIEWER):
                message_placeholder = st.empty(); message_placeholder.markdown("Thinking...")
                api_messages = []; message_interviewer = ""
                if api == "openai":
                    if st.session_state.messages and st.session_state.messages[0].get("role") == 'system': api_messages = [st.session_state.messages[0]]
                if api == "anthropic": api_messages = [{"role": "user", "content": "Please begin the interview."}]
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
                     message_placeholder.error(f"Error connecting to the AI assistant after multiple attempts: {e_retry}. Switching to manual fallback.")
                     utils.save_interview_data(username=username, transcripts_directory=config.TRANSCRIPTS_DIRECTORY, times_directory=config.TIMES_DIRECTORY, is_final_save=True, messages_to_format=st.session_state.messages)
                     st.session_state.partial_ai_transcript_formatted = st.session_state.pop("current_formatted_transcript_for_gsheet", "ERROR: Partial transcript format failed.")
                     st.session_state.current_stage = MANUAL_INTERVIEW_STAGE
                     utils.save_interview_state_to_firestore(username, {"current_stage": MANUAL_INTERVIEW_STAGE, "interview_active": False, "manual_fallback_triggered": True, "partial_ai_transcript_formatted": st.session_state.partial_ai_transcript_formatted})
                     st.rerun()
                except Exception as e_fatal:
                     print(f"Non-retryable initial API error: {e_fatal}")
                     message_placeholder.error(f"An unexpected error occurred connecting to the AI assistant: {e_fatal}. Switching to manual fallback.")
                     utils.save_interview_data(username=username, transcripts_directory=config.TRANSCRIPTS_DIRECTORY, times_directory=config.TIMES_DIRECTORY, is_final_save=True, messages_to_format=st.session_state.messages)
                     st.session_state.partial_ai_transcript_formatted = st.session_state.pop("current_formatted_transcript_for_gsheet", "ERROR: Partial transcript format failed.")
                     st.session_state.current_stage = MANUAL_INTERVIEW_STAGE
                     utils.save_interview_state_to_firestore(username, {"current_stage": MANUAL_INTERVIEW_STAGE, "interview_active": False, "manual_fallback_triggered": True, "partial_ai_transcript_formatted": st.session_state.partial_ai_transcript_formatted})
                     st.rerun()
            assistant_msg_dict = {"role": "assistant", "content": message_interviewer.strip()}
            st.session_state.messages.append(assistant_msg_dict)
            utils.save_message_to_firestore(username, assistant_msg_dict)
            print("Initial message obtained and saved."); time.sleep(0.1); st.rerun()
        except Exception as e:
            if 'message_placeholder' in locals(): message_placeholder.empty()
            st.error(f"Failed during initial message setup: {e}"); st.stop()
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
                                         if code == current_content_stripped: detected_code = code; message_interviewer = full_response_content.replace(code, "").strip(); stream_closed = True; break
                                     if stream_closed: break
                                     message_interviewer = full_response_content; message_placeholder.markdown(message_interviewer + "▌")
                        if not stream_closed: message_placeholder.markdown(message_interviewer)
                    elif api == "anthropic":
                         with anthropic_client.messages.stream(**api_kwargs) as stream:
                            for text_delta in stream.text_stream:
                                 if text_delta is not None:
                                     full_response_content += text_delta; current_content_stripped = full_response_content.strip()
                                     for code in config.CLOSING_MESSAGES.keys():
                                         if code == current_content_stripped: detected_code = code; message_interviewer = full_response_content.replace(code,"").strip(); stream_closed = True; break
                                     if stream_closed: break
                                     message_interviewer = full_response_content; message_placeholder.markdown(message_interviewer + "▌")
                         if not stream_closed: message_placeholder.markdown(message_interviewer)
                    assistant_msg_content = full_response_content.strip()
                    assistant_msg_dict = {"role": "assistant", "content": assistant_msg_content}
                    if not st.session_state.messages or st.session_state.messages[-1] != assistant_msg_dict:
                        st.session_state.messages.append(assistant_msg_dict); utils.save_message_to_firestore(username, assistant_msg_dict)
                    if detected_code:
                        st.session_state.interview_active = False; st.session_state.interview_completed_flag = True
                        closing_message_display = config.CLOSING_MESSAGES[detected_code]
                        if message_interviewer: message_placeholder.markdown(message_interviewer)
                        utils.save_interview_data(username=username, transcripts_directory=config.TRANSCRIPTS_DIRECTORY, times_directory=config.TIMES_DIRECTORY, is_final_save=True, messages_to_format=st.session_state.messages)
                        utils.save_interview_state_to_firestore(username, {"interview_active": False, "interview_completed_flag": True, "current_stage": SURVEY_STAGE})
                        if closing_message_display: st.success(closing_message_display)
                        st.session_state.current_stage = SURVEY_STAGE; print("Moving to Survey Stage after code detection."); time.sleep(2); st.rerun()
                 except RETRYABLE_ERRORS as e_retry:
                     print(f"API call failed during chat stream after potential retries: {e_retry}")
                     message_placeholder.error(f"Connection to the AI assistant failed: {e_retry}. Switching to manual fallback.")
                     utils.save_interview_data(username=username, transcripts_directory=config.TRANSCRIPTS_DIRECTORY, times_directory=config.TIMES_DIRECTORY, is_final_save=True, messages_to_format=st.session_state.messages)
                     st.session_state.partial_ai_transcript_formatted = st.session_state.pop("current_formatted_transcript_for_gsheet", "ERROR: Partial transcript format failed.")
                     st.session_state.current_stage = MANUAL_INTERVIEW_STAGE
                     utils.save_interview_state_to_firestore(username, {"current_stage": MANUAL_INTERVIEW_STAGE, "interview_active": False, "manual_fallback_triggered": True, "partial_ai_transcript_formatted": st.session_state.partial_ai_transcript_formatted})
                     st.rerun()
                 except Exception as e_fatal:
                     print(f"Unhandled API error during chat stream: {e_fatal}")
                     message_placeholder.error(f"An unexpected error occurred: {e_fatal}. Switching to manual fallback.")
                     utils.save_interview_data(username=username, transcripts_directory=config.TRANSCRIPTS_DIRECTORY, times_directory=config.TIMES_DIRECTORY, is_final_save=True, messages_to_format=st.session_state.messages)
                     st.session_state.partial_ai_transcript_formatted = st.session_state.pop("current_formatted_transcript_for_gsheet", "ERROR: Partial transcript format failed.")
                     st.session_state.current_stage = MANUAL_INTERVIEW_STAGE
                     utils.save_interview_state_to_firestore(username, {"current_stage": MANUAL_INTERVIEW_STAGE, "interview_active": False, "manual_fallback_triggered": True, "partial_ai_transcript_formatted": st.session_state.partial_ai_transcript_formatted})
                     st.rerun()
        except Exception as e:
            if 'message_placeholder' in locals(): message_placeholder.empty()
            st.error(f"An error occurred: {e}. Switching to manual fallback.")
            utils.save_interview_data(username=username, transcripts_directory=config.TRANSCRIPTS_DIRECTORY, times_directory=config.TIMES_DIRECTORY, is_final_save=True, messages_to_format=st.session_state.messages)
            st.session_state.partial_ai_transcript_formatted = st.session_state.pop("current_formatted_transcript_for_gsheet", "ERROR: Partial transcript format failed.")
            st.session_state.current_stage = MANUAL_INTERVIEW_STAGE
            utils.save_interview_state_to_firestore(username, {"current_stage": MANUAL_INTERVIEW_STAGE, "interview_active": False, "manual_fallback_triggered": True, "partial_ai_transcript_formatted": st.session_state.partial_ai_transcript_formatted})
            st.rerun()


# --- Section 1.5: Manual Interview Fallback Stage ---
elif st.session_state.get("current_stage") == MANUAL_INTERVIEW_STAGE:
    st.title("Part 1: Interview (Manual Fallback)")
    st.warning("We encountered an issue connecting to the AI interviewer. Please answer the remaining core questions manually below. Your previous answers (if any) have been saved.")
    last_part_idx = find_last_ai_part_completed(st.session_state.get("messages", []))
    start_part_idx = last_part_idx + 1
    print(f"Manual fallback starting from part index: {start_part_idx}")
    manual_answers = {}; questions_to_ask = []
    for i in range(start_part_idx, len(part_key_sequence)):
        part_key = part_key_sequence[i]
        if part_key in manual_questions_map: questions_to_ask.extend(manual_questions_map[part_key])
    if not questions_to_ask:
        st.error("Could not determine which manual questions to ask. Please contact the researcher.")
        for part_key in part_key_sequence:
             if part_key in manual_questions_map: questions_to_ask.extend(manual_questions_map[part_key])
        if not questions_to_ask: st.stop()
    with st.form("manual_interview_form"):
        st.markdown("Please provide your answers in the text boxes below.")
        for q_data in questions_to_ask:
            manual_answers[q_data["key"]] = st.text_area(q_data["text"], key=f"manual_{q_data['key']}", height=150)
        manual_submitted = st.form_submit_button("Submit Manual Answers & Proceed to Survey")
    if manual_submitted:
        st.info("Processing manual answers...")
        manual_transcript_parts = ["MANUAL FALLBACK ANSWERS\n---\n"]
        for q_data in questions_to_ask:
            answer = manual_answers.get(q_data["key"], "").strip()
            manual_transcript_parts.append(f"Question: {q_data['text']}\nAnswer: {answer if answer else '[No answer provided]'}\n---")
        manual_formatted_answers = "\n".join(manual_transcript_parts)
        st.session_state.manual_answers_formatted = manual_formatted_answers
        st.session_state.current_formatted_transcript_for_gsheet = st.session_state.get("partial_ai_transcript_formatted", "ERROR: Partial AI transcript missing.")
        st.session_state.interview_active = False; st.session_state.interview_completed_flag = True
        utils.save_interview_state_to_firestore(username, { "interview_active": False, "interview_completed_flag": True, "current_stage": SURVEY_STAGE, "manual_fallback_triggered": True, "manual_answers_formatted": manual_formatted_answers })
        st.session_state.current_stage = SURVEY_STAGE
        print("Moving to Survey Stage after Manual Fallback submission."); st.rerun()

# --- Section 2: Survey Stage ---
elif st.session_state.get("current_stage") == SURVEY_STAGE:
    st.title("Part 2: Survey")
    st.info(f"Thank you, please answer a few final questions.")
    if "current_formatted_transcript_for_gsheet" not in st.session_state:
         print("CRITICAL WARNING: Formatted transcript key missing at survey stage entry.")
         if st.session_state.get("manual_answers_formatted"):
             st.session_state.current_formatted_transcript_for_gsheet = st.session_state.get("partial_ai_transcript_formatted", "ERROR: Partial AI transcript missing after fallback.")
         else:
             print("Attempting last-resort transcript formatting...")
             utils.save_interview_data(username=username, transcripts_directory=config.TRANSCRIPTS_DIRECTORY, times_directory=config.TIMES_DIRECTORY, is_final_save=True, messages_to_format=st.session_state.get("messages", []))
         if "current_formatted_transcript_for_gsheet" not in st.session_state:
              st.error("Error: Could not generate or find transcript for saving.")
              st.session_state.current_formatted_transcript_for_gsheet = "ERROR: Transcript generation/retrieval failed before survey."
    age_options = ["Select...", "Under 18"] + [str(i) for i in range(18, 36)] + ["Older than 35"]; gender_options = ["Select...", "Male", "Female", "Non-binary", "Prefer not to say"]; major_options = ["Select...", "Computer Science", "Engineering (Other)", "Business", "Humanities", "Social Sciences", "Natural Sciences", "Arts", "Health Sciences", "Other", "Not Applicable"]; year_options = ["Select...", "1st Year Undergraduate", "2nd Year Undergraduate", "3rd Year Undergraduate", "4th+ Year Undergraduate", "Graduate Student", "Postgraduate/Doctoral", "Not a Student"]; gpa_values = np.round(np.arange(5.0, 10.01, 0.1), 1); gpa_options = ["Select...", "Below 5.0"] + [f"{gpa:.1f}" for gpa in gpa_values] + ["Prefer not to say / Not applicable"]; ai_freq_options = ["Select...", "Frequently (Daily/Weekly)", "Occasionally (Monthly)", "Rarely (Few times a year)", "Never", "Unsure"]
    with st.form("survey_form"):
        st.subheader("Demographic Information"); age = st.selectbox("Age?", age_options, key="age"); gender = st.selectbox("Gender?", gender_options, key="gender"); major = st.selectbox("Major/Field?", major_options, key="major"); year_of_study = st.selectbox("Year?", year_options, key="year"); gpa = st.selectbox("GPA?", gpa_options, key="gpa")
        st.subheader("AI Usage"); ai_frequency = st.selectbox("AI Use Frequency?", ai_freq_options, key="ai_frequency"); ai_model = st.text_input("AI Model(s) Used?", key="ai_model")
        submitted = st.form_submit_button("Submit Survey Responses")
    if submitted:
        if (age == "Select..." or gender == "Select..." or major == "Select..." or year_of_study == "Select..." or gpa == "Select..." or ai_frequency == "Select..."):
            st.warning("Please answer all dropdown questions.")
        else:
            survey_responses = {"age": age, "gender": gender, "major": major, "year": year_of_study, "gpa": gpa, "ai_frequency": ai_frequency, "ai_model": ai_model}
            save_successful = utils.save_survey_data(username, survey_responses)
            if save_successful:
                st.session_state.survey_completed_flag = True; st.session_state.current_stage = COMPLETED_STAGE
                utils.save_interview_state_to_firestore(username, {"current_stage": COMPLETED_STAGE, "survey_completed_flag": True})
                st.success("Survey submitted! Thank you."); st.balloons(); time.sleep(3); st.rerun()
            else:
                st.warning("Could not save to Google Sheets. Your responses may have been saved to our backup system. Please try submitting again or contact the researcher.")

# --- Section 3: Completed Stage ---
elif st.session_state.get("current_stage") == COMPLETED_STAGE:
    st.title("Thank You!")
    if st.session_state.get("survey_completed_flag", False):
        st.success("You have completed the interview and the survey. Your contribution is greatly appreciated!")
        st.markdown("You may now close this window.")
    else:
        st.warning("Navigated to completion page, but survey completion status not confirmed.")
        st.markdown("If you believe this is an error, please contact the researcher.")


# --- Fallback / Initializing ---
else:
    st.spinner("Loading application state...")
    print(f"Info: Fallback/Loading state. User: {username}, Stage: {st.session_state.get('current_stage')}, Initialized: {st.session_state.get('session_initialized')}")
    time.sleep(1.0)
    if username and st.session_state.get("session_initialized"): determine_current_stage(username)
    st.rerun()