# 🚀 On-/Offboarding Workflow System

Ein webbasiertes System zur Digitalisierung und Automatisierung von Mitarbeiter-Onboarding- und Offboarding-Prozessen, entwickelt mit Flask, Active Directory-Integration und n8n für die Workflow-Automatisierung.

[![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python&logoColor=white)](https://www.python.org/) [![Flask](https://img.shields.io/badge/Flask-2.x-green?logo=flask&logoColor=white)](https://flask.palletsprojects.com/) [![Docker](https://img.shields.io/badge/Docker-Compose-blue?logo=docker&logoColor=white)](https://www.docker.com/) [![n8n](https://img.shields.io/badge/n8n-Workflow%20Automation-orange?logo=n8n&logoColor=white)](https://n8n.io/)

---

## 🎯 Ziel des Projekts

Das manuelle Management von Onboarding- und Offboarding-Prozessen für Mitarbeiter ist fehleranfällig, zeitaufwendig und oft intransparent. Dieses Projekt zielt darauf ab, diese Prozesse durch eine zentrale Webanwendung zu digitalisieren, zu standardisieren und mithilfe von Workflow-Automatisierung (n8n) und Active Directory-Integration effizienter zu gestalten.

Von der Antragsstellung über Genehmigungen bis hin zur Abarbeitung der technischen und administrativen Aufgaben durch verschiedene Abteilungen (IT, HR, Bauamt, Vorzimmer) wird alles in einem System erfasst und der Status nachverfolgbar gemacht.

---

## ✨ Features

* **📝 Web-Formular**: Zentrales Formular zur Erfassung aller relevanten Daten für On- und Offboarding-Anträge.
* **🔄 Dynamische Formularfelder**: Anpassung der Formularfelder basierend auf vorherigen Eingaben (z.B. Anzeige von Abteilungs- und Vorgesetztenauswahl nur bei Anforderung eines Windows-Kontos).
* **🔐 Active Directory Integration**:
    * Benutzerauthentifizierung für den Zugriff auf die Anwendung.
    * Dynamisches Laden von Organisationseinheiten (OUs), Benutzern und Vorgesetzten aus dem AD.
    * Granulare Zugriffskontrolle basierend auf AD-Gruppenmitgliedschaften für verschiedene Funktionen und Ansichten.
* **⚙️ Workflow-Automatisierung mit n8n**:
    * Anstoßen von n8n-Workflows via Webhooks nach Genehmigung eines Antrags.
    * Potenzial für automatisierte AD-Benutzeranlage, E-Mail-Erstellung, Hardware-Bestellung etc. durch n8n.
    * Rückmeldung von n8n an die Flask-Anwendung über den Status von Automatisierungsaufgaben.
* **📊 Status-Tracking**: Detaillierte Statusverfolgung für Aufgaben der beteiligten Abteilungen:
    * IT (Windows-Konto, E-Mail, Software-Zugänge, Hardware)
    * HR & AIDA (Verträge, Datenschutz, Systemzugänge)
    * Bauamt (Schlüssel, Zimmer, Arbeitsplatzausstattung)
    * Vorzimmer (Organisatorisches, Termine, Kommunikation)
* **👍 Genehmigungsprozess**: Mehrstufiger Prozess mit Genehmigung durch den Vorgesetzten.
* **📄 PDF-Generierung**: Erstellung eines zusammenfassenden PDF-Dokuments für jeden Antrag, inklusive der Möglichkeit, relevante Anhänge (z.B. Schlüsselübergabeprotokolle) beizufügen.
* **🗄️ Archivierung**: Archivierung abgeschlossener und abgelehnter Anträge.
* **🐳 Docker-Deployment**: Einfaches Setup und Deployment der gesamten Anwendung (Flask-Webanwendung & n8n) mithilfe von Docker und Docker Compose.
* **🔑 Konfiguration über `.env`**: Flexible Anpassung an die lokale Umgebung durch Umgebungsvariablen.

---

## 🛠️ Technologie-Stack

* **Backend**: Python 3.11+, Flask
* **Frontend**: HTML5, Bootstrap 5, JavaScript, jQuery, jsTree
* **Datenbank**: SQLite
* **Verzeichnisdienst-Integration**: `python-ldap3` für Active Directory
* **PDF-Generierung**: WeasyPrint, PyPDF2 (oder pypdf)
* **Workflow-Automatisierung**: n8n (via Webhooks)
* **Containerisierung**: Docker, Docker Compose

---

## ⚙️ Setup & Installation

### Voraussetzungen

* Docker ([Anleitung](https://docs.docker.com/get-docker/))
* Docker Compose ([Anleitung](https://docs.docker.com/compose/install/))
* Ein funktionierendes Active Directory für die Benutzerauthentifizierung und Datenabfrage.
* Ein SMTP-Server für den E-Mail-Versand.
* Eine laufende n8n-Instanz (kann auch die im Docker Compose enthaltene sein) mit konfigurierten Webhooks.

### 1. Repository klonen

```bash
git clone [https://github.com/GerryCrooked/onoffboarder.git](https://github.com/GerryCrooked/onoffboarder.git)
cd onoffboarder
