# tools/gmail_tools.py
#
# Gmail read/send/manage tools via IMAP + SMTP.
#
# Requires SMTP_EMAIL and SMTP_PASSWORD in .env (an App Password, not
# the account password — create one at
# https://myaccount.google.com/apppasswords).
#
# All functions use IMAP UIDs (stable across sessions) rather than
# sequence numbers (which shift as messages arrive). read_latest_emails
# returns the UID in each block so subsequent calls can pass it to
# read_email, reply_to_email, mark_email_read, download_attachments,
# delete_email, etc.
#
# Tools exposed (registry keys in gm.py):
#   read_latest_emails   → "Read Gmail"
#   read_email           → "Read Email"
#   search_emails        → "Search Emails"
#   send_email           → "Send Gmail"
#   reply_to_email       → "Reply To Email"
#   forward_email        → "Forward Email"
#   list_gmail_folders   → "List Gmail Folders"
#   mark_email_read      → "Mark Email Read"
#   mark_email_unread    → "Mark Email Unread"
#   move_email_to_folder → "Move Email To Folder"
#   download_attachments → "Download Attachments"
#   delete_email         → "Delete Email"
#   unread_count         → "Unread Count"
import email
import imaplib
import mimetypes
import os
import re
import smtplib
from email import encoders
from email.header import decode_header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid, parseaddr

from bs4 import BeautifulSoup
from crewai.tools import tool


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────
IMAP_TIMEOUT = 30
SMTP_TIMEOUT = 30
GMAIL_IMAP_HOST = "imap.gmail.com"
GMAIL_SMTP_HOST = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587
TRASH_FOLDER = "[Gmail]/Trash"


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
def _log(tool_name: str, detail: str) -> None:
    """Lazy log helper — avoids circular import with empire_tools."""
    try:
        from empire_tools import log_agent_action
        log_agent_action(tool_name, detail)
    except Exception:
        pass


def _creds():
    return os.getenv("SMTP_EMAIL"), os.getenv("SMTP_PASSWORD")


def _imap():
    """
    Return (mail, None) with a connected, authenticated IMAP4_SSL
    session, or (None, error_string) on failure. Auth failures get
    a distinct message from network failures so the agent can tell
    the user which problem it is.
    """
    user, pwd = _creds()
    if not user or not pwd:
        return None, "❌ Missing SMTP_EMAIL or SMTP_PASSWORD in .env."

    try:
        mail = imaplib.IMAP4_SSL(GMAIL_IMAP_HOST, timeout=IMAP_TIMEOUT)
        mail.login(user, pwd)
        return mail, None
    except imaplib.IMAP4.error as e:
        msg = str(e)
        if "AUTHENTICATIONFAILED" in msg or "Invalid credentials" in msg:
            return None, (
                "❌ Gmail authentication failed. The app password is "
                "wrong or revoked. Regenerate it at "
                "https://myaccount.google.com/apppasswords and update "
                "SMTP_PASSWORD in .env."
            )
        return None, f"❌ Gmail IMAP error: {e}"
    except Exception as e:
        return None, f"❌ Gmail IMAP connection failed: {e}"


def _smtp():
    """
    Return (server, user, None) with a connected, authenticated SMTP
    session, or (None, None, error_string).
    """
    user, pwd = _creds()
    if not user or not pwd:
        return None, None, "❌ Missing SMTP_EMAIL or SMTP_PASSWORD in .env."

    try:
        server = smtplib.SMTP(GMAIL_SMTP_HOST, GMAIL_SMTP_PORT, timeout=SMTP_TIMEOUT)
        server.starttls()
        server.login(user, pwd)
        return server, user, None
    except smtplib.SMTPAuthenticationError:
        return None, None, (
            "❌ Gmail authentication failed. The app password is wrong "
            "or revoked. Regenerate it at "
            "https://myaccount.google.com/apppasswords and update "
            "SMTP_PASSWORD in .env."
        )
    except Exception as e:
        return None, None, f"❌ Gmail SMTP connection failed: {e}"


def _decode_header(raw: str) -> str:
    """Best-effort decode of a MIME header value to str."""
    if not raw:
        return ""
    try:
        parts = decode_header(raw)
        out = []
        for value, enc in parts:
            if isinstance(value, bytes):
                out.append(value.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(value)
        return "".join(out)
    except Exception:
        return str(raw)


def _html_to_text(html: str) -> str:
    """
    Convert HTML to readable plain text using BeautifulSoup.

    Removes script/style/nav/footer/header blocks. Preserves some
    paragraph and line-break structure so the output is readable.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        # Crude regex strip if bs4 fails for any reason.
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()

    for junk in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        junk.decompose()

    # Convert <br> and block boundaries into newlines so the output
    # has paragraph structure, not one wall of text.
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(
        ["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"]
    ):
        block.append("\n")

    text = soup.get_text(separator="")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_body(msg) -> str:
    """
    Return the best-effort plain-text body of an email.

    Strategy:
      1. If there's a text/plain part, use it.
      2. Otherwise convert text/html to readable text.
      3. Walk nested multipart structures, honouring the charset.
    """
    plain, html = "", ""

    for part in msg.walk():
        ctype = part.get_content_type()
        disp  = str(part.get("Content-Disposition") or "")
        if "attachment" in disp:
            continue

        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        if not payload:
            continue

        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            text = payload.decode("utf-8", errors="replace")

        if ctype == "text/plain" and not plain:
            plain = text
        elif ctype == "text/html" and not html:
            html = text

    if plain.strip():
        return plain

    if html.strip():
        return _html_to_text(html)

    # Not multipart — decode the top level directly.
    if not msg.is_multipart():
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                return payload.decode(charset, errors="replace")
        except Exception:
            pass

    return ""


def _list_attachments(msg) -> list:
    """Return [(filename, raw_bytes, content_type), ...] for each attachment."""
    out = []
    for part in msg.walk():
        disp = str(part.get("Content-Disposition") or "")
        if "attachment" not in disp:
            continue
        fname = _decode_header(part.get_filename() or "attachment")
        ctype = part.get_content_type() or "application/octet-stream"
        payload = part.get_payload(decode=True)
        if payload is not None:
            out.append((fname, payload, ctype))
    return out


def _safe_filename(name: str) -> str:
    """Strip any path components and illegal characters from a filename."""
    name = os.path.basename(name or "")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.strip(" .") or "attachment"
    return name[:200]


def _attach_file(msg: MIMEMultipart, path: str) -> bool:
    """
    Attach a local file to a MIMEMultipart message.
    Returns True on success, False if the file doesn't exist.
    """
    if not os.path.isfile(path):
        return False
    ctype, _ = mimetypes.guess_type(path)
    maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
    with open(path, "rb") as f:
        part = MIMEBase(maintype, subtype)
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header(
        "Content-Disposition",
        f'attachment; filename="{os.path.basename(path)}"',
    )
    msg.attach(part)
    return True


def _move_uid(mail, uid: str, folder: str) -> tuple:
    """
    Copy `uid` to `folder` then expunge from the currently selected
    folder. Returns (ok, error_message).
    """
    status, _ = mail.uid("COPY", str(uid).strip(), folder)
    if status != "OK":
        return False, f"Could not copy UID {uid} to '{folder}'."
    mail.uid("STORE", str(uid).strip(), "+FLAGS", "(\\Deleted)")
    mail.expunge()
    return True, ""


# ──────────────────────────────────────────────────────────────────────
# 📧 READ LATEST EMAILS
# ──────────────────────────────────────────────────────────────────────
@tool("Read Gmail")
def read_latest_emails(search_keyword: str = "ALL", limit: int = 5):
    """
    Reads the latest emails from Gmail using IMAP.
    Use search_keyword to filter (e.g., 'Indeed', 'Password',
    'from:hr@company.com').
    Each result block includes a UID — pass that UID to read_email,
    reply_to_email, mark_email_read, download_attachments, or
    move_email_to_folder.
    Requires SMTP_EMAIL and SMTP_PASSWORD in .env.
    """
    _log("Read Gmail", f"Keyword: {search_keyword} | Limit: {limit}")

    mail, err = _imap()
    if err:
        return err

    try:
        mail.select("inbox")
        criteria = (
            f'(TEXT "{search_keyword}")' if search_keyword != "ALL" else "ALL"
        )
        status, data = mail.uid("SEARCH", None, criteria)
        if status != "OK" or not data[0]:
            mail.logout()
            return f"📭 No emails found matching '{search_keyword}'."

        uids = data[0].split()[-limit:]
        blocks = []
        for uid in reversed(uids):
            status, msg_data = mail.uid("FETCH", uid, "(RFC822)")
            if status != "OK":
                continue
            for part in msg_data:
                if not isinstance(part, tuple):
                    continue
                msg = email.message_from_bytes(part[1])
                subject = _decode_header(msg.get("Subject", "No Subject"))
                sender  = _decode_header(msg.get("From", "?"))
                date    = msg.get("Date", "?")
                body    = _extract_body(msg)
                atts    = [name for name, _, _ in _list_attachments(msg)]
                att_line = f"\nATTACHMENTS: {', '.join(atts)}" if atts else ""
                blocks.append(
                    f"📩 UID: {uid.decode()}\n"
                    f"SUBJECT: {subject}\n"
                    f"FROM: {sender}\n"
                    f"DATE: {date}{att_line}\n"
                    f"BODY:\n{body[:1000]}"
                    f"{'...[truncated, use read_email for full body]' if len(body) > 1000 else ''}\n"
                    f"{'-'*50}"
                )
        mail.logout()
        return "\n".join(blocks) if blocks else "📭 No emails fetched."

    except Exception as e:
        return f"❌ Gmail IMAP Error: {e}"


# ──────────────────────────────────────────────────────────────────────
# 📖 READ ONE EMAIL (full body + attachments list)
# ──────────────────────────────────────────────────────────────────────
@tool("Read Email")
def read_email(uid: str):
    """
    Read the full body of one email by UID.
    UID comes from read_latest_emails or search_emails output.

    Args:
      uid: The email UID as a string (e.g. '42').
    """
    _log("Read Email", f"UID: {uid}")

    mail, err = _imap()
    if err:
        return err

    try:
        mail.select("inbox")
        status, msg_data = mail.uid("FETCH", str(uid).strip(), "(RFC822)")
        if status != "OK":
            mail.logout()
            return f"❌ Could not fetch UID {uid}."

        for part in msg_data:
            if not isinstance(part, tuple):
                continue
            msg = email.message_from_bytes(part[1])
            subject = _decode_header(msg.get("Subject", "No Subject"))
            sender  = _decode_header(msg.get("From", "?"))
            to      = _decode_header(msg.get("To", "?"))
            cc      = _decode_header(msg.get("Cc", "") or "")
            date    = msg.get("Date", "?")
            msg_id  = msg.get("Message-ID", "")
            body    = _extract_body(msg)
            atts    = _list_attachments(msg)

            att_section = ""
            if atts:
                att_section = "\nATTACHMENTS:\n" + "\n".join(
                    f"  • {name} ({ctype}, {len(data):,} bytes)"
                    for name, data, ctype in atts
                )
                att_section += (
                    "\n  (use Download Attachments to save them to disk)"
                )

            mail.logout()
            return (
                f"📩 UID: {uid}\n"
                f"SUBJECT: {subject}\n"
                f"FROM: {sender}\n"
                f"TO: {to}\n"
                + (f"CC: {cc}\n" if cc else "")
                + f"DATE: {date}\n"
                f"MESSAGE-ID: {msg_id}"
                f"{att_section}\n\n"
                f"BODY:\n{body}"
            )

        mail.logout()
        return f"❌ UID {uid} returned no parsable message."

    except Exception as e:
        return f"❌ Read Email Error: {e}"


# ──────────────────────────────────────────────────────────────────────
# 🔎 SEARCH EMAILS (structured)
# ──────────────────────────────────────────────────────────────────────
@tool("Search Emails")
def search_emails(
    from_: str = "",
    subject: str = "",
    since: str = "",
    before: str = "",
    unread_only: bool = False,
    limit: int = 10,
):
    """
    Structured IMAP search across the inbox.

    Args:
      from_:       Sender substring (e.g. 'hetzner', 'hr@company.com').
      subject:     Subject substring.
      since:       Date filter 'DD-Mon-YYYY' (e.g. '01-Jan-2026').
      before:      Date filter 'DD-Mon-YYYY'.
      unread_only: If true, only unread messages.
      limit:       Max results (default 10).

    Returns the same UID-bearing blocks as read_latest_emails.
    """
    _log("Search Emails", f"from={from_} subj={subject} since={since} "
                          f"before={before} unread={unread_only}")

    mail, err = _imap()
    if err:
        return err

    try:
        mail.select("inbox")
        parts = []
        if from_:       parts.append(f'FROM "{from_}"')
        if subject:     parts.append(f'SUBJECT "{subject}"')
        if since:       parts.append(f'SINCE {since}')
        if before:      parts.append(f'BEFORE {before}')
        if unread_only: parts.append("UNSEEN")
        criteria = " ".join(parts) if parts else "ALL"

        status, data = mail.uid("SEARCH", None, criteria)
        if status != "OK" or not data[0]:
            mail.logout()
            return f"📭 No emails matched: {criteria}"

        uids = data[0].split()[-limit:]
        blocks = []
        for uid in reversed(uids):
            status, msg_data = mail.uid("FETCH", uid, "(RFC822)")
            if status != "OK":
                continue
            for part in msg_data:
                if not isinstance(part, tuple):
                    continue
                msg = email.message_from_bytes(part[1])
                subject_val = _decode_header(msg.get("Subject", "No Subject"))
                sender      = _decode_header(msg.get("From", "?"))
                date        = msg.get("Date", "?")
                body        = _extract_body(msg)
                blocks.append(
                    f"📩 UID: {uid.decode()}\n"
                    f"SUBJECT: {subject_val}\n"
                    f"FROM: {sender}\n"
                    f"DATE: {date}\n"
                    f"BODY:\n{body[:400]}"
                    f"{'...' if len(body) > 400 else ''}\n"
                    f"{'-'*50}"
                )
        mail.logout()
        return f"🔎 {len(blocks)} match(es) for: {criteria}\n\n" + "\n".join(blocks)

    except Exception as e:
        return f"❌ Search Emails Error: {e}"


# ──────────────────────────────────────────────────────────────────────
# 📥 DOWNLOAD ATTACHMENTS
# ──────────────────────────────────────────────────────────────────────
@tool("Download Attachments")
def download_attachments(uid: str, dest_dir: str = "downloads"):
    """
    Download every attachment from the email with the given UID to
    disk. Returns the full paths of the files written.

    After downloading, use file_manager(action="read", path="...")
    to read text-based files (txt, csv, md, json). For PDFs or
    spreadsheets, tell the user the file has been saved and where —
    these cannot be read by the file tool.

    Args:
      uid:      UID of the email. Get it from read_latest_emails or
                search_emails output.
      dest_dir: Directory to save into, relative to the workspace
                root. Defaults to 'downloads/'. Created if missing.
    """
    _log("Download Attachments", f"UID: {uid} | dest={dest_dir}")

    mail, err = _imap()
    if err:
        return err

    try:
        mail.select("inbox")
        status, msg_data = mail.uid("FETCH", str(uid).strip(), "(RFC822)")
        if status != "OK":
            mail.logout()
            return f"❌ Could not fetch UID {uid}."

        msg = None
        for part in msg_data:
            if isinstance(part, tuple):
                msg = email.message_from_bytes(part[1])
                break
        mail.logout()
        if msg is None:
            return f"❌ UID {uid} could not be parsed."

        attachments = _list_attachments(msg)
        if not attachments:
            return f"📭 UID {uid} has no attachments."

        # Resolve dest_dir against the workspace root (cwd).
        target_dir = dest_dir
        if not os.path.isabs(target_dir):
            target_dir = os.path.join(os.getcwd(), target_dir)
        os.makedirs(target_dir, exist_ok=True)

        written = []
        for fname, data, _ctype in attachments:
            safe = _safe_filename(fname)
            path = os.path.join(target_dir, safe)
            # If a file with that name already exists, add a numeric suffix.
            base, ext = os.path.splitext(path)
            n = 1
            while os.path.exists(path):
                path = f"{base}_{n}{ext}"
                n += 1
            with open(path, "wb") as f:
                f.write(data)
            written.append((path, len(data)))

        lines = [f"✅ Downloaded {len(written)} attachment(s) from UID {uid}:"]
        for path, size in written:
            lines.append(f"  • {path} ({size:,} bytes)")
        lines.append(
            "\nUse file_manager(action=\"read\", path=\"...\") to read "
            "text-based files. PDFs and spreadsheets cannot be read "
            "by the file tool — tell the user the file is saved and "
            "where."
        )
        return "\n".join(lines)

    except Exception as e:
        return f"❌ Download Attachments Error: {e}"


# ──────────────────────────────────────────────────────────────────────
# 📤 SEND EMAIL (with optional attachments, CC, BCC, HTML)
# ──────────────────────────────────────────────────────────────────────
@tool("Send Gmail")
def send_email(
    to_email: str,
    subject: str,
    body: str,
    attachments: list = None,
    cc: list = None,
    bcc: list = None,
    html: bool = False,
):
    """
    Sends an email using Gmail SMTP.

    Args:
      to_email:    Recipient address.
      subject:     Subject line.
      body:        Email body. If html=True, may contain HTML.
      attachments: Optional list of local file paths to attach.
                   Paths are resolved against the workspace root.
      cc:          Optional list of CC recipients.
      bcc:         Optional list of BCC recipients (not shown in
                   headers, but the message is delivered to them).
      html:        If true, send the body as HTML. Default false
                   sends plain text.

    Requires SMTP_EMAIL and SMTP_PASSWORD in .env.
    """
    _log("Send Gmail", f"To: {to_email} | Subject: {subject} "
                       f"| CC: {cc or []} | BCC: {bcc or []} "
                       f"| Attachments: {attachments or []}")

    server, user, err = _smtp()
    if err:
        return err

    try:
        msg = MIMEMultipart()
        msg["From"]    = user
        msg["To"]      = to_email
        msg["Subject"] = subject
        msg["Date"]    = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        if cc:
            msg["Cc"] = ", ".join(cc)

        subtype = "html" if html else "plain"
        msg.attach(MIMEText(body, subtype))

        attached, missing = [], []
        for path in (attachments or []):
            if _attach_file(msg, path):
                attached.append(os.path.basename(path))
            else:
                missing.append(path)

        # BCC is deliberately not in headers — pass to send_message
        # explicitly so the envelope reaches those addresses.
        envelope = [to_email] + list(cc or []) + list(bcc or [])
        server.send_message(msg, to_addrs=envelope)
        server.quit()

        result = f"✅ Email sent to {to_email} — subject '{subject}'"
        if cc:
            result += f"\n   CC: {', '.join(cc)}"
        if bcc:
            result += f"\n   BCC: {', '.join(bcc)} (not shown in headers)"
        if attached:
            result += f"\n   Attached: {', '.join(attached)}"
        if missing:
            result += f"\n   ⚠️  Missing files (not attached): {', '.join(missing)}"
        return result

    except Exception as e:
        return f"❌ Gmail SMTP Error: {e}"


# ──────────────────────────────────────────────────────────────────────
# ↩️  REPLY TO EMAIL (in-thread, optional attachments)
# ──────────────────────────────────────────────────────────────────────
@tool("Reply To Email")
def reply_to_email(
    uid: str,
    body: str,
    reply_all: bool = False,
    attachments: list = None,
):
    """
    Reply to an existing email, keeping the thread intact.
    Sets In-Reply-To and References headers so Gmail threads the reply.

    Args:
      uid:         UID of the email being replied to.
      body:        Plain-text reply body.
      reply_all:   If true, include every original To/Cc recipient
                   (minus self).
      attachments: Optional list of local file paths to attach.

    Requires SMTP_EMAIL and SMTP_PASSWORD in .env.
    """
    _log("Reply To Email", f"UID: {uid} | reply_all={reply_all} "
                           f"| Attachments: {attachments or []}")

    server, user, err = _smtp()
    if err:
        return err

    mail, err = _imap()
    if err:
        return err

    try:
        mail.select("inbox")
        status, msg_data = mail.uid("FETCH", str(uid).strip(), "(RFC822)")
        if status != "OK":
            mail.logout()
            server.quit()
            return f"❌ Could not fetch UID {uid}."

        original = None
        for part in msg_data:
            if isinstance(part, tuple):
                original = email.message_from_bytes(part[1])
                break
        mail.logout()
        if original is None:
            server.quit()
            return f"❌ UID {uid} could not be parsed."

        orig_subject = _decode_header(original.get("Subject", ""))
        orig_msgid   = original.get("Message-ID", "")
        orig_refs    = original.get("References", "")
        orig_from    = original.get("From", "")
        reply_to_hdr = original.get("Reply-To") or orig_from

        to_addrs = []
        if reply_to_hdr:
            to_addrs.append(parseaddr(reply_to_hdr)[1] or reply_to_hdr)
        if reply_all:
            for hdr in ("To", "Cc"):
                for addr in (_decode_header(original.get(hdr, "") or "")).split(","):
                    a = parseaddr(addr.strip())[1]
                    if a and a != user and a not in to_addrs:
                        to_addrs.append(a)

        subject = orig_subject if orig_subject.lower().startswith("re:") \
                  else f"Re: {orig_subject}"

        msg = MIMEMultipart()
        msg["From"]    = user
        msg["To"]      = ", ".join(to_addrs) if to_addrs else user
        msg["Subject"] = subject
        msg["Date"]    = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        if orig_msgid:
            msg["In-Reply-To"] = orig_msgid
            msg["References"] = f"{orig_refs} {orig_msgid}".strip()
        msg.attach(MIMEText(body, "plain"))

        attached, missing = [], []
        for path in (attachments or []):
            if _attach_file(msg, path):
                attached.append(os.path.basename(path))
            else:
                missing.append(path)

        server.send_message(msg)
        server.quit()

        result = (
            f"✅ Reply sent to {', '.join(to_addrs) or user} "
            f"(subject '{subject}')"
        )
        if attached:
            result += f"\n   Attached: {', '.join(attached)}"
        if missing:
            result += f"\n   ⚠️  Missing files (not attached): {', '.join(missing)}"
        return result

    except Exception as e:
        try:
            server.quit()
        except Exception:
            pass
        return f"❌ Reply Error: {e}"


# ──────────────────────────────────────────────────────────────────────
# ➡️  FORWARD EMAIL
# ──────────────────────────────────────────────────────────────────────
@tool("Forward Email")
def forward_email(uid: str, to_email: str, note: str = ""):
    """
    Forward an email to a new recipient.

    Args:
      uid:      UID of the email being forwarded.
      to_email: Recipient address.
      note:     Optional note to prepend above the forwarded content.

    Requires SMTP_EMAIL and SMTP_PASSWORD in .env.
    """
    _log("Forward Email", f"UID: {uid} → {to_email}")

    server, user, err = _smtp()
    if err:
        return err

    mail, err = _imap()
    if err:
        return err

    try:
        mail.select("inbox")
        status, msg_data = mail.uid("FETCH", str(uid).strip(), "(RFC822)")
        if status != "OK":
            mail.logout()
            server.quit()
            return f"❌ Could not fetch UID {uid}."

        original = None
        for part in msg_data:
            if isinstance(part, tuple):
                original = email.message_from_bytes(part[1])
                break
        mail.logout()
        if original is None:
            server.quit()
            return f"❌ UID {uid} could not be parsed."

        orig_subject = _decode_header(original.get("Subject", ""))
        orig_from    = _decode_header(original.get("From", ""))
        orig_date    = original.get("Date", "")
        orig_body    = _extract_body(original)

        subject = orig_subject if orig_subject.lower().startswith("fwd:") \
                  else f"Fwd: {orig_subject}"

        forwarded = (
            (f"{note}\n\n" if note else "")
            + "---------- Forwarded message ----------\n"
            + f"From: {orig_from}\n"
            + f"Date: {orig_date}\n"
            + f"Subject: {orig_subject}\n\n"
            + orig_body
        )

        msg = MIMEMultipart()
        msg["From"]    = user
        msg["To"]      = to_email
        msg["Subject"] = subject
        msg["Date"]    = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        msg.attach(MIMEText(forwarded, "plain"))

        server.send_message(msg)
        server.quit()

        return f"✅ Forwarded UID {uid} to {to_email}"

    except Exception as e:
        try:
            server.quit()
        except Exception:
            pass
        return f"❌ Forward Error: {e}"


# ──────────────────────────────────────────────────────────────────────
# 🗂️  LIST FOLDERS (Gmail labels)
# ──────────────────────────────────────────────────────────────────────
@tool("List Gmail Folders")
def list_gmail_folders():
    """
    List IMAP folders (Gmail labels). Includes special folders like
    '[Gmail]/All Mail', '[Gmail]/Trash', '[Gmail]/Spam', '[Gmail]/Sent Mail',
    plus any custom labels.
    """
    _log("List Gmail Folders", "")

    mail, err = _imap()
    if err:
        return err

    try:
        status, data = mail.list()
        mail.logout()
        if status != "OK":
            return "❌ Could not list folders."

        lines = []
        for raw in data:
            if not raw:
                continue
            try:
                text = raw.decode(errors="replace")
            except Exception:
                text = str(raw)
            # Format is: (\HasNoChildren) "/" "Folder/Name"
            parts = text.rsplit('"', 2)
            name = parts[1] if len(parts) >= 2 else text
            lines.append(f"  • {name}")
        return "📁 Gmail folders:\n" + "\n".join(sorted(lines))

    except Exception as e:
        return f"❌ List Folders Error: {e}"


# ──────────────────────────────────────────────────────────────────────
# ✅ MARK READ / UNREAD
# ──────────────────────────────────────────────────────────────────────
@tool("Mark Email Read")
def mark_email_read(uid: str):
    """Mark an email as read by UID."""
    _log("Mark Email Read", f"UID: {uid}")
    mail, err = _imap()
    if err:
        return err
    try:
        mail.select("inbox")
        status, _ = mail.uid("STORE", str(uid).strip(), "+FLAGS", "(\\Seen)")
        mail.logout()
        return (
            f"✅ Marked UID {uid} as read."
            if status == "OK"
            else f"❌ Could not mark UID {uid} as read."
        )
    except Exception as e:
        return f"❌ Mark Read Error: {e}"


@tool("Mark Email Unread")
def mark_email_unread(uid: str):
    """Mark an email as unread by UID."""
    _log("Mark Email Unread", f"UID: {uid}")
    mail, err = _imap()
    if err:
        return err
    try:
        mail.select("inbox")
        status, _ = mail.uid("STORE", str(uid).strip(), "-FLAGS", "(\\Seen)")
        mail.logout()
        return (
            f"✅ Marked UID {uid} as unread."
            if status == "OK"
            else f"❌ Could not mark UID {uid} as unread."
        )
    except Exception as e:
        return f"❌ Mark Unread Error: {e}"


# ──────────────────────────────────────────────────────────────────────
# 📦 MOVE EMAIL TO FOLDER (archive, trash, custom label)
# ──────────────────────────────────────────────────────────────────────
@tool("Move Email To Folder")
def move_email_to_folder(uid: str, folder: str):
    """
    Move an email to a folder / apply a Gmail label.

    Common folders:
      '[Gmail]/All Mail' — archive (removes from Inbox, keeps in All Mail)
      '[Gmail]/Trash'    — delete
      '[Gmail]/Spam'     — mark as spam
      Any custom label   — e.g. 'Work', 'Receipts'

    Args:
      uid:    UID of the email to move.
      folder: Target folder or label name.

    Uses IMAP COPY + delete-from-source so it works on both Gmail and
    standard IMAP servers. For Gmail specifically this is equivalent
    to applying the target label and removing the Inbox label.
    """
    _log("Move Email To Folder", f"UID: {uid} → {folder}")

    mail, err = _imap()
    if err:
        return err

    try:
        mail.select("inbox")
        ok, why = _move_uid(mail, uid, folder)
        mail.logout()
        if not ok:
            return (
                f"❌ {why} "
                f"Use list_gmail_folders to check the exact folder name."
            )
        return f"✅ Moved UID {uid} to '{folder}'."

    except Exception as e:
        return f"❌ Move Error: {e}"


# ──────────────────────────────────────────────────────────────────────
# 🗑️  DELETE EMAIL (shortcut to Trash)
# ──────────────────────────────────────────────────────────────────────
@tool("Delete Email")
def delete_email(uid: str):
    """
    Delete an email by moving it to [Gmail]/Trash.

    Gmail auto-empties Trash after 30 days. To archive instead of
    delete, use move_email_to_folder with '[Gmail]/All Mail'.

    Args:
      uid: UID of the email to delete.
    """
    _log("Delete Email", f"UID: {uid}")

    mail, err = _imap()
    if err:
        return err

    try:
        mail.select("inbox")
        ok, why = _move_uid(mail, uid, TRASH_FOLDER)
        mail.logout()
        if not ok:
            return f"❌ {why}"
        return f"🗑️ Moved UID {uid} to Trash."

    except Exception as e:
        return f"❌ Delete Error: {e}"


# ──────────────────────────────────────────────────────────────────────
# 📬 UNREAD COUNT
# ──────────────────────────────────────────────────────────────────────
@tool("Unread Count")
def unread_count():
    """
    Return the number of unread messages in the inbox.
    Use this for a quick status check — 'do I have any new emails?' —
    without fetching message bodies.
    """
    _log("Unread Count", "")

    mail, err = _imap()
    if err:
        return err

    try:
        mail.select("inbox")
        status, data = mail.uid("SEARCH", None, "UNSEEN")
        mail.logout()
        if status != "OK":
            return "❌ Could not count unread messages."
        uids = (data[0].split() if data and data[0] else [])
        n = len(uids)
        if n == 0:
            return "📭 No unread messages."
        return f"📬 {n} unread message{'s' if n != 1 else ''} in the inbox."

    except Exception as e:
        return f"❌ Unread Count Error: {e}"
