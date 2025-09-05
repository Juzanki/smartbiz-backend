import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from jinja2 import Template
from backend.crud.email_crud import get_template_by_name
from sqlalchemy.orm import Session

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SENDER_NAME = os.getenv("SENDER_NAME", "SmartBiz Notifications")

def send_email_with_template(
    db: Session,
    to_email: str,
    template_name: str,
    data: dict
):
    template = get_template_by_name(db, template_name)
    if not template:
        raise Exception(f"Email template '{template_name}' not found")

    # Render HTML content
    jinja_template = Template(template.html_content)
    rendered_html = jinja_template.render(**data)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = template.subject
    msg["From"] = f"{SENDER_NAME} <{SMTP_USER}>"
    msg["To"] = to_email

    msg.attach(MIMEText(rendered_html, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, to_email, msg.as_string())
