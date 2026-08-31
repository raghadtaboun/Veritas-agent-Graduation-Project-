import streamlit as st

if "nav" not in st.session_state:
    st.session_state["nav"] = "home"

st.radio("Nav", ["home", "events"], key="nav")

def go_events():
    st.session_state["nav"] = "events"

st.button("Go to events", on_click=go_events)
