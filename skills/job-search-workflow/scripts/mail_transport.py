"""MIME drafts, single-recipient SMTP_SSL transport and current-user Windows DPAPI.

This module never retries, logs credentials, or infers final delivery from SMTP acceptance.
The optional transport_factory is an in-process test seam, not a CLI setting.
"""
from __future__ import annotations

import ctypes
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate
import os
from pathlib import Path
import re
import smtplib
import ssl


_SECRET_MAGIC = b'JOBWF-DPAPI-USER-v1\x00'
_SECRET_ENTROPY = b'job-search-workflow/mail-transport/current-user/v1'


def _header(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be a nonempty string')
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f'{label} contains a forbidden header control character')
    return value


def _mailbox(value: object, label: str) -> str:
    """Accept one bare ASCII mailbox, never a display name or an address list."""
    value = _header(value, label)
    if value != value.strip() or not value.isascii() or any(c.isspace() for c in value):
        raise ValueError(f'{label} must be one bare ASCII email address')
    try:
        addr = Address(addr_spec=value)
    except (ValueError, IndexError) as exc:
        raise ValueError(f'{label} is not one valid email address') from None
    if not addr.username or not addr.domain or addr.addr_spec != value:
        raise ValueError(f'{label} must be one bare email address')
    return value


def _message_id(value: object) -> str:
    value = _header(value, 'message_id')
    if not value.isascii() or not re.fullmatch(r'<[^<>@\s]+@[^<>@\s]+>', value):
        raise ValueError('message_id must be a single bracketed ASCII Message-ID')
    return value


def build_message(payload: dict, pdf: Path) -> EmailMessage:
    """Build one reviewable message with exactly one original-name PDF attachment."""
    sender = _mailbox(payload.get('sender'), 'sender')
    recipient = _mailbox(payload.get('recipient'), 'recipient')
    subject = _header(payload.get('subject'), 'subject')
    message_id = _message_id(payload.get('message_id'))
    body = payload.get('body')
    if not isinstance(body, str) or not body.strip() or '\x00' in body:
        raise ValueError('body must be nonempty text without NUL')
    pdf = Path(pdf)
    if pdf.suffix.lower() != '.pdf' or not pdf.is_file():
        raise ValueError('attachment must be an existing PDF file')
    _header(pdf.name, 'attachment filename')
    content = pdf.read_bytes()
    if not content.startswith(b'%PDF-'):
        raise ValueError('attachment does not have a PDF header')
    message = EmailMessage(policy=SMTP)
    message['From'] = sender
    message['To'] = recipient
    message['Subject'] = subject
    message['Message-ID'] = message_id
    message['Date'] = formatdate(localtime=False, usegmt=True)
    # Keep the entire MIME wire 7-bit safe even if the server lacks 8BITMIME.
    message.set_content(body, cte='quoted-printable')
    message.add_attachment(content, maintype='application', subtype='pdf', filename=pdf.name)
    return message


def write_draft(message: EmailMessage, path: Path) -> None:
    """Create an .eml exclusively; an existing draft is never replaced."""
    if not isinstance(message, EmailMessage):
        raise ValueError('message must be EmailMessage')
    path = Path(path)
    if path.suffix.lower() != '.eml':
        raise ValueError('draft path must end in .eml')
    data = message.as_bytes(policy=SMTP)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def _envelope(message: EmailMessage, sender: str) -> tuple[str, str]:
    if not isinstance(message, EmailMessage):
        raise ValueError('message must be EmailMessage')
    if any(name.lower() in ('cc', 'bcc') or name.lower().startswith('resent-')
           for name in message.keys()):
        raise ValueError('Cc, Bcc and Resent headers are not supported')
    for name in ('From', 'To', 'Message-ID'):
        if len(message.get_all(name, [])) != 1:
            raise ValueError(f'exactly one {name} header is required')
    actual_sender = _mailbox(str(message['From']), 'From')
    recipient = _mailbox(str(message['To']), 'To')
    _message_id(str(message['Message-ID']))
    _header(str(message['Subject']), 'Subject')
    if actual_sender != sender:
        raise ValueError('message sender differs from settings sender')
    return actual_sender, recipient


def send_message(message: EmailMessage, settings: dict, secret: str,
                 transport_factory=None) -> dict:
    """Send once over verified implicit TLS; return accepted/not_sent/unknown.

    A 250 reply after DATA means only SMTP acceptance. Socket failures after any
    body bytes may have been transmitted are unknown. A definitive rejection is
    not_sent. Cleanup failures never turn an accepted send into a retry candidate.
    """
    client = None
    body_started = False
    final_reply = False
    stage = 'prepare'

    def result(status: str, reason: str, code=None) -> dict:
        out = {'status': status, 'reason': reason, 'stage': stage,
               'retry_automatically': False}
        if isinstance(code, int):
            out['smtp_code'] = code
        return out

    try:
        sender = _mailbox(settings.get('sender'), 'settings sender')
        sender, recipient = _envelope(message, sender)
        host = _header(settings.get('host'), 'SMTP host')
        if any(c.isspace() for c in host) or '/' in host or ':' in host:
            raise ValueError('SMTP host must be a hostname')
        port = settings.get('port')
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('SMTP port must be an integer from 1 to 65535')
        timeout = settings.get('timeout', 30)
        if type(timeout) not in (int, float) or not 0 < timeout <= 300:
            raise ValueError('SMTP timeout must be between 0 and 300 seconds')
        _header(secret, 'authorization code')
        wire = message.as_bytes(policy=SMTP)
        if not wire.endswith(b'\r\n'):
            wire += b'\r\n'
        # RFC 5321 transparency: a line beginning with a dot needs an extra dot.
        wire = re.sub(br'(?m)^\.', b'..', wire) + b'.\r\n'
        context = ssl.create_default_context()
        factory = transport_factory if transport_factory is not None else smtplib.SMTP_SSL
        stage = 'connect'
        client = factory(host, port, timeout=timeout, context=context)
        stage = 'authenticate'
        client.login(sender, secret)
        stage = 'mail_from'
        code, _ = client.mail(sender)
        if not 200 <= code < 300:
            return result('not_sent', 'sender_rejected', code)
        stage = 'rcpt_to'
        code, _ = client.rcpt(recipient)
        if not 200 <= code < 300:
            return result('not_sent', 'recipient_rejected', code)
        stage = 'data_command'
        code, _ = client.docmd('DATA')
        if code != 354:
            return result('not_sent', 'data_command_rejected', code)
        stage = 'data_body'
        body_started = True
        client.send(wire)
        stage = 'data_reply'
        code, _ = client.getreply()
        final_reply = True
        if code == 250:
            return result('smtp_accepted', 'accepted_by_submission_server', code)
        if 400 <= code < 600:
            return result('not_sent', 'data_rejected', code)
        return result('unknown', 'unexpected_data_reply', code)
    except smtplib.SMTPAuthenticationError:
        return result('not_sent', 'authentication_failed')
    except smtplib.SMTPRecipientsRefused:
        return result('not_sent', 'recipient_rejected')
    except smtplib.SMTPDataError as exc:
        if 400 <= exc.smtp_code < 600:
            return result('not_sent', 'data_rejected', exc.smtp_code)
        return result('unknown' if body_started else 'not_sent', 'smtp_data_error')
    except (OSError, smtplib.SMTPException):
        return result('unknown' if body_started else 'not_sent',
                      'send_result_unknown' if body_started else 'connection_or_protocol_failed')
    except (ValueError, TypeError, KeyError, AttributeError):
        return result('unknown' if body_started else 'not_sent',
                      'send_result_unknown' if body_started else 'invalid_input')
    finally:
        if client is not None:
            # Never put QUIT into a stream whose DATA completion is unknown.
            if not body_started or final_reply:
                try:
                    client.quit()
                except Exception:
                    pass
            try:
                client.close()
            except Exception:
                pass


def _dpapi(data: bytes, protect: bool) -> bytes:
    if os.name != 'nt':
        raise OSError('Windows current-user DPAPI is required')
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]

    def blob(value):
        buffer = ctypes.create_string_buffer(value, len(value))
        return Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer

    source, source_buffer = blob(data)
    entropy, entropy_buffer = blob(_SECRET_ENTROPY)
    output = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    function = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # UI_FORBIDDEN only; deliberately omit LOCAL_MACHINE to bind to current user.
    if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(output)):
        raise OSError(ctypes.get_last_error(), 'Windows DPAPI operation failed')
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


def save_secret(path: Path, secret: str) -> None:
    """Exclusively create a current-user DPAPI file, never plaintext or overwrite."""
    _header(secret, 'authorization code')
    data = _SECRET_MAGIC + _dpapi(secret.encode('utf-8'), protect=True)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())


def load_secret(path: Path) -> str:
    data = Path(path).read_bytes()
    if not data.startswith(_SECRET_MAGIC) or len(data) <= len(_SECRET_MAGIC):
        raise ValueError('not a workflow current-user DPAPI secret file')
    raw = _dpapi(data[len(_SECRET_MAGIC):], protect=False)
    try:
        secret = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('DPAPI secret is not valid UTF-8') from None
    return _header(secret, 'authorization code')
