"""Gmail SMTP sender — requires GMAIL_USER + GMAIL_APP_PASSWORD in env."""
import html
import os
import re
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
import markdown as md


_HTML_WRAPPER = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          max-width: 640px; margin: 0 auto; padding: 20px; color: #222; }}
  h1 {{ color: #1a1a1a; border-bottom: 2px solid #e44; padding-bottom: 8px; }}
  h2 {{ color: #333; margin-top: 32px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }}
  li {{ margin-bottom: 12px; line-height: 1.5; }}
  em {{ color: #666; }}
  a {{ color: #c0392b; text-decoration: underline; }}
  p {{ color: #555; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


# URL may contain balanced parens (legacy digests); digest.py percent-encodes ( ) and spaces.
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://(?:[^()\s]|\([^()\s]*\))+)\)")


def markdown_to_plain(md_text: str) -> str:
    """Plain-text alternative: 'Title — URL' lines, no markdown syntax."""
    t = _MD_LINK.sub(lambda m: f"{m.group(1)} — {m.group(2)}", md_text)
    t = html.unescape(t)   # digest escapes scraped &,<,> for the HTML part
    t = re.sub(r"^#{1,6}\s*(.+)$", lambda m: m.group(1).upper(), t, flags=re.M)
    t = t.replace("**", "")
    t = re.sub(r"(?<![\w])_([^_\n]+)_(?![\w])", r"\1", t)
    return t


def render_html(digest_markdown: str) -> str:
    return _HTML_WRAPPER.format(body=md.markdown(digest_markdown, extensions=["extra"]))


def build_message(digest_markdown: str, subject: str, sender: str) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = sender
    msg["Reply-To"] = sender
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(sender.split("@")[-1] or "localhost"))
    msg.attach(MIMEText(markdown_to_plain(digest_markdown), "plain", "utf-8"))
    msg.attach(MIMEText(render_html(digest_markdown), "html", "utf-8"))
    return msg


def send_digest(digest_markdown: str, dry_run: bool = False, label: str = "London Events") -> None:
    gmail_user = os.environ.get("GMAIL_USER", "")
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "")

    if not gmail_user or not app_password:
        print("  [Email] GMAIL_USER or GMAIL_APP_PASSWORD not set — skipping email")
        return

    subject = f"{label} — {date.today().strftime('%d %B %Y')}"
    msg = build_message(digest_markdown, subject, gmail_user)

    if dry_run:
        print(f"  [Email] Dry run — would send '{subject}' to {gmail_user}")
        return

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, app_password.replace(" ", ""))
        server.sendmail(gmail_user, gmail_user, msg.as_string())

    print(f"  [Email] Sent '{subject}' to {gmail_user}")
