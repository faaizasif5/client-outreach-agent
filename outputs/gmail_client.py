"""Google OAuth + a thin Gmail wrapper (spec section 14).

Scopes: gmail.compose (drafts) + gmail.metadata (headers only, to detect sent/replies) +
drive.file (only files this tool creates: the tracker sheet and uploaded resumes).
No gmail.readonly / gmail.modify / full access. Sent mail and inbox messages are never
modified or deleted; only the tool's own drafts are."""
import base64
import logging
import mimetypes
import time
from email.message import EmailMessage
from pathlib import Path

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.metadata",
    "https://www.googleapis.com/auth/drive.file",
]
JOB_HEADER = "X-Job-Agent-Key"


class NeedsSetup(Exception):
    """No usable token: run `python main.py setup-google` once (opens a browser)."""


def load_credentials(credentials_path: Path, token_path: Path, interactive: bool = False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif interactive:
        if not credentials_path.exists():
            raise NeedsSetup(f"{credentials_path} not found (OAuth desktop client from Google Cloud)")
        creds = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES).run_local_server(port=0)
    else:
        raise NeedsSetup("Google token missing or revoked: run `python main.py setup-google`")
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_services(creds) -> dict:
    from googleapiclient.discovery import build
    return {name: build(name, version, credentials=creds, cache_discovery=False)
            for name, version in (("gmail", "v1"), ("sheets", "v4"), ("drive", "v3"))}


def execute(request, retries: int = 3, sleep=time.sleep):
    """Bounded retries on rate limits and server errors."""
    from googleapiclient.errors import HttpError
    for attempt in range(retries + 1):
        try:
            return request.execute()
        except HttpError as e:
            status = getattr(e.resp, "status", 0)
            if status in (429, 500, 502, 503, 504) and attempt < retries:
                sleep(2 ** attempt * 2)
                continue
            raise


def is_not_found(err: Exception) -> bool:
    return getattr(getattr(err, "resp", None), "status", None) == 404


def build_mime(*, sender: str, to: str, subject: str, body: str, job_key: str,
               attachment: Path | None = None, filename: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["To"] = to
    if sender:
        msg["From"] = sender
    msg["Subject"] = subject
    msg[JOB_HEADER] = job_key            # lets a crashed run find its own draft again
    msg.set_content(body)
    if attachment:
        data = Path(attachment).read_bytes()
        ctype = mimetypes.guess_type(str(attachment))[0] or "application/pdf"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename or Path(attachment).name)
    return msg


def _raw(msg: EmailMessage) -> str:
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


class GmailClient:
    def __init__(self, service, address: str):
        self.svc = service.users()
        self.address = address.lower()

    def create_draft(self, msg: EmailMessage) -> tuple[str, str]:
        draft = execute(self.svc.drafts().create(userId="me", body={"message": {"raw": _raw(msg)}}))
        return draft["id"], draft["message"]["threadId"]

    def delete_draft(self, draft_id: str) -> None:
        try:
            execute(self.svc.drafts().delete(userId="me", id=draft_id))
        except Exception as e:
            if not is_not_found(e):
                raise

    def draft_exists(self, draft_id: str) -> bool:
        try:
            execute(self.svc.drafts().get(userId="me", id=draft_id, format="minimal"))
            return True
        except Exception as e:
            if is_not_found(e):
                return False
            raise

    def find_draft(self, job_key: str) -> tuple[str, str] | None:
        """Our own draft for a job, matched by the X-Job-Agent-Key header."""
        token = None
        while True:
            page = execute(self.svc.drafts().list(userId="me", pageToken=token, maxResults=100))
            for d in page.get("drafts", []):
                full = execute(self.svc.drafts().get(userId="me", id=d["id"], format="metadata"))
                headers = {h["name"].lower(): h["value"] for h in full["message"]["payload"].get("headers", [])}
                if headers.get(JOB_HEADER.lower()) == job_key:
                    return d["id"], full["message"]["threadId"]
            token = page.get("nextPageToken")
            if not token:
                return None

    def thread_status(self, thread_id: str) -> dict:
        """{'sent': bool, 'sent_at_ms': int|None, 'reply': bool} from message labels and From headers."""
        try:
            thread = execute(self.svc.threads().get(userId="me", id=thread_id, format="metadata",
                                                    metadataHeaders=["From"]))
        except Exception as e:
            if is_not_found(e):
                return {"sent": False, "sent_at_ms": None, "reply": False}
            raise
        sent_at, reply = None, False
        for m in thread.get("messages", []):
            labels = set(m.get("labelIds", []))
            sender = next((h["value"] for h in m.get("payload", {}).get("headers", [])
                           if h["name"].lower() == "from"), "").lower()
            if "SENT" in labels:
                sent_at = sent_at or int(m.get("internalDate", 0))
            elif "DRAFT" not in labels and self.address not in sender:
                reply = True
        return {"sent": sent_at is not None, "sent_at_ms": sent_at, "reply": reply}

    def recent_inbox_headers(self, limit: int = 50) -> list[dict]:
        """From/Subject/Date of recent inbox messages (metadata scope cannot search with q)."""
        page = execute(self.svc.messages().list(userId="me", labelIds=["INBOX"], maxResults=limit))
        out = []
        for m in page.get("messages", []):
            msg = execute(self.svc.messages().get(userId="me", id=m["id"], format="metadata",
                                                  metadataHeaders=["From", "Subject", "Date"]))
            headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
            out.append({"id": m["id"], "from": headers.get("from", ""), "subject": headers.get("subject", ""),
                        "internal_ms": int(msg.get("internalDate", 0))})
        return out
