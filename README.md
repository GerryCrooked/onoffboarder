OnOffBoarder
OnOffBoarder ist eine Open-Source-Lösung zur Automatisierung von Onboarding- und Offboarding-Prozessen in Unternehmen. Durch die Integration von Webformularen, Datenbanken und Automatisierungs-Workflows ermöglicht es eine effiziente Verwaltung von Mitarbeiterwechseln.

Funktionen
Webbasierte Formulare zur Erfassung relevanter Mitarbeiterdaten

Datenbankintegration zur sicheren Speicherung und Verwaltung von Informationen

Automatisierte Workflows zur Durchführung von Onboarding- und Offboarding-Prozessen

Docker-Containerisierung für einfache Bereitstellung und Skalierbarkeit

Installation
Voraussetzungen
Docker und Docker Compose

Optional: n8n für erweiterte Workflow-Automatisierung

Schritte
Repository klonen:
´´´
git clone https://github.com/GerryCrooked/onoffboarder.git
cd onoffboarder
´´´

Umgebungsvariablen konfigurieren:
```cp .env.example .env ```


Container starten:
```docker-compose up -d ```

Projektstruktur

onoffboarder/
├── db/                 # Datenbankkonfigurationen und -skripte
├── n8n/                # n8n-Workflows und -Konfigurationen
├── webform/            # Webformular-Frontend
├── .env.example        # Beispiel für Umgebungsvariablen
├── docker-compose.yml  # Docker-Compose-Konfiguration
└── LICENSE             # Lizenzinformationen
Lizenz
Dieses Projekt steht unter der MIT-Lizenz.

Mitwirken
Beiträge sind herzlich willkommen. Bitte öffne ein Issue oder erstelle einen Pull Request, um zur Weiterentwicklung beizutragen.
