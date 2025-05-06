import smtplib
from email.mime.text import MIMEText
import os

def send_mail(subject, body, recipient):
    smtp_server = os.getenv("SMTP_SERVER")
    smtp_port = int(os.getenv("SMTP_PORT", 25))
    mail_user = os.getenv("MAIL_USER", "")
    mail_pass = os.getenv("MAIL_PASS", "")
    sender = mail_user if mail_user else f"no-reply@{os.getenv('DOMAIN')}"

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    try:
        if mail_user:
            server = smtplib.SMTP(smtp_server, smtp_port)
            server.starttls()
            server.login(mail_user, mail_pass)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port)

        server.sendmail(sender, [recipient], msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print(f"Fehler beim E-Mail-Versand: {e}")
        return False
