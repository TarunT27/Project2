"""Fail-closed OIDC identity checks; loopback-only local development identity."""
import os
import time


def emails(name):
    return {s.strip().lower() for s in os.environ.get(name, "").split(",") if s.strip()}


def verified_identity(claims, store):
    if claims.get("exp") is not None and float(claims["exp"]) <= time.time():
        raise PermissionError("Your sign-in has expired. Sign out and sign in again.")
    if claims.get("email_verified") is not True:
        raise PermissionError("Your identity provider must return a verified email address.")
    email = str(claims.get("email", "")).strip().lower()
    if not email or "@" not in email or not claims.get("sub"):
        raise PermissionError("Your identity provider did not return the required identity claims.")
    admins = emails("DISASTER_ADMIN_EMAILS")
    if email not in admins | emails("DISASTER_ALLOWED_EMAILS") and not store.invited(email):
        raise PermissionError("This account has not been granted workspace access.")
    store.register_user(email, str(claims.get("name") or email), email in admins)
    return email


def require_identity(st, store):
    mode = os.environ.get("DISASTER_AUTH_MODE", "local")
    if mode == "local":
        if st.get_option("server.address") not in {"127.0.0.1", "localhost", "::1"}:
            st.error("Local mode requires a loopback bind address. Configure OIDC before shared hosting.")
            st.stop()
        store.register_user("local", "Local workspace", admin=True)
        return "local"
    if mode != "oidc":
        st.error("Unknown authentication mode. Use local or oidc.")
        st.stop()
    if not getattr(st.user, "is_logged_in", False):
        st.title("Sign in to Disaster Review")
        st.caption("Use the account your incident owner or administrator invited.")
        if st.button("Sign in", type="primary"):
            try:
                st.login()
            except Exception:
                st.error("Sign-in is not configured. An administrator must configure the identity provider in Streamlit secrets.")
        st.stop()
    try:
        return verified_identity(dict(st.user), store)
    except (PermissionError, ValueError, TypeError) as exc:
        st.error(str(exc))
        if st.button("Sign out"):
            st.logout()
        st.stop()
