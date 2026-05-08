"""Minimal Odoo XML-RPC client used by the Sales Support Dashboard."""
from typing import List, Dict, Optional
import xmlrpc.client


def _validate_creds(url, db, username=None, password=None, api_key=None):
    if not url or not db:
        raise ValueError("url and db are required")
    if api_key:
        return
    if not username or not password:
        raise ValueError("username and password are required when api_key is not provided")


def _xmlrpc_auth(url, db, username, password, api_key=None):
    _validate_creds(url, db, username, password, api_key)
    auth_password = api_key if api_key else password
    common = xmlrpc.client.ServerProxy(f"{url.rstrip('/')}/xmlrpc/2/common")
    uid = common.authenticate(db, username, auth_password, {})
    if not uid:
        raise RuntimeError("Odoo authentication failed")
    models = xmlrpc.client.ServerProxy(f"{url.rstrip('/')}/xmlrpc/2/object")
    return uid, auth_password, models


def xmlrpc_execute(url, db, username, password, model, method,
                   args: Optional[List] = None,
                   kwargs: Optional[Dict] = None,
                   api_key: Optional[str] = None):
    """Generic execute_kw wrapper for any Odoo model method."""
    uid, auth_password, models = _xmlrpc_auth(url, db, username, password, api_key)
    return models.execute_kw(
        db, uid, auth_password, model, method,
        args or [], kwargs or {},
    )
