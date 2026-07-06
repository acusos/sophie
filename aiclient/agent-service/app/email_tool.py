import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime

EMAIL_MAILBOXES = []

def init_email():
    """Initialize email connections from environment variables."""
    global EMAIL_MAILBOXES

    # Gmail
    gmail_user = None
    gmail_pass = None
    gmail_enabled = False
    import os
    for key, val in os.environ.items():
        if key.startswith("EMAIL_GMAIL_"):
            suffix = key[len("EMAIL_GMAIL_"):]
            if suffix == "USER":
                gmail_user = val
            elif suffix == "PASS":
                gmail_pass = val
            elif suffix == "ENABLED":
                gmail_enabled = val.lower() == "true"

    if gmail_enabled and gmail_user and gmail_pass:
        EMAIL_MAILBOXES.append({
            "name": "gmail",
            "label": "Gmail",
            "server": "imap.gmail.com",
            "port": 993,
            "user": gmail_user,
            "password": gmail_pass,
            "folder": "INBOX",
        })

    # Outlook
    outlook_user = None
    outlook_pass = None
    outlook_enabled = False
    for key, val in os.environ.items():
        if key.startswith("EMAIL_OUTLOOK_"):
            suffix = key[len("EMAIL_OUTLOOK_"):]
            if suffix == "USER":
                outlook_user = val
            elif suffix == "PASS":
                outlook_pass = val
            elif suffix == "ENABLED":
                outlook_enabled = val.lower() == "true"

    if outlook_enabled and outlook_user and outlook_pass:
        EMAIL_MAILBOXES.append({
            "name": "outlook",
            "label": "Outlook",
            "server": "outlook.office365.com",
            "port": 993,
            "user": outlook_user,
            "password": outlook_pass,
            "folder": "INBOX",
        })

async def check_email(mailbox_name: str = None, max_count: int = 10, search_term: str = None) -> str:
    """Check email for a mailbox. Returns summary of recent emails."""
    import asyncio
    import time

    start_time = time.time()
    timeout = 30.0

    target_mailboxes = []
    if mailbox_name:
        target_mailboxes = [m for m in EMAIL_MAILBOXES if m["name"].lower() == mailbox_name.lower()]
        if not target_mailboxes:
            return f"Mailbox '{mailbox_name}' not configured. Available: {', '.join(m['label'] for m in EMAIL_MAILBOXES)}"
    else:
        target_mailboxes = EMAIL_MAILBOXES

    if not target_mailboxes:
        return "No mailboxes configured. Set EMAIL_GMAIL_USER/PASS or EMAIL_OUTLOOK_USER/PASS env vars."

    all_results = []

    for mailbox in target_mailboxes:
        result = await _check_single_mailbox(mailbox, max_count, search_term, start_time, timeout)
        all_results.append(result)

    if not any(r.strip() for r in all_results):
        return "No results from any mailbox."

    return "\n\n---\n\n".join(r for r in all_results if r.strip())

async def _check_single_mailbox(mailbox, max_count, search_term, start_time, timeout):
    """Connect to a single mailbox and fetch recent emails."""
    try:
        conn = imaplib.IMAP4_SSL(mailbox["server"], mailbox["port"])
        conn.login(mailbox["user"], mailbox["password"])
        conn.select(mailbox["folder"])

        # Build search criteria
        search_parts = search_term.split() if search_term else []
        if search_parts:
            # Search in body or from field
            from_terms = [f'FROM "{w}"' for w in search_parts]
            body_terms = [f'BODY "{w}"' for w in search_parts]
            combined = from_terms + body_terms
            search_cmd = '('.join(combined) + ')'
            conn.search(None, search_cmd)
        else:
            conn.search(None, "ALL")

        status, ids_data = conn.search(None, "ALL") if not search_term else (status, ids_data)
        if status != "OK":
            return f"{mailbox['label']}: Search failed - {ids_data}"

        mail_ids = ids_data[0].split()
        if not mail_ids:
            return f"{mailbox['label']}: No emails found."

        # Get the most recent emails
        mail_ids = mail_ids[-max_count:]
        emails = []

        for mid in reversed(mail_ids):
            if time.time() - start_time > timeout:
                return f"{mailbox['label']}: Timeout reached. Partial results."

            status, msg_data = conn.fetch(mid, "(RFC822)")
            if status != "OK":
                continue

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            # Get subject
            subject = msg["Subject"] or "(no subject)"
            # Decode subject
            if isinstance(subject, bytes):
                subject = subject.decode("utf-8", errors="replace")
            elif isinstance(subject, str):
                pass
            # Decode header if needed
            decoded_subject = decode_header(subject)
            if decoded_subject:
                subject_parts = []
                for part, charset in decoded_subject:
                    if isinstance(part, bytes):
                        charset = charset or "utf-8"
                        part = part.decode(charset, errors="replace")
                    subject_parts.append(part)
                subject = "".join(subject_parts)

            # Get sender
            sender = msg["From"] or "Unknown"

            # Get date
            date_str = msg["Date"] or "Unknown"
            try:
                date_dt = parsedate_to_datetime(date_str)
                date_display = date_dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                date_display = date_str

            # Get snippet (first ~200 chars of text body)
            snippet = ""
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        payload = part.get_payload(decode=True)
                        if isinstance(payload, bytes):
                            payload = payload.decode("utf-8", errors="replace")
                        snippet = payload.strip()[:200]
                        break
                    except Exception:
                        continue
            if not snippet:
                # Try HTML
                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        try:
                            payload = part.get_payload(decode=True)
                            if isinstance(payload, bytes):
                                payload = payload.decode("utf-8", errors="replace")
                            # Strip HTML tags roughly
                            import re
                            text = re.sub(r'<[^>]+>', '', payload)
                            snippet = text.strip()[:200]
                            break
                        except Exception:
                            continue

            emails.append({
                "subject": subject,
                "from": sender,
                "date": date_display,
                "snippet": snippet,
            })

        conn.logout()

        if not emails:
            return f"{mailbox['label']}: No readable emails."

        lines = [f"**{mailbox['label']}** — {len(emails)} email(s):"]
        for i, e in enumerate(emails, 1):
            lines.append(f"{i}. From: {e['from']}")
            lines.append(f"   Subject: {e['subject']}")
            lines.append(f"   Date: {e['date']}")
            if e['snippet']:
                lines.append(f"   Snippet: {e['snippet']}")
            lines.append("")

        return "\n".join(lines)

    except imaplib.IMAP4.error as e:
        return f"{mailbox['label']} connection error: {e}"
    except Exception as e:
        return f"{mailbox['label']} error: {e}"
