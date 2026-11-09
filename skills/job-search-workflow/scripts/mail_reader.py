"""Read-only QQ/Foxmail IMAP, sharing the local DPAPI SMTP credential."""
import argparse
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
import imaplib
import json
from pathlib import Path
import re
import ssl

from mail_transport import load_secret

from workflow import DEFAULT_ROOT
MAX_MESSAGE_BYTES = 2 * 1024 * 1024


class MailReadError(Exception):
    pass


def require_ok(result):
    status, data = result
    if status != 'OK':
        raise MailReadError('imap_command_failed')
    return data


def clean(value):
    return ''.join(ch for ch in str(value or '') if ch in '\n\t' or ord(ch) >= 32)


@contextmanager
def open_inbox(root, factory=None):
    settings = json.loads((Path(root) / 'preferences.json').read_text(encoding='utf-8-sig'))
    account = settings['submission_channels']['email']
    imap = account.get('imap', {})
    host, port = imap.get('host', 'imap.qq.com'), int(imap.get('port', 993))
    # This adapter is calibrated for this account and service only.
    if not imap.get('enabled',False) or not host or not account['sender'] or not 1 <= port <= 65535:
        raise MailReadError('unexpected_account_or_server')
    client = None
    try:
        secret = load_secret(Path(root) / 'private/smtp-auth.dpapi')
        client = (factory or imaplib.IMAP4_SSL)(
            host, port, ssl_context=ssl.create_default_context(), timeout=30)
        require_ok(client.login(account['sender'], secret))
        del secret
        total = require_ok(client.select('INBOX', readonly=True))
        validity = client.response('UIDVALIDITY')[1]
        if not validity or not validity[0] or not validity[0].isdigit():
            raise MailReadError('missing_uidvalidity')
        yield client, {'mailbox': 'INBOX', 'readonly': True,
                       'uidvalidity': validity[0].decode('ascii'),
                       'total': int(total[0])}
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


def search_uids(client, unread=False):
    data = require_ok(client.uid('SEARCH', None, 'UNSEEN' if unread else 'ALL'))
    tokens = (data[0] or b'').split()
    if any(not token.isdigit() for token in tokens):
        raise MailReadError('invalid_uid_response')
    return sorted((int(token) for token in tokens))


def check(client, mailbox):
    return dict(mailbox, unread=len(search_uids(client, unread=True)), authenticated=True)


def fetch_literal(client, uid, query):
    data = require_ok(client.uid('FETCH', str(uid), query))
    parts = [part for part in data if isinstance(part, tuple) and len(part) == 2]
    if len(parts) != 1 or not isinstance(parts[0][1], bytes):
        raise MailReadError('message_missing_or_invalid_response')
    metadata, raw = parts[0]
    found = re.search(rb'\bUID (\d+)\b', metadata)
    if not found or int(found[1]) != uid:
        raise MailReadError('uid_mismatch')
    return metadata, raw


def header_message(client, uid):
    metadata, raw = fetch_literal(client, uid,
        '(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE MESSAGE-ID)]<0.32768>)')
    message = BytesParser(policy=policy.default).parsebytes(raw)
    flags = re.search(rb'\bFLAGS \(([^)]*)\)', metadata)
    size = re.search(rb'\bRFC822.SIZE (\d+)\b', metadata)
    if not flags or not size:
        raise MailReadError('missing_message_metadata')
    return {'uid': str(uid), 'subject': clean(message['Subject']),
            'from': clean(message['From']), 'to': clean(message['To']),
            'date': clean(message['Date']), 'message_id': clean(message['Message-ID']),
            'unread': b'\\Seen' not in flags[1].split(), 'size': int(size[1])}


def list_messages(client, mailbox, limit=5, unread=False):
    if not 1 <= limit <= 20:
        raise MailReadError('limit_must_be_1_to_20')
    uids = search_uids(client, unread)
    return dict(mailbox, matched=len(uids), messages=[
        header_message(client, uid) for uid in reversed(uids[-limit:])])


def read_message(client, mailbox, uid, uidvalidity):
    if str(uidvalidity) != mailbox['uidvalidity']:
        raise MailReadError('mailbox_uidvalidity_changed_relist_first')
    if not str(uid).isdigit() or int(uid) < 1:
        raise MailReadError('invalid_uid')
    uid = int(uid)
    info = header_message(client, uid)
    if info['size'] > MAX_MESSAGE_BYTES:
        raise MailReadError('message_exceeds_2mb_limit')
    _, raw = fetch_literal(client, uid, f'(UID BODY.PEEK[]<0.{MAX_MESSAGE_BYTES + 1}>)')
    if len(raw) != info['size'] or len(raw) > MAX_MESSAGE_BYTES:
        raise MailReadError('incomplete_or_oversized_message')
    message = BytesParser(policy=policy.default).parsebytes(raw)
    body = message.get_body(preferencelist=('plain', 'html'))
    text = body.get_content() if body is not None else ''
    return dict(mailbox, **info, body=clean(text)[:30000],
                body_type=body.get_content_type() if body else None,
                body_truncated=len(text) > 30000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('check')
    listing = commands.add_parser('list')
    listing.add_argument('--limit', type=int, default=5)
    listing.add_argument('--unread', action='store_true')
    reading = commands.add_parser('read')
    reading.add_argument('--uid', required=True)
    reading.add_argument('--uidvalidity', required=True)
    args = parser.parse_args()
    try:
        with open_inbox(args.root) as (client, mailbox):
            if args.command == 'check':
                result = check(client, mailbox)
            elif args.command == 'list':
                result = list_messages(client, mailbox, args.limit, args.unread)
            else:
                result = read_message(client, mailbox, args.uid, args.uidvalidity)
        print(json.dumps({'ok': True, **result}, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Never echo server responses, authorization codes, or tracebacks.
        reason = str(exc) if isinstance(exc, MailReadError) else type(exc).__name__
        print(json.dumps({'ok': False, 'reason': reason}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
