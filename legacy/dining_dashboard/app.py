import os, yaml, streamlit as st, streamlit_authenticator as stauth
import requests
from io import StringIO
import pandas as pd
from yaml.loader import SafeLoader

# Prefer ENV; allow inline only as a local fallback
INLINE_AUTH_YAML = """
credentials:
  usernames:
    analyst:
      email: cbasken@berkeley.edu
      hashed_password: $2b$12$3zAuuEeiJZKalDszyb.9aOrQzT/QxCb.qNvdffdYdRIYBP0BD3Eby
    viewer:
      email: viewer@example.com
      hashed_password: $2b$12$3zAuuEeiJZKalDszyb.9aOrQzT/QxCb.qNvdffdYdRIYBP0BD3Eby
cookie:
  name: st_auth_cookie
  key: _qbVFRIvRxtW1iSMBQXoUB80tFpxtjBAfnvdPN5VX28
  expiry_days: 7
preauthorized:
  emails: []
""".strip()

auth_yaml = os.getenv("AUTH_CONFIG_YAML", INLINE_AUTH_YAML).strip()

try:
    config = yaml.load(auth_yaml, Loader=SafeLoader)
except Exception as e:
    st.error(f"Auth config error: {e}")
    st.stop()

authenticator = stauth.Authenticate(
    config["credentials"],
    config["cookie"]["name"],
    config["cookie"]["key"],
    cookie_expiry_days=config["cookie"]["expiry_days"],
    auto_hash=False,  # you already provided bcrypt hashes
)

# ---- Simple login UI ----
if "auth_status" not in st.session_state:
    st.session_state["auth_status"] = None

result = authenticator.login(
    location="main",
    fields={
        "Form name": "Login",
        "Username": "Username",
        "Password": "Password",
        "Login": "Login",
    },
)

# Newer versions should return a tuple, but guard just in case
if result is None:
    st.stop()

name, auth_status, username = result
st.session_state["authentication_status"] = auth_status  # optional: used by sub-pages

if auth_status is False:
    st.error("Invalid username or password")
    st.stop()
elif auth_status is None:
    st.info("Please enter your username and password")
    st.stop()
else:
    with st.sidebar:
        authenticator.logout("Logout", "sidebar")

# === Done ===

st.set_page_config(page_title="UC Sustainable Procurement Dashboard", layout="wide")

st.sidebar.success("Select from the choices above")

