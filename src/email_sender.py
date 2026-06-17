"""Gmail SMTP sender — requires GMAIL_USER + GMAIL_APP_PASSWORD in env."""
import os
import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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
  a {{ color: #e44; }}
  p {{ color: #555; font-style: italic; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def send_digest(digest_markdown: str, dry_run: bool = False) -> None:
    gmail_user = os.environ.get("GMAIL_USER", "")
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "")

    if not gmail_user or not app_password:
        print("  [Email] GMAIL_USER or GMAIL_APP_PASSWORD not set — skipping email")
        return

    today = date.today().strftime("%d %B %Y")
    subject = f"London Events — {today}"

    html_body = md.markdown(digest_markdown, extensions=["extra"])
    html_full = _HTML_WRAPPER.format(body=html_body)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = gmail_user
    msg.attach(MIMEText(digest_markdown, "plain"))
    msg.attach(MIMEText(html_full, "html"))

    if dry_run:
        print(f"  [Email] Dry run — would send '{subject}' to {gmail_user}")
        return

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, app_password.replace(" ", ""))
        server.sendmail(gmail_user, gmail_user, msg.as_string())

    print(f"  [Email] Sent '{subject}' to {gmail_user}")
